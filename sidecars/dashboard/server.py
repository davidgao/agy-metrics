#!/usr/bin/env python3
"""HTTP + SSE dashboard server for the agy-metrics sidecar."""

from __future__ import annotations

import argparse
import errno
from html import escape as html_escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import secrets
import select
import signal
import socket
import sys
import threading
import time
from typing import Any
import urllib.parse

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agy_metrics.engine import MetricsEngine  # noqa: E402

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_INDEX_PATH = _STATIC_DIR / "index.html"
_STATIC_MIME_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
}
_JS_BUNDLE_ORDER = (
    "format.js",
    "theme.js",
    "state.js",
    "charts.js",
    "pricing-modal.js",
    "app.js",
)
_JS_IMPORT_RE = re.compile(
    r"^\s*import\s+[\s\S]*?from\s+['\"][^'\"]+['\"];?\s*$",
    re.MULTILINE,
)
_JS_EXPORT_PREFIX_RE = re.compile(
    r"^export\s+(?=(?:async\s+)?(?:function|const|let|var|class)\b)",
    re.MULTILINE,
)
_STYLE_LINK_TAG = '<link rel="stylesheet" href="/static/styles.css" />'
_APP_MODULE_TAG = '<script type="module" src="/static/app.js"></script>'


def assemble_dashboard_html(static_dir: Path, csrf_token: str) -> bytes:
    """Assemble self-contained dashboard HTML with inline CSS, JS IIFE, and CSRF token."""
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    if _STYLE_LINK_TAG not in html:
        raise RuntimeError(f"Missing stylesheet placeholder {_STYLE_LINK_TAG!r} in index.html")
    if _APP_MODULE_TAG not in html:
        raise RuntimeError(f"Missing module script placeholder {_APP_MODULE_TAG!r} in index.html")

    css = (static_dir / "styles.css").read_text(encoding="utf-8")
    css = css.replace("</style", "<\\/style")

    js_chunks: list[str] = []
    for fname in _JS_BUNDLE_ORDER:
        src = (static_dir / fname).read_text(encoding="utf-8")
        src = _JS_IMPORT_RE.sub("", src)
        src = _JS_EXPORT_PREFIX_RE.sub("", src)
        src = src.replace("</script", "<\\/script")
        js_chunks.append(f"// --- {fname} ---\n{src.strip()}")

    js_iife = '(function () {\n"use strict";\n' + "\n\n".join(js_chunks) + "\n})();"
    safe_csrf = html_escape(csrf_token, quote=True)
    style_block = (
        f'<meta name="agy-csrf-token" content="{safe_csrf}" />\n'
        f"  <style>\n{css}\n  </style>"
    )
    script_block = f"<script>\n{js_iife}\n  </script>"

    assembled = html.replace(_STYLE_LINK_TAG, style_block, 1)
    assembled = assembled.replace(_APP_MODULE_TAG, script_block, 1)
    return assembled.encode("utf-8")
_SINGLE_LABEL_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _compute_local_hostnames() -> frozenset[str]:
    names: set[str] = set()
    for fn in (socket.gethostname, socket.getfqdn):
        try:
            val = fn().strip().rstrip(".").lower()
            if val:
                names.add(val)
        except OSError:
            pass
    return frozenset(names)


_LOCAL_HOSTNAMES = _compute_local_hostnames()


def _is_trusted_local_hostname(hostname: str | None) -> bool:
    if not hostname:
        return False
    h = hostname.strip().rstrip(".").lower()
    if not h:
        return False
    if h == "localhost" or h.endswith(".localhost"):
        return True
    if h in _LOCAL_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        pass
    return bool(_SINGLE_LABEL_HOST_RE.fullmatch(h))


def _safe_log(msg: str) -> None:
    try:
        sys.stderr.write(msg)
        sys.stderr.flush()
    except OSError:
        pass


class SSEHub:
    """Thread-safe pub/sub hub for Server-Sent Events clients."""

    def __init__(self, max_subscribers: int = 32) -> None:
        self._lock = threading.Lock()
        self._max_subscribers = max(1, int(max_subscribers))
        self._subscribers: dict[
            str, tuple[queue.Queue[str | None], tuple[str | None, str]]
        ] = {}

    @staticmethod
    def _signal_close(q: queue.Queue[str | None]) -> None:
        try:
            q.put_nowait(None)
        except queue.Full:
            try:
                q.get_nowait()
            except queue.Empty:
                pass
            try:
                q.put_nowait(None)
            except queue.Full:
                pass

    def register(
        self, sub_target: tuple[str | None, str]
    ) -> tuple[str, queue.Queue[str | None]]:
        q: queue.Queue[str | None] = queue.Queue(maxsize=32)
        evicted: list[queue.Queue[str | None]] = []
        with self._lock:
            while len(self._subscribers) >= self._max_subscribers:
                oldest_id = next(iter(self._subscribers))
                old_q, _ = self._subscribers.pop(oldest_id)
                evicted.append(old_q)
            sub_id = secrets.token_hex(8)
            while sub_id in self._subscribers:
                sub_id = secrets.token_hex(8)
            self._subscribers[sub_id] = (q, sub_target)
        for old_q in evicted:
            self._signal_close(old_q)
        return sub_id, q

    def unregister(self, sub_id: str) -> None:
        with self._lock:
            self._subscribers.pop(sub_id, None)

    def update_target(
        self, sub_id: str, sub_target: tuple[str | None, str]
    ) -> bool:
        with self._lock:
            entry = self._subscribers.get(sub_id)
            if entry is None:
                return False
            q, _ = entry
            self._subscribers[sub_id] = (q, sub_target)
            saw_close = False
            while True:
                try:
                    item = q.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    saw_close = True
            if saw_close:
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass
            return True

    def evict_oldest(self, count: int = 4) -> int:
        evicted: list[queue.Queue[str | None]] = []
        with self._lock:
            for _ in range(max(0, count)):
                if not self._subscribers:
                    break
                oldest_id = next(iter(self._subscribers))
                old_q, _ = self._subscribers.pop(oldest_id)
                evicted.append(old_q)
        for old_q in evicted:
            self._signal_close(old_q)
        return len(evicted)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def notify_all(self, engine: MetricsEngine) -> None:
        with self._lock:
            subs = list(self._subscribers.items())
        if not subs:
            return
        convs = engine.list_conversations(refresh=False)
        cache: dict[tuple[str | None, str], str] = {}
        for _, (q, target) in subs:
            if target not in cache:
                conv_id, window = target
                metrics = engine.get_metrics(
                    conv_id,
                    window=window,
                    refresh=False,
                )
                cache[target] = json.dumps(
                    {"conversations": convs, "metrics": metrics},
                    separators=(",", ":"),
                )
            payload = cache[target]
            try:
                q.put_nowait(payload)
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    pass

    def close_all(self) -> None:
        with self._lock:
            subs = list(self._subscribers.values())
        for q, _ in subs:
            self._signal_close(q)


class MetricsHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass: type[BaseHTTPRequestHandler],
        engine: MetricsEngine,
        hub: SSEHub,
        keepalive_s: float = 15.0,
    ) -> None:
        self.engine = engine
        self.hub = hub
        self.keepalive_s = max(0.05, float(keepalive_s))
        self.csrf_token = secrets.token_urlsafe(24)
        self._html_lock = threading.Lock()
        self._html_cache_sig: tuple[tuple[str, int, int], ...] | None = None
        self._html_cache_bytes: bytes | None = None
        self._consecutive_accept_errors = 0
        super().__init__(server_address, RequestHandlerClass)

    def get_index_html_bytes(self) -> bytes:
        watched = ("index.html", "styles.css", *_JS_BUNDLE_ORDER)
        sig_parts: list[tuple[str, int, int]] = []
        for name in watched:
            st = (_STATIC_DIR / name).stat()
            sig_parts.append((name, st.st_mtime_ns, st.st_size))
        sig = tuple(sig_parts)
        with self._html_lock:
            if self._html_cache_sig == sig and self._html_cache_bytes is not None:
                return self._html_cache_bytes
            data = assemble_dashboard_html(_STATIC_DIR, self.csrf_token)
            self._html_cache_sig = sig
            self._html_cache_bytes = data
            return data

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        _safe_log(f"[agy-metrics] request error from {client_address}: {exc}\n")

    def process_request(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request(request, client_address)
        except RuntimeError:
            self.close_request(request)
            raise

    def _handle_request_noblock(self) -> None:
        try:
            super()._handle_request_noblock()
            self._consecutive_accept_errors = 0
        except (OSError, RuntimeError) as exc:
            self._consecutive_accept_errors += 1
            evicted = self.hub.evict_oldest(4)
            _safe_log(
                f"[agy-metrics] accept/spawn error ({self._consecutive_accept_errors}, evicted={evicted}): {exc}\n"
            )
            if self._consecutive_accept_errors >= 40:
                raise
            time.sleep(0.05)


class MetricsRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30.0

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress per-request access logs; startup and errors print to stderr directly.
        return

    def _send_bytes(
        self,
        status: int,
        content_type: str,
        body: bytes,
    ) -> None:
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, data: Any) -> None:
        body = json.dumps(data, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, "application/json; charset=utf-8", body)

    def _is_client_disconnected(self) -> bool:
        sock = getattr(self, "connection", None)
        if sock is None or not hasattr(sock, "recv"):
            return False
        try:
            rlist, _, _ = select.select([sock], [], [], 0.0)
        except (ValueError, OSError, TypeError):
            return True
        if not rlist:
            return False
        dontwait = getattr(socket, "MSG_DONTWAIT", 0)
        try:
            if dontwait:
                data = sock.recv(1, socket.MSG_PEEK | dontwait)
            else:
                prev_timeout = sock.gettimeout()
                sock.settimeout(0.0)
                try:
                    data = sock.recv(1, socket.MSG_PEEK)
                finally:
                    sock.settimeout(prev_timeout)
            return len(data) == 0
        except (BlockingIOError, InterruptedError):
            return False
        except OSError:
            return True

    def do_GET(self) -> None:
        try:
            srv: MetricsHTTPServer = self.server  # type: ignore[assignment]
            host_hdr = self.headers.get("Host")
            if not self._is_allowed_host(host_hdr, srv.server_port):
                _safe_log(
                    f"[agy-metrics] 403 forbidden host on GET {self.path}: {host_hdr!r}\n"
                )
                self._send_json(403, {"error": "forbidden host"})
                return

            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            params = urllib.parse.parse_qs(parsed.query)
            conv_id = params.get("conversation_id", [None])[0] or None
            window = params.get("window", ["all"])[0] or "all"
            sub_id_param = params.get("sub_id", [""])[0] or ""

            if path in ("/", "/index.html"):
                try:
                    html_bytes = srv.get_index_html_bytes()
                except (OSError, RuntimeError) as exc:
                    self._send_json(500, {"error": f"index.html unreadable: {exc}"})
                    return
                self._send_bytes(200, "text/html; charset=utf-8", html_bytes)
                return

            if path.startswith("/static/"):
                self._serve_static(path[len("/static/") :], path)
                return

            if path in ("/healthz", "/health"):
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "pid": os.getpid(),
                        "sse_subscribers": srv.hub.subscriber_count(),
                    },
                )
                return

            if path == "/api/conversations":
                self._send_json(200, srv.engine.list_conversations())
                return

            if path == "/api/metrics":
                self._send_json(200, srv.engine.get_metrics(conv_id, window=window))
                return

            if path == "/api/snapshot":
                retargeted = (
                    srv.hub.update_target(sub_id_param, (conv_id, window))
                    if sub_id_param
                    else False
                )
                snap = srv.engine.get_snapshot(conv_id, window=window)
                snap["retargeted"] = retargeted
                self._send_json(200, snap)
                return

            if path == "/api/pricing":
                self._send_json(200, srv.engine.get_pricing_config())
                return

            if path == "/events":
                self._handle_sse(srv, conv_id, window)
                return

            self._send_json(404, {"error": "not found", "path": path})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            _safe_log(f"[agy-metrics] GET {self.path} error: {exc}\n")
            try:
                self._send_json(500, {"error": str(exc)})
            except OSError:
                pass

    def _serve_static(self, rel_raw: str, full_path: str) -> None:
        rel_unquoted = urllib.parse.unquote(rel_raw)
        if not rel_unquoted or "\\" in rel_unquoted or "\x00" in rel_unquoted:
            self._send_json(403, {"error": "forbidden static path"})
            return
        parts = [p for p in rel_unquoted.split("/") if p]
        if not parts or any(part.startswith(".") for part in parts):
            self._send_json(403, {"error": "forbidden static path"})
            return
        static_root = _STATIC_DIR.resolve()
        candidate = (_STATIC_DIR.joinpath(*parts)).resolve()
        if not candidate.is_relative_to(static_root):
            self._send_json(403, {"error": "forbidden static path"})
            return
        if not candidate.is_file():
            self._send_json(404, {"error": "not found", "path": full_path})
            return
        ctype = _STATIC_MIME_TYPES.get(
            candidate.suffix.lower(), "application/octet-stream"
        )
        try:
            data = candidate.read_bytes()
        except OSError as exc:
            self._send_json(500, {"error": f"static asset unreadable: {exc}"})
            return
        self._send_bytes(200, ctype, data)

    @staticmethod
    def _is_allowed_host(host_raw: str | None, server_port: int = 0) -> bool:
        _ = server_port  # Reverse proxies may forward a separate loopback or sidecar port.
        if not host_raw:
            return False
        raw = host_raw.strip()
        if not raw or any(ch.isspace() for ch in raw):
            return False
        if raw == "::1":
            return True
        try:
            u = urllib.parse.urlsplit(f"//{raw}")
            port = u.port
        except ValueError:
            return False
        if (
            u.path != ""
            or u.query != ""
            or u.fragment != ""
            or u.username is not None
            or u.password is not None
        ):
            return False
        return _is_trusted_local_hostname(u.hostname) and (
            port is None or 1 <= port <= 65535
        )

    @staticmethod
    def _is_allowed_origin(origin_raw: str) -> bool:
        orig = origin_raw.strip()
        if not orig or orig.lower() == "null" or any(ch.isspace() for ch in orig):
            return False
        try:
            u = urllib.parse.urlsplit(orig)
            port = u.port
        except ValueError:
            return False
        if port is not None and not (1 <= port <= 65535):
            return False
        return (
            u.scheme in ("http", "https", "sidecar", "vscode-webview", "app")
            and _is_trusted_local_hostname(u.hostname)
            and u.path == ""
            and u.query == ""
            and u.fragment == ""
            and u.username is None
            and u.password is None
        )

    def do_POST(self) -> None:
        try:
            srv: MetricsHTTPServer = self.server  # type: ignore[assignment]
            host_hdr = self.headers.get("Host")
            if not self._is_allowed_host(host_hdr, srv.server_port):
                _safe_log(
                    f"[agy-metrics] 403 forbidden host on POST {self.path}: {host_hdr!r}\n"
                )
                self._send_json(403, {"error": "forbidden host"})
                return

            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            if path != "/api/pricing":
                self._send_json(404, {"error": "not found", "path": path})
                return

            ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                self._send_json(415, {"error": "Content-Type must be application/json"})
                return

            raw_len = self.headers.get("Content-Length")
            try:
                content_len = int(raw_len) if raw_len is not None else 0
            except ValueError:
                content_len = 0
            if content_len <= 0:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if content_len > 65536:
                self._send_json(413, {"error": "payload too large"})
                return

            try:
                raw_body = self.rfile.read(content_len)
                body = json.loads(raw_body.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": f"invalid JSON body: {exc}"})
                return

            if not isinstance(body, dict):
                self._send_json(400, {"error": "JSON body must be an object"})
                return

            req_csrf = body.pop("_csrf_token", None)
            has_valid_csrf = (
                isinstance(req_csrf, str)
                and bool(req_csrf)
                and secrets.compare_digest(req_csrf, srv.csrf_token)
            )
            if not has_valid_csrf:
                sec_fetch_site = (
                    (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
                )
                if sec_fetch_site == "cross-site":
                    self._send_json(403, {"error": "forbidden cross-site request"})
                    return

                origin = self.headers.get("Origin")
                if origin is not None and not self._is_allowed_origin(origin):
                    _safe_log(
                        f"[agy-metrics] 403 forbidden origin on POST {self.path}: {origin!r}\n"
                    )
                    self._send_json(403, {"error": "forbidden origin"})
                    return

            try:
                updated = srv.engine.update_pricing_config(body)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return

            srv.hub.notify_all(srv.engine)
            self._send_json(200, updated)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            _safe_log(f"[agy-metrics] POST {self.path} error: {exc}\n")
            try:
                self._send_json(500, {"error": str(exc)})
            except OSError:
                pass

    def _handle_sse(
        self,
        srv: MetricsHTTPServer,
        conv_id: str | None,
        window: str = "all",
    ) -> None:
        self.close_connection = True
        sub_id, q = srv.hub.register((conv_id, window))
        try:
            try:
                snap = srv.engine.get_snapshot(conv_id, window=window)
                initial = json.dumps(
                    {"sub_id": sub_id, **snap},
                    separators=(",", ":"),
                )
            except Exception as exc:
                self._send_json(500, {"error": f"metrics snapshot failed: {exc}"})
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            self.wfile.write(f"event: snapshot\ndata: {initial}\n\n".encode("utf-8"))
            self.wfile.flush()

            next_ping_mono = time.monotonic() + srv.keepalive_s
            while True:
                wait_s = max(0.01, min(0.5, next_ping_mono - time.monotonic()))
                try:
                    msg = q.get(timeout=wait_s)
                except queue.Empty:
                    if self._is_client_disconnected():
                        break
                    if time.monotonic() >= next_ping_mono:
                        self.wfile.write(b"event: ping\ndata: {}\n\n")
                        self.wfile.flush()
                        next_ping_mono = time.monotonic() + srv.keepalive_s
                    continue

                if msg is None:
                    break
                self.wfile.write(f"event: update\ndata: {msg}\n\n".encode("utf-8"))
                self.wfile.flush()
                next_ping_mono = time.monotonic() + srv.keepalive_s
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            srv.hub.unregister(sub_id)


def _watcher_loop(
    engine: MetricsEngine,
    hub: SSEHub,
    stop_event: threading.Event,
    poll_interval_s: float = 0.4,
) -> None:
    while not stop_event.is_set():
        wait_s = poll_interval_s
        try:
            if engine.refresh_if_changed(fast_poll=True):
                hub.notify_all(engine)
            if engine.is_generating():
                wait_s = min(poll_interval_s, 0.08)
        except Exception as exc:
            _safe_log(f"[agy-metrics] watcher error: {exc}\n")
        stop_event.wait(wait_s)


def create_server(
    host: str = "127.0.0.1",
    port: int = 3389,
    engine: MetricsEngine | None = None,
    poll_interval_s: float = 0.4,
    keepalive_s: float = 15.0,
) -> tuple[MetricsHTTPServer, threading.Event, threading.Thread]:
    eng = engine or MetricsEngine()
    try:
        eng.refresh_if_changed()
    except Exception as exc:
        _safe_log(f"[agy-metrics] initial refresh warning: {exc}\n")
    hub = SSEHub()
    server = MetricsHTTPServer(
        (host, port), MetricsRequestHandler, eng, hub, keepalive_s=keepalive_s
    )
    stop_event = threading.Event()
    watcher = threading.Thread(
        target=_watcher_loop,
        args=(eng, hub, stop_event, poll_interval_s),
        name="agy-metrics-watcher",
        daemon=True,
    )
    watcher.start()
    return server, stop_event, watcher


def _resolve_port(cli_port: int | None) -> int:
    if cli_port is not None:
        return cli_port
    for env_key in ("ANTIGRAVITY_SIDECAR_WEB_PORT", "PORT"):
        raw = os.environ.get(env_key)
        if raw:
            try:
                return int(raw)
            except ValueError:
                pass
    return 3389


def main() -> None:
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)

    parser = argparse.ArgumentParser(description="AGY Metrics Sidecar Dashboard Server")
    parser.add_argument("--host", default="127.0.0.1", help="Loopback bind address")
    parser.add_argument("--port", type=int, default=None, help="Listen port")
    args = parser.parse_args()

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        _safe_log(f"[agy-metrics] Warning: binding non-loopback host {args.host}\n")

    port = _resolve_port(args.port)
    eng = MetricsEngine()
    server: MetricsHTTPServer | None = None
    stop_event: threading.Event | None = None
    for attempt in range(5):
        try:
            server, stop_event, _ = create_server(
                host=args.host, port=port, engine=eng
            )
            break
        except OSError as exc:
            if port != 0 and exc.errno == errno.EADDRINUSE and attempt < 4:
                _safe_log(
                    f"[agy-metrics] port {port} busy, retrying bind ({attempt + 1}/5)\n"
                )
                time.sleep(0.25)
                continue
            raise

    assert server is not None and stop_event is not None
    actual_port = server.server_address[1]
    _safe_log(f"[agy-metrics] listening on http://{args.host}:{actual_port}/\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.hub.close_all()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
