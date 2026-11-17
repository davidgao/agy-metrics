"""Tests for SQLite rollups, lazy hydration, migrations, windowed compactions, and orphan cleanup."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from helpers import running_server, write_jsonl  # noqa: E402
from agy_metrics.engine import (  # noqa: E402
    ALL_CONVERSATIONS_ID,
    MetricsEngine,
    _MAX_HYDRATED_CONVS,
)
from agy_metrics.models import _ROLLUP_SCHEMA_VERSION  # noqa: E402


class TestStorageAndLifecycle(unittest.TestCase):
    def test_all_conversations_mode_windows_subagents_and_unattributed_guard(self) -> None:
        fixed_now = datetime(2026, 11, 14, 12, 30, 0, tzinfo=timezone.utc).timestamp()

        def _iso(epoch_s: float) -> str:
            return datetime.fromtimestamp(epoch_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        def _make_warm_conv(
            brain: Path,
            state: Path,
            cid: str,
            title: str,
            model_name: str | None,
            steps_spec: list[tuple[float, int, float]],
            trailing_subagent_id: str | None = None,
        ) -> None:
            tr: list[dict] = []
            hk: list[dict] = []
            settings_tag = (
                f"\n<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to {model_name}. No need to comment.\n</USER_SETTINGS_CHANGE>"
                if model_name is not None
                else ""
            )
            tr.append(
                {
                    "step_index": 0,
                    "type": "USER_INPUT",
                    "created_at": _iso(steps_spec[0][0] - 1.0),
                    "content": f"<USER_REQUEST>\n{title}\n</USER_REQUEST>{settings_tag}",
                }
            )
            idx = 1
            for s_pos, (ep_s, out_tok, dur_s) in enumerate(steps_spec):
                ts_iso = _iso(ep_s)
                is_last = s_pos == len(steps_spec) - 1
                tcalls = (
                    [{"name": "invoke_subagent", "args": {"Role": '"Alpha Helper"'}}]
                    if (is_last and trailing_subagent_id is not None)
                    else []
                )
                tr.append(
                    {
                        "step_index": idx,
                        "type": "PLANNER_RESPONSE",
                        "created_at": ts_iso,
                        "input_tokens": 300,
                        "cache_read_tokens": 12000,
                        "output_tokens": out_tok,
                        "thinking": "",
                        "content": "ok " * 5,
                        "tool_calls": tcalls,
                    }
                )
                hk.append({"ts_ms": int(ep_s * 1000), "event": "PreInvocation", "conversation_id": cid})
                hk.append({"ts_ms": int((ep_s + dur_s) * 1000), "event": "PostInvocation", "conversation_id": cid})
                idx += 1
                if is_last and trailing_subagent_id is not None:
                    sub_iso = _iso(ep_s + dur_s + 0.5)
                    tr.append(
                        {
                            "step_index": idx,
                            "type": "GENERIC",
                            "created_at": sub_iso,
                            "content": f"Created At: {sub_iso}\nCompleted At: {sub_iso}\nLaunched subagent {trailing_subagent_id}\n",
                        }
                    )
                    idx += 1
            write_jsonl(brain / cid / ".system_generated" / "logs" / "transcript_full.jsonl", tr)
            write_jsonl(state / "conversations" / f"{cid}.hooks.jsonl", hk)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"

            cid_parent = "aaaaaaaa-0000-0000-0000-000000000001"
            cid_sub = "bbbbbbbb-0000-0000-0000-000000000002"
            cid_unk = "cccccccc-0000-0000-0000-000000000003"
            cid_many = "dddddddd-0000-0000-0000-000000000004"

            def _ep(y: int, mo: int, d: int, h: int, mi: int, s: int = 0) -> float:
                return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp()

            _make_warm_conv(
                brain,
                state,
                cid_parent,
                "Parent Alpha Task",
                "Model Alpha",
                [
                    (_ep(2026, 10, 1, 10, 0), 200, 2.0),
                    (_ep(2026, 10, 25, 10, 0), 300, 2.5),
                    (_ep(2026, 11, 10, 10, 0), 450, 3.25),
                    (_ep(2026, 11, 13, 12, 15), 600, 4.0),
                    (_ep(2026, 11, 13, 12, 45), 250, 2.25),
                ],
                trailing_subagent_id=cid_sub,
            )
            _make_warm_conv(
                brain,
                state,
                cid_sub,
                "Child Alpha Research",
                "Model Alpha",
                [
                    (_ep(2026, 11, 13, 13, 0), 500, 3.5),
                    (_ep(2026, 11, 13, 13, 5), 900, 5.5),
                    (_ep(2026, 11, 13, 13, 10), 1300, 7.5),
                ],
            )
            _make_warm_conv(
                brain,
                state,
                cid_unk,
                "Unattributed Session",
                None,
                [
                    (_ep(2026, 11, 14, 8, 0), 100, 1.4),
                    (_ep(2026, 11, 14, 8, 1), 200, 1.8),
                    (_ep(2026, 11, 14, 8, 2), 400, 2.6),
                    (_ep(2026, 11, 14, 8, 3), 700, 3.8),
                    (_ep(2026, 11, 14, 8, 4), 1000, 5.0),
                    (_ep(2026, 11, 14, 8, 5), 1400, 6.6),
                ],
            )
            final_beta_ep = _ep(2026, 11, 14, 12, 25)
            beta_start_ep = final_beta_ep - (229 * 60)
            beta_steps = [
                (beta_start_ep + i * 60, 200 + (i % 10) * 150, 1.0 + (200 + (i % 10) * 150) / 300.0)
                for i in range(230)
            ]
            _make_warm_conv(brain, state, cid_many, "Beta Heavy Session", "Model Beta", beta_steps)

            with patch("agy_metrics.engine.time.time", return_value=fixed_now):
                engine = MetricsEngine(brain_dir=brain, state_dir=state)
                try:
                    m24 = engine.get_metrics(ALL_CONVERSATIONS_ID, window="24h")
                    self.assertEqual(m24["mode"], "all")
                    self.assertEqual(m24["window"], "24h")
                    self.assertNotIn("context", m24)
                    self.assertEqual(m24["steps"], [])
                    self.assertEqual(m24["conversations_count"], 3)
                    self.assertEqual(m24["subagents_count"], 1)
                    self.assertEqual(m24["latest_step"]["conv_id"], cid_many)

                    by24 = {bm["model_name"]: bm for bm in m24["by_model"]}
                    self.assertEqual(by24["Model Alpha"]["steps_count"], 1)
                    self.assertEqual(by24["Subagent · Model Alpha"]["steps_count"], 3)
                    self.assertTrue(by24["Model Alpha"]["latency_fit"]["available"])
                    self.assertFalse(by24["unknown"]["latency_fit"]["available"])
                    self.assertEqual(by24["unknown"]["latency_fit"]["reason"], "unattributed")
                    self.assertEqual(by24["Model Beta"]["steps_count"], 230)
                    self.assertEqual(len(by24["Model Beta"]["scatter_steps"]), 100)

                    m7 = engine.get_metrics(ALL_CONVERSATIONS_ID, window="7d")
                    m30 = engine.get_metrics(ALL_CONVERSATIONS_ID, window="30d")
                    mall = engine.get_metrics(ALL_CONVERSATIONS_ID, window="all")
                    by7 = {bm["model_name"]: bm for bm in m7["by_model"]}
                    by30 = {bm["model_name"]: bm for bm in m30["by_model"]}
                    by_all = {bm["model_name"]: bm for bm in mall["by_model"]}
                    self.assertEqual(by7["Model Alpha"]["steps_count"], 3)
                    self.assertEqual(by7["Subagent · Model Alpha"]["steps_count"], 3)
                    self.assertEqual(by30["Model Alpha"]["steps_count"], 4)
                    self.assertEqual(by30["Subagent · Model Alpha"]["steps_count"], 3)
                    self.assertEqual(by_all["Model Alpha"]["steps_count"], 5)
                    self.assertEqual(by_all["Subagent · Model Alpha"]["steps_count"], 3)

                    import urllib.request

                    with running_server(engine, poll_interval_s=0.1) as (_, base):
                        with urllib.request.urlopen(
                            f"{base}/api/metrics?conversation_id={ALL_CONVERSATIONS_ID}&window=7d",
                            timeout=3,
                        ) as r:
                            api_m7 = json.loads(r.read())
                            self.assertEqual(api_m7["window"], "7d")
                            api_by7 = {bm["model_name"]: bm for bm in api_m7["by_model"]}
                            self.assertEqual(api_by7["Model Alpha"]["steps_count"], 3)
                            self.assertEqual(api_by7["Subagent · Model Alpha"]["steps_count"], 3)
                finally:
                    engine.close()

    def test_persistent_rollup_cache_lazy_hydration_and_eviction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"
            cids = [f"10000000-0000-0000-0000-{i:012d}" for i in range(22)]
            base_ep = datetime(2026, 11, 14, 9, 0, 0, tzinfo=timezone.utc).timestamp()

            def _iso(ep_s: float) -> str:
                return datetime.fromtimestamp(ep_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            for i, cid in enumerate(cids):
                t0 = base_ep + i * 600
                tr = [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": _iso(t0 - 1.0),
                        "content": f"<USER_REQUEST>\nScale Conv {i}\n</USER_REQUEST>\n<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to Model Scale.\n</USER_SETTINGS_CHANGE>",
                    }
                ]
                hk = []
                for j in range(5):
                    ep_s = t0 + j * 20
                    out_tok = 200 + j * 200
                    dur_s = 1.0 + out_tok / 250.0
                    tr.append(
                        {
                            "step_index": j + 1,
                            "type": "PLANNER_RESPONSE",
                            "created_at": _iso(ep_s),
                            "input_tokens": 300,
                            "cache_read_tokens": 12000,
                            "output_tokens": out_tok,
                            "thinking": "",
                            "content": "done",
                            "tool_calls": [],
                        }
                    )
                    hk.append({"ts_ms": int(ep_s * 1000), "event": "PreInvocation", "conversation_id": cid})
                    hk.append({"ts_ms": int((ep_s + dur_s) * 1000), "event": "PostInvocation", "conversation_id": cid})
                write_jsonl(brain / cid / ".system_generated" / "logs" / "transcript_full.jsonl", tr)
                write_jsonl(state / "conversations" / f"{cid}.hooks.jsonl", hk)

            engine1 = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                m1 = engine1.get_metrics(ALL_CONVERSATIONS_ID)
                self.assertTrue((state / "rollup_cache.sqlite3").is_file())
                self.assertEqual(m1["planner_steps_count"], 110)
                self.assertLessEqual(
                    sum(1 for c in engine1._conversations.values() if c.hydrated),
                    _MAX_HYDRATED_CONVS,
                )
            finally:
                engine1.close()

            engine2 = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                convs = engine2.list_conversations()
                m2 = engine2.get_metrics(ALL_CONVERSATIONS_ID)
                self.assertEqual(len(convs["conversations"]), 22)
                self.assertEqual(m2["planner_steps_count"], 110)
                self.assertEqual(engine2._transcript_open_count, 0)
                self.assertTrue(all(not c.hydrated for c in engine2._conversations.values()))

                fits_after_first = engine2._fit_compute_count
                self.assertEqual(fits_after_first, 1)
                engine2.get_metrics(ALL_CONVERSATIONS_ID)
                self.assertEqual(engine2._fit_compute_count, fits_after_first)

                target_cid = cids[0]
                t_log = brain / target_cid / ".system_generated" / "logs" / "transcript_full.jsonl"
                h_log = state / "conversations" / f"{target_cid}.hooks.jsonl"
                new_ep = base_ep + 200
                new_rec = {
                    "step_index": 6,
                    "type": "PLANNER_RESPONSE",
                    "created_at": _iso(new_ep),
                    "input_tokens": 300,
                    "cache_read_tokens": 12000,
                    "output_tokens": 1200,
                    "thinking": "",
                    "content": "extra",
                    "tool_calls": [],
                }
                with open(t_log, "a", encoding="utf-8") as f:
                    f.write(json.dumps(new_rec) + "\n")
                with open(h_log, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"ts_ms": int(new_ep * 1000), "event": "PreInvocation", "conversation_id": target_cid}) + "\n")
                    f.write(json.dumps({"ts_ms": int((new_ep + 5.8) * 1000), "event": "PostInvocation", "conversation_id": target_cid}) + "\n")

                engine2._last_full_scan_mono = 0.0
                m3 = engine2.get_metrics(ALL_CONVERSATIONS_ID)
                self.assertEqual(m3["planner_steps_count"], 111)
                self.assertEqual(engine2._transcript_open_count, 1)

                for cid in cids:
                    cm = engine2.get_metrics(cid)
                    self.assertEqual(cm["conversation_id"], cid)
                    self.assertLessEqual(
                        sum(1 for c in engine2._conversations.values() if c.hydrated),
                        _MAX_HYDRATED_CONVS,
                    )
            finally:
                engine2.close()

    def test_corrupt_sqlite_recovery_and_v3_to_v4_schema_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"
            cid = "77777777-1111-2222-3333-444444444444"
            now_dt = datetime.now(timezone.utc)
            write_jsonl(
                brain / cid / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": (now_dt - timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
                        "content": "Recent question",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": (now_dt - timedelta(minutes=9)).isoformat().replace("+00:00", "Z"),
                        "input_tokens": 3000,
                        "cache_read_tokens": 1000,
                        "output_tokens": 150,
                        "content": "Done",
                        "tool_calls": [],
                    },
                ],
            )

            # 1. Corrupt DB + WAL on startup
            state.mkdir(parents=True, exist_ok=True)
            db_path = state / "rollup_cache.sqlite3"
            db_path.write_bytes(b"not a valid sqlite3 file!!" * 30)
            Path(f"{db_path}-wal").write_bytes(b"corrupt wal bytes")

            eng1 = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                m5 = eng1.get_metrics(ALL_CONVERSATIONS_ID, window="5h")
                self.assertEqual(m5["totals"]["output_tokens"], 150)
                self.assertEqual(eng1.get_metrics(cid)["totals"]["output_tokens"], 150)
            finally:
                eng1.close()

            # 2. Legacy v3, v4, v5 & v6 SQLite schemas upgrade cleanly to current version
            for old_ver in (3, 4, 5, 6):
                db_path.unlink(missing_ok=True)
                old_conn = sqlite3.connect(str(db_path))
                old_conn.execute(f"PRAGMA user_version = {old_ver}")
                old_conn.execute("CREATE TABLE conv_meta (conv_id TEXT PRIMARY KEY)")
                old_conn.execute("CREATE TABLE step_lite (conv_id TEXT, ordinal INT)")
                old_conn.commit()
                old_conn.close()

                eng_mig = MetricsEngine(brain_dir=brain, state_dir=state)
                try:
                    m_all = eng_mig.get_metrics(ALL_CONVERSATIONS_ID, window="all")
                    self.assertEqual(m_all["totals"]["output_tokens"], 150)
                    self.assertEqual(
                        eng_mig._db.execute("PRAGMA user_version").fetchone()[0],
                        _ROLLUP_SCHEMA_VERSION,
                    )
                finally:
                    eng_mig.close()

            eng2 = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                # 3. Runtime SQLite failure falls back cleanly to :memory:
                eng2._db.close()
                m_rec = eng2.get_metrics(ALL_CONVERSATIONS_ID, window="5h")
                self.assertEqual(m_rec["totals"]["output_tokens"], 150)
                cfg = eng2.get_pricing_config()
                self.assertIn("credits_per_usd", cfg)
            finally:
                eng2.close()

    def test_malformed_jsonl_values_and_hook_compaction_resilience(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"
            cid = "77777777-2222-3333-4444-555555555555"
            log_path = brain / cid / ".system_generated" / "logs" / "transcript_full.jsonl"
            log_path.parent.mkdir(parents=True, exist_ok=True)

            lines = [
                json.dumps({"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-15T10:00:00Z", "content": "Test"}),
                "{corrupt_json_line",
                "[1, 2, 3]",
                json.dumps(
                    {
                        "step_index": "oops",
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-15T10:00:01Z",
                        "input_tokens": "bad_int",
                        "cache_read_tokens": None,
                        "output_tokens": "nan_str",
                        "content": "partial",
                        "tool_calls": [],
                    }
                ),
                json.dumps(
                    {
                        "step_index": 2,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-15T10:01:00Z",
                        "input_tokens": 1200,
                        "cache_read_tokens": 800,
                        "output_tokens": 90,
                        "content": "",
                        "tool_calls": [{"name": "read_file"}],
                    }
                ),
                json.dumps(
                    {
                        "step_index": 3,
                        "type": "GENERIC",
                        "created_at": "2026-11-15T10:01:01Z",
                        "content": "Created At: 2026-11-15T10:01:01Z\nCompleted At: 2026-11-15T10:01:02Z\nok",
                    }
                ),
            ]
            log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            t0_ms = int(datetime(2026, 11, 15, 10, 1, 0, tzinfo=timezone.utc).timestamp() * 1000)
            hook_records = [{"ts_ms": t0_ms, "event": "PreInvocation", "conversation_id": cid}]
            for i in range(50):
                hook_records.append({"ts_ms": t0_ms + 1000 + i * 20, "event": "PreToolUse", "conversation_id": cid})
                hook_records.append({"ts_ms": t0_ms + 1010 + i * 20, "event": "PostToolUse", "conversation_id": cid})
            hook_records.append({"ts_ms": t0_ms + 2500, "event": "PostInvocation", "conversation_id": cid})
            hook_path = state / "conversations" / f"{cid}.hooks.jsonl"
            write_jsonl(hook_path, hook_records)

            eng = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                met = eng.get_metrics(cid)
                self.assertEqual(met["planner_steps_count"], 2)
                step2 = met["steps"][1]
                self.assertEqual(step2["duration_s"], 1.0)
                self.assertEqual(step2["duration_source"], "hook_ms")
                expected_tool_s = round(((1010 + 49 * 20) - 1000) / 1000.0, 2)
                self.assertEqual(step2["tool_exec_s"], expected_tool_s)
                self.assertLessEqual(len(eng._conversations[cid].hook_events), 8)

                changes_before = eng._db.total_changes
                with open(hook_path, "a", encoding="utf-8") as f:
                    f.write(
                        json.dumps({"ts_ms": int(time.time() * 1000), "event": "PreInvocation", "conversation_id": cid})
                        + "\n"
                    )
                eng.refresh_if_changed(target_id=cid)
                self.assertEqual(eng._db.total_changes - changes_before, 1)
                self.assertEqual(eng.get_metrics(cid)["status"], "generating")
            finally:
                eng.close()

    def test_windowed_all_conversations_compacted_count(self) -> None:
        """All-Conversations time windows compute exact compacted_count without double-counting."""
        fixed_now = datetime(2026, 11, 15, 18, 0, 0, tzinfo=timezone.utc).timestamp()

        def _iso(ep: float) -> str:
            return datetime.fromtimestamp(ep, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"
            cid = "11111111-cccc-0000-0000-000000000001"

            # 1 compaction inside 10d (outside 7d), 1 CHECKPOINT compaction inside 24h, 1 heuristic drop inside 5h
            t_10d = fixed_now - 10 * 86400
            t_12h = fixed_now - 12 * 3600
            t_2h = fixed_now - 2 * 3600

            write_jsonl(
                brain / cid / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {"step_index": 0, "type": "USER_INPUT", "created_at": _iso(t_10d - 1), "content": "Old"},
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": _iso(t_10d),
                        "input_tokens": 20000,
                        "cache_read_tokens": 0,
                        "output_tokens": 100,
                        "content": "big",
                        "tool_calls": [],
                    },
                    {
                        "step_index": 2,
                        "type": "CHECKPOINT",
                        "created_at": _iso(t_10d + 10),
                        "input_tokens": 500,
                        "cache_read_tokens": 19500,
                        "output_tokens": 500,
                        "content": "<summary>s1</summary>",
                    },
                    {
                        "step_index": 3,
                        "type": "PLANNER_RESPONSE",
                        "created_at": _iso(t_10d + 20),
                        "input_tokens": 5000,
                        "cache_read_tokens": 0,
                        "output_tokens": 100,
                        "content": "post1",
                        "tool_calls": [],
                    },
                    {
                        "step_index": 4,
                        "type": "CHECKPOINT",
                        "created_at": _iso(t_12h),
                        "input_tokens": 400,
                        "cache_read_tokens": 8000,
                        "output_tokens": 400,
                        "content": "<summary>s2</summary>",
                    },
                    {
                        "step_index": 5,
                        "type": "PLANNER_RESPONSE",
                        "created_at": _iso(t_12h + 10),
                        "input_tokens": 4000,
                        "cache_read_tokens": 10000,
                        "output_tokens": 100,
                        "content": "post2",
                        "tool_calls": [],
                    },
                    {
                        "step_index": 6,
                        "type": "PLANNER_RESPONSE",
                        "created_at": _iso(t_2h),
                        "input_tokens": 2000,
                        "cache_read_tokens": 0,
                        "output_tokens": 100,
                        "content": "heuristic drop (< 0.85 * 14000)",
                        "tool_calls": [],
                    },
                ],
            )

            with patch("agy_metrics.engine.time.time", return_value=fixed_now):
                eng = MetricsEngine(brain_dir=brain, state_dir=state)
                try:
                    m_5h = eng.get_metrics(ALL_CONVERSATIONS_ID, window="5h")
                    m_24h = eng.get_metrics(ALL_CONVERSATIONS_ID, window="24h")
                    m_7d = eng.get_metrics(ALL_CONVERSATIONS_ID, window="7d")
                    m_all = eng.get_metrics(ALL_CONVERSATIONS_ID, window="all")
                    self.assertEqual(m_5h["totals"]["compacted_count"], 1)
                    self.assertEqual(m_24h["totals"]["compacted_count"], 2)
                    self.assertEqual(m_7d["totals"]["compacted_count"], 2)
                    self.assertEqual(m_all["totals"]["compacted_count"], 3)
                finally:
                    eng.close()

    def test_orphan_hooks_grace_window_and_expiration_cleanup(self) -> None:
        """Hook-only convs survive within grace window or while generating; expired orphans are purged."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"
            cid_fresh = "aaaaaaaa-dddd-0000-0000-000000000001"
            cid_gen = "bbbbbbbb-dddd-0000-0000-000000000002"
            cid_expired = "cccccccc-dddd-0000-0000-000000000003"

            now_s = time.time()
            now_ms = int(now_s * 1000)

            fresh_hk = state / "conversations" / f"{cid_fresh}.hooks.jsonl"
            gen_hk = state / "conversations" / f"{cid_gen}.hooks.jsonl"
            expired_hk = state / "conversations" / f"{cid_expired}.hooks.jsonl"

            write_jsonl(fresh_hk, [{"ts_ms": now_ms - 10_000, "event": "SessionStart", "conversation_id": cid_fresh}])
            write_jsonl(gen_hk, [{"ts_ms": now_ms - 60_000, "event": "PreInvocation", "conversation_id": cid_gen}])
            write_jsonl(expired_hk, [{"ts_ms": now_ms - 900_000, "event": "SessionStart", "conversation_id": cid_expired}])

            # Make gen_hk and expired_hk older than 60s grace window
            old_mtime = now_s - 400.0
            os.utime(gen_hk, (old_mtime, old_mtime))
            os.utime(expired_hk, (old_mtime, old_mtime))

            (state / "latest.json").write_text(
                json.dumps(
                    {
                        "ts_ms": now_ms - 5000,
                        "event": "PreInvocation",
                        "conversation_id": cid_gen,
                        "status": "generating",
                    }
                ),
                encoding="utf-8",
            )

            # Seed stale SQLite row for expired orphan too
            seed_eng = MetricsEngine(brain_dir=brain, state_dir=state, orphan_hook_grace_s=1000.0)
            seed_eng.list_conversations()
            self.assertEqual(
                len(seed_eng._db.execute("SELECT 1 FROM conv_meta WHERE conv_id = ?", (cid_expired,)).fetchall()),
                1,
            )
            seed_eng.close()

            eng = MetricsEngine(brain_dir=brain, state_dir=state, orphan_hook_grace_s=60.0)
            try:
                convs = eng.list_conversations()
                cids = {c["conversation_id"] for c in convs["conversations"]}
                # Fresh hook within grace window + live generating hook stay
                self.assertIn(cid_fresh, cids)
                self.assertIn(cid_gen, cids)
                # Expired idle orphan without brain/ dir is pruned from list, disk, and SQLite
                self.assertNotIn(cid_expired, cids)
                self.assertFalse(expired_hk.exists())
                self.assertEqual(
                    eng._db.execute("SELECT COUNT(*) FROM conv_meta WHERE conv_id = ?", (cid_expired,)).fetchone()[0],
                    0,
                )
            finally:
                eng.close()


if __name__ == "__main__":
    unittest.main()
