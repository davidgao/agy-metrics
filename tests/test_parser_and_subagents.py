"""Tests for transcript/hook parsing, context breakdown, and subagent rollups."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from helpers import write_jsonl  # noqa: E402
from agy_metrics.engine import ALL_CONVERSATIONS_ID, MetricsEngine  # noqa: E402
from agy_metrics.models import humanize_model_name, normalize_step_model  # noqa: E402


class TestParserAndSubagents(unittest.TestCase):
    def test_synthetic_trajectory_hooks_and_subagent_rollup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"
            parent_id = "11111111-1111-1111-1111-111111111111"
            sub_id = "22222222-2222-2222-2222-222222222222"

            parent_log = brain / parent_id / ".system_generated" / "logs" / "transcript_full.jsonl"
            write_jsonl(
                parent_log,
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-14T05:00:00Z",
                        "content": "<USER_REQUEST>\nAnalyze repo layout\n</USER_REQUEST>\n<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to Gemini 4 Carbon (High). No need to comment.\n</USER_SETTINGS_CHANGE>",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-14T05:00:01Z",
                        "input_tokens": 10000,
                        "cache_read_tokens": 0,
                        "output_tokens": 500,
                        "thinking": "Planning tool calls...",
                        "content": "",
                        "tool_calls": [
                            {"name": "read_file", "args": {"Path": '"/tmp/a.py"'}},
                            {"name": "invoke_subagent", "args": {"Role": '"Codebase Researcher"'}},
                        ],
                    },
                    {
                        "step_index": 2,
                        "type": "GENERIC",
                        "created_at": "2026-11-14T05:00:05Z",
                        "content": "Created At: 2026-11-14T05:00:05Z\nCompleted At: 2026-11-14T05:00:06Z\n" + ("x" * 380),
                    },
                    {
                        "step_index": 3,
                        "type": "GENERIC",
                        "created_at": "2026-11-14T05:00:05Z",
                        "content": f"Created At: 2026-11-14T05:00:05Z\nCompleted At: 2026-11-14T05:00:09Z\nLaunched subagent {sub_id}\n",
                    },
                    {
                        "step_index": 4,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-14T05:00:10Z",
                        "input_tokens": 1200,
                        "cache_read_tokens": 10000,
                        "output_tokens": 200,
                        "thinking": "",
                        "content": "All done analyzing.",
                        "tool_calls": [],
                    },
                ],
            )

            sub_log = brain / sub_id / ".system_generated" / "logs" / "transcript_full.jsonl"
            write_jsonl(
                sub_log,
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-14T05:00:06Z",
                        "content": "Research sub-task",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-14T05:00:06Z",
                        "input_tokens": 4000,
                        "cache_read_tokens": 1000,
                        "output_tokens": 150,
                        "thinking": "",
                        "content": "Subagent summary.",
                        "tool_calls": [],
                    },
                ],
            )

            ep1 = int(datetime(2026, 11, 14, 5, 0, 1, tzinfo=timezone.utc).timestamp() * 1000)
            ep4 = int(datetime(2026, 11, 14, 5, 0, 10, tzinfo=timezone.utc).timestamp() * 1000)
            write_jsonl(
                state / "conversations" / f"{parent_id}.hooks.jsonl",
                [
                    {"ts_ms": ep1, "event": "PreInvocation", "conversation_id": parent_id},
                    {"ts_ms": ep1 + 8000, "event": "PostInvocation", "conversation_id": parent_id},
                    {"ts_ms": ep4, "event": "PreInvocation", "conversation_id": parent_id},
                    {"ts_ms": ep4 + 2500, "event": "PostInvocation", "conversation_id": parent_id},
                ],
            )

            engine = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                metrics = engine.get_metrics(parent_id)

                self.assertEqual(metrics["model_name"], "Gemini 4 Carbon (High)")
                self.assertEqual(metrics["planner_steps_count"], 2)

                s1, s2 = metrics["steps"]
                self.assertEqual(s1["total_context"], 10000)
                self.assertEqual(s1["tool_exec_s"], 4.0)
                self.assertEqual(s1["duration_s"], 4.0)
                self.assertEqual(s1["duration_source"], "hook_ms")
                self.assertEqual(s1["effective_tps"], 125.0)
                self.assertGreater(s1["est_thinking_tokens"], 0)

                self.assertEqual(s2["total_context"], 11200)
                self.assertEqual(s2["est_thinking_tokens"], 0)
                self.assertEqual(s2["est_visible_tokens"], 200)
                self.assertEqual(s2["duration_s"], 2.5)
                self.assertEqual(s2["duration_source"], "hook_ms")
                self.assertEqual(s2["effective_tps"], 80.0)

                brk = metrics["context"]["breakdown"]
                self.assertEqual(
                    brk["system_tokens"]
                    + brk["summary_tokens"]
                    + brk["tool_tokens"]
                    + brk["model_tokens"]
                    + brk["user_tokens"],
                    metrics["context"]["current_tokens"],
                )
                self.assertEqual(brk["summary_tokens"], 0)
                self.assertGreater(brk["system_tokens"], 9900)

                self.assertEqual(metrics["subagents"]["count"], 1)
                self.assertEqual(metrics["subagents"]["items"][0]["role"], "Codebase Researcher")
                self.assertEqual(metrics["subagents"]["totals"]["output_tokens"], 150)
                self.assertEqual(metrics["grand_totals"]["output_tokens"], 850)
            finally:
                engine.close()

    def test_partial_jsonl_line_handling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"
            cid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
            log_path = brain / cid / ".system_generated" / "logs" / "transcript_full.jsonl"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            partial = (
                json.dumps(
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-14T05:00:00Z",
                        "content": "Test partial",
                    }
                )
                + "\n"
                + '{"step_index":1,"type":"PLANNER_RESPONSE","input_tokens":1000,'
            )
            log_path.write_text(partial, encoding="utf-8")

            engine = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                m1 = engine.get_metrics(cid)
                self.assertEqual(m1["planner_steps_count"], 0)

                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(
                        '"cache_read_tokens":200,"output_tokens":50,"content":"ok","tool_calls":[]}\n'
                    )

                m2 = engine.get_metrics(cid)
                self.assertEqual(m2["planner_steps_count"], 1)
                self.assertEqual(m2["context"]["current_tokens"], 1200)
            finally:
                engine.close()

    def test_orchestrator_multi_model_attribution_and_aggregation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"
            cid = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
            sub_id = "ffffffff-ffff-ffff-ffff-ffffffffffff"

            def ms(h: int, m: int, s: int) -> int:
                return int(
                    datetime(2026, 11, 14, h, m, s, tzinfo=timezone.utc).timestamp() * 1000
                )

            write_jsonl(
                brain / cid / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-14T06:00:00Z",
                        "content": "<USER_REQUEST>\nInitial question\n</USER_REQUEST>\n<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to Gemini 4 Carbon (High). No need to comment.\n</USER_SETTINGS_CHANGE>",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-14T06:00:01Z",
                        "input_tokens": 10000,
                        "cache_read_tokens": 0,
                        "output_tokens": 200,
                        "thinking": "",
                        "content": "Initial reply.",
                        "tool_calls": [],
                    },
                    {
                        "step_index": 2,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-14T06:01:00Z",
                        "content": "<USER_REQUEST>\nImplement v0.2\n</USER_REQUEST>\n<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from Gemini 4 Carbon (High) to Orchestrator (Carbon). No need to comment.\n</USER_SETTINGS_CHANGE>",
                    },
                    {
                        "step_index": 3,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-14T06:01:01Z",
                        "input_tokens": 12000,
                        "cache_read_tokens": 0,
                        "output_tokens": 400,
                        "thinking": "Planning step 1...",
                        "content": "",
                        "tool_calls": [{"name": "exit_plan_mode", "args": {"plan": '"Step 1"'}}],
                    },
                    {
                        "step_index": 4,
                        "type": "GENERIC",
                        "created_at": "2026-11-14T06:01:05Z",
                        "content": "Created At: 2026-11-14T06:01:05Z\nCompleted At: 2026-11-14T06:01:06Z\nPlan approved.",
                    },
                    {
                        "step_index": 5,
                        "type": "SYSTEM_MESSAGE",
                        "created_at": "2026-11-14T06:01:07Z",
                        "content": "<SYSTEM_MESSAGE>\nPlan mode is off: this turn runs on the execution model.\n</SYSTEM_MESSAGE>",
                    },
                    {
                        "step_index": 6,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-14T06:01:08Z",
                        "input_tokens": 13000,
                        "cache_read_tokens": 0,
                        "output_tokens": 600,
                        "thinking": "",
                        "content": "",
                        "tool_calls": [
                            {"name": "invoke_subagent", "args": {"Role": '"Reviewer"', "Model": '"flash"'}}
                        ],
                    },
                    {
                        "step_index": 7,
                        "type": "GENERIC",
                        "created_at": "2026-11-14T06:01:10Z",
                        "content": f"Created At: 2026-11-14T06:01:10Z\nCompleted At: 2026-11-14T06:01:12Z\nSpawned {sub_id}",
                    },
                    {
                        "step_index": 8,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-14T06:01:13Z",
                        "input_tokens": 1000,
                        "cache_read_tokens": 13600,
                        "output_tokens": 300,
                        "thinking": "",
                        "content": "Done executing.",
                        "tool_calls": [],
                    },
                    {
                        "step_index": 9,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-14T06:02:00Z",
                        "content": "<USER_REQUEST>\nFollow-up review\n</USER_REQUEST>",
                    },
                    {
                        "step_index": 10,
                        "type": "CHECKPOINT",
                        "created_at": "2026-11-14T06:02:01Z",
                        "input_tokens": 500,
                        "cache_read_tokens": 14100,
                        "output_tokens": 1800,
                        "content": "# Resuming\n<summary>\n" + ("a" * 3800) + "\n</summary>",
                    },
                    {
                        "step_index": 11,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-14T06:02:02Z",
                        "input_tokens": 8000,
                        "cache_read_tokens": 0,
                        "output_tokens": 250,
                        "thinking": "Reviewing after checkpoint...",
                        "content": "Reviewed cleanly.",
                        "tool_calls": [],
                    },
                ],
            )

            write_jsonl(
                brain / sub_id / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-14T06:01:10Z",
                        "content": "Review diff",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-14T06:01:11Z",
                        "input_tokens": 2000,
                        "cache_read_tokens": 500,
                        "output_tokens": 120,
                        "thinking": "",
                        "content": "LGTM",
                        "tool_calls": [],
                    },
                ],
            )

            write_jsonl(
                state / "conversations" / f"{cid}.hooks.jsonl",
                [
                    {"ts_ms": ms(6, 0, 1), "event": "PreInvocation", "conversation_id": cid},
                    {"ts_ms": ms(6, 0, 3), "event": "PostInvocation", "conversation_id": cid},
                    {"ts_ms": ms(6, 1, 1), "event": "PreInvocation", "conversation_id": cid},
                    {"ts_ms": ms(6, 1, 6), "event": "PostInvocation", "conversation_id": cid},
                    {"ts_ms": ms(6, 1, 8), "event": "PreInvocation", "conversation_id": cid},
                    {"ts_ms": ms(6, 1, 12), "event": "PostInvocation", "conversation_id": cid},
                    {"ts_ms": ms(6, 1, 13), "event": "PreInvocation", "conversation_id": cid},
                    {"ts_ms": ms(6, 1, 14), "event": "PostInvocation", "conversation_id": cid},
                    {"ts_ms": ms(6, 2, 2), "event": "PreInvocation", "conversation_id": cid},
                    {"ts_ms": ms(6, 2, 4), "event": "PostInvocation", "conversation_id": cid},
                ],
            )

            engine = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                metrics = engine.get_metrics(cid)

                self.assertEqual(metrics["model_name"], "Orchestrator (Carbon) · Planner")
                steps = metrics["steps"]
                self.assertEqual(
                    [s["model_name"] for s in steps],
                    [
                        "Gemini 4 Carbon (High)",
                        "Orchestrator (Carbon) · Planner",
                        "Orchestrator (Carbon) · Executor",
                        "Orchestrator (Carbon) · Executor",
                        "Orchestrator (Carbon) · Planner",
                        "Orchestrator (Carbon) · Planner",
                    ],
                )
                self.assertEqual(
                    [s["step_kind"] for s in steps],
                    ["planner", "planner", "planner", "planner", "checkpoint", "planner"],
                )
                self.assertEqual(
                    [s["model_switched"] for s in steps],
                    [False, True, True, False, True, False],
                )
                ckpt_step, post_step = steps[4], steps[5]
                self.assertIsNone(ckpt_step["duration_s"])
                self.assertIsNone(ckpt_step["effective_tps"])
                self.assertGreater(ckpt_step["est_thinking_tokens"], 0)
                self.assertFalse(ckpt_step["compacted"])
                self.assertTrue(post_step["compacted"])

                brk = metrics["context"]["breakdown"]
                self.assertGreater(brk["system_tokens"], 0)
                self.assertGreater(brk["summary_tokens"], 0)
                self.assertEqual(brk["tool_tokens"], 0)
                self.assertEqual(brk["model_tokens"], 0)
                self.assertEqual(
                    brk["system_tokens"]
                    + brk["summary_tokens"]
                    + brk["tool_tokens"]
                    + brk["model_tokens"]
                    + brk["user_tokens"],
                    metrics["context"]["current_tokens"],
                )

                self.assertEqual(metrics["subagents"]["count"], 1)
                self.assertEqual(metrics["subagents"]["items"][0]["model_name"], "Subagent · Gemini Flash")

                by_model = {bm["model_name"]: bm for bm in metrics["by_model"]}
                self.assertEqual(
                    set(by_model),
                    {
                        "Gemini 4 Carbon (High)",
                        "Orchestrator (Carbon) · Planner",
                        "Orchestrator (Carbon) · Executor",
                    },
                )
                self.assertEqual(by_model["Gemini 4 Carbon (High)"]["steps_count"], 1)
                self.assertEqual(by_model["Orchestrator (Carbon) · Planner"]["steps_count"], 3)
                self.assertEqual(by_model["Orchestrator (Carbon) · Planner"]["compaction_steps_count"], 1)
                self.assertEqual(by_model["Orchestrator (Carbon) · Planner"]["timed_steps_count"], 2)
                self.assertEqual(by_model["Orchestrator (Carbon) · Planner"]["model_time_s"], 6.0)
                self.assertEqual(by_model["Orchestrator (Carbon) · Planner"]["output_tokens"], 2450)
                self.assertAlmostEqual(
                    by_model["Orchestrator (Carbon) · Planner"]["avg_effective_tps"],
                    round(650 / 6.0, 1),
                    places=1,
                )
                self.assertEqual(by_model["Orchestrator (Carbon) · Executor"]["steps_count"], 2)
                self.assertEqual(by_model["Orchestrator (Carbon) · Executor"]["output_tokens"], 900)
                self.assertEqual(by_model["Orchestrator (Carbon) · Executor"]["avg_effective_tps"], 300.0)
                self.assertTrue(by_model["Orchestrator (Carbon) · Planner"]["is_active"])
                self.assertFalse(by_model["Orchestrator (Carbon) · Executor"]["is_active"])

                self.assertEqual(
                    sum(bm["total_tokens"] for bm in metrics["by_model"]),
                    metrics["totals"]["total_tokens"],
                )
                self.assertEqual(
                    sum(bm["model_time_s"] for bm in metrics["by_model"]),
                    metrics["totals"]["model_time_s"],
                )
            finally:
                engine.close()

    def test_orchestrator_inferred_without_settings_change_and_enter_plan_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"
            cid = "99999999-9999-9999-9999-999999999999"

            write_jsonl(
                brain / cid / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-14T07:00:00Z",
                        "content": "Plan something without settings tag",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-14T07:00:01Z",
                        "input_tokens": 5000,
                        "cache_read_tokens": 0,
                        "output_tokens": 100,
                        "content": "",
                        "tool_calls": [{"name": "exit_plan_mode", "args": {"plan": '"Go"'}}],
                    },
                    {
                        "step_index": 2,
                        "type": "SYSTEM_MESSAGE",
                        "created_at": "2026-11-14T07:00:02Z",
                        "content": "Plan mode is off: this turn runs on the execution model.",
                    },
                    {
                        "step_index": 3,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-14T07:00:03Z",
                        "input_tokens": 6000,
                        "cache_read_tokens": 0,
                        "output_tokens": 80,
                        "content": "",
                        "tool_calls": [{"name": "enter_plan_mode", "args": {"reason": '"Need design decision"'}}],
                    },
                    {
                        "step_index": 4,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-14T07:00:05Z",
                        "input_tokens": 7000,
                        "cache_read_tokens": 0,
                        "output_tokens": 120,
                        "content": "Updated plan.",
                        "tool_calls": [],
                    },
                ],
            )

            engine = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                metrics = engine.get_metrics(cid)
                self.assertEqual(
                    [s["model_name"] for s in metrics["steps"]],
                    [
                        "Orchestrator · Planner",
                        "Orchestrator · Executor",
                        "Orchestrator · Planner",
                    ],
                )
                self.assertEqual(
                    [s["model_switched"] for s in metrics["steps"]],
                    [False, True, True],
                )
            finally:
                engine.close()

    def test_checkpoint_latest_step_and_hook_isolation(self) -> None:
        def ms(h: int, m: int, s: int) -> int:
            return int(datetime(2026, 11, 14, h, m, s, tzinfo=timezone.utc).timestamp() * 1000)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"
            cid = "conv-ckpt-latest"

            write_jsonl(
                brain / cid / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-14T07:00:00Z",
                        "content": "<USER_REQUEST>\nInitial ask\n</USER_REQUEST>",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-14T07:00:02Z",
                        "input_tokens": 12000,
                        "cache_read_tokens": 0,
                        "output_tokens": 500,
                        "thinking": "",
                        "content": "Running tools.",
                        "tool_calls": [{"name": "read_file", "args": {"Path": "/tmp/a"}}],
                    },
                    {
                        "step_index": 2,
                        "type": "GENERIC",
                        "created_at": "2026-11-14T07:00:03Z",
                        "content": "x" * 38000,
                    },
                    {
                        "step_index": 3,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-14T07:00:05Z",
                        "input_tokens": 2000,
                        "cache_read_tokens": 20500,
                        "output_tokens": 300,
                        "thinking": "",
                        "content": "Context is large.",
                        "tool_calls": [],
                    },
                    {
                        "step_index": 4,
                        "type": "CHECKPOINT",
                        "created_at": "2026-11-14T07:00:10Z",
                        "input_tokens": 1000,
                        "cache_read_tokens": 21800,
                        "output_tokens": 1500,
                        "content": "# Resuming\n<summary>\n" + ("b" * 3800) + "\n</summary>",
                    },
                ],
            )

            write_jsonl(
                state / "conversations" / f"{cid}.hooks.jsonl",
                [
                    {"ts_ms": ms(7, 0, 1), "event": "PreInvocation", "conversation_id": cid},
                    {"ts_ms": ms(7, 0, 2), "event": "PostInvocation", "conversation_id": cid},
                    {"ts_ms": ms(7, 0, 4), "event": "PreInvocation", "conversation_id": cid},
                    {"ts_ms": ms(7, 0, 5), "event": "PostInvocation", "conversation_id": cid},
                    {"ts_ms": ms(7, 0, 9), "event": "PreInvocation", "conversation_id": cid},
                    {"ts_ms": ms(7, 0, 10), "event": "PostInvocation", "conversation_id": cid},
                ],
            )

            engine = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                metrics = engine.get_metrics(cid)

                steps = metrics["steps"]
                self.assertEqual([s["step_kind"] for s in steps], ["planner", "planner", "checkpoint"])
                ckpt = steps[2]
                self.assertIsNone(ckpt["duration_s"])
                self.assertIsNone(ckpt["effective_tps"])

                self.assertEqual(len(metrics["by_model"]), 1)
                bm = metrics["by_model"][0]
                self.assertEqual(bm["steps_count"], 3)
                self.assertEqual(bm["compaction_steps_count"], 1)
                self.assertEqual(bm["timed_steps_count"], 2)
                self.assertEqual(bm["input_tokens"], 15000)
                self.assertEqual(bm["cache_read_tokens"], 42300)
                self.assertEqual(bm["output_tokens"], 2300)

                self.assertLess(metrics["context"]["current_tokens"], 20000)
                brk = metrics["context"]["breakdown"]
                self.assertGreater(brk["system_tokens"], 0)
                self.assertGreater(brk["summary_tokens"], 0)
                self.assertEqual(brk["tool_tokens"], 0)
                self.assertEqual(brk["model_tokens"], 0)
                self.assertEqual(
                    brk["system_tokens"]
                    + brk["summary_tokens"]
                    + brk["tool_tokens"]
                    + brk["model_tokens"]
                    + brk["user_tokens"],
                    metrics["context"]["current_tokens"],
                )
            finally:
                engine.close()

    def test_multi_phase_orchestrator_compaction_and_roofline_end_to_end(self) -> None:
        """Portable fixture covering Carbon -> Orchestrator Planner/Executor -> CHECKPOINT -> rooflines."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"
            cid = "7e5e5e4e-0000-4000-9000-000000000001"
            t0 = datetime(2026, 11, 14, 15, 0, 0, tzinfo=timezone.utc).timestamp()

            def _iso(ep: float) -> str:
                return datetime.fromtimestamp(ep, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            tr: list[dict] = [
                {
                    "step_index": 0,
                    "type": "USER_INPUT",
                    "created_at": _iso(t0),
                    "content": (
                        "<USER_REQUEST>\nMulti-phase integration fixture\n</USER_REQUEST>\n"
                        "<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to Gemini 4 Carbon (High). No need to comment.\n</USER_SETTINGS_CHANGE>"
                    ),
                },
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "created_at": _iso(t0 + 1),
                    "input_tokens": 5500,
                    "cache_read_tokens": 0,
                    "output_tokens": 200,
                    "thinking": "",
                    "content": "Initial Carbon answer.",
                    "tool_calls": [],
                },
                {
                    "step_index": 2,
                    "type": "USER_INPUT",
                    "created_at": _iso(t0 + 10),
                    "content": (
                        "<USER_REQUEST>\nPlan refactor\n</USER_REQUEST>\n"
                        "<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from Gemini 4 Carbon (High) to Orchestrator (Carbon). No need to comment.\n</USER_SETTINGS_CHANGE>"
                    ),
                },
            ]
            hk: list[dict] = [
                {"ts_ms": int((t0 + 1) * 1000), "event": "PreInvocation", "conversation_id": cid},
                {"ts_ms": int((t0 + 3) * 1000), "event": "PostInvocation", "conversation_id": cid},
            ]
            idx = 3
            cur = t0 + 15
            # 4 Planner steps (150 tok/s nominal + 2.0s TTFT)
            for out_tok in (100, 300, 600, 1000):
                dur_s = 2.0 + out_tok / 150.0
                tr.append(
                    {
                        "step_index": idx,
                        "type": "PLANNER_RESPONSE",
                        "created_at": _iso(cur),
                        "input_tokens": 1000,
                        "cache_read_tokens": 6000,
                        "output_tokens": out_tok,
                        "thinking": "Thinking...",
                        "content": "Planner step.",
                        "tool_calls": [],
                    }
                )
                hk.append({"ts_ms": int(cur * 1000), "event": "PreInvocation", "conversation_id": cid})
                hk.append({"ts_ms": int((cur + dur_s) * 1000), "event": "PostInvocation", "conversation_id": cid})
                idx += 1
                cur += dur_s + 5

            # Compaction checkpoint + switch to Executor (350 tok/s nominal + 0.7s TTFT)
            tr.append(
                {
                    "step_index": idx,
                    "type": "CHECKPOINT",
                    "created_at": _iso(cur),
                    "input_tokens": 500,
                    "cache_read_tokens": 7000,
                    "output_tokens": 900,
                    "content": "# Compaction\n<summary>\n" + ("s" * 2400) + "\n</summary>",
                }
            )
            idx += 1
            cur += 5
            tr.append(
                {
                    "step_index": idx,
                    "type": "SYSTEM_MESSAGE",
                    "created_at": _iso(cur),
                    "content": "<SYSTEM_MESSAGE>\nPlan mode is off: this turn runs on the execution model.\n</SYSTEM_MESSAGE>",
                }
            )
            idx += 1
            cur += 2
            for out_tok in (150, 400, 800, 1400):
                dur_s = 0.7 + out_tok / 350.0
                tr.append(
                    {
                        "step_index": idx,
                        "type": "PLANNER_RESPONSE",
                        "created_at": _iso(cur),
                        "input_tokens": 800,
                        "cache_read_tokens": 5500,
                        "output_tokens": out_tok,
                        "thinking": "",
                        "content": "Executor step.",
                        "tool_calls": [],
                    }
                )
                hk.append({"ts_ms": int(cur * 1000), "event": "PreInvocation", "conversation_id": cid})
                hk.append({"ts_ms": int((cur + dur_s) * 1000), "event": "PostInvocation", "conversation_id": cid})
                idx += 1
                cur += dur_s + 5

            write_jsonl(brain / cid / ".system_generated" / "logs" / "transcript_full.jsonl", tr)
            write_jsonl(state / "conversations" / f"{cid}.hooks.jsonl", hk)

            engine = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                metrics = engine.get_metrics(cid)
                self.assertGreaterEqual(metrics["planner_steps_count"], 9)
                self.assertGreater(metrics["context"]["current_tokens"], 5000)
                self.assertTrue(any(s["step_kind"] == "checkpoint" for s in metrics["steps"]))

                brk = metrics["context"]["breakdown"]
                self.assertGreater(brk["summary_tokens"], 0)
                self.assertEqual(
                    brk["system_tokens"]
                    + brk["summary_tokens"]
                    + brk["tool_tokens"]
                    + brk["model_tokens"]
                    + brk["user_tokens"],
                    metrics["context"]["current_tokens"],
                )

                by_m = {bm["model_name"]: bm for bm in metrics["by_model"]}
                self.assertTrue(
                    {
                        "Gemini 4 Carbon (High)",
                        "Orchestrator (Carbon) · Planner",
                        "Orchestrator (Carbon) · Executor",
                    }.issubset(set(by_m))
                )
                self.assertEqual(
                    sum(bm["total_tokens"] for bm in metrics["by_model"]),
                    metrics["totals"]["total_tokens"],
                )

                planner_fit = by_m["Orchestrator (Carbon) · Planner"]["latency_fit"]
                executor_fit = by_m["Orchestrator (Carbon) · Executor"]["latency_fit"]
                self.assertTrue(planner_fit["available"])
                self.assertTrue(executor_fit["available"])
                self.assertGreater(
                    executor_fit["inferred_decode_tps"],
                    planner_fit["inferred_decode_tps"],
                )
                self.assertLess(
                    executor_fit["inferred_ttft_s"],
                    planner_fit["inferred_ttft_s"],
                )
                self.assertGreater(executor_fit["inferred_decode_tps"], 280.0)
                self.assertLess(executor_fit["inferred_decode_tps"], 450.0)
            finally:
                engine.close()

    def test_compact_transcript_truncated_fields_does_not_inflate_thinking(self) -> None:
        """Compact transcript.jsonl with truncated_fields avoids counting omitted visible chars as thinking."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"
            cid = "conv-compact-truncated"

            write_jsonl(
                brain / cid / ".system_generated" / "logs" / "transcript.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-15T09:00:00Z",
                        "content": "Truncated compact log test",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-15T09:00:02Z",
                        "input_tokens": 2000,
                        "cache_read_tokens": 3000,
                        "output_tokens": 2000,
                        "thinking": "t" * 380,  # ~100 tokens untruncated
                        "content": "truncated...",
                        "tool_calls": [],
                        "truncated_fields": ["content"],
                    },
                    {
                        "step_index": 2,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-15T09:00:10Z",
                        "input_tokens": 500,
                        "cache_read_tokens": 6000,
                        "output_tokens": 1000,
                        "thinking": "t" * 300,
                        "content": "c" * 300,
                        "tool_calls": [],
                        "truncated_fields": ["thinking", "content"],
                    },
                ],
            )

            engine = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                m = engine.get_metrics(cid)
                s1, s2 = m["steps"]
                # Step 1: content truncated, thinking untruncated -> thinking ~= 100 (NOT 1997!)
                self.assertEqual(s1["est_thinking_tokens"], 100)
                self.assertEqual(s1["est_visible_tokens"], 1900)
                # Step 2: both truncated equally -> ~50/50 split instead of all-thinking
                self.assertEqual(s2["est_thinking_tokens"], 500)
                self.assertEqual(s2["est_visible_tokens"], 500)
            finally:
                engine.close()

    def test_orchestrator_inherit_subagent_uses_base_model_without_inflating_savings(self) -> None:
        """Orchestrator invoking Model='inherit' passes Subagent · Gemini 4 Carbon / Hydrogen, not Orchestrator."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"
            cid_parent = "11111111-aaaa-0000-0000-000000000001"
            cid_plan_sub = "22222222-aaaa-0000-0000-000000000002"
            cid_exec_sub = "33333333-aaaa-0000-0000-000000000003"

            write_jsonl(
                brain / cid_parent / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-15T10:00:00Z",
                        "content": (
                            "<USER_REQUEST>\nOrchestrator inherit test\n</USER_REQUEST>\n"
                            "<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to Orchestrator (Carbon). No need to comment.\n</USER_SETTINGS_CHANGE>"
                        ),
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-15T10:00:01Z",
                        "input_tokens": 1_000_000,
                        "cache_read_tokens": 0,
                        "output_tokens": 100_000,
                        "content": "",
                        "tool_calls": [
                            {"name": "invoke_subagent", "args": {"Role": '"Plan Helper"', "Model": '"inherit"'}},
                            {"name": "exit_plan_mode", "args": {"plan": '"Go"'}},
                        ],
                    },
                    {
                        "step_index": 2,
                        "type": "GENERIC",
                        "created_at": "2026-11-15T10:00:02Z",
                        "content": f"Spawned {cid_plan_sub}",
                    },
                    {
                        "step_index": 3,
                        "type": "GENERIC",
                        "created_at": "2026-11-15T10:00:03Z",
                        "content": "Plan approved.",
                    },
                    {
                        "step_index": 4,
                        "type": "SYSTEM_MESSAGE",
                        "created_at": "2026-11-15T10:00:04Z",
                        "content": "<SYSTEM_MESSAGE>\nPlan mode is off: this turn runs on the execution model.\n</SYSTEM_MESSAGE>",
                    },
                    {
                        "step_index": 5,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-15T10:00:05Z",
                        "input_tokens": 1_000_000,
                        "cache_read_tokens": 0,
                        "output_tokens": 100_000,
                        "content": "",
                        "tool_calls": [{"name": "invoke_subagent", "args": {"Role": '"Exec Helper"'}}],
                    },
                    {
                        "step_index": 6,
                        "type": "GENERIC",
                        "created_at": "2026-11-15T10:00:06Z",
                        "content": f"Spawned {cid_exec_sub}",
                    },
                ],
            )
            for scid in (cid_plan_sub, cid_exec_sub):
                write_jsonl(
                    brain / scid / ".system_generated" / "logs" / "transcript_full.jsonl",
                    [
                        {
                            "step_index": 0,
                            "type": "USER_INPUT",
                            "created_at": "2026-11-15T10:00:02Z",
                            "content": "Work",
                        },
                        {
                            "step_index": 1,
                            "type": "PLANNER_RESPONSE",
                            "created_at": "2026-11-15T10:00:03Z",
                            "input_tokens": 1_000_000,
                            "cache_read_tokens": 0,
                            "output_tokens": 100_000,
                            "content": "Done",
                            "tool_calls": [],
                        },
                    ],
                )

            engine = MetricsEngine(brain_dir=brain, state_dir=state, as_of_date="2026-11-15")
            try:
                m = engine.get_metrics(cid_parent)
                sub_by_id = {it["conversation_id"]: it for it in m["subagents"]["items"]}
                self.assertEqual(sub_by_id[cid_plan_sub]["model_name"], "Subagent · Gemini 4 Carbon")
                self.assertEqual(sub_by_id[cid_exec_sub]["model_name"], "Subagent · Gemini 4 Hydrogen")
                # Subagents must NOT increment parent's Orchestrator executor_steps or savings
                orch = m["valuation"]["orchestrator"]
                self.assertEqual(orch["planner_steps"], 1)
                self.assertEqual(orch["executor_steps"], 1)
                self.assertAlmostEqual(orch["savings_usd"], 0.75, places=6)
            finally:
                engine.close()

    def test_nested_subagent_transitive_rollup_and_swarm_workflow_discovery(self) -> None:
        """Parent -> child -> grandchild (via invoke_subagent/swarm/run_workflow) rolls up transitively."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"
            cid_root = "11111111-bbbb-0000-0000-000000000001"
            cid_child = "22222222-bbbb-0000-0000-000000000002"
            cid_grand = "33333333-bbbb-0000-0000-000000000003"
            cid_wf = "44444444-bbbb-0000-0000-000000000004"

            write_jsonl(
                brain / cid_root / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-15T10:00:00Z",
                        "content": "<USER_REQUEST>\nRoot\n</USER_REQUEST>\n<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to Gemini 3 Flash. No need to comment.\n</USER_SETTINGS_CHANGE>",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-15T10:00:01Z",
                        "input_tokens": 1000,
                        "cache_read_tokens": 0,
                        "output_tokens": 100,
                        "content": "",
                        "tool_calls": [
                            {"name": "invoke_subagent", "args": {"Role": '"Lead"', "Model": '"flash"'}},
                            {"name": "run_workflow", "args": {"Description": '"Audit files"'}},
                        ],
                    },
                    {"step_index": 2, "type": "GENERIC", "created_at": "2026-11-15T10:00:02Z", "content": f"Child {cid_child}"},
                    {"step_index": 3, "type": "GENERIC", "created_at": "2026-11-15T10:00:02Z", "content": f"Workflow {cid_wf}"},
                ],
            )
            write_jsonl(
                brain / cid_child / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-15T10:00:02Z", "content": "Child"},
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-15T10:00:03Z",
                        "input_tokens": 500,
                        "cache_read_tokens": 0,
                        "output_tokens": 50,
                        "content": "",
                        "tool_calls": [
                            {"name": "swarm", "args": {"action": "add", "name": "worker", "model": "flash_lite"}}
                        ],
                    },
                    {
                        "step_index": 2,
                        "type": "GENERIC",
                        "created_at": "2026-11-15T10:00:04Z",
                        # Also echo cid_root to confirm parent cycle guard does not loop
                        "content": f"Spawned {cid_grand} under root {cid_root}",
                    },
                ],
            )
            write_jsonl(
                brain / cid_grand / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-15T10:00:04Z", "content": "Worker"},
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-15T10:00:05Z",
                        "input_tokens": 300,
                        "cache_read_tokens": 0,
                        "output_tokens": 30,
                        "content": "Done",
                        "tool_calls": [],
                    },
                ],
            )
            write_jsonl(
                brain / cid_wf / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-15T10:00:04Z", "content": "Workflow"},
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-15T10:00:05Z",
                        "input_tokens": 400,
                        "cache_read_tokens": 0,
                        "output_tokens": 40,
                        "content": "WF done",
                        "tool_calls": [],
                    },
                ],
            )

            engine = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                m = engine.get_metrics(cid_root)
                self.assertEqual(m["subagents"]["count"], 3)
                by_id = {it["conversation_id"]: it for it in m["subagents"]["items"]}
                self.assertEqual(set(by_id), {cid_child, cid_wf, cid_grand})
                self.assertEqual(by_id[cid_grand]["role"], "worker")
                self.assertEqual(by_id[cid_grand]["model_name"], "Subagent · Gemini Flash-Lite")
                self.assertEqual(by_id[cid_wf]["role"], "Audit files")
                self.assertEqual(m["subagents"]["totals"]["output_tokens"], 50 + 30 + 40)
                self.assertEqual(m["grand_totals"]["output_tokens"], 100 + 50 + 30 + 40)

                convs = engine.list_conversations()
                c_map = {c["conversation_id"]: c for c in convs["conversations"]}
                self.assertFalse(c_map[cid_root]["is_subagent"])
                self.assertTrue(c_map[cid_child]["is_subagent"])
                self.assertTrue(c_map[cid_grand]["is_subagent"])
                self.assertEqual(c_map[cid_grand]["parent_id"], cid_child)
            finally:
                engine.close()

    def test_error_and_canceled_transcript_step_guards(self) -> None:
        """Zero-token error/cancel steps do not trigger fake compaction or drop context; token-bearing ones stay untimed."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"
            cid = "11111111-eeee-0000-0000-000000000001"

            write_jsonl(
                brain / cid / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-15T10:00:00Z",
                        "content": "<USER_REQUEST>\nGuard test\n</USER_REQUEST>\n<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to Gemini 4 Carbon (High).\n</USER_SETTINGS_CHANGE>",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "created_at": "2026-11-15T10:00:01Z",
                        "input_tokens": 50000,
                        "cache_read_tokens": 0,
                        "output_tokens": 300,
                        "content": "Baseline step.",
                        "tool_calls": [],
                    },
                    {
                        "step_index": 2,
                        "type": "ERROR_MESSAGE",
                        "created_at": "2026-11-15T10:00:10Z",
                        "content": "Transient upstream error",
                    },
                    {
                        "step_index": 3,
                        "type": "PLANNER_RESPONSE",
                        "status": "ERROR",
                        "created_at": "2026-11-15T10:00:15Z",
                        "input_tokens": 0,
                        "cache_read_tokens": 0,
                        "output_tokens": 0,
                        "content": "",
                        "tool_calls": [],
                    },
                    {
                        "step_index": 4,
                        "type": "CHECKPOINT",
                        "status": "CANCELED",
                        "created_at": "2026-11-15T10:00:20Z",
                        "input_tokens": 0,
                        "cache_read_tokens": 0,
                        "output_tokens": 0,
                        "content": "",
                    },
                    {
                        "step_index": 5,
                        "type": "PLANNER_RESPONSE",
                        "status": "CANCELED",
                        "created_at": "2026-11-15T10:00:25Z",
                        "input_tokens": 0,
                        "cache_read_tokens": 0,
                        "output_tokens": 0,
                        "content": "",
                        "tool_calls": [],
                    },
                    {
                        "step_index": 6,
                        "type": "PLANNER_RESPONSE",
                        "status": "CANCELED",
                        "created_at": "2026-11-15T10:00:30Z",
                        "input_tokens": 2000,
                        "cache_read_tokens": 48000,
                        "output_tokens": 40,
                        "content": "partial canceled output",
                        "tool_calls": [{"name": "read_file", "args": {"Path": "/tmp/x"}}],
                    },
                    {
                        "step_index": 7,
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "created_at": "2026-11-15T10:00:40Z",
                        "input_tokens": 2500,
                        "cache_read_tokens": 48000,
                        "output_tokens": 200,
                        "content": "Recovered reply.",
                        "tool_calls": [],
                    },
                ],
            )

            engine = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                m = engine.get_metrics(cid)
                # 0-token error/cancel steps and canceled checkpoint are skipped -> 3 steps (#1 DONE, #6 CANCELED w/ tokens, #7 DONE)
                self.assertEqual(m["planner_steps_count"], 3)
                self.assertEqual(m["totals"]["compacted_count"], 0)
                self.assertFalse(any(s["compacted"] for s in m["steps"]))
                self.assertEqual(m["context"]["current_tokens"], 50500)
                self.assertEqual(m["totals"]["output_tokens"], 540)
                # Token-bearing canceled step stays untimed and never leaks pending tools
                s_canceled = m["steps"][1]
                self.assertEqual(s_canceled["step_index"], 6)
                self.assertIsNone(s_canceled["duration_s"])
                self.assertIsNone(s_canceled["effective_tps"])
                self.assertEqual(s_canceled["tool_names"], [])
            finally:
                engine.close()

    def test_humanize_and_normalize_model_names(self) -> None:
        cases = [
            ("gemini-4-hydrogen-tiered-high", "Gemini 4 Hydrogen (High)"),
            ("gemini-4-carbon-tiered-high", "Gemini 4 Carbon (High)"),
            ("gemini-4-neon-tiered-high", "Gemini 4 Neon (High)"),
            ("gemini-4-neon-tiered-xhigh", "Gemini 4 Neon (XHigh)"),
            ("neon", "Gemini 4 Neon"),
            ("gemini-3.1-pro-tiered-high", "Gemini 3.1 Pro (High)"),
            ("gemini-3-flash-tiered-low", "Gemini 3 Flash (Low)"),
            ("claude-sonnet-4-6", "Claude Sonnet 4.6"),
            ("claude-opus-4-6-thinking", "Claude Opus 4.6 (Thinking)"),
            ("gpt-oss-120b-medium", "GPT-OSS 120B (Medium)"),
            ("claude-3-5-sonnet-20241022", "Claude 3.5 Sonnet"),
            ("flash_lite", "Gemini Flash-Lite"),
            ("pro", "Gemini Pro"),
            ("Gemini 4 Carbon (High)", "Gemini 4 Carbon (High)"),
            ("Orchestrator (Carbon) · Planner", "Orchestrator (Carbon) · Planner"),
            ("unknown", "unknown"),
            ("auto", "auto"),
        ]
        for raw, expected in cases:
            self.assertEqual(humanize_model_name(raw), expected, raw)

        self.assertEqual(
            normalize_step_model("gemini-4-hydrogen-tiered-high", is_subagent=True),
            "Subagent · Gemini 4 Hydrogen (High)",
        )
        self.assertEqual(
            normalize_step_model("Orchestrator (Carbon) · Executor", is_subagent=True),
            "Subagent · Gemini 4 Hydrogen",
        )
        self.assertEqual(
            normalize_step_model("Orchestrator (Neon) · Planner", is_subagent=True),
            "Subagent · Gemini 4 Neon",
        )
        self.assertEqual(
            normalize_step_model("Subagent · gemini-4-hydrogen-tiered-high", is_subagent=True),
            "Subagent · Gemini 4 Hydrogen (High)",
        )
        self.assertEqual(normalize_step_model("unknown", is_subagent=True), "unknown")
        self.assertEqual(normalize_step_model("auto", is_subagent=True), "auto")

    def test_subagent_hook_model_slug_normalized_and_warm_restarted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"
            cid_parent = "11111111-cccc-0000-0000-000000000001"
            cid_sub = "22222222-cccc-0000-0000-000000000002"

            t0 = int(datetime(2026, 11, 16, 10, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)

            write_jsonl(
                brain / cid_parent / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-16T10:00:00Z",
                        "content": "<USER_REQUEST>\nDelegate work\n</USER_REQUEST>\n<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to Gemini 4 Carbon (High). No need to comment.\n</USER_SETTINGS_CHANGE>",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-16T10:00:01Z",
                        "input_tokens": 10000,
                        "cache_read_tokens": 0,
                        "output_tokens": 100,
                        "content": "",
                        "tool_calls": [
                            {"name": "invoke_subagent", "args": {"Role": '"Helper"'}}
                        ],
                    },
                    {
                        "step_index": 2,
                        "type": "GENERIC",
                        "created_at": "2026-11-16T10:00:02Z",
                        "content": f"Spawned {cid_sub}",
                    },
                ],
            )
            write_jsonl(
                brain / cid_sub / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-16T10:00:02Z",
                        "content": "Execute subtask",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-16T10:00:03Z",
                        "input_tokens": 5000,
                        "cache_read_tokens": 0,
                        "output_tokens": 200,
                        "content": "Subtask complete.",
                        "tool_calls": [],
                    },
                ],
            )
            write_jsonl(
                state / "conversations" / f"{cid_sub}.hooks.jsonl",
                [
                    {
                        "ts_ms": t0 + 3000,
                        "event": "PreInvocation",
                        "conversation_id": cid_sub,
                        "model_name": "gemini-4-hydrogen-tiered-high",
                    },
                    {
                        "ts_ms": t0 + 5000,
                        "event": "PostInvocation",
                        "conversation_id": cid_sub,
                        "model_name": "gemini-4-hydrogen-tiered-high",
                    },
                ],
            )

            eng1 = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                sub_m = eng1.get_metrics(cid_sub)
                self.assertEqual(sub_m["model_name"], "Subagent · Gemini 4 Hydrogen (High)")
                self.assertEqual(
                    sub_m["steps"][0]["model_name"],
                    "Subagent · Gemini 4 Hydrogen (High)",
                )
                self.assertEqual(
                    [bm["model_name"] for bm in sub_m["by_model"]],
                    ["Subagent · Gemini 4 Hydrogen (High)"],
                )
                self.assertEqual(sub_m["by_model"][0]["valuation"]["status"], "priced")

                parent_m = eng1.get_metrics(cid_parent)
                self.assertEqual(
                    parent_m["subagents"]["items"][0]["model_name"],
                    "Subagent · Gemini 4 Hydrogen (High)",
                )

                all_m = eng1.get_metrics(ALL_CONVERSATIONS_ID, window="all")
                self.assertEqual(
                    {bm["model_name"] for bm in all_m["by_model"]},
                    {"Gemini 4 Carbon (High)", "Subagent · Gemini 4 Hydrogen (High)"},
                )
            finally:
                eng1.close()

            eng2 = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                all_m2 = eng2.get_metrics(ALL_CONVERSATIONS_ID, window="all")
                self.assertEqual(eng2._transcript_open_count, 0)
                self.assertEqual(
                    {bm["model_name"] for bm in all_m2["by_model"]},
                    {"Gemini 4 Carbon (High)", "Subagent · Gemini 4 Hydrogen (High)"},
                )
            finally:
                eng2.close()


if __name__ == "__main__":
    unittest.main()
