"""Shared synthetic fixture builders and HTTP helpers for agy-metrics tests."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sys
import threading
import urllib.request
from typing import Any, Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agy_metrics.engine import MetricsEngine  # noqa: E402
from sidecars.dashboard.server import create_server  # noqa: E402


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


@contextmanager
def running_server(
    engine: MetricsEngine,
    *,
    poll_interval_s: float = 0.1,
    keepalive_s: float = 15.0,
) -> Iterator[tuple[Any, str]]:
    server, stop_event, _ = create_server(
        host="127.0.0.1",
        port=0,
        engine=engine,
        poll_interval_s=poll_interval_s,
        keepalive_s=keepalive_s,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        yield server, f"http://127.0.0.1:{port}"
    finally:
        stop_event.set()
        server.hub.close_all()
        server.shutdown()
        server.server_close()


def http_json(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 3.0,
) -> tuple[int, Any]:
    req = urllib.request.Request(
        url, data=body, method=method, headers=headers or {}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))
