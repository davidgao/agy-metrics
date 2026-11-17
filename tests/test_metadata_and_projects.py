"""Tests for Antigravity metadata resolution, live title renames, and project-scoped rollups."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.request

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from helpers import http_json, running_server, write_jsonl  # noqa: E402
from agy_metrics.engine import (  # noqa: E402
    ALL_CONVERSATIONS_ID,
    MetricsEngine,
    PROJECT_SCOPE_PREFIX,
)
from agy_metrics.metadata import parse_pbtxt_title  # noqa: E402


def _create_summaries_db(
    db_path: Path,
    rows: list[tuple[str, str, str, str, str, str, int]],
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE conversation_summaries (
                conversation_id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                workspace_uris TEXT NOT NULL DEFAULT '[]',
                project_id TEXT NOT NULL DEFAULT '',
                agent_name TEXT NOT NULL DEFAULT '',
                parent_conversation_id TEXT NOT NULL DEFAULT '',
                nesting_depth INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO conversation_summaries (
                conversation_id, title, workspace_uris, project_id,
                agent_name, parent_conversation_id, nesting_depth
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()


class TestMetadataAndProjects(unittest.TestCase):
    def test_pbtxt_octal_utf8_and_live_rename_when_dehydrated(self) -> None:
        self.assertEqual(
            parse_pbtxt_title(
                'title:"Caf\\303\\251 \\342\\200\\224 Fix \\"Selector\\""  last_user_view_time:{seconds:1}'
            ),
            'Café — Fix "Selector"',
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_data = root / "antigravity"
            brain = app_data / "brain"
            state = app_data / "plugin_data" / "agy-metrics"
            projects_dir = root / "config" / "projects"
            projects_dir.mkdir(parents=True)

            cid = "11111111-aaaa-1111-aaaa-111111111111"
            write_jsonl(
                brain / cid / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-16T01:00:00Z",
                        "content": "<USER_REQUEST>\nVery long initial prompt text that should be replaced\n</USER_REQUEST>",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-16T01:00:02Z",
                        "input_tokens": 1000,
                        "cache_read_tokens": 500,
                        "output_tokens": 120,
                        "content": "Done.",
                        "tool_calls": [],
                    },
                ],
            )
            _create_summaries_db(
                app_data / "conversation_summaries.db",
                [
                    (
                        cid,
                        "Old Summary Title",
                        '["file:///tmp/demo"]',
                        "",
                        "",
                        "",
                        0,
                    )
                ],
            )
            ann_path = app_data / "annotations" / f"{cid}.pbtxt"
            ann_path.parent.mkdir(parents=True)
            ann_path.write_text('title:"Initial Sidebar Name"\n', encoding="utf-8")
            db_mtime_ns = (app_data / "conversation_summaries.db").stat().st_mtime_ns
            os.utime(ann_path, ns=(db_mtime_ns + 5_000_000, db_mtime_ns + 5_000_000))

            eng1 = MetricsEngine(
                brain_dir=brain,
                state_dir=state,
                app_data_dir=app_data,
                projects_dir=projects_dir,
            )
            try:
                convs1 = eng1.list_conversations()
                self.assertEqual(convs1["conversations"][0]["title"], "Initial Sidebar Name")
            finally:
                eng1.close()

            # Reopen cold (dehydrated from rollup cache) and rename via annotations .pbtxt
            eng2 = MetricsEngine(
                brain_dir=brain,
                state_dir=state,
                app_data_dir=app_data,
                projects_dir=projects_dir,
            )
            try:
                convs_cold = eng2.list_conversations()
                self.assertEqual(convs_cold["conversations"][0]["title"], "Initial Sidebar Name")
                self.assertFalse(eng2._conversations[cid].hydrated)
                etag_before = eng2.get_state_etag(cid, refresh=False)

                ann_path.write_text('title:"Renamed By User \\342\\234\\223"\n', encoding="utf-8")
                os.utime(
                    ann_path,
                    ns=(db_mtime_ns + 20_000_000, db_mtime_ns + 20_000_000),
                )

                self.assertTrue(eng2.refresh_if_changed())
                convs_after = eng2.list_conversations(refresh=False)
                self.assertEqual(convs_after["conversations"][0]["title"], "Renamed By User ✓")
                self.assertEqual(eng2.get_metrics(cid, refresh=False)["title"], "Renamed By User ✓")
                self.assertNotEqual(eng2.get_state_etag(cid, refresh=False), etag_before)

                # Adding/updating a different conversation in conversation_summaries.db bumps
                # the DB mtime past ann_path's mtime without reverting cid's renamed title.
                other_cid = "22222222-bbbb-2222-bbbb-222222222222"
                db_path = app_data / "conversation_summaries.db"
                conn = sqlite3.connect(str(db_path))
                try:
                    conn.execute(
                        """
                        INSERT INTO conversation_summaries (
                            conversation_id, title, workspace_uris, project_id,
                            agent_name, parent_conversation_id, nesting_depth
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (other_cid, "Another Conv", '["file:///tmp/demo"]', "", "", "", 0),
                    )
                    conn.commit()
                finally:
                    conn.close()
                os.utime(
                    db_path,
                    ns=(db_mtime_ns + 40_000_000, db_mtime_ns + 40_000_000),
                )
                eng2.refresh_if_changed()
                self.assertEqual(
                    eng2.list_conversations(refresh=False)["conversations"][0]["title"],
                    "Renamed By User ✓",
                )
                self.assertEqual(
                    eng2.get_metrics(cid, refresh=False)["title"],
                    "Renamed By User ✓",
                )
            finally:
                eng2.close()

    def test_projects_hierarchy_subagent_naming_and_hook_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_data = root / "antigravity"
            brain = app_data / "brain"
            state = app_data / "plugin_data" / "agy-metrics"
            projects_dir = root / "config" / "projects"
            projects_dir.mkdir(parents=True)

            p1_id = "proj-1111"
            p2_id = "proj-2222"
            p_sidecar_id = "proj-sidecar"
            (projects_dir / f"{p1_id}.json").write_text(
                json.dumps(
                    {
                        "id": p1_id,
                        "name": "agy-metrics",
                        "projectResources": {
                            "resources": [
                                {"gitFolder": {"folderUri": "file:///work/agy-metrics"}}
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )
            (projects_dir / f"{p2_id}.json").write_text(
                json.dumps(
                    {
                        "id": p2_id,
                        "name": "other-repo",
                        "projectResources": {
                            "resources": [{"folderUri": "file:///work/other-repo"}]
                        },
                    }
                ),
                encoding="utf-8",
            )
            (projects_dir / f"{p_sidecar_id}.json").write_text(
                json.dumps(
                    {
                        "id": p_sidecar_id,
                        "name": "agy-metrics/dashboard",
                        "projectResources": {},
                        "sidecar": {"sidecarId": "agy-metrics/dashboard"},
                    }
                ),
                encoding="utf-8",
            )

            cid_parent = "aaaaaaaa-1111-1111-1111-111111111111"
            cid_sub = "bbbbbbbb-2222-2222-2222-222222222222"
            cid_hook_only = "cccccccc-3333-3333-3333-333333333333"
            cid_basename = "dddddddd-4444-4444-4444-444444444444"

            write_jsonl(
                brain / cid_parent / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-16T03:00:00Z",
                        "content": "<USER_REQUEST>\nLong parent prompt text\n</USER_REQUEST>",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-16T03:00:02Z",
                        "input_tokens": 2000,
                        "cache_read_tokens": 1000,
                        "output_tokens": 100,
                        "content": "",
                        "tool_calls": [
                            {"name": "invoke_subagent", "args": {"Role": '"Codebase Researcher"'}}
                        ],
                    },
                    {
                        "step_index": 2,
                        "type": "GENERIC",
                        "created_at": "2026-11-16T03:00:03Z",
                        "content": f"Created At: 2026-11-16T03:00:03Z\nCompleted At: 2026-11-16T03:00:05Z\nSubagent {cid_sub}\n",
                    },
                ],
            )
            write_jsonl(
                brain / cid_sub / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-16T03:00:03Z",
                        "content": "Inspect codebase",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-16T03:00:04Z",
                        "input_tokens": 500,
                        "cache_read_tokens": 0,
                        "output_tokens": 50,
                        "content": "Done",
                        "tool_calls": [],
                    },
                ],
            )
            write_jsonl(
                brain / cid_hook_only / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-16T02:00:00Z",
                        "content": "Fix other repo",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-16T02:00:02Z",
                        "input_tokens": 800,
                        "cache_read_tokens": 0,
                        "output_tokens": 40,
                        "content": "Fixed",
                        "tool_calls": [],
                    },
                ],
            )
            write_jsonl(
                state / "conversations" / f"{cid_hook_only}.hooks.jsonl",
                [
                    {
                        "ts_ms": 1794794400000,
                        "event": "SessionStart",
                        "conversation_id": cid_hook_only,
                        "workspace_paths": ["/work/other-repo/subdir"],
                    }
                ],
            )
            write_jsonl(
                brain / cid_basename / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-16T01:00:00Z",
                        "content": "Standalone scratch",
                    }
                ],
            )
            write_jsonl(
                state / "conversations" / f"{cid_basename}.hooks.jsonl",
                [
                    {
                        "ts_ms": 1794790800000,
                        "event": "SessionStart",
                        "conversation_id": cid_basename,
                        "workspace_paths": ["/tmp/unregistered-workspace"],
                    }
                ],
            )

            _create_summaries_db(
                app_data / "conversation_summaries.db",
                [
                    (cid_parent, "UI Selector Work", '["file:///work/agy-metrics"]', p1_id, "", "", 0),
                    (cid_sub, "", '["file:///work/agy-metrics"]', p1_id, "browser", cid_parent, 1),
                ],
            )

            eng = MetricsEngine(
                brain_dir=brain,
                state_dir=state,
                app_data_dir=app_data,
                projects_dir=projects_dir,
            )
            try:
                convs = eng.list_conversations()
                proj_ids = [p["project_id"] for p in convs["projects"]]
                self.assertNotIn(p_sidecar_id, proj_ids)
                self.assertIn(p1_id, proj_ids)
                self.assertIn(p2_id, proj_ids)
                self.assertIn("path:/tmp/unregistered-workspace", proj_ids)

                proj_by_id = {p["project_id"]: p for p in convs["projects"]}
                self.assertEqual(proj_by_id["path:/tmp/unregistered-workspace"]["name"], "unregistered-workspace")
                self.assertEqual(proj_by_id[p1_id]["conversations_count"], 1)
                self.assertEqual(proj_by_id[p1_id]["subagents_count"], 1)

                items = convs["conversations"]
                by_id = {c["conversation_id"]: c for c in items}
                self.assertEqual(by_id[cid_parent]["title"], "UI Selector Work")
                self.assertEqual(by_id[cid_parent]["prompt_preview"], "Long parent prompt text")
                self.assertEqual(by_id[cid_sub]["title"], "Codebase Researcher")
                self.assertEqual(by_id[cid_sub]["agent_name"], "browser")
                self.assertEqual(by_id[cid_sub]["project_id"], p1_id)
                self.assertEqual(by_id[cid_sub]["nesting_depth"], 1)
                self.assertEqual(by_id[cid_hook_only]["project_id"], p2_id)
                self.assertEqual(by_id[cid_hook_only]["project_name"], "other-repo")

                # Subagent immediately follows its parent in tree order
                parent_idx = next(i for i, c in enumerate(items) if c["conversation_id"] == cid_parent)
                self.assertEqual(items[parent_idx + 1]["conversation_id"], cid_sub)

                # Subagent metrics carry parent_title and parent_id; parent subagent items include agent_name
                sub_met = eng.get_metrics(cid_sub)
                self.assertTrue(sub_met["is_subagent"])
                self.assertEqual(sub_met["parent_id"], cid_parent)
                self.assertEqual(sub_met["parent_title"], "UI Selector Work")

                parent_met = eng.get_metrics(cid_parent)
                self.assertEqual(parent_met["subagents"]["items"][0]["agent_name"], "browser")
            finally:
                eng.close()

    def test_project_scoped_aggregate_api_windows_and_sse(self) -> None:
        fixed_now = datetime(2026, 11, 16, 12, 0, 0, tzinfo=timezone.utc).timestamp()

        def _iso(ep_s: float) -> str:
            return datetime.fromtimestamp(ep_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_data = root / "antigravity"
            brain = app_data / "brain"
            state = app_data / "plugin_data" / "agy-metrics"
            projects_dir = root / "config" / "projects"
            projects_dir.mkdir(parents=True)

            p1_id = "p1-uuid"
            p2_id = "p2-uuid"
            (projects_dir / f"{p1_id}.json").write_text(
                json.dumps(
                    {
                        "id": p1_id,
                        "name": "Project One",
                        "projectResources": {"resources": [{"folderUri": "file:///work/one"}]},
                    }
                ),
                encoding="utf-8",
            )
            (projects_dir / f"{p2_id}.json").write_text(
                json.dumps(
                    {
                        "id": p2_id,
                        "name": "Project Two",
                        "projectResources": {"resources": [{"folderUri": "file:///work/two"}]},
                    }
                ),
                encoding="utf-8",
            )

            c1 = "11111111-0000-0000-0000-000000000001"
            c1_sub = "11111111-0000-0000-0000-000000000002"
            c2 = "22222222-0000-0000-0000-000000000001"

            t_old = fixed_now - 48 * 3600
            t_recent = fixed_now - 1800
            t_sub = fixed_now - 900

            write_jsonl(
                brain / c1 / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": _iso(t_old - 5),
                        "content": "<USER_REQUEST>\nP1 Prompt\n</USER_REQUEST>\n<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to Gemini 4 Carbon (High).\n</USER_SETTINGS_CHANGE>",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": _iso(t_old),
                        "input_tokens": 1000,
                        "cache_read_tokens": 500,
                        "output_tokens": 100,
                        "content": "",
                        "tool_calls": [{"name": "invoke_subagent", "args": {"Role": '"P1 Helper"'}}],
                    },
                    {
                        "step_index": 2,
                        "type": "GENERIC",
                        "created_at": _iso(t_old + 2),
                        "content": f"Created At: {_iso(t_old + 2)}\nCompleted At: {_iso(t_old + 4)}\nSpawned {c1_sub}\n",
                    },
                    {
                        "step_index": 3,
                        "type": "PLANNER_RESPONSE",
                        "created_at": _iso(t_recent),
                        "input_tokens": 400,
                        "cache_read_tokens": 1600,
                        "output_tokens": 200,
                        "content": "Recent P1 step",
                        "tool_calls": [],
                    },
                ],
            )
            write_jsonl(
                brain / c1_sub / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": _iso(t_sub - 5),
                        "content": "Subagent task\n<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to Gemini 4 Carbon (High).\n</USER_SETTINGS_CHANGE>",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": _iso(t_sub),
                        "input_tokens": 300,
                        "cache_read_tokens": 200,
                        "output_tokens": 50,
                        "content": "Sub done",
                        "tool_calls": [],
                    },
                ],
            )
            write_jsonl(
                brain / c2 / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": _iso(t_recent - 5),
                        "content": "<USER_REQUEST>\nP2 Prompt\n</USER_REQUEST>\n<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to Gemini 4 Carbon (High).\n</USER_SETTINGS_CHANGE>",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": _iso(t_recent + 60),
                        "input_tokens": 5000,
                        "cache_read_tokens": 0,
                        "output_tokens": 900,
                        "content": "P2 big reply",
                        "tool_calls": [],
                    },
                ],
            )
            _create_summaries_db(
                app_data / "conversation_summaries.db",
                [
                    (c1, "P1 Conv", '["file:///work/one"]', p1_id, "", "", 0),
                    (c1_sub, "", '["file:///work/one"]', p1_id, "research", c1, 1),
                    (c2, "P2 Conv", '["file:///work/two"]', p2_id, "", "", 0),
                ],
            )

            with patch("agy_metrics.engine.time.time", return_value=fixed_now):
                eng = MetricsEngine(
                    brain_dir=brain,
                    state_dir=state,
                    app_data_dir=app_data,
                    projects_dir=projects_dir,
                )
                try:
                    p1_scope = f"{PROJECT_SCOPE_PREFIX}{p1_id}"
                    with running_server(eng, poll_interval_s=0.1) as (_, base):
                        st_all, m_all = http_json(
                            f"{base}/api/metrics?conversation_id={ALL_CONVERSATIONS_ID}&window=all"
                        )
                        self.assertEqual(st_all, 200)
                        self.assertEqual(m_all["totals"]["output_tokens"], 1250)

                        st_p1, m_p1 = http_json(
                            f"{base}/api/metrics?conversation_id={p1_scope}&window=all"
                        )
                        self.assertEqual(st_p1, 200)
                        self.assertEqual(m_p1["mode"], "project")
                        self.assertEqual(m_p1["project_id"], p1_id)
                        self.assertEqual(m_p1["title"], "Project One")
                        self.assertEqual(m_p1["conversations_count"], 1)
                        self.assertEqual(m_p1["subagents_count"], 1)
                        self.assertEqual(m_p1["totals"]["steps_count"], 3)
                        self.assertEqual(m_p1["totals"]["output_tokens"], 350)
                        self.assertEqual(m_p1["latest_step"]["conv_id"], c1_sub)
                        self.assertEqual(m_p1["latest_step"]["conv_title"], "P1 Helper")

                        st_5h, m_p1_5h = http_json(
                            f"{base}/api/metrics?conversation_id={p1_scope}&window=5h"
                        )
                        self.assertEqual(st_5h, 200)
                        self.assertEqual(m_p1_5h["totals"]["steps_count"], 2)
                        self.assertEqual(m_p1_5h["totals"]["output_tokens"], 250)
                        self.assertAlmostEqual(
                            m_p1_5h["valuation"]["windows"]["5h"]["cost_usd"],
                            m_p1_5h["valuation"]["windows"]["selected"]["cost_usd"],
                            places=8,
                        )
                        self.assertGreater(
                            m_p1_5h["valuation"]["windows"]["7d"]["cost_usd"],
                            m_p1_5h["valuation"]["windows"]["5h"]["cost_usd"],
                        )

                        # SSE retarget to project scope
                        sse = urllib.request.urlopen(f"{base}/events", timeout=3)
                        try:
                            _ = sse.readline()
                            data_line = sse.readline().decode("utf-8").strip()
                            _ = sse.readline()
                            sub_id = json.loads(data_line[6:])["sub_id"]

                            _, snap_p1 = http_json(
                                f"{base}/api/snapshot?conversation_id={p1_scope}&window=7d&sub_id={sub_id}"
                            )
                            self.assertTrue(snap_p1["retargeted"])
                            self.assertEqual(snap_p1["metrics"]["mode"], "project")
                            self.assertEqual(snap_p1["metrics"]["totals"]["output_tokens"], 350)
                            self.assertEqual(
                                snap_p1["conversations"]["active_project_id"], p2_id
                            )
                        finally:
                            sse.close()
                finally:
                    eng.close()


if __name__ == "__main__":
    unittest.main()
