"""Tests for step latency timing sources, P95 roofline fitting, and backfill."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import random
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from helpers import http_json, running_server, write_jsonl  # noqa: E402
from agy_metrics.engine import ALL_CONVERSATIONS_ID, MetricsEngine  # noqa: E402


class TestRooflineAndTiming(unittest.TestCase):
    def test_idle_gap_regression_for_final_reply_without_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"
            cid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
            log_path = brain / cid / ".system_generated" / "logs" / "transcript_full.jsonl"
            write_jsonl(
                log_path,
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-14T05:00:00Z",
                        "content": "Hello",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-14T05:00:01Z",
                        "input_tokens": 5000,
                        "cache_read_tokens": 0,
                        "output_tokens": 300,
                        "thinking": "",
                        "content": "Final reply.",
                        "tool_calls": [],
                    },
                    {
                        "step_index": 2,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-14T05:06:30Z",
                        "content": "Follow up 6 minutes later",
                    },
                ],
            )
            engine = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                metrics = engine.get_metrics(cid)
                step = metrics["steps"][0]
                self.assertIsNone(step["duration_s"])
                self.assertIsNone(step["effective_tps"])
                self.assertIsNone(metrics["totals"]["avg_effective_tps"])
            finally:
                engine.close()

    def test_fast_subsecond_tool_hook_timing_and_pre_tool_support(self) -> None:
        def ms(h: int, m: int, s: int, extra_ms: int = 0) -> int:
            return int(datetime(2026, 11, 14, h, m, s, tzinfo=timezone.utc).timestamp() * 1000) + extra_ms

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"
            cid = "conv-fast-tool-timing"

            write_jsonl(
                brain / cid / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-14T08:00:00Z",
                        "content": "<USER_REQUEST>\nCheck fast tools\n</USER_REQUEST>",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-14T08:00:00Z",
                        "input_tokens": 9000,
                        "cache_read_tokens": 0,
                        "output_tokens": 400,
                        "thinking": "",
                        "content": "",
                        "tool_calls": [{"name": "read_file", "args": {"Path": "/tmp/foo.py"}}],
                    },
                    {
                        "step_index": 2,
                        "type": "GENERIC",
                        "created_at": "2026-11-14T08:00:12Z",
                        "content": "Created At: 2026-11-14T08:00:12Z\nCompleted At: 2026-11-14T08:00:12Z\ncontents",
                    },
                    {
                        "step_index": 3,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-14T08:01:00Z",
                        "input_tokens": 1000,
                        "cache_read_tokens": 9400,
                        "output_tokens": 342,
                        "thinking": "",
                        "content": "",
                        "tool_calls": [{"name": "mcp_tool", "args": {}}],
                    },
                    {
                        "step_index": 4,
                        "type": "GENERIC",
                        "created_at": "2026-11-14T08:01:05Z",
                        "content": "raw MCP tool result without timestamps",
                    },
                ],
            )

            write_jsonl(
                state / "conversations" / f"{cid}.hooks.jsonl",
                [
                    {"ts_ms": ms(8, 0, 0, 250), "event": "PreInvocation", "conversation_id": cid},
                    {"ts_ms": ms(8, 0, 12, 610), "event": "PostToolUse", "conversation_id": cid, "tool_name": "read_file"},
                    {"ts_ms": ms(8, 0, 12, 640), "event": "PostInvocation", "conversation_id": cid},
                    {"ts_ms": ms(8, 1, 0, 0), "event": "PreInvocation", "conversation_id": cid},
                    {"ts_ms": ms(8, 1, 3, 420), "event": "PreToolUse", "conversation_id": cid, "tool_name": "mcp_tool"},
                    {"ts_ms": ms(8, 1, 5, 920), "event": "PostToolUse", "conversation_id": cid, "tool_name": "mcp_tool"},
                    {"ts_ms": ms(8, 1, 5, 950), "event": "PostInvocation", "conversation_id": cid},
                ],
            )

            engine = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                metrics = engine.get_metrics(cid)
                s1, s2 = metrics["steps"]
                self.assertEqual(s1["tool_exec_s"], 0.0)
                self.assertEqual(s1["duration_source"], "hook_ms")
                self.assertEqual(s1["duration_s"], 12.36)

                self.assertEqual(s2["tool_exec_s"], 2.5)
                self.assertEqual(s2["duration_source"], "hook_ms")
                self.assertEqual(s2["duration_s"], 3.42)
                self.assertEqual(s2["effective_tps"], 100.0)
            finally:
                engine.close()

    def test_roofline_latency_fit_outlier_resistance_and_guards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"
            cid = "conv-theil-sen-fit"

            base_epoch = int(datetime(2026, 11, 14, 9, 0, 0, tzinfo=timezone.utc).timestamp())
            transcript_records: list[dict] = []
            hook_records: list[dict] = []
            step_idx = 0
            cur_s = 0

            def add_model_switch(prev_m: str, next_m: str) -> None:
                nonlocal step_idx, cur_s
                ts_iso = datetime.fromtimestamp(base_epoch + cur_s, tz=timezone.utc).isoformat()
                transcript_records.append(
                    {
                        "step_index": step_idx,
                        "type": "USER_INPUT",
                        "created_at": ts_iso,
                        "content": (
                            "<USER_REQUEST>\nSwitch model\n</USER_REQUEST>\n"
                            "<USER_SETTINGS_CHANGE>\n"
                            f"The user changed setting `Model Selection` from {prev_m} to {next_m}. No need to comment.\n"
                            "</USER_SETTINGS_CHANGE>"
                        ),
                    }
                )
                step_idx += 1
                cur_s += 5

            def add_timed_step(out_tok: int, dur_s: float, cold: bool = False) -> None:
                nonlocal step_idx, cur_s
                ts_iso = datetime.fromtimestamp(base_epoch + cur_s, tz=timezone.utc).isoformat()
                t_ms = (base_epoch + cur_s) * 1000
                transcript_records.append(
                    {
                        "step_index": step_idx,
                        "type": "PLANNER_RESPONSE",
                        "created_at": ts_iso,
                        "input_tokens": 40000 if cold else 2000,
                        "cache_read_tokens": 0 if cold else 8000,
                        "output_tokens": out_tok,
                        "thinking": "",
                        "content": f"Step {step_idx}",
                        "tool_calls": [],
                    }
                )
                hook_records.append(
                    {"ts_ms": t_ms, "event": "PreInvocation", "conversation_id": cid}
                )
                hook_records.append(
                    {
                        "ts_ms": t_ms + int(round(dur_s * 1000)),
                        "event": "PostInvocation",
                        "conversation_id": cid,
                    }
                )
                step_idx += 1
                cur_s += int(dur_s) + 10

            add_model_switch("None", "Model A")
            for n_tok in (200, 500, 1000, 1500, 2500, 4000):
                add_timed_step(n_tok, 4.0 + n_tok / 100.0, cold=False)
            add_timed_step(400, 55.0, cold=False)
            add_timed_step(800, 80.0, cold=True)
            transcript_records.append(
                {
                    "step_index": step_idx,
                    "type": "CHECKPOINT",
                    "created_at": datetime.fromtimestamp(
                        base_epoch + cur_s, tz=timezone.utc
                    ).isoformat(),
                    "input_tokens": 500,
                    "cache_read_tokens": 9500,
                    "output_tokens": 900,
                    "content": "# Summary\n<summary>\ncompacted\n</summary>",
                }
            )
            step_idx += 1
            cur_s += 10

            add_model_switch("Model A", "Model B")
            for n_tok in (100, 400, 900):
                add_timed_step(n_tok, 2.0 + n_tok / 200.0, cold=False)

            add_model_switch("Model B", "Model C")
            for n_tok in (100, 140, 180, 220):
                add_timed_step(n_tok, 2.0 + n_tok / 100.0, cold=False)

            write_jsonl(
                brain / cid / ".system_generated" / "logs" / "transcript_full.jsonl",
                transcript_records,
            )
            write_jsonl(state / "conversations" / f"{cid}.hooks.jsonl", hook_records)

            engine = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                metrics = engine.get_metrics(cid)
                by_m_entries = {bm["model_name"]: bm for bm in metrics["by_model"]}
                by_m = {name: bm["latency_fit"] for name, bm in by_m_entries.items()}

                fit_a = by_m["Model A"]
                self.assertTrue(fit_a["available"])
                self.assertEqual(fit_a["warm_count"], 7)
                self.assertEqual(fit_a["cold_excluded_count"], 1)
                self.assertLessEqual(abs(fit_a["inferred_ttft_s"] - 4.0), 0.25)
                self.assertLessEqual(abs(fit_a["inferred_decode_tps"] - 100.0), 3.0)
                self.assertEqual(by_m_entries["Model A"]["cold_timed_steps_count"], 1)
                self.assertEqual(by_m_entries["Model A"]["cold_model_time_s"], 80.0)
                self.assertIsNotNone(by_m_entries["Model A"]["est_total_delay_s"])

                fit_b = by_m["Model B"]
                self.assertFalse(fit_b["available"])
                self.assertEqual(fit_b["reason"], "min_steps")
                self.assertEqual(fit_b["warm_count"], 3)

                fit_c = by_m["Model C"]
                self.assertFalse(fit_c["available"])
                self.assertEqual(fit_c["reason"], "min_spread")
                self.assertEqual(fit_c["token_spread"], 120)

                self.assertEqual(metrics["totals"]["cold_timed_steps_count"], 1)
                self.assertEqual(metrics["totals"]["est_delay_steps_count"], 7)
                self.assertEqual(metrics["totals"]["warm_timed_steps_count"], 14)
            finally:
                engine.close()

    def test_latency_fit_memoization_skips_redundant_fits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"
            bg_cid = "conv-memo-bg"
            cid = "conv-memo-active"

            now_s = int(time.time())
            t0 = now_s - 200

            bg_log = brain / bg_cid / ".system_generated" / "logs" / "transcript_full.jsonl"
            write_jsonl(
                bg_log,
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": datetime.fromtimestamp(t0, tz=timezone.utc).isoformat(),
                        "content": "<USER_REQUEST>\nBackground conv\n</USER_REQUEST>\n<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to Bg Model. No need to comment.\n</USER_SETTINGS_CHANGE>",
                    },
                    *[
                        {
                            "step_index": i + 1,
                            "type": "PLANNER_RESPONSE",
                            "created_at": datetime.fromtimestamp(t0 + 5 + i * 20, tz=timezone.utc).isoformat(),
                            "input_tokens": 1000,
                            "cache_read_tokens": 2000,
                            "output_tokens": tok,
                            "content": "bg",
                            "tool_calls": [],
                        }
                        for i, tok in enumerate((100, 300, 600, 1000))
                    ],
                ],
            )
            write_jsonl(
                state / "conversations" / f"{bg_cid}.hooks.jsonl",
                [
                    ev
                    for i, tok in enumerate((100, 300, 600, 1000))
                    for ev in (
                        {"ts_ms": (t0 + 5 + i * 20) * 1000, "event": "PreInvocation", "conversation_id": bg_cid},
                        {"ts_ms": (t0 + 5 + i * 20) * 1000 + int((2.0 + tok / 100.0) * 1000), "event": "PostInvocation", "conversation_id": bg_cid},
                    )
                ],
            )

            tr_path = brain / cid / ".system_generated" / "logs" / "transcript_full.jsonl"
            hk_path = state / "conversations" / f"{cid}.hooks.jsonl"

            alpha_pts = [(150, 4.5), (400, 7.0), (800, 11.0), (1400, 17.0)]
            beta_pts = [(200, 3.0), (500, 4.5), (900, 6.5), (1500, 9.5)]
            tr_recs: list[dict] = []
            hk_recs: list[dict] = []
            idx = 0
            cur = t0

            tr_recs.append(
                {
                    "step_index": idx,
                    "type": "USER_INPUT",
                    "created_at": datetime.fromtimestamp(cur, tz=timezone.utc).isoformat(),
                    "content": "<USER_REQUEST>\nMemo test\n</USER_REQUEST>\n<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to Model Alpha. No need to comment.\n</USER_SETTINGS_CHANGE>",
                }
            )
            idx += 1
            cur += 2

            for tok, dur_s in alpha_pts:
                tr_recs.append(
                    {
                        "step_index": idx,
                        "type": "PLANNER_RESPONSE",
                        "created_at": datetime.fromtimestamp(cur, tz=timezone.utc).isoformat(),
                        "input_tokens": 1500,
                        "cache_read_tokens": 5000,
                        "output_tokens": tok,
                        "content": "alpha",
                        "tool_calls": [],
                    }
                )
                hk_recs.extend(
                    [
                        {"ts_ms": cur * 1000, "event": "PreInvocation", "conversation_id": cid},
                        {"ts_ms": cur * 1000 + int(dur_s * 1000), "event": "PostInvocation", "conversation_id": cid},
                    ]
                )
                idx += 1
                cur += int(dur_s) + 2

            tr_recs.append(
                {
                    "step_index": idx,
                    "type": "USER_INPUT",
                    "created_at": datetime.fromtimestamp(cur, tz=timezone.utc).isoformat(),
                    "content": "<USER_REQUEST>\nSwitch to Beta\n</USER_REQUEST>\n<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from Model Alpha to Model Beta. No need to comment.\n</USER_SETTINGS_CHANGE>",
                }
            )
            idx += 1
            cur += 2

            for tok, dur_s in beta_pts:
                tr_recs.append(
                    {
                        "step_index": idx,
                        "type": "PLANNER_RESPONSE",
                        "created_at": datetime.fromtimestamp(cur, tz=timezone.utc).isoformat(),
                        "input_tokens": 1500,
                        "cache_read_tokens": 5000,
                        "output_tokens": tok,
                        "content": "beta",
                        "tool_calls": [],
                    }
                )
                hk_recs.extend(
                    [
                        {"ts_ms": cur * 1000, "event": "PreInvocation", "conversation_id": cid},
                        {"ts_ms": cur * 1000 + int(dur_s * 1000), "event": "PostInvocation", "conversation_id": cid},
                    ]
                )
                idx += 1
                cur += int(dur_s) + 2

            write_jsonl(tr_path, tr_recs)
            write_jsonl(hk_path, hk_recs)

            engine = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                engine.list_conversations()
                self.assertEqual(engine._fit_compute_count, 0)

                m1 = engine.get_metrics(cid)
                self.assertEqual(engine._fit_compute_count, 2)
                self.assertEqual({bm["model_name"] for bm in m1["by_model"]}, {"Model Alpha", "Model Beta"})

                engine.get_metrics(cid)
                engine.get_metrics(cid)
                self.assertEqual(engine._fit_compute_count, 2)

                cur = max(cur + 1, now_s - 5)
                with open(hk_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"ts_ms": cur * 1000, "event": "PreInvocation", "conversation_id": cid}) + "\n")
                m_gen = engine.get_metrics(cid)
                self.assertEqual(m_gen["status"], "generating")
                self.assertEqual(engine._fit_compute_count, 2)

                with open(hk_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"ts_ms": (cur + 4) * 1000, "event": "PostInvocation", "conversation_id": cid}) + "\n")
                    f.write(json.dumps({"ts_ms": (cur + 10) * 1000, "event": "PreInvocation", "conversation_id": cid}) + "\n")
                    f.write(json.dumps({"ts_ms": (cur + 25) * 1000, "event": "PostInvocation", "conversation_id": cid}) + "\n")
                with open(tr_path, "a", encoding="utf-8") as f:
                    f.write(
                        json.dumps(
                            {
                                "step_index": idx,
                                "type": "CHECKPOINT",
                                "created_at": datetime.fromtimestamp(cur + 1, tz=timezone.utc).isoformat(),
                                "input_tokens": 500,
                                "cache_read_tokens": 6000,
                                "output_tokens": 700,
                                "content": "<summary>\ncompacted\n</summary>",
                            }
                        )
                        + "\n"
                    )
                    idx += 1
                    f.write(
                        json.dumps(
                            {
                                "step_index": idx,
                                "type": "PLANNER_RESPONSE",
                                "created_at": datetime.fromtimestamp(cur + 10, tz=timezone.utc).isoformat(),
                                "input_tokens": 35000,
                                "cache_read_tokens": 0,
                                "output_tokens": 600,
                                "content": "cold prefill step",
                                "tool_calls": [],
                            }
                        )
                        + "\n"
                    )
                    idx += 1

                m_cold = engine.get_metrics(cid)
                by_m_cold = {bm["model_name"]: bm for bm in m_cold["by_model"]}
                self.assertEqual(by_m_cold["Model Beta"]["compaction_steps_count"], 1)
                self.assertEqual(by_m_cold["Model Beta"]["latency_fit"]["cold_excluded_count"], 1)
                self.assertEqual(engine._fit_compute_count, 2)

                cur += 30
                with open(tr_path, "a", encoding="utf-8") as f:
                    f.write(
                        json.dumps(
                            {
                                "step_index": idx,
                                "type": "PLANNER_RESPONSE",
                                "created_at": datetime.fromtimestamp(cur, tz=timezone.utc).isoformat(),
                                "input_tokens": 1200,
                                "cache_read_tokens": 8800,
                                "output_tokens": 2100,
                                "content": "new warm beta step",
                                "tool_calls": [],
                            }
                        )
                        + "\n"
                    )
                with open(hk_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"ts_ms": cur * 1000, "event": "PreInvocation", "conversation_id": cid}) + "\n")
                    f.write(json.dumps({"ts_ms": (cur + 12) * 1000, "event": "PostInvocation", "conversation_id": cid}) + "\n")

                m_new = engine.get_metrics(cid)
                by_m_new = {bm["model_name"]: bm for bm in m_new["by_model"]}
                self.assertEqual(by_m_new["Model Beta"]["latency_fit"]["warm_count"], 5)
                self.assertEqual(engine._fit_compute_count, 3)
            finally:
                engine.close()

    def test_multi_tool_streaming_created_at_does_not_inflate_tool_exec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"
            cid_hooks = "conv-multi-tool-hooks"
            cid_no_hooks = "conv-multi-tool-nohooks"
            base_epoch = int(datetime(2026, 11, 14, 10, 0, 0, tzinfo=timezone.utc).timestamp())
            t0_ms = base_epoch * 1000

            def ts_utc(offset_s: int) -> str:
                return datetime.fromtimestamp(base_epoch + offset_s, tz=timezone.utc).isoformat()

            transcript = [
                {
                    "step_index": 0,
                    "type": "USER_INPUT",
                    "created_at": ts_utc(0),
                    "content": "Apply batched edits",
                },
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "created_at": ts_utc(0),
                    "input_tokens": 2000,
                    "cache_read_tokens": 8000,
                    "output_tokens": 5000,
                    "thinking": "",
                    "content": "",
                    "tool_calls": [
                        {"name": "edit_file", "args": {"Path": "/tmp/a.py"}},
                        {"name": "edit_file", "args": {"Path": "/tmp/b.py"}},
                    ],
                },
                {
                    "step_index": 2,
                    "type": "GENERIC",
                    "created_at": ts_utc(3),
                    "content": f"Created At: {ts_utc(3)}\nCompleted At: {ts_utc(23)}\nEdited /tmp/a.py",
                },
                {
                    "step_index": 3,
                    "type": "GENERIC",
                    "created_at": ts_utc(21),
                    "content": f"Created At: {ts_utc(21)}\nCompleted At: {ts_utc(25)}\nEdited /tmp/b.py",
                },
            ]

            write_jsonl(brain / cid_hooks / ".system_generated" / "logs" / "transcript_full.jsonl", transcript)
            write_jsonl(
                state / "conversations" / f"{cid_hooks}.hooks.jsonl",
                [
                    {"ts_ms": t0_ms, "event": "PreInvocation", "conversation_id": cid_hooks},
                    {"ts_ms": t0_ms + 23020, "event": "PostToolUse", "conversation_id": cid_hooks, "step_idx": 2, "tool_name": "edit_file"},
                    {"ts_ms": t0_ms + 25350, "event": "PostToolUse", "conversation_id": cid_hooks, "step_idx": 3, "tool_name": "edit_file"},
                    {"ts_ms": t0_ms + 25380, "event": "PostInvocation", "conversation_id": cid_hooks},
                ],
            )
            write_jsonl(brain / cid_no_hooks / ".system_generated" / "logs" / "transcript_full.jsonl", transcript)

            engine = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                m_hooks = engine.get_metrics(cid_hooks)
                s_hooks = m_hooks["steps"][0]
                self.assertEqual(s_hooks["duration_source"], "hook_ms")
                self.assertEqual(s_hooks["tool_exec_s"], 4.0)
                self.assertEqual(s_hooks["duration_s"], 21.02)
                self.assertLess(s_hooks["effective_tps"], 300.0)

                m_no_hooks = engine.get_metrics(cid_no_hooks)
                s_no_hooks = m_no_hooks["steps"][0]
                self.assertEqual(s_no_hooks["duration_source"], "transcript_s")
                self.assertEqual(s_no_hooks["tool_exec_s"], 4.0)
                self.assertEqual(s_no_hooks["duration_s"], 21.0)
            finally:
                engine.close()

    def test_p95_roofline_recovers_nominal_ceiling_under_traffic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"
            cid = "conv-p95-roofline-traffic"

            rng = random.Random(12345)
            base_epoch = int(datetime(2026, 11, 14, 11, 0, 0, tzinfo=timezone.utc).timestamp())
            tr_recs: list[dict] = []
            hk_recs: list[dict] = []
            step_idx = 0
            cur_s = 0

            def add_switch(prev_m: str, next_m: str) -> None:
                nonlocal step_idx, cur_s
                tr_recs.append(
                    {
                        "step_index": step_idx,
                        "type": "USER_INPUT",
                        "created_at": datetime.fromtimestamp(base_epoch + cur_s, tz=timezone.utc).isoformat(),
                        "content": (
                            "<USER_REQUEST>\nSwitch\n</USER_REQUEST>\n"
                            "<USER_SETTINGS_CHANGE>\n"
                            f"The user changed setting `Model Selection` from {prev_m} to {next_m}. No need to comment.\n"
                            "</USER_SETTINGS_CHANGE>"
                        ),
                    }
                )
                step_idx += 1
                cur_s += 5

            def add_step(out_tok: int, dur_s: float) -> None:
                nonlocal step_idx, cur_s
                t_ms = (base_epoch + cur_s) * 1000
                tr_recs.append(
                    {
                        "step_index": step_idx,
                        "type": "PLANNER_RESPONSE",
                        "created_at": datetime.fromtimestamp(base_epoch + cur_s, tz=timezone.utc).isoformat(),
                        "input_tokens": 2000,
                        "cache_read_tokens": 10000,
                        "output_tokens": out_tok,
                        "thinking": "",
                        "content": f"Step {step_idx}",
                        "tool_calls": [],
                    }
                )
                hk_recs.extend(
                    [
                        {"ts_ms": t_ms, "event": "PreInvocation", "conversation_id": cid},
                        {
                            "ts_ms": t_ms + int(round(dur_s * 1000)),
                            "event": "PostInvocation",
                            "conversation_id": cid,
                        },
                    ]
                )
                step_idx += 1
                cur_s += int(dur_s) + 5

            add_switch("None", "Model Traffic")
            uncongested_toks = [150, 280, 450, 750, 1100, 1600, 2200, 3000, 4000, 5000]
            congested_toks = [rng.randint(150, 5000) for _ in range(30)]
            steps_plan = [(n, rng.uniform(0.0, 0.15)) for n in uncongested_toks] + [
                (n, rng.uniform(3.0, 18.0)) for n in congested_toks
            ]
            rng.shuffle(steps_plan)
            for out_tok, delay in steps_plan:
                add_step(out_tok, 1.0 + out_tok / 300.0 + delay)

            add_switch("Model Traffic", "Model Negative")
            for out_tok, dur_s in [(100, 20.0), (200, 18.0), (300, 15.0), (500, 10.0)]:
                add_step(out_tok, dur_s)

            add_switch("Model Negative", "Model SkewedNeg")
            for out_tok, dur_s in [(100, 10.0), (200, 11.0), (300, 12.0), (400, 13.0), (1200, 9.5)]:
                add_step(out_tok, dur_s)

            write_jsonl(brain / cid / ".system_generated" / "logs" / "transcript_full.jsonl", tr_recs)
            write_jsonl(state / "conversations" / f"{cid}.hooks.jsonl", hk_recs)

            engine = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                metrics = engine.get_metrics(cid)
                by_m_entries = {bm["model_name"]: bm for bm in metrics["by_model"]}
                by_m = {name: bm["latency_fit"] for name, bm in by_m_entries.items()}

                fit_tr = by_m["Model Traffic"]
                self.assertTrue(fit_tr["available"])
                self.assertEqual(fit_tr["quantile_pct"], 95)
                self.assertGreaterEqual(fit_tr["inferred_ttft_s"], 0.75)
                self.assertLessEqual(fit_tr["inferred_ttft_s"], 1.35)
                self.assertGreaterEqual(fit_tr["inferred_decode_tps"], 285.0)
                self.assertLessEqual(fit_tr["inferred_decode_tps"], 315.0)
                self.assertGreater(fit_tr["avg_delay_s"], 2.0)

                bm_tr = by_m_entries["Model Traffic"]
                self.assertEqual(bm_tr["warm_timed_steps_count"], 40)
                self.assertEqual(bm_tr["est_delay_steps_count"], 40)
                self.assertEqual(bm_tr["avg_delay_s"], fit_tr["avg_delay_s"])

                self.assertFalse(by_m["Model Negative"]["available"])
                self.assertEqual(by_m["Model Negative"]["reason"], "non_positive_slope")
                self.assertFalse(by_m["Model SkewedNeg"]["available"])
                self.assertEqual(by_m["Model SkewedNeg"]["reason"], "non_positive_slope")

                self.assertEqual(metrics["totals"]["est_total_delay_s"], bm_tr["est_total_delay_s"])
                self.assertEqual(metrics["totals"]["est_delay_steps_count"], 40)
                self.assertEqual(metrics["totals"]["warm_timed_steps_count"], 49)
            finally:
                engine.close()

    def test_global_roofline_cold_start_backfill_freeze_and_regime_shift(self) -> None:
        def _iso(epoch_s: float) -> str:
            return datetime.fromtimestamp(epoch_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        def _write_warm_conv(
            brain: Path,
            state: Path,
            cid: str,
            title: str,
            model_name: str,
            steps_spec: list[tuple[float, int, float]],
        ) -> None:
            tr: list[dict] = [
                {
                    "step_index": 0,
                    "type": "USER_INPUT",
                    "created_at": _iso(steps_spec[0][0] - 1.0),
                    "content": (
                        f"<USER_REQUEST>\n{title}\n</USER_REQUEST>\n"
                        "<USER_SETTINGS_CHANGE>\n"
                        f"The user changed setting `Model Selection` from None to {model_name}. No need to comment.\n"
                        "</USER_SETTINGS_CHANGE>"
                    ),
                }
            ]
            hk: list[dict] = []
            for idx, (ep_s, out_tok, dur_s) in enumerate(steps_spec, start=1):
                tr.append(
                    {
                        "step_index": idx,
                        "type": "PLANNER_RESPONSE",
                        "created_at": _iso(ep_s),
                        "input_tokens": 300,
                        "cache_read_tokens": 12000,
                        "output_tokens": out_tok,
                        "thinking": "",
                        "content": "ok " * 5,
                        "tool_calls": [],
                    }
                )
                hk.append({"ts_ms": int(ep_s * 1000), "event": "PreInvocation", "conversation_id": cid})
                hk.append({"ts_ms": int((ep_s + dur_s) * 1000), "event": "PostInvocation", "conversation_id": cid})
            write_jsonl(brain / cid / ".system_generated" / "logs" / "transcript_full.jsonl", tr)
            write_jsonl(state / "conversations" / f"{cid}.hooks.jsonl", hk)

        def _fetch_step_delays(
            engine: MetricsEngine, model_name: str, cid: str | None = None
        ) -> list[tuple[str, int, float | None, int]]:
            if cid is not None:
                rows = engine._db.execute(
                    "SELECT conv_id, ordinal, est_delay_s, delay_finalized FROM step_lite "
                    "WHERE model_name = ? AND conv_id = ? AND is_checkpoint = 0 AND cold_cache_spike = 0 "
                    "AND duration_s > 0 ORDER BY ordinal",
                    (model_name, cid),
                ).fetchall()
            else:
                rows = engine._db.execute(
                    "SELECT conv_id, ordinal, est_delay_s, delay_finalized FROM step_lite "
                    "WHERE model_name = ? AND is_checkpoint = 0 AND cold_cache_spike = 0 "
                    "AND duration_s > 0 ORDER BY epoch_s, conv_id, ordinal",
                    (model_name,),
                ).fetchall()
            return [(r[0], int(r[1]), None if r[2] is None else float(r[2]), int(r[3])) for r in rows]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"
            model = "Test Roofline Pro"
            t0 = datetime(2026, 11, 14, 10, 0, 0, tzinfo=timezone.utc).timestamp()

            cid1 = "11111111-0000-0000-0000-000000000001"
            cid2 = "22222222-0000-0000-0000-000000000002"
            cid3 = "33333333-0000-0000-0000-000000000003"
            cid4 = "44444444-0000-0000-0000-000000000004"

            c1_steps = [
                (t0 + 10.0, 200, 2.0),
                (t0 + 20.0, 400, 3.0),
                (t0 + 30.0, 600, 4.5),
            ]
            _write_warm_conv(brain, state, cid1, "Global Conv 1", model, c1_steps)

            engine = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                m1 = engine.get_metrics(cid1)
                bm1 = {bm["model_name"]: bm for bm in m1["by_model"]}[model]
                self.assertFalse(bm1["latency_fit"]["available"])
                self.assertEqual(bm1["latency_fit"]["warm_count"], 3)

                c1_steps.append((t0 + 40.0, 800, 5.0))
                _write_warm_conv(brain, state, cid1, "Global Conv 1", model, c1_steps)
                m1_4 = engine.get_metrics(cid1)
                bm1_4 = {bm["model_name"]: bm for bm in m1_4["by_model"]}[model]
                self.assertTrue(bm1_4["latency_fit"]["available"])
                rows_4 = _fetch_step_delays(engine, model, cid1)
                self.assertTrue(all(d is not None and fin == 0 for _, _, d, fin in rows_4))

                _write_warm_conv(brain, state, cid2, "Global Conv 2", model, [(t0 + 100.0, 500, 4.1)])
                engine._last_full_scan_mono = 0.0
                m2 = engine.get_metrics(cid2)
                self.assertEqual(m2["by_model"][0]["latency_fit"]["warm_count"], 5)
                self.assertGreater(m2["steps"][0]["est_delay_s"], 0.4)

                c3_steps = [
                    (t0 + 200.0 + i * 10.0, 200 + i * 100, 1.0 + (200 + i * 100) / 200.0)
                    for i in range(10)
                ]
                _write_warm_conv(brain, state, cid3, "Global Conv 3", model, c3_steps)
                engine._last_full_scan_mono = 0.0
                mall_15 = engine.get_metrics(ALL_CONVERSATIONS_ID)
                self.assertEqual(
                    {bm["model_name"]: bm for bm in mall_15["by_model"]}[model]["latency_fit"]["warm_count"],
                    15,
                )
                rows_15 = _fetch_step_delays(engine, model)
                self.assertEqual(len(rows_15), 15)
                self.assertTrue(all(d is not None and fin == 1 for _, _, d, fin in rows_15))

                frozen_c1 = [d for _, _, d, _ in _fetch_step_delays(engine, model, cid1)]
                frozen_c2 = [d for _, _, d, _ in _fetch_step_delays(engine, model, cid2)]
                frozen_c2_total = engine.get_metrics(cid2)["totals"]["est_total_delay_s"]

                c4_steps = [
                    (t0 + 500.0 + i * 30.0, 200 + (i % 8) * 150, 2.5 + (200 + (i % 8) * 150) / 80.0)
                    for i in range(90)
                ]
                _write_warm_conv(brain, state, cid4, "Global Conv 4 Slowdown", model, c4_steps)
                engine._last_full_scan_mono = 0.0
                mall_shift = engine.get_metrics(ALL_CONVERSATIONS_ID)
                fit_shift = {bm["model_name"]: bm for bm in mall_shift["by_model"]}[model]["latency_fit"]
                self.assertEqual(fit_shift["warm_count"], 105)
                self.assertEqual(fit_shift["used_count"], 100)
                self.assertGreaterEqual(fit_shift["inferred_decode_tps"], 70.0)
                self.assertLessEqual(fit_shift["inferred_decode_tps"], 92.0)

                self.assertEqual([d for _, _, d, _ in _fetch_step_delays(engine, model, cid1)], frozen_c1)
                self.assertEqual([d for _, _, d, _ in _fetch_step_delays(engine, model, cid2)], frozen_c2)
                self.assertEqual(engine.get_metrics(cid2)["totals"]["est_total_delay_s"], frozen_c2_total)

                with running_server(engine, poll_interval_s=0.1, keepalive_s=0.15) as (_, base):
                    import urllib.request

                    req = urllib.request.Request(
                        f"{base}/events?conversation_id={ALL_CONVERSATIONS_ID}&window=all"
                    )
                    seen_events: set[str] = set()
                    deadline = time.monotonic() + 1.5
                    with urllib.request.urlopen(req, timeout=2.0) as resp:
                        self.assertEqual(resp.status, 200)
                        while time.monotonic() < deadline and not ({"snapshot", "ping"} <= seen_events):
                            raw_line = resp.readline()
                            if not raw_line:
                                break
                            line = raw_line.decode("utf-8", errors="replace").strip()
                            if line.startswith("event:"):
                                seen_events.add(line.split(":", 1)[1].strip())
                    self.assertIn("snapshot", seen_events)
                    self.assertIn("ping", seen_events)
            finally:
                engine.close()

    def test_model_timing_excludes_tools_hooks_and_stale_polls(self) -> None:
        t0 = datetime(2026, 11, 15, 10, 0, 0, tzinfo=timezone.utc).timestamp()

        def _iso(epoch_s: float) -> str:
            return datetime.fromtimestamp(epoch_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"

            # Case A: Stale / batched live poll after tool execution + slow PostInvocation/Stop hooks
            cid_a = "conv-timing-case-a"
            tr_a = brain / cid_a / ".system_generated" / "logs" / "transcript_full.jsonl"
            hk_a = state / "conversations" / f"{cid_a}.hooks.jsonl"
            write_jsonl(
                tr_a,
                [
                    {"step_index": 0, "type": "USER_INPUT", "created_at": _iso(t0), "content": "Start"},
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": _iso(t0 + 1.0),
                        "input_tokens": 1000,
                        "cache_read_tokens": 2000,
                        "output_tokens": 100,
                        "content": "Ready",
                        "tool_calls": [],
                    },
                ],
            )
            write_jsonl(
                hk_a,
                [
                    {"ts_ms": int((t0 + 1.0) * 1000), "event": "PreInvocation", "conversation_id": cid_a},
                    {"ts_ms": int((t0 + 2.0) * 1000), "event": "PostInvocation", "conversation_id": cid_a},
                ],
            )

            now_box = [t0 + 5.0]
            with patch("agy_metrics.engine.time.time", side_effect=lambda: now_box[0]):
                engine = MetricsEngine(brain_dir=brain, state_dir=state)
                try:
                    engine.refresh_if_changed(target_id=cid_a)
                    with open(tr_a, "a", encoding="utf-8") as f:
                        f.write(
                            json.dumps(
                                {
                                    "step_index": 2,
                                    "type": "PLANNER_RESPONSE",
                                    "created_at": _iso(t0 + 10.0),
                                    "input_tokens": 1200,
                                    "cache_read_tokens": 3000,
                                    "output_tokens": 300,
                                    "content": "",
                                    "tool_calls": [{"name": "run_command", "args": {"CommandLine": "sleep 8"}}],
                                }
                            )
                            + "\n"
                        )
                        f.write(
                            json.dumps(
                                {
                                    "step_index": 3,
                                    "type": "GENERIC",
                                    "created_at": _iso(t0 + 12.0),
                                    "content": f"Created At: {_iso(t0 + 12.0)}\nCompleted At: {_iso(t0 + 20.0)}\nok\n",
                                }
                            )
                            + "\n"
                        )
                    with open(hk_a, "a", encoding="utf-8") as f:
                        f.write(json.dumps({"ts_ms": int((t0 + 10.0) * 1000), "event": "PreInvocation", "conversation_id": cid_a}) + "\n")
                        f.write(json.dumps({"ts_ms": int((t0 + 20.02) * 1000), "event": "PostToolUse", "conversation_id": cid_a, "tool_name": "run_command"}) + "\n")
                        f.write(json.dumps({"ts_ms": int((t0 + 20.50) * 1000), "event": "PostInvocation", "conversation_id": cid_a}) + "\n")
                        f.write(json.dumps({"ts_ms": int((t0 + 21.20) * 1000), "event": "Stop", "conversation_id": cid_a}) + "\n")

                    now_box[0] = t0 + 32.0
                    m_a = engine.get_metrics(cid_a)
                    s_a2 = m_a["steps"][1]
                    self.assertEqual(s_a2["duration_source"], "hook_ms")
                    self.assertEqual(s_a2["tool_exec_s"], 8.0)
                    self.assertEqual(s_a2["duration_s"], 2.02)
                finally:
                    engine.close()

            # Case B: Slow sweep mid-tool
            cid_b = "conv-timing-case-b"
            tr_b = brain / cid_b / ".system_generated" / "logs" / "transcript_full.jsonl"
            hk_b = state / "conversations" / f"{cid_b}.hooks.jsonl"
            write_jsonl(
                tr_b,
                [{"step_index": 0, "type": "USER_INPUT", "created_at": _iso(t0), "content": "Run sweep test"}],
            )
            write_jsonl(hk_b, [])
            now_box[0] = t0 + 5.0
            with patch("agy_metrics.engine.time.time", side_effect=lambda: now_box[0]):
                engine = MetricsEngine(brain_dir=brain, state_dir=state)
                try:
                    engine.refresh_if_changed(target_id=cid_b)
                    with open(tr_b, "a", encoding="utf-8") as f:
                        f.write(
                            json.dumps(
                                {
                                    "step_index": 1,
                                    "type": "PLANNER_RESPONSE",
                                    "created_at": _iso(t0 + 10.0),
                                    "input_tokens": 1200,
                                    "cache_read_tokens": 3000,
                                    "output_tokens": 200,
                                    "content": "",
                                    "tool_calls": [{"name": "run_command", "args": {"CommandLine": "sleep 10"}}],
                                }
                            )
                            + "\n"
                        )
                    with open(hk_b, "a", encoding="utf-8") as f:
                        f.write(json.dumps({"ts_ms": int((t0 + 10.0) * 1000), "event": "PreInvocation", "conversation_id": cid_b}) + "\n")

                    now_box[0] = t0 + 14.0
                    engine.refresh_if_changed(target_id=cid_b)

                    with open(tr_b, "a", encoding="utf-8") as f:
                        f.write(
                            json.dumps(
                                {
                                    "step_index": 2,
                                    "type": "GENERIC",
                                    "created_at": _iso(t0 + 11.0),
                                    "content": f"Created At: {_iso(t0 + 11.0)}\nCompleted At: {_iso(t0 + 21.0)}\ndone\n",
                                }
                            )
                            + "\n"
                        )
                    with open(hk_b, "a", encoding="utf-8") as f:
                        f.write(json.dumps({"ts_ms": int((t0 + 21.015) * 1000), "event": "PostToolUse", "conversation_id": cid_b, "tool_name": "run_command"}) + "\n")
                        f.write(json.dumps({"ts_ms": int((t0 + 21.030) * 1000), "event": "PostInvocation", "conversation_id": cid_b}) + "\n")

                    now_box[0] = t0 + 21.2
                    m_b = engine.get_metrics(cid_b)
                    s_b = m_b["steps"][0]
                    self.assertEqual(s_b["duration_source"], "hook_ms")
                    self.assertEqual(s_b["tool_exec_s"], 10.0)
                    self.assertEqual(s_b["duration_s"], 1.02)
                finally:
                    engine.close()

            # Case C: Out-of-order parallel tool completion
            cid_c = "conv-timing-case-c"
            write_jsonl(
                brain / cid_c / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {"step_index": 0, "type": "USER_INPUT", "created_at": _iso(t0), "content": "Parallel tools"},
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": _iso(t0),
                        "input_tokens": 1500,
                        "cache_read_tokens": 4000,
                        "output_tokens": 400,
                        "content": "",
                        "tool_calls": [
                            {"name": "run_command", "args": {"CommandLine": "slow"}},
                            {"name": "read_file", "args": {"Path": "/tmp/fast.py"}},
                        ],
                    },
                    {
                        "step_index": 2,
                        "type": "GENERIC",
                        "created_at": _iso(t0 + 3.0),
                        "content": f"Created At: {_iso(t0 + 3.0)}\nCompleted At: {_iso(t0 + 15.0)}\nslow out\n",
                    },
                    {
                        "step_index": 3,
                        "type": "GENERIC",
                        "created_at": _iso(t0 + 3.0),
                        "content": f"Created At: {_iso(t0 + 3.0)}\nCompleted At: {_iso(t0 + 5.0)}\nfast out\n",
                    },
                ],
            )
            write_jsonl(
                state / "conversations" / f"{cid_c}.hooks.jsonl",
                [
                    {"ts_ms": int(t0 * 1000), "event": "PreInvocation", "conversation_id": cid_c},
                    {"ts_ms": int((t0 + 5.03) * 1000), "event": "PostToolUse", "conversation_id": cid_c, "tool_name": "read_file"},
                    {"ts_ms": int((t0 + 15.40) * 1000), "event": "PostToolUse", "conversation_id": cid_c, "tool_name": "run_command"},
                    {"ts_ms": int((t0 + 15.42) * 1000), "event": "PostInvocation", "conversation_id": cid_c},
                ],
            )
            engine_c = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                m_c = engine_c.get_metrics(cid_c)
                s_c = m_c["steps"][0]
                self.assertEqual(s_c["duration_source"], "hook_ms")
                self.assertEqual(s_c["tool_exec_s"], 12.0)
                self.assertEqual(s_c["duration_s"], 3.03)
            finally:
                engine_c.close()

            # Case D: Mixed batch with headerless ask_question tool does not leak 40s user wait
            cid_d = "conv-timing-case-d"
            write_jsonl(
                brain / cid_d / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {"step_index": 0, "type": "USER_INPUT", "created_at": _iso(t0), "content": "Mixed headered + ask_question"},
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": _iso(t0),
                        "input_tokens": 1500,
                        "cache_read_tokens": 4000,
                        "output_tokens": 250,
                        "content": "",
                        "tool_calls": [
                            {"name": "read_file", "args": {"Path": "/tmp/fast.py"}},
                            {"name": "ask_question", "args": {"questions": []}},
                        ],
                    },
                    {
                        "step_index": 2,
                        "type": "GENERIC",
                        "created_at": _iso(t0 + 2.0),
                        "content": f"Created At: {_iso(t0 + 2.0)}\nCompleted At: {_iso(t0 + 2.0)}\nfast out\n",
                    },
                    {
                        "step_index": 3,
                        "type": "GENERIC",
                        "created_at": _iso(t0 + 2.0),
                        "content": "User answered 40s later without headers",
                    },
                ],
            )
            write_jsonl(
                state / "conversations" / f"{cid_d}.hooks.jsonl",
                [
                    {"ts_ms": int(t0 * 1000), "event": "PreInvocation", "conversation_id": cid_d},
                    {"ts_ms": int((t0 + 42.0) * 1000), "event": "PostToolUse", "conversation_id": cid_d, "tool_name": "ask_question"},
                    {"ts_ms": int((t0 + 42.02) * 1000), "event": "PostInvocation", "conversation_id": cid_d},
                ],
            )
            engine_d = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                m_d = engine_d.get_metrics(cid_d)
                s_d = m_d["steps"][0]
                self.assertEqual(s_d["duration_source"], "transcript_s")
                self.assertLessEqual(s_d["duration_s"], 2.5)
                self.assertEqual(s_d["duration_s"], 2.0)
            finally:
                engine_d.close()

    def test_intra_invocation_retries_and_compaction_span_matching(self) -> None:
        """Delayed >5s starts separate retry_wait_s from inference duration_s while preserving compaction isolation."""
        t0 = datetime(2026, 11, 15, 11, 0, 0, tzinfo=timezone.utc).timestamp()

        def _iso(epoch_s: float) -> str:
            return datetime.fromtimestamp(epoch_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"
            cid = "conv-retry-and-ckpt"

            # 4 baseline warm steps (100 tok/s + 1.0s TTFT), then:
            # - Step 5: warm retry (>5s backoff, with tools): 60s retry wait, 11s inference (1000 out -> 10s roofline + 1s delay)
            # - Step 6: cold retry (>5s backoff, final reply w/ no tools): 300s retry wait, 6s inference
            # - Step 7: CHECKPOINT + post-compaction reply sharing one PreInvocation window -> retry_wait_s MUST be 0.0
            tr: list[dict] = [
                {
                    "step_index": 0,
                    "type": "USER_INPUT",
                    "created_at": _iso(t0),
                    "content": (
                        "<USER_REQUEST>\nRetry and compaction timing\n</USER_REQUEST>\n"
                        "<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to Model RetryTest. No need to comment.\n</USER_SETTINGS_CHANGE>"
                    ),
                }
            ]
            hk: list[dict] = []
            idx = 1
            cur = t0 + 2.0

            for out_tok in (100, 300, 600, 1000):
                dur_s = 1.0 + out_tok / 100.0
                tr.append(
                    {
                        "step_index": idx,
                        "type": "PLANNER_RESPONSE",
                        "created_at": _iso(cur),
                        "input_tokens": 1000,
                        "cache_read_tokens": 5000,
                        "output_tokens": out_tok,
                        "content": "warm baseline",
                        "tool_calls": [],
                    }
                )
                hk.append({"ts_ms": int(cur * 1000), "event": "PreInvocation", "conversation_id": cid})
                hk.append({"ts_ms": int((cur + dur_s) * 1000), "event": "PostInvocation", "conversation_id": cid})
                idx += 1
                cur += dur_s + 5.0

            # Step 5: 60s retry backoff -> warm tool-calling PLANNER_RESPONSE (11s inference + 2s tool)
            inv5_start = cur
            hk.append({"ts_ms": int(inv5_start * 1000), "event": "PreInvocation", "conversation_id": cid})
            tr.append(
                {
                    "step_index": idx,
                    "type": "ERROR_MESSAGE",
                    "created_at": _iso(inv5_start + 30.0),
                    "content": "Upstream 503",
                }
            )
            idx += 1
            step5_start = inv5_start + 60.0
            tr.append(
                {
                    "step_index": idx,
                    "type": "PLANNER_RESPONSE",
                    "created_at": _iso(step5_start),
                    "input_tokens": 1200,
                    "cache_read_tokens": 6000,
                    "output_tokens": 1000,
                    "content": "",
                    "tool_calls": [{"name": "read_file", "args": {"Path": "/tmp/a.py"}}],
                }
            )
            idx += 1
            tr.append(
                {
                    "step_index": idx,
                    "type": "GENERIC",
                    "created_at": _iso(step5_start + 11.0),
                    "content": f"Created At: {_iso(step5_start + 11.0)}\nCompleted At: {_iso(step5_start + 13.0)}\nok",
                }
            )
            hk.append({"ts_ms": int((step5_start + 13.0) * 1000), "event": "PostToolUse", "conversation_id": cid, "step_idx": idx, "tool_name": "read_file"})
            hk.append({"ts_ms": int((step5_start + 13.05) * 1000), "event": "PostInvocation", "conversation_id": cid})
            idx += 1
            cur = step5_start + 20.0

            # Step 6: 300s retry backoff -> cold final no-tool reply (6s inference)
            inv6_start = cur
            hk.append({"ts_ms": int(inv6_start * 1000), "event": "PreInvocation", "conversation_id": cid})
            step6_start = inv6_start + 300.0
            tr.append(
                {
                    "step_index": idx,
                    "type": "PLANNER_RESPONSE",
                    "created_at": _iso(step6_start),
                    "input_tokens": 170000,
                    "cache_read_tokens": 0,
                    "output_tokens": 500,
                    "content": "Recovered after 5m backoff.",
                    "tool_calls": [],
                }
            )
            hk.append({"ts_ms": int((step6_start + 6.0) * 1000), "event": "PostInvocation", "conversation_id": cid})
            hk.append({"ts_ms": int((step6_start + 6.02) * 1000), "event": "Stop", "conversation_id": cid, "terminationReason": "NO_TOOL_CALL"})
            idx += 1
            cur = step6_start + 15.0

            # Step 7: CHECKPOINT inside PreInvocation followed by post-compaction final reply (NO retry_wait_s!)
            inv7_start = cur
            hk.append({"ts_ms": int(inv7_start * 1000), "event": "PreInvocation", "conversation_id": cid})
            tr.append(
                {
                    "step_index": idx,
                    "type": "CHECKPOINT",
                    "created_at": _iso(inv7_start),
                    "input_tokens": 1000,
                    "cache_read_tokens": 10000,
                    "output_tokens": 900,
                    "content": "<summary>\ncompacted\n</summary>",
                }
            )
            idx += 1
            post_ckpt_start = inv7_start + 40.0
            tr.append(
                {
                    "step_index": idx,
                    "type": "PLANNER_RESPONSE",
                    "created_at": _iso(post_ckpt_start),
                    "input_tokens": 20000,
                    "cache_read_tokens": 0,
                    "output_tokens": 300,
                    "content": "Post compaction reply.",
                    "tool_calls": [],
                }
            )
            hk.append({"ts_ms": int((post_ckpt_start + 4.0) * 1000), "event": "PostInvocation", "conversation_id": cid})
            hk.append({"ts_ms": int((post_ckpt_start + 4.02) * 1000), "event": "Stop", "conversation_id": cid, "terminationReason": "NO_TOOL_CALL"})

            write_jsonl(brain / cid / ".system_generated" / "logs" / "transcript_full.jsonl", tr)
            write_jsonl(state / "conversations" / f"{cid}.hooks.jsonl", hk)

            eng1 = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                m = eng1.get_metrics(cid)
                steps = m["steps"]
                s5 = steps[4]
                s6 = steps[5]
                s_ckpt = steps[6]
                s_post_ckpt = steps[7]

                self.assertEqual(s5["retry_wait_s"], 60.0)
                self.assertEqual(s5["retry_count"], 1)
                self.assertEqual(s5["duration_s"], 11.0)
                self.assertEqual(s5["duration_source"], "transcript_s")
                self.assertAlmostEqual(s5["effective_tps"], round(1000 / 11.0, 1), places=1)

                self.assertEqual(s6["retry_wait_s"], 300.0)
                self.assertEqual(s6["retry_count"], 1)
                self.assertTrue(s6["cold_cache_spike"])
                self.assertEqual(s6["duration_s"], 6.0)
                self.assertEqual(s6["duration_source"], "transcript_s")

                self.assertIsNone(s_ckpt["duration_s"])
                self.assertEqual(s_ckpt["retry_wait_s"], 0.0)
                self.assertEqual(s_post_ckpt["retry_wait_s"], 0.0)
                self.assertEqual(s_post_ckpt["duration_s"], 4.0)

                tot = m["totals"]
                expected_inf = round((2.0 + 4.0 + 7.0 + 11.0) + 11.0 + 6.0 + 4.0, 2)
                self.assertEqual(tot["inference_time_s"], expected_inf)
                self.assertEqual(tot["wasted_time_s"], 360.0)
                self.assertEqual(tot["model_time_s"], round(expected_inf + 360.0, 2))
                self.assertEqual(tot["retry_steps_count"], 2)
                self.assertEqual(tot["failed_turns_count"], 0)
                # Warm roofline stays uncorrupted (~100 tok/s, ~1.0s TTFT), and est_total_delay_s == warm + wasted
                fit = m["by_model"][0]["latency_fit"]
                self.assertTrue(fit["available"])
                self.assertLessEqual(abs(fit["inferred_decode_tps"] - 100.0), 5.0)
                self.assertLessEqual(abs(fit["inferred_ttft_s"] - 1.0), 0.25)
                self.assertIsNotNone(tot["est_warm_delay_s"])
                self.assertAlmostEqual(
                    tot["est_total_delay_s"],
                    round(tot["est_warm_delay_s"] + 360.0, 2),
                    places=2,
                )
                # avg_delay_s stays warm-only (does NOT inflate by +72s per step from 360s retry wait)
                self.assertLess(tot["avg_delay_s"], 2.0)
                # avg_effective_tps uses pure inference_time_s, not model_time_s
                self.assertAlmostEqual(
                    tot["avg_effective_tps"],
                    round(tot["timed_output_tokens"] / expected_inf, 1),
                    places=1,
                )
            finally:
                eng1.close()

            # Re-open from cold SQLite cache + re-hydrate after dehydration -> final reply & retry metrics survive
            eng2 = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                eng2._conversations[cid].dehydrate()
                m2 = eng2.get_metrics(cid)
                self.assertEqual(m2["steps"][5]["duration_s"], 6.0)
                self.assertEqual(m2["steps"][5]["retry_wait_s"], 300.0)
                self.assertEqual(m2["totals"]["wasted_time_s"], 360.0)
            finally:
                eng2.close()

    def test_failed_turns_and_user_cancellations_across_windows(self) -> None:
        """Unrecovered failed turns roll into wasted_time_s/model_time_s/delay across windows; user cancellations are safely ignored."""
        now_s = datetime(2026, 11, 15, 20, 0, 0, tzinfo=timezone.utc).timestamp()

        def _iso(epoch_s: float) -> str:
            return datetime.fromtimestamp(epoch_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"

            # Conv 1: 1 successful step, 1 Stop(ERROR) failed turn (45s), 1 transcript ERROR_MESSAGE failed turn (20s),
            #         1 Stop(USER_CANCELED) turn (90s -> MUST be ignored!), and 1 rapid <5s silent abort + resend
            cid_mix = "conv-fail-and-cancel"
            t_old = now_s - 20 * 3600  # 20h ago (inside 24h & all, outside 5h)
            t_recent = now_s - 1800    # 30m ago (inside 5h, 24h, all)

            tr_mix = [
                {
                    "step_index": 0,
                    "type": "USER_INPUT",
                    "created_at": _iso(t_old),
                    "content": "<USER_REQUEST>\nMixed fails & cancels\n</USER_REQUEST>\n<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to Model Mix. No need to comment.\n</USER_SETTINGS_CHANGE>",
                },
                # Recent transcript ERROR_MESSAGE without Stop (20s wasted) + user retry silent abort (<5s) + winning step
                {
                    "step_index": 1,
                    "type": "ERROR_MESSAGE",
                    "created_at": _iso(t_recent + 20.0),
                    "content": "Network reset",
                },
                {
                    "step_index": 2,
                    "type": "USER_INPUT",
                    "created_at": _iso(t_recent + 100.0),
                    "content": "<USER_REQUEST>\nCanceled ask\n</USER_REQUEST>",
                },
                {
                    "step_index": 3,
                    "type": "PLANNER_RESPONSE",
                    "status": "CANCELED",
                    "created_at": _iso(t_recent + 101.0),
                    "input_tokens": 0,
                    "cache_read_tokens": 0,
                    "output_tokens": 0,
                    "content": "",
                    "tool_calls": [],
                },
                {
                    "step_index": 4,
                    "type": "USER_INPUT",
                    "created_at": _iso(t_recent + 200.0),
                    "content": "<USER_REQUEST>\nSilent rapid cancel resend\n</USER_REQUEST>",
                },
                {
                    "step_index": 5,
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": _iso(t_recent + 202.0),
                    "input_tokens": 2000,
                    "cache_read_tokens": 0,
                    "output_tokens": 250,
                    "content": "Final answer.",
                    "tool_calls": [],
                },
            ]
            hk_mix = [
                # 20h ago: PreInvocation -> Stop(ERROR) after 45s
                {"ts_ms": int((t_old + 1.0) * 1000), "event": "PreInvocation", "conversation_id": cid_mix},
                {"ts_ms": int((t_old + 46.0) * 1000), "event": "Stop", "conversation_id": cid_mix, "terminationReason": "ERROR", "has_error": True},
                # 30m ago: PreInvocation -> ERROR_MESSAGE at +20s -> user starts next turn at +100s
                {"ts_ms": int(t_recent * 1000), "event": "PreInvocation", "conversation_id": cid_mix},
                # User-canceled turn: PreInvocation -> Stop(USER_CANCELED) after 90s
                {"ts_ms": int((t_recent + 100.5) * 1000), "event": "PreInvocation", "conversation_id": cid_mix},
                {"ts_ms": int((t_recent + 190.5) * 1000), "event": "Stop", "conversation_id": cid_mix, "terminationReason": "USER_CANCELED"},
                # Rapid silent abort (<5s) followed 2s later by new PreInvocation + PostInvocation (3.0s)
                {"ts_ms": int((t_recent + 200.0) * 1000), "event": "PreInvocation", "conversation_id": cid_mix},
                {"ts_ms": int((t_recent + 202.0) * 1000), "event": "PreInvocation", "conversation_id": cid_mix},
                {"ts_ms": int((t_recent + 205.0) * 1000), "event": "PostInvocation", "conversation_id": cid_mix},
                {"ts_ms": int((t_recent + 205.05) * 1000), "event": "Stop", "conversation_id": cid_mix, "terminationReason": "NO_TOOL_CALL"},
            ]
            write_jsonl(brain / cid_mix / ".system_generated" / "logs" / "transcript_full.jsonl", tr_mix)
            write_jsonl(state / "conversations" / f"{cid_mix}.hooks.jsonl", hk_mix)
            # Simulate stale latest.json left at "generating" while transcript/Stop already closed
            (state / "latest.json").write_text(
                json.dumps({"ts_ms": int((t_recent + 202.0) * 1000), "event": "PreInvocation", "conversation_id": cid_mix, "status": "generating"}),
                encoding="utf-8",
            )

            with patch("agy_metrics.engine.time.time", return_value=now_s):
                eng = MetricsEngine(brain_dir=brain, state_dir=state)
                try:
                    m = eng.get_metrics(cid_mix)
                    self.assertEqual(m["status"], "idle")
                    self.assertFalse(eng.is_generating())
                    # Only 1 real PLANNER_RESPONSE step; rapid silent abort didn't collide with step 5's 3.0s duration!
                    self.assertEqual(m["planner_steps_count"], 1)
                    self.assertEqual(m["steps"][0]["duration_s"], 3.0)
                    self.assertEqual(m["steps"][0]["duration_source"], "hook_ms")
                    self.assertEqual(m["steps"][0]["retry_wait_s"], 0.0)

                    # 45s (Stop: ERROR) + 20s (ERROR_MESSAGE) = 65s wasted; 90s USER_CANCELED & 2s silent abort ignored!
                    tot = m["totals"]
                    self.assertEqual(tot["failed_turns_count"], 2)
                    self.assertEqual(tot["wasted_time_s"], 65.0)
                    self.assertEqual(tot["inference_time_s"], 3.0)
                    self.assertEqual(tot["model_time_s"], 68.0)
                    self.assertEqual(tot["est_total_delay_s"], 65.0)
                    self.assertIsNone(tot["est_warm_delay_s"])

                    # All-Conversations windows split 5h (20s) vs 24h/all (65s) cleanly
                    m5 = eng.get_metrics(ALL_CONVERSATIONS_ID, window="5h")
                    m24 = eng.get_metrics(ALL_CONVERSATIONS_ID, window="24h")
                    self.assertEqual(m5["totals"]["failed_turns_count"], 1)
                    self.assertEqual(m5["totals"]["wasted_time_s"], 20.0)
                    self.assertEqual(m5["totals"]["model_time_s"], 23.0)
                    self.assertEqual(m24["totals"]["failed_turns_count"], 2)
                    self.assertEqual(m24["totals"]["wasted_time_s"], 65.0)
                    self.assertEqual(m24["totals"]["model_time_s"], 68.0)
                finally:
                    eng.close()

            # Silent cancel in tail window followed by USER_INPUT also clears generating immediately
            cid_tail_cancel = "conv-tail-cancel"
            write_jsonl(
                brain / cid_tail_cancel / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {"step_index": 0, "type": "USER_INPUT", "created_at": _iso(now_s - 20.0), "content": "First"},
                    {"step_index": 1, "type": "USER_INPUT", "created_at": _iso(now_s - 5.0), "content": "Resent"},
                ],
            )
            write_jsonl(
                state / "conversations" / f"{cid_tail_cancel}.hooks.jsonl",
                [{"ts_ms": int((now_s - 19.0) * 1000), "event": "PreInvocation", "conversation_id": cid_tail_cancel}],
            )
            with patch("agy_metrics.engine.time.time", return_value=now_s):
                eng2 = MetricsEngine(brain_dir=brain, state_dir=state)
                try:
                    m_tc = eng2.get_metrics(cid_tail_cancel)
                    self.assertEqual(m_tc["status"], "idle")
                    self.assertEqual(m_tc["totals"]["failed_turns_count"], 0)
                    self.assertEqual(m_tc["totals"]["wasted_time_s"], 0.0)
                finally:
                    eng2.close()


if __name__ == "__main__":
    unittest.main()
