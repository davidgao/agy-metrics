"""Tests for HTTP server routes, Host/Origin CSRF hardening, /static/* serving, and SSE."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import http.client
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from helpers import http_json, running_server, write_jsonl  # noqa: E402
from agy_metrics.engine import ALL_CONVERSATIONS_ID, MetricsEngine  # noqa: E402
from sidecars.dashboard.server import SSEHub  # noqa: E402


class TestServer(unittest.TestCase):
    def test_http_and_concurrent_sse_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"
            cid = "dddddddd-dddd-dddd-dddd-dddddddddddd"
            write_jsonl(
                brain / cid / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-14T05:00:00Z", "content": "SSE smoke test"},
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-14T05:00:01Z",
                        "input_tokens": 2048,
                        "cache_read_tokens": 512,
                        "output_tokens": 64,
                        "content": "Reply",
                        "tool_calls": [],
                    },
                ],
            )
            engine = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                with running_server(engine, poll_interval_s=0.1) as (_, base):
                    with urllib.request.urlopen(f"{base}/?token=test123", timeout=3) as r:
                        self.assertEqual(r.status, 200)
                        html_body = r.read().decode("utf-8")
                        self.assertIn("AGY Metrics", html_body)
                        self.assertIn("--fb-bg", html_body)
                        self.assertIn("agy-csrf-token", html_body)
                        self.assertIn("initDashboard();", html_body)
                        self.assertNotIn('<script type="module"', html_body)

                    st_h, health = http_json(f"{base}/healthz")
                    self.assertEqual(st_h, 200)
                    self.assertTrue(health["ok"])

                    st_c, convs = http_json(f"{base}/api/conversations")
                    self.assertEqual(st_c, 200)
                    self.assertEqual(convs["active_conversation_id"], cid)

                    st_m, met = http_json(f"{base}/api/metrics")
                    self.assertEqual(st_m, 200)
                    self.assertEqual(met["context"]["current_tokens"], 2560)

                    with urllib.request.urlopen(f"{base}/events?token=test123", timeout=3) as sse:
                        self.assertEqual(sse.status, 200)
                        self.assertIn("text/event-stream", sse.headers.get("Content-Type", ""))
                        event_line = sse.readline().decode("utf-8").strip()
                        data_line = sse.readline().decode("utf-8").strip()
                        _ = sse.readline()
                        self.assertEqual(event_line, "event: snapshot")
                        snap = json.loads(data_line[6:])
                        self.assertEqual(snap["metrics"]["conversation_id"], cid)

                        _, concurrent_health = http_json(f"{base}/healthz")
                        self.assertTrue(concurrent_health["ok"])
            finally:
                engine.close()

    def test_pricing_overrides_hot_reload_and_http_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"
            cid = "dddddddd-4444-4444-4444-444444444444"

            write_jsonl(
                brain / cid / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-15T12:00:00Z",
                        "content": "<USER_REQUEST>\nTest overrides\n</USER_REQUEST>\n<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to My Custom X. No need to comment.\n</USER_SETTINGS_CHANGE>",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-15T12:00:02Z",
                        "input_tokens": 1_000_000,
                        "cache_read_tokens": 500_000,
                        "output_tokens": 100_000,
                        "content": "Custom reply.",
                        "tool_calls": [],
                    },
                ],
            )

            engine = MetricsEngine(brain_dir=brain, state_dir=state, as_of_date="2026-11-15")
            try:
                with running_server(engine, poll_interval_s=0.1, keepalive_s=5.0) as (_, base):
                    port = int(base.rsplit(":", 1)[1])

                    st, cfg = http_json(f"{base}/api/pricing")
                    self.assertEqual(st, 200)
                    self.assertFalse(cfg["has_overrides"])
                    self.assertEqual(cfg["credits_per_usd"], 100.0)
                    self.assertEqual(
                        engine.get_metrics(cid)["valuation"]["coverage"]["unpriced_models"][0]["model_name"],
                        "My Custom X",
                    )

                    sse_updates: list[dict] = []
                    sse_ready = threading.Event()
                    sse_done = threading.Event()

                    def _listen_sse() -> None:
                        req_sse = urllib.request.Request(f"{base}/events?conversation_id={cid}&window=all")
                        with urllib.request.urlopen(req_sse, timeout=3.0) as resp:
                            cur_ev = ""
                            while not sse_done.is_set():
                                raw = resp.readline()
                                if not raw:
                                    break
                                line = raw.decode("utf-8", errors="replace").strip()
                                if line.startswith("event:"):
                                    cur_ev = line.split(":", 1)[1].strip()
                                elif line.startswith("data:"):
                                    payload_ev = json.loads(line.split(":", 1)[1].strip())
                                    if cur_ev == "snapshot":
                                        sse_ready.set()
                                    elif cur_ev == "update":
                                        sse_updates.append(payload_ev)
                                        sse_done.set()

                    sse_thread = threading.Thread(target=_listen_sse, daemon=True)
                    sse_thread.start()
                    self.assertTrue(sse_ready.wait(timeout=2.0))

                    st, cfg_custom = http_json(
                        f"{base}/api/pricing",
                        method="POST",
                        body=json.dumps(
                            {
                                "action": "set_custom_model",
                                "pattern": "my custom x",
                                "label": "Custom X",
                                "match_mode": "substring",
                                "pool": "custom",
                                "rates": {"input": 5.0, "cache_read": 1.0, "output": 20.0},
                            }
                        ).encode("utf-8"),
                        headers={"Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"},
                    )
                    self.assertEqual(st, 200)
                    self.assertTrue(cfg_custom["has_overrides"])
                    override_path = state / "pricing.json"
                    self.assertTrue(override_path.exists())
                    self.assertEqual(stat.S_IMODE(override_path.stat().st_mode), 0o600)

                    self.assertTrue(sse_done.wait(timeout=2.0))
                    sse_thread.join(timeout=1.0)
                    self.assertAlmostEqual(sse_updates[0]["metrics"]["valuation"]["cost_usd"], 7.50, places=6)

                    http_json(
                        f"{base}/api/pricing",
                        method="POST",
                        body=json.dumps({"action": "set_credits_per_usd", "credits_per_usd": 50.0}).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                    )
                    self.assertAlmostEqual(engine.get_metrics(cid)["valuation"]["credits"], 375.0, places=6)

                    raw_ovr = json.loads(override_path.read_text(encoding="utf-8"))
                    raw_ovr["credits_per_usd"] = 200.0
                    override_path.write_text(json.dumps(raw_ovr), encoding="utf-8")
                    bumped_ns = override_path.stat().st_mtime_ns + 5_000_000
                    os.utime(override_path, ns=(bumped_ns, bumped_ns))
                    self.assertTrue(engine.refresh_if_changed())
                    self.assertAlmostEqual(engine.get_metrics(cid)["valuation"]["credits"], 1500.0, places=6)

                    _, cfg_ovr = http_json(
                        f"{base}/api/pricing",
                        method="POST",
                        body=json.dumps(
                            {
                                "action": "set_catalog_override",
                                "id": "gemini-flash",
                                "rates": {"input": 9.0, "cache_read": 0.9, "output": 30.0},
                            }
                        ).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                    )
                    flash_row = next(e for e in cfg_ovr["catalog"] if e["id"] == "gemini-flash")
                    self.assertEqual(flash_row["source"], "override")

                    _, cfg_reset_one = http_json(
                        f"{base}/api/pricing",
                        method="POST",
                        body=json.dumps({"action": "reset_catalog_override", "id": "gemini-flash"}).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                    )
                    self.assertEqual(
                        next(e for e in cfg_reset_one["catalog"] if e["id"] == "gemini-flash")["source"],
                        "bundled",
                    )

                    _, cfg_clean = http_json(
                        f"{base}/api/pricing",
                        method="POST",
                        body=json.dumps({"action": "reset_all"}).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                    )
                    self.assertFalse(cfg_clean["has_overrides"])
                    self.assertFalse(override_path.exists())
            finally:
                engine.close()

    def test_snapshot_endpoint_live_sse_retargeting_and_fast_disconnect_reap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"
            cid_recent = "77777777-3333-4444-5555-666666666666"
            cid_old = "77777777-3333-4444-5555-777777777777"
            now_dt = datetime.now(timezone.utc)

            recent_log = brain / cid_recent / ".system_generated" / "logs" / "transcript_full.jsonl"
            write_jsonl(
                recent_log,
                [
                    {"step_index": 0, "type": "USER_INPUT", "created_at": (now_dt - timedelta(minutes=15)).isoformat().replace("+00:00", "Z"), "content": "Recent"},
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": (now_dt - timedelta(minutes=14)).isoformat().replace("+00:00", "Z"),
                        "input_tokens": 1000,
                        "cache_read_tokens": 500,
                        "output_tokens": 100,
                        "content": "Recent step 1",
                        "tool_calls": [],
                    },
                ],
            )
            write_jsonl(
                brain / cid_old / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {"step_index": 0, "type": "USER_INPUT", "created_at": (now_dt - timedelta(hours=48)).isoformat().replace("+00:00", "Z"), "content": "Old"},
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": (now_dt - timedelta(hours=47)).isoformat().replace("+00:00", "Z"),
                        "input_tokens": 2000,
                        "cache_read_tokens": 0,
                        "output_tokens": 400,
                        "content": "Old step",
                        "tool_calls": [],
                    },
                ],
            )

            engine = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                with running_server(engine, poll_interval_s=0.05, keepalive_s=5.0) as (server, base):
                    sse = urllib.request.urlopen(f"{base}/events", timeout=3)
                    try:
                        ev_line = sse.readline().decode("utf-8").strip()
                        data_line = sse.readline().decode("utf-8").strip()
                        _ = sse.readline()
                        self.assertEqual(ev_line, "event: snapshot")
                        sub_id = json.loads(data_line[6:])["sub_id"]

                        _, h_json = http_json(f"{base}/healthz")
                        self.assertEqual(h_json["sse_subscribers"], 1)

                        _, d_all = http_json(
                            f"{base}/api/snapshot?conversation_id={ALL_CONVERSATIONS_ID}&window=all&sub_id={sub_id}"
                        )
                        self.assertTrue(d_all["retargeted"])
                        self.assertEqual(d_all["metrics"]["totals"]["output_tokens"], 500)

                        _, d_5h = http_json(
                            f"{base}/api/snapshot?conversation_id={ALL_CONVERSATIONS_ID}&window=5h&sub_id={sub_id}"
                        )
                        self.assertTrue(d_5h["retargeted"])
                        self.assertEqual(d_5h["metrics"]["totals"]["output_tokens"], 100)
                        self.assertEqual(server.hub.subscriber_count(), 1)

                        with open(recent_log, "a", encoding="utf-8") as f:
                            f.write(
                                json.dumps(
                                    {
                                        "step_index": 2,
                                        "type": "PLANNER_RESPONSE",
                                        "created_at": (now_dt - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
                                        "input_tokens": 500,
                                        "cache_read_tokens": 1200,
                                        "output_tokens": 75,
                                        "content": "Live update step",
                                        "tool_calls": [],
                                    }
                                )
                                + "\n"
                            )

                        update_payload = None
                        deadline = time.monotonic() + 3.0
                        while time.monotonic() < deadline:
                            line = sse.readline().decode("utf-8").strip()
                            if line == "event: update":
                                d_line = sse.readline().decode("utf-8").strip()
                                _ = sse.readline()
                                update_payload = json.loads(d_line[6:])
                                if update_payload["metrics"]["totals"]["output_tokens"] == 175:
                                    break
                        self.assertIsNotNone(update_payload)
                        self.assertEqual(update_payload["metrics"]["totals"]["output_tokens"], 175)

                        _, d_bad = http_json(
                            f"{base}/api/snapshot?conversation_id={ALL_CONVERSATIONS_ID}&window=5h&sub_id=unknown_sub"
                        )
                        self.assertFalse(d_bad["retargeted"])
                    finally:
                        sse.close()

                    reap_deadline = time.monotonic() + 1.5
                    while time.monotonic() < reap_deadline and server.hub.subscriber_count() > 0:
                        time.sleep(0.05)
                    self.assertEqual(server.hub.subscriber_count(), 0)
            finally:
                engine.close()

    def test_host_header_and_origin_csrf_hardening(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = MetricsEngine(brain_dir=root / "brain", state_dir=root / "state")
            try:
                with running_server(engine) as (server, base):
                    port = server.server_address[1]

                    # DNS rebinding / external multi-label Hosts rejected on both GET and POST
                    for method, path, hdrs, body in (
                        ("GET", "/healthz", {"Host": "evil.example:3389"}, None),
                        ("GET", "/api/snapshot", {"Host": f"127-0-0-1.nip.io:{port}"}, None),
                        (
                            "POST",
                            "/api/pricing",
                            {"Host": "attacker.local", "Content-Type": "application/json"},
                            b'{"action":"reset_all"}',
                        ),
                    ):
                        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
                        conn.request(method, path, body=body, headers=hdrs)
                        resp = conn.getresponse()
                        resp.read()
                        conn.close()
                        self.assertEqual(resp.status, 403, f"Expected 403 for {method} {path} {hdrs}")

                    # Origin: null, foreign Origin, and Sec-Fetch-Site: cross-site rejected on POST
                    for bad_hdrs in (
                        {"Origin": "null", "Content-Type": "application/json"},
                        {"Origin": "https://evil.example", "Content-Type": "application/json"},
                        {"Sec-Fetch-Site": "cross-site", "Content-Type": "application/json"},
                    ):
                        with self.assertRaises(urllib.error.HTTPError) as ctx:
                            http_json(
                                f"{base}/api/pricing",
                                method="POST",
                                body=b'{"action":"reset_all"}',
                                headers=bad_hdrs,
                            )
                        self.assertEqual(ctx.exception.code, 403, bad_hdrs)

                    # Reverse-proxied loopback/sidecar Hosts and Origins succeed on GET and POST
                    for hdrs in (
                        {"Host": "127.0.0.1:9999"},
                        {"Host": "localhost:54321"},
                        {"Host": "agy-metrics.localhost:54321", "Origin": "https://agy-metrics.localhost:54321"},
                        {"Host": "[::1]:54321"},
                        {"Host": "agy-metrics", "Origin": "sidecar://agy-metrics"},
                    ):
                        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
                        conn.request("GET", "/api/snapshot", headers=hdrs)
                        get_resp = conn.getresponse()
                        get_resp.read()
                        conn.close()
                        self.assertEqual(get_resp.status, 200, f"Expected 200 GET for {hdrs}")

                        post_hdrs = {**hdrs, "Content-Type": "application/json"}
                        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
                        conn.request(
                            "POST",
                            "/api/pricing",
                            body=b'{"action":"reset_all"}',
                            headers=post_hdrs,
                        )
                        post_resp = conn.getresponse()
                        post_resp.read()
                        conn.close()
                        self.assertEqual(post_resp.status, 200, f"Expected 200 POST for {post_hdrs}")

                    # Direct loopback localhost Host succeeds
                    st, health = http_json(
                        f"http://localhost:{port}/healthz",
                        headers={"Host": f"localhost:{port}"},
                    )
                    self.assertEqual(st, 200)
                    self.assertTrue(health["ok"])

                    # Sandboxed opaque-origin iframe (Origin: null + Sec-Fetch-Site: cross-site)
                    # is rejected without _csrf_token, and allowed with token from GET /
                    with urllib.request.urlopen(f"{base}/", timeout=2.0) as root_resp:
                        root_html = root_resp.read().decode("utf-8")
                    marker = '<meta name="agy-csrf-token" content="'
                    self.assertIn(marker, root_html)
                    csrf_tok = root_html.split(marker, 1)[1].split('"', 1)[0]
                    self.assertTrue(csrf_tok)

                    sandbox_hdrs = {
                        "Origin": "null",
                        "Sec-Fetch-Site": "cross-site",
                        "Content-Type": "application/json",
                    }
                    with self.assertRaises(urllib.error.HTTPError) as ctx_no_tok:
                        http_json(
                            f"{base}/api/pricing",
                            method="POST",
                            body=b'{"action":"reset_all"}',
                            headers=sandbox_hdrs,
                        )
                    self.assertEqual(ctx_no_tok.exception.code, 403)

                    st_tok, cfg_tok = http_json(
                        f"{base}/api/pricing",
                        method="POST",
                        body=json.dumps({"action": "reset_all", "_csrf_token": csrf_tok}).encode("utf-8"),
                        headers=sandbox_hdrs,
                    )
                    self.assertEqual(st_tok, 200)
                    self.assertEqual(cfg_tok["credits_per_usd"], 100.0)
            finally:
                engine.close()

    def test_static_asset_serving_and_path_traversal_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = MetricsEngine(brain_dir=root / "brain", state_dir=root / "state")
            try:
                with running_server(engine) as (_, base):
                    with urllib.request.urlopen(f"{base}/static/styles.css", timeout=2.0) as r:
                        self.assertEqual(r.status, 200)
                        self.assertIn("text/css", r.headers.get("Content-Type", ""))
                        self.assertIn("--fb-bg", r.read().decode("utf-8"))

                    with urllib.request.urlopen(f"{base}/static/app.js", timeout=2.0) as r:
                        self.assertEqual(r.status, 200)
                        self.assertIn("javascript", r.headers.get("Content-Type", ""))
                        self.assertIn("connectSSE", r.read().decode("utf-8"))

                    for traversal in ("/static/../server.py", "/static/..%2Fsidecar.json", "/static/.hidden"):
                        with self.assertRaises(urllib.error.HTTPError) as ctx:
                            urllib.request.urlopen(f"{base}{traversal}", timeout=2.0)
                        self.assertEqual(ctx.exception.code, 403, traversal)

                    with self.assertRaises(urllib.error.HTTPError) as ctx_404:
                        urllib.request.urlopen(f"{base}/static/does_not_exist.js", timeout=2.0)
                    self.assertEqual(ctx_404.exception.code, 404)
            finally:
                engine.close()

    def test_post_pricing_validation_errors_and_404(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = MetricsEngine(brain_dir=root / "brain", state_dir=root / "state")
            try:
                with running_server(engine) as (_, base):
                    cases = [
                        ("/api/unknown", b'{}', {"Content-Type": "application/json"}, 404),
                        ("/api/pricing", b'{"action":"reset_all"}', {"Content-Type": "text/plain"}, 415),
                        ("/api/pricing", b"", {"Content-Type": "application/json"}, 400),
                        ("/api/pricing", b"{not-json", {"Content-Type": "application/json"}, 400),
                        ("/api/pricing", b'{"action":"set_credits_per_usd","credits_per_usd":-5}', {"Content-Type": "application/json"}, 400),
                        ("/api/pricing", b"x" * 70000, {"Content-Type": "application/json"}, 413),
                    ]
                    for path, body, hdrs, exp_code in cases:
                        with self.assertRaises(urllib.error.HTTPError) as ctx:
                            http_json(f"{base}{path}", method="POST", body=body, headers=hdrs)
                        self.assertEqual(ctx.exception.code, exp_code, path)
            finally:
                engine.close()

    def test_sse_hub_eviction_and_notify_all_single_refresh(self) -> None:
        hub = SSEHub(max_subscribers=2)
        s1, q1 = hub.register(("conv-1", "all"))
        s2, q2 = hub.register(("conv-1", "all"))
        s3, _q3 = hub.register(("conv-2", "5h"))
        self.assertEqual(hub.subscriber_count(), 2)
        self.assertIsNone(q1.get_nowait())  # Oldest evicted with sentinel None

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = MetricsEngine(brain_dir=root / "brain", state_dir=root / "state")
            try:
                with patch.object(engine, "refresh_if_changed", wraps=engine.refresh_if_changed) as mock_ref:
                    hub.notify_all(engine)
                    self.assertEqual(mock_ref.call_count, 0)
                payload = json.loads(q2.get_nowait() or "{}")
                self.assertIn("conversations", payload)
                self.assertIn("metrics", payload)
                hub.unregister(s2)
                hub.unregister(s3)
                self.assertEqual(hub.subscriber_count(), 0)
                _ = s1
            finally:
                engine.close()

    def test_remote_control_hosts_managed_sidecar_and_proxy_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"
            cid = "eeeeeeee-1111-2222-3333-444444444444"
            t_log = brain / cid / ".system_generated" / "logs" / "transcript_full.jsonl"
            write_jsonl(
                t_log,
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-16T01:00:00Z",
                        "content": "Remote control test",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-16T01:00:02Z",
                        "input_tokens": 1200,
                        "cache_read_tokens": 300,
                        "output_tokens": 80,
                        "content": "First step",
                        "tool_calls": [],
                    },
                ],
            )
            engine = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                # 1. Strict mode (managed_sidecar=False): trusted Remote Control / sidecar / CGNAT / private IP hosts pass
                with running_server(engine, managed_sidecar=False) as (server, _base):
                    port = server.server_address[1]
                    trusted_cases = (
                        ("antigravity.google", "https://antigravity.google"),
                        ("r-123.antigravity.google", "https://r-123.antigravity.google"),
                        ("antigravity.google.com", "https://antigravity.google.com"),
                        ("abc-sidecar.usercontent.goog", "https://abc-sidecar.usercontent.goog"),
                        ("agy-metrics/dashboard", "sidecar://agy-metrics/dashboard"),
                        ("100.64.1.2:3389", "http://100.64.1.2:3389"),
                        ("box.tail1234.ts.net", "https://box.tail1234.ts.net"),
                        ("192.168.1.10:3389", "http://192.168.1.10:3389"),
                    )
                    for host_val, origin_val in trusted_cases:
                        for method, path in (
                            ("GET", "/"),
                            ("GET", "/api/snapshot"),
                            ("HEAD", "/"),
                            ("HEAD", "/healthz"),
                        ):
                            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
                            conn.request(method, path, headers={"Host": host_val})
                            resp = conn.getresponse()
                            body = resp.read()
                            conn.close()
                            self.assertEqual(resp.status, 200, f"{method} {path} Host={host_val}")
                            if method == "HEAD":
                                self.assertEqual(body, b"")
                                self.assertGreater(int(resp.getheader("Content-Length") or "0"), 0)

                        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
                        conn.request(
                            "POST",
                            "/api/pricing",
                            body=b'{"action":"reset_all"}',
                            headers={
                                "Host": host_val,
                                "Origin": origin_val,
                                "Content-Type": "application/json",
                            },
                        )
                        post_resp = conn.getresponse()
                        post_resp.read()
                        conn.close()
                        self.assertEqual(
                            post_resp.status,
                            200,
                            f"POST /api/pricing Host={host_val} Origin={origin_val}",
                        )

                    # AGY_METRICS_ALLOWED_HOSTS permits explicit exact & suffix matches, rejects others
                    with patch.dict(
                        os.environ,
                        {"AGY_METRICS_ALLOWED_HOSTS": "custom.example.org,.corp.internal"},
                        clear=False,
                    ):
                        for ok_host in ("custom.example.org", "dev.corp.internal:8080"):
                            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
                            conn.request("GET", "/healthz", headers={"Host": ok_host})
                            resp = conn.getresponse()
                            resp.read()
                            conn.close()
                            self.assertEqual(resp.status, 200, ok_host)

                        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
                        conn.request("GET", "/healthz", headers={"Host": "evil.example"})
                        resp = conn.getresponse()
                        resp.read()
                        conn.close()
                        self.assertEqual(resp.status, 403)

                    # OPTIONS preflight CORS headers
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
                    conn.request(
                        "OPTIONS",
                        "/api/pricing",
                        headers={
                            "Host": "antigravity.google.com",
                            "Origin": "https://antigravity.google.com",
                        },
                    )
                    opt_ok = conn.getresponse()
                    opt_ok.read()
                    conn.close()
                    self.assertEqual(opt_ok.status, 204)
                    self.assertEqual(
                        opt_ok.getheader("Access-Control-Allow-Origin"),
                        "https://antigravity.google.com",
                    )

                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
                    conn.request(
                        "OPTIONS",
                        "/api/pricing",
                        headers={
                            "Host": "antigravity.google.com",
                            "Origin": "https://evil.example",
                        },
                    )
                    opt_bad = conn.getresponse()
                    opt_bad.read()
                    conn.close()
                    self.assertEqual(opt_bad.status, 204)
                    self.assertIsNone(opt_bad.getheader("Access-Control-Allow-Origin"))

                    # Sub-path proxy routes & /preload.js
                    for sub_path in (
                        "/preload.js",
                        "/r/inst1/sidecars/agy-metrics/dashboard/",
                        "/r/inst1/sidecars/agy-metrics/dashboard/preload.js",
                        "/r/inst1/sidecars/agy-metrics/dashboard/static/styles.css",
                        "/r/inst1/sidecars/agy-metrics/dashboard/api/snapshot",
                        "/r/inst1/sidecars/agy-metrics/dashboard/api/pricing",
                    ):
                        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
                        conn.request("GET", sub_path, headers={"Host": "antigravity.google"})
                        sub_resp = conn.getresponse()
                        sub_resp.read()
                        conn.close()
                        self.assertEqual(sub_resp.status, 200, sub_path)

                    # Conditional ?etag= on /api/snapshot
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
                    conn.request("GET", "/api/snapshot", headers={"Host": "antigravity.google"})
                    r1 = conn.getresponse()
                    d1 = json.loads(r1.read().decode("utf-8"))
                    conn.close()
                    self.assertEqual(r1.status, 200)
                    etag1 = d1["etag"]
                    self.assertTrue(etag1)
                    self.assertEqual(d1["metrics"]["totals"]["output_tokens"], 80)

                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
                    conn.request(
                        "GET",
                        f"/api/snapshot?etag={etag1}",
                        headers={"Host": "antigravity.google"},
                    )
                    r2 = conn.getresponse()
                    d2 = json.loads(r2.read().decode("utf-8"))
                    conn.close()
                    self.assertEqual(r2.status, 200)
                    self.assertTrue(d2["unchanged"])
                    self.assertEqual(d2["etag"], etag1)
                    self.assertFalse(d2["retargeted"])

                    with open(t_log, "a", encoding="utf-8") as f:
                        f.write(
                            json.dumps(
                                {
                                    "step_index": 2,
                                    "type": "PLANNER_RESPONSE",
                                    "created_at": "2026-11-16T01:00:05Z",
                                    "input_tokens": 1300,
                                    "cache_read_tokens": 300,
                                    "output_tokens": 45,
                                    "content": "Second step",
                                    "tool_calls": [],
                                }
                            )
                            + "\n"
                        )
                    engine.refresh_if_changed()

                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
                    conn.request(
                        "GET",
                        f"/api/snapshot?etag={etag1}",
                        headers={"Host": "antigravity.google"},
                    )
                    r3 = conn.getresponse()
                    d3 = json.loads(r3.read().decode("utf-8"))
                    conn.close()
                    self.assertEqual(r3.status, 200)
                    self.assertNotIn("unchanged", d3)
                    self.assertNotEqual(d3["etag"], etag1)
                    self.assertEqual(d3["metrics"]["totals"]["output_tokens"], 125)

                # 2. Managed sidecar mode via ANTIGRAVITY_SIDECAR_WEB_PORT:
                # unlisted proxy host works for GET and CSRF-token POST, rejected on POST without CSRF token
                with patch.dict(
                    os.environ, {"ANTIGRAVITY_SIDECAR_WEB_PORT": "3389"}, clear=False
                ):
                    with running_server(engine) as (managed_srv, _mbase):
                        self.assertTrue(managed_srv.managed_sidecar)
                        mport = managed_srv.server_address[1]
                        proxy_host = "relay.enterprise-proxy.example:8443"

                        conn = http.client.HTTPConnection("127.0.0.1", mport, timeout=2.0)
                        conn.request("GET", "/", headers={"Host": proxy_host})
                        root_r = conn.getresponse()
                        root_html = root_r.read().decode("utf-8")
                        conn.close()
                        self.assertEqual(root_r.status, 200)
                        marker = '<meta name="agy-csrf-token" content="'
                        csrf_tok = root_html.split(marker, 1)[1].split('"', 1)[0]
                        self.assertTrue(csrf_tok)

                        conn = http.client.HTTPConnection("127.0.0.1", mport, timeout=2.0)
                        conn.request("GET", "/api/snapshot", headers={"Host": proxy_host})
                        snap_r = conn.getresponse()
                        snap_r.read()
                        conn.close()
                        self.assertEqual(snap_r.status, 200)

                        conn = http.client.HTTPConnection("127.0.0.1", mport, timeout=2.0)
                        conn.request(
                            "POST",
                            "/api/pricing",
                            body=b'{"action":"reset_all"}',
                            headers={"Host": proxy_host, "Content-Type": "application/json"},
                        )
                        bad_post = conn.getresponse()
                        bad_post.read()
                        conn.close()
                        self.assertEqual(bad_post.status, 403)

                        conn = http.client.HTTPConnection("127.0.0.1", mport, timeout=2.0)
                        conn.request(
                            "POST",
                            "/api/pricing",
                            body=json.dumps(
                                {"action": "reset_all", "_csrf_token": csrf_tok}
                            ).encode("utf-8"),
                            headers={"Host": proxy_host, "Content-Type": "application/json"},
                        )
                        ok_post = conn.getresponse()
                        ok_post.read()
                        conn.close()
                        self.assertEqual(ok_post.status, 200)
            finally:
                engine.close()


if __name__ == "__main__":
    unittest.main()
