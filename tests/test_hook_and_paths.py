"""Tests for hooks/run_hook.py, agy_metrics/paths.py parity, and multi-conv generation state."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from helpers import REPO_ROOT, write_jsonl  # noqa: E402
from agy_metrics.engine import MetricsEngine  # noqa: E402
from agy_metrics.paths import get_app_data_dir, get_brain_dir, get_state_dir  # noqa: E402
from hooks import run_hook  # noqa: E402


class TestHookAndPaths(unittest.TestCase):
    def test_hook_subprocess_contract_and_speed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            env = dict(os.environ)
            env["AGY_METRICS_STATE_DIR"] = str(state)
            cid = "cccccccc-cccc-cccc-cccc-cccccccccccc"
            events = [
                ("SessionStart", {}),
                ("PreInvocation", {"invocationNum": 1}),
                ("PostToolUse", {"stepIdx": 2, "toolCall": {"name": "read_file"}}),
                ("PostInvocation", {"invocationNum": 1}),
                ("Stop", {"terminationReason": "NO_TOOL_CALL"}),
                ("Stop", {"terminationReason": "ERROR", "error": "model timeout"}),
            ]
            t0 = time.perf_counter()
            for ev_name, extra in events:
                payload = {
                    "conversationId": cid,
                    "modelName": "auto",
                    "hookEventName": ev_name,
                    **extra,
                }
                proc = subprocess.run(
                    ["python3", "-S", "./hooks/run_hook.py", ev_name],
                    cwd=str(REPO_ROOT),
                    env=env,
                    input=json.dumps(payload),
                    text=True,
                    capture_output=True,
                    check=True,
                )
                out = json.loads(proc.stdout)
                if ev_name == "Stop":
                    self.assertEqual(out.get("decision"), "allow")
                else:
                    self.assertEqual(out, {})
            avg_ms = (time.perf_counter() - t0) / len(events) * 1000.0
            self.assertLess(avg_ms, 50.0, f"Hook too slow: {avg_ms:.2f}ms")

            hook_log = state / "conversations" / f"{cid}.hooks.jsonl"
            self.assertTrue(hook_log.is_file())
            hook_records = [json.loads(line) for line in hook_log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(hook_records), 6)
            self.assertFalse(hook_records[4]["has_error"])
            self.assertTrue(hook_records[5]["has_error"])
            latest = json.loads((state / "latest.json").read_text(encoding="utf-8"))
            self.assertEqual(latest["conversation_id"], cid)
            self.assertEqual(latest["status"], "idle")
            self.assertTrue(latest["has_error"])

    def test_run_hook_path_traversal_and_malformed_stdin_resilience(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            env = dict(os.environ)
            env["AGY_METRICS_STATE_DIR"] = str(state)

            # Unsafe conversationId with traversal is ignored without creating outside files
            bad_proc = subprocess.run(
                ["python3", "-S", "./hooks/run_hook.py", "SessionStart"],
                cwd=str(REPO_ROOT),
                env=env,
                input=json.dumps({"conversationId": "../../escape", "hookEventName": "SessionStart"}),
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(json.loads(bad_proc.stdout), {})
            self.assertFalse(state.exists())

            # Corrupt JSON stdin on Stop still safely allows turn to complete
            stop_proc = subprocess.run(
                ["python3", "-S", "./hooks/run_hook.py", "Stop"],
                cwd=str(REPO_ROOT),
                env=env,
                input="{not-valid-json",
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(json.loads(stop_proc.stdout), {"decision": "allow"})

            # Temp file is unlinked if os.replace fails mid-write
            with (
                patch.dict(os.environ, {"AGY_METRICS_STATE_DIR": str(state)}, clear=False),
                patch("sys.stdin.read", return_value=json.dumps({"conversationId": "conv-err", "hookEventName": "PreInvocation"})),
                patch("hooks.run_hook.os.replace", side_effect=OSError("disk full")),
            ):
                with self.assertRaises(OSError):
                    run_hook._record_event(int(time.time() * 1000), "PreInvocation")
            leftovers = [p for p in state.iterdir() if ".tmp." in p.name]
            self.assertEqual(leftovers, [])

    def test_hook_and_paths_env_var_precedence_parity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_a = root / "app_a"
            app_b = root / "app_b"
            explicit_state = root / "explicit_state"
            plugin_data = root / "plugin_data"

            # Legacy agy_metrics fallback when plugin_data does not exist
            (app_a / "agy_metrics").mkdir(parents=True)
            (app_b / "plugin_data").mkdir(parents=True)

            base_clean = {
                k: v
                for k, v in os.environ.items()
                if k not in ("AGY_METRICS_STATE_DIR", "PLUGIN_DATA", "AGY_METRICS_APP_DATA_DIR", "ANTIGRAVITY_APP_DATA_DIR")
            }

            cases = [
                ({"AGY_METRICS_STATE_DIR": str(explicit_state), "PLUGIN_DATA": str(plugin_data)}, explicit_state),
                ({"PLUGIN_DATA": str(plugin_data)}, plugin_data),
                ({"AGY_METRICS_APP_DATA_DIR": str(app_a), "ANTIGRAVITY_APP_DATA_DIR": str(app_b)}, app_a / "agy_metrics"),
                ({"AGY_METRICS_APP_DATA_DIR": str(app_b)}, app_b / "plugin_data" / "agy-metrics"),
                ({"ANTIGRAVITY_APP_DATA_DIR": str(app_a)}, app_a / "agy_metrics"),
            ]
            for extra_env, expected in cases:
                with patch.dict(os.environ, {**base_clean, **extra_env}, clear=True):
                    self.assertEqual(get_state_dir(), expected, extra_env)
                    self.assertEqual(Path(run_hook._get_state_dir()), expected, extra_env)

    def test_multi_conversation_is_generating_stays_true_after_sibling_stop(self) -> None:
        """Completing one conversation/subagent in latest.json does not hide another generating turn."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"
            cid_main = "conv-multi-main"
            cid_sib = "conv-multi-sibling"
            now_ms = int(time.time() * 1000)

            for cid in (cid_main, cid_sib):
                write_jsonl(
                    brain / cid / ".system_generated" / "logs" / "transcript_full.jsonl",
                    [{"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-15T12:00:00Z", "content": cid}],
                )

            write_jsonl(
                state / "conversations" / f"{cid_main}.hooks.jsonl",
                [{"ts_ms": now_ms - 2000, "event": "PreInvocation", "conversation_id": cid_main}],
            )
            write_jsonl(
                state / "conversations" / f"{cid_sib}.hooks.jsonl",
                [
                    {"ts_ms": now_ms - 1500, "event": "PreInvocation", "conversation_id": cid_sib},
                    {"ts_ms": now_ms - 500, "event": "PostInvocation", "conversation_id": cid_sib},
                ],
            )
            (state / "latest.json").write_text(
                json.dumps(
                    {
                        "ts_ms": now_ms - 500,
                        "event": "PostInvocation",
                        "conversation_id": cid_sib,
                        "status": "idle",
                    }
                ),
                encoding="utf-8",
            )

            eng = MetricsEngine(brain_dir=brain, state_dir=state)
            try:
                eng.refresh_if_changed(fast_poll=True)
                self.assertTrue(eng.is_generating())
            finally:
                eng.close()

    def test_executable_data_dir_and_transcript_path_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exec_data = root / "custom_app" / "sidecar_data" / "agy-metrics" / "dashboard" / "data"
            exec_data.mkdir(parents=True)
            base_clean = {
                k: v
                for k, v in os.environ.items()
                if k
                not in (
                    "AGY_METRICS_STATE_DIR",
                    "PLUGIN_DATA",
                    "AGY_METRICS_APP_DATA_DIR",
                    "ANTIGRAVITY_APP_DATA_DIR",
                    "ANTIGRAVITY_EXECUTABLE_DATA_DIR",
                )
            }
            with patch.dict(
                os.environ,
                {**base_clean, "ANTIGRAVITY_EXECUTABLE_DATA_DIR": str(exec_data)},
                clear=True,
            ):
                self.assertEqual(get_app_data_dir(), root / "custom_app")
                self.assertEqual(get_brain_dir(), root / "custom_app" / "brain")
                self.assertEqual(
                    get_state_dir(), root / "custom_app" / "plugin_data" / "agy-metrics"
                )
                self.assertEqual(
                    Path(run_hook._get_state_dir()),
                    root / "custom_app" / "plugin_data" / "agy-metrics",
                )

            # Fallback scan under ~/.gemini when default antigravity/brain is absent
            fake_home = root / "home"
            (fake_home / ".gemini" / "config" / "brain").mkdir(parents=True)
            (fake_home / ".gemini" / "antigravity_remote" / "brain").mkdir(parents=True)
            with patch.dict(
                os.environ, {**base_clean, "HOME": str(fake_home)}, clear=True
            ):
                self.assertEqual(
                    get_app_data_dir(), fake_home / ".gemini" / "antigravity_remote"
                )

            # run_hook derives <appData> from transcriptPath when env vars are unset
            cid = "ffffffff-1111-2222-3333-444444444444"
            t_path = root / "remote_app" / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"
            t_path.parent.mkdir(parents=True)
            t_path.write_text("", encoding="utf-8")
            proc = subprocess.run(
                ["python3", "-S", "./hooks/run_hook.py", "PreInvocation"],
                cwd=str(REPO_ROOT),
                env={**base_clean, "HOME": str(fake_home)},
                input=json.dumps(
                    {
                        "conversationId": cid,
                        "modelName": "Carbon",
                        "hookEventName": "PreInvocation",
                        "transcriptPath": str(t_path),
                    }
                ),
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(json.loads(proc.stdout), {})
            expected_hook_log = (
                root / "remote_app" / "plugin_data" / "agy-metrics" / "conversations" / f"{cid}.hooks.jsonl"
            )
            self.assertTrue(expected_hook_log.is_file())


if __name__ == "__main__":
    unittest.main()
