"""Tests for bundled pricing resolution, overrides, Orchestrator savings, and pools."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from helpers import REPO_ROOT, write_jsonl  # noqa: E402
from agy_metrics.engine import ALL_CONVERSATIONS_ID, MetricsEngine  # noqa: E402
from agy_metrics.pricing import PricingCatalog  # noqa: E402


class TestPricing(unittest.TestCase):
    def test_pricing_catalog_resolution_and_date_promotion(self) -> None:
        raw_bundled = json.loads(
            (REPO_ROOT / "agy_metrics" / "pricing.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("quota_plans", raw_bundled)

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            cat_pre = PricingCatalog(state_dir=state, as_of_date="2026-11-15")
            self.assertEqual(cat_pre.credits_per_usd, 100.0)
            self.assertFalse(cat_pre.override_active)

            cases = [
                ("Gemini 4 Carbon (High)", "gemini-4-carbon", 2.0, 0.1, 10.0),
                ("Gemini 4 Argon", "gemini-4-argon", 2.0, 0.1, 10.0),
                ("gemini-4-hydrogen", "gemini-4-hydrogen", 1.5, 0.075, 7.5),
                ("Gemini 3 Flash-Lite", "gemini-flash-lite", 0.3, 0.03, 2.5),
                ("Subagent · flash_lite", "gemini-flash-lite", 0.3, 0.03, 2.5),
                ("Gemini 3 Flash", "gemini-flash", 0.75, 0.075, 3.75),
                ("Gemini 3.1 Pro (High)", "gemini-pro", 2.0, 0.2, 12.0),
                ("Claude Opus 4.6 (Thinking)", "claude-opus", 4.0, 0.2, 20.0),
                ("Claude Sonnet 4.6", "claude-sonnet", 2.0, 0.1, 10.0),
                ("Claude Haiku 4.5", "claude-haiku", 0.8, 0.08, 4.0),
                ("GPT-OSS 120B (Medium)", "gpt-oss", 0.15, 0.015, 0.6),
            ]
            for raw_name, exp_id, exp_in, exp_cr, exp_out in cases:
                res = cat_pre.resolve_model(raw_name)
                self.assertEqual(res.status, "priced", raw_name)
                assert res.entry is not None
                self.assertEqual(res.entry.catalog_id, exp_id, raw_name)
                self.assertEqual(
                    res.entry.pool,
                    "partner" if exp_id.startswith(("claude-", "gpt-")) else "gemini",
                    raw_name,
                )
                self.assertAlmostEqual(res.entry.rates["input"], exp_in, places=6, msg=raw_name)
                self.assertAlmostEqual(res.entry.rates["cache_read"], exp_cr, places=6, msg=raw_name)
                self.assertAlmostEqual(res.entry.rates["output"], exp_out, places=6, msg=raw_name)

            exec_carbon = cat_pre.resolve_model("Orchestrator (Carbon) · Executor")
            self.assertEqual(exec_carbon.status, "priced")
            self.assertEqual(exec_carbon.orchestrator_role, "executor")
            assert exec_carbon.entry is not None and exec_carbon.counterfactual_planner is not None
            self.assertEqual(exec_carbon.entry.catalog_id, "gemini-4-hydrogen")
            self.assertEqual(exec_carbon.counterfactual_planner.catalog_id, "gemini-4-carbon")

            exec_argon = cat_pre.resolve_model("Orchestrator (Argon) · Executor")
            self.assertEqual(exec_argon.orchestrator_role, "executor")
            assert exec_argon.counterfactual_planner is not None
            self.assertEqual(exec_argon.counterfactual_planner.catalog_id, "gemini-4-argon")

            plan_carbon = cat_pre.resolve_model("Orchestrator (Carbon) · Planner")
            self.assertEqual(plan_carbon.orchestrator_role, "planner")
            assert plan_carbon.entry is not None
            self.assertEqual(plan_carbon.entry.catalog_id, "gemini-4-carbon")

            for unattr in ("unknown", "auto", ""):
                res_u = cat_pre.resolve_model(unattr)
                self.assertEqual(res_u.status, "unattributed")
                self.assertIsNone(res_u.entry)

            self.assertEqual(cat_pre.resolve_model("some-future-custom-model-9000").status, "unpriced")

            carbon_pre = cat_pre.resolve_model("Gemini 4 Carbon").entry
            flash_pre = cat_pre.resolve_model("Gemini 3 Flash").entry
            assert carbon_pre is not None and flash_pre is not None
            self.assertTrue(carbon_pre.has_future_change)
            self.assertEqual(carbon_pre.future_effective_date, "2026-12-01")
            self.assertAlmostEqual(carbon_pre.future_rates["input"], 4.0, places=6)
            self.assertTrue(flash_pre.has_future_change)
            self.assertEqual(flash_pre.future_effective_date, "2027-01-01")

            cat_dec = PricingCatalog(state_dir=state, as_of_date="2026-12-01")
            carbon_dec = cat_dec.resolve_model("Orchestrator (Carbon) · Planner").entry
            argon_dec = cat_dec.resolve_model("Orchestrator (Argon) · Planner").entry
            flash_dec = cat_dec.resolve_model("Gemini 3 Flash").entry
            assert carbon_dec is not None and argon_dec is not None and flash_dec is not None
            self.assertAlmostEqual(carbon_dec.rates["input"], 4.0, places=6)
            self.assertFalse(carbon_dec.has_future_change)
            self.assertAlmostEqual(argon_dec.rates["input"], 4.0, places=6)
            self.assertFalse(argon_dec.has_future_change)
            self.assertAlmostEqual(flash_dec.rates["input"], 0.75, places=6)
            self.assertTrue(flash_dec.has_future_change)

            cat_jan = PricingCatalog(state_dir=state, as_of_date="2027-01-01")
            flash_jan = cat_jan.resolve_model("Gemini 3 Flash").entry
            assert flash_jan is not None
            self.assertAlmostEqual(flash_jan.rates["input"], 1.5, places=6)
            self.assertFalse(flash_jan.has_future_change)

    def test_valuation_orchestrator_savings_subagents_and_all_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"
            cid_orch = "aaaaaaaa-1111-1111-1111-111111111111"
            cid_sub = "bbbbbbbb-2222-2222-2222-222222222222"
            cid_flash = "cccccccc-3333-3333-3333-333333333333"

            write_jsonl(
                brain / cid_orch / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-15T10:00:00Z",
                        "content": "<USER_REQUEST>\nOrchestrated run\n</USER_REQUEST>\n<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to Orchestrator (Carbon). No need to comment.\n</USER_SETTINGS_CHANGE>",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-15T10:00:01Z",
                        "input_tokens": 1_000_000,
                        "cache_read_tokens": 2_000_000,
                        "output_tokens": 100_000,
                        "thinking": "Plan.",
                        "content": "",
                        "tool_calls": [{"name": "exit_plan_mode", "args": {"plan": '"Go"'}}],
                    },
                    {
                        "step_index": 2,
                        "type": "SYSTEM_MESSAGE",
                        "created_at": "2026-11-15T10:00:05Z",
                        "content": "<SYSTEM_MESSAGE>\nPlan mode is off: this turn runs on the execution model.\n</SYSTEM_MESSAGE>",
                    },
                    {
                        "step_index": 3,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-15T10:00:06Z",
                        "input_tokens": 1_000_000,
                        "cache_read_tokens": 2_000_000,
                        "output_tokens": 100_000,
                        "thinking": "",
                        "content": "",
                        "tool_calls": [
                            {"name": "invoke_subagent", "args": {"Role": '"Fast Helper"', "Model": '"flash_lite"'}}
                        ],
                    },
                    {
                        "step_index": 4,
                        "type": "GENERIC",
                        "created_at": "2026-11-15T10:00:08Z",
                        "content": f"Created At: 2026-11-15T10:00:08Z\nCompleted At: 2026-11-15T10:00:10Z\nSpawned {cid_sub}\n",
                    },
                ],
            )
            write_jsonl(
                brain / cid_sub / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-15T10:00:07Z", "content": "<USER_REQUEST>\nDo subagent task\n</USER_REQUEST>"},
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-15T10:00:08Z",
                        "input_tokens": 1_000_000,
                        "cache_read_tokens": 0,
                        "output_tokens": 100_000,
                        "thinking": "",
                        "content": "Subagent finished.",
                        "tool_calls": [],
                    },
                ],
            )
            write_jsonl(
                brain / cid_flash / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-15T11:00:00Z",
                        "content": "<USER_REQUEST>\nFlash conversation\n</USER_REQUEST>\n<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to Gemini 3 Flash. No need to comment.\n</USER_SETTINGS_CHANGE>",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-15T11:00:02Z",
                        "input_tokens": 1_000_000,
                        "cache_read_tokens": 0,
                        "output_tokens": 200_000,
                        "thinking": "",
                        "content": "Flash reply.",
                        "tool_calls": [],
                    },
                ],
            )

            engine = MetricsEngine(brain_dir=brain, state_dir=state, as_of_date="2026-11-15")
            try:
                m_orch = engine.get_metrics(cid_orch)
                val = m_orch["valuation"]
                self.assertAlmostEqual(val["own"]["cost_usd"], 5.60, places=6)
                self.assertAlmostEqual(val["own"]["credits"], 560.0, places=6)
                self.assertAlmostEqual(val["own"]["future_cost_usd"], 8.80, places=6)
                self.assertAlmostEqual(val["subagents"]["cost_usd"], 0.55, places=6)
                self.assertAlmostEqual(val["cost_usd"], 6.15, places=6)
                self.assertAlmostEqual(val["future_cost_usd"], 9.35, places=6)
                self.assertTrue(val["has_future_change"])
                self.assertEqual(val["future_effective_dates"], ["2026-12-01"])

                orch_sav = val["orchestrator"]
                self.assertTrue(orch_sav["present"])
                self.assertAlmostEqual(orch_sav["savings_usd"], 0.80, places=6)
                self.assertAlmostEqual(orch_sav["savings_pct"], 12.5, places=4)
                self.assertAlmostEqual(orch_sav["future_savings_usd"], 4.00, places=6)
                self.assertAlmostEqual(orch_sav["future_savings_pct"], 31.2, places=4)

                self.assertAlmostEqual(m_orch["steps"][0]["cost_usd"], 3.20, places=6)
                self.assertAlmostEqual(m_orch["steps"][1]["cost_usd"], 2.40, places=6)
                self.assertAlmostEqual(m_orch["subagents"]["items"][0]["cost_usd"], 0.55, places=6)

                m_flash = engine.get_metrics(cid_flash)
                self.assertAlmostEqual(m_flash["valuation"]["cost_usd"], 1.50, places=6)
                self.assertAlmostEqual(m_flash["valuation"]["future_cost_usd"], 3.00, places=6)
                self.assertFalse(m_flash["valuation"]["orchestrator"]["present"])

                engine2 = MetricsEngine(brain_dir=brain, state_dir=state, as_of_date="2026-11-15")
                try:
                    m_all = engine2.get_metrics(ALL_CONVERSATIONS_ID, window="all")
                    self.assertEqual(engine2._transcript_open_count, 0)
                    self.assertEqual(sum(1 for c in engine2._conversations.values() if c.hydrated), 0)
                finally:
                    engine2.close()
                v_all = m_all["valuation"]
                self.assertAlmostEqual(v_all["cost_usd"], 7.65, places=6)
                self.assertAlmostEqual(v_all["future_cost_usd"], 12.35, places=6)
                self.assertEqual(v_all["future_effective_dates"], ["2026-12-01", "2027-01-01"])

                dumped = json.dumps(m_all).lower()
                self.assertNotIn("cache_savings", dumped)
                self.assertNotIn("kv_savings", dumped)
            finally:
                engine.close()

    def test_multi_window_5h_7d_pool_and_model_scoped_valuation(self) -> None:
        fixed_now = datetime(2026, 11, 15, 18, 25, 0, tzinfo=timezone.utc).timestamp()

        def _iso(ep: float) -> str:
            return datetime.fromtimestamp(ep, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            state = root / "state"
            cid_span = "eeeeeeee-5555-5555-5555-555555555555"
            cid_sub = "ffffffff-6666-6666-6666-666666666666"
            cid_fresh = "11111111-7777-7777-7777-777777777777"
            cid_arch = "22222222-8888-8888-8888-888888888888"

            t_10d = fixed_now - 10 * 86400
            t_2d = fixed_now - 2 * 86400
            t_2h = fixed_now - 2 * 3600
            t_1h = fixed_now - 3600
            t_4h55m = fixed_now - (4 * 3600 + 55 * 60)
            t_14d = fixed_now - 14 * 86400

            write_jsonl(
                brain / cid_span / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": _iso(t_10d - 1.0),
                        "content": "<USER_REQUEST>\nStart on Sonnet\n</USER_REQUEST>\n<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to Claude Sonnet 4.6. No need to comment.\n</USER_SETTINGS_CHANGE>",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": _iso(t_10d),
                        "input_tokens": 1_000_000,
                        "cache_read_tokens": 2_000_000,
                        "output_tokens": 100_000,
                        "content": "Partner step done.",
                        "tool_calls": [],
                    },
                    {
                        "step_index": 2,
                        "type": "USER_INPUT",
                        "created_at": _iso(t_2d - 1.0),
                        "content": "<USER_REQUEST>\nContinue with Orchestrator\n</USER_REQUEST>\n<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from Claude Sonnet 4.6 to Orchestrator (Carbon). No need to comment.\n</USER_SETTINGS_CHANGE>",
                    },
                    {
                        "step_index": 3,
                        "type": "PLANNER_RESPONSE",
                        "created_at": _iso(t_2d),
                        "input_tokens": 1_000_000,
                        "cache_read_tokens": 2_000_000,
                        "output_tokens": 100_000,
                        "thinking": "Planning.",
                        "content": "",
                        "tool_calls": [{"name": "exit_plan_mode", "args": {"plan": '"Go"'}}],
                    },
                    {
                        "step_index": 4,
                        "type": "SYSTEM_MESSAGE",
                        "created_at": _iso(t_2h - 1.0),
                        "content": "<SYSTEM_MESSAGE>\nPlan mode is off: this turn runs on the execution model.\n</SYSTEM_MESSAGE>",
                    },
                    {
                        "step_index": 5,
                        "type": "PLANNER_RESPONSE",
                        "created_at": _iso(t_2h),
                        "input_tokens": 1_000_000,
                        "cache_read_tokens": 2_000_000,
                        "output_tokens": 100_000,
                        "content": "",
                        "tool_calls": [{"name": "invoke_subagent", "args": {"Role": '"Helper"', "Model": '"flash_lite"'}}],
                    },
                    {
                        "step_index": 6,
                        "type": "GENERIC",
                        "created_at": _iso(t_2h + 1.0),
                        "content": f"Created At: {_iso(t_2h + 1.0)}\nCompleted At: {_iso(t_2h + 2.0)}\nSpawned {cid_sub}\n",
                    },
                ],
            )
            write_jsonl(
                brain / cid_sub / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {"step_index": 0, "type": "USER_INPUT", "created_at": _iso(t_1h - 1.0), "content": "<USER_REQUEST>\nRun subagent\n</USER_REQUEST>"},
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": _iso(t_1h),
                        "input_tokens": 1_000_000,
                        "cache_read_tokens": 0,
                        "output_tokens": 100_000,
                        "content": "Subagent finished.",
                        "tool_calls": [],
                    },
                ],
            )
            write_jsonl(
                brain / cid_fresh / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": _iso(t_4h55m - 1.0),
                        "content": "<USER_REQUEST>\nFresh Haiku run\n</USER_REQUEST>\n<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to Claude Haiku 4.5. No need to comment.\n</USER_SETTINGS_CHANGE>",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": _iso(t_4h55m),
                        "input_tokens": 1_000_000,
                        "cache_read_tokens": 1_000_000,
                        "output_tokens": 100_000,
                        "content": "Haiku reply.",
                        "tool_calls": [],
                    },
                ],
            )
            write_jsonl(
                brain / cid_arch / ".system_generated" / "logs" / "transcript_full.jsonl",
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": _iso(t_14d - 1.0),
                        "content": "<USER_REQUEST>\nArchived Flash run\n</USER_REQUEST>\n<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to Gemini 3 Flash. No need to comment.\n</USER_SETTINGS_CHANGE>",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": _iso(t_14d),
                        "input_tokens": 1_000_000,
                        "cache_read_tokens": 0,
                        "output_tokens": 200_000,
                        "content": "Old flash reply.",
                        "tool_calls": [],
                    },
                ],
            )

            with patch("agy_metrics.engine.time.time", return_value=fixed_now):
                engine = MetricsEngine(brain_dir=brain, state_dir=state, as_of_date="2026-11-15")
                try:
                    m_span = engine.get_metrics(cid_span)
                    json.dumps(m_span)
                    v_span = m_span["valuation"]
                    self.assertGreaterEqual(v_span["conversation_age_s"], 7 * 86400)
                    w_span = v_span["windows"]

                    w5 = w_span["5h"]
                    self.assertEqual(w5["total_steps"], 2)
                    self.assertAlmostEqual(w5["cost_usd"], 2.95, places=6)
                    self.assertAlmostEqual(w5["by_pool"]["gemini"]["orchestrator"]["savings_usd"], 0.80, places=6)
                    self.assertNotIn("Orchestrator (Carbon) · Planner", w5["by_model"])

                    w7 = w_span["7d"]
                    self.assertEqual(w7["total_steps"], 3)
                    self.assertAlmostEqual(w7["cost_usd"], 6.15, places=6)

                    w_tot = w_span["total"]
                    self.assertEqual(w_tot["total_steps"], 4)
                    self.assertAlmostEqual(w_tot["cost_usd"], 9.35, places=6)
                    self.assertAlmostEqual(w_tot["by_pool"]["gemini"]["cost_usd"], 6.15, places=6)
                    self.assertAlmostEqual(w_tot["by_pool"]["partner"]["cost_usd"], 3.20, places=6)

                    m_fresh = engine.get_metrics(cid_fresh)
                    self.assertLess(m_fresh["valuation"]["conversation_age_s"], 5 * 3600)
                    self.assertAlmostEqual(m_fresh["valuation"]["cost_usd"], 1.28, places=6)

                    m_arch = engine.get_metrics(cid_arch)
                    self.assertEqual(m_arch["valuation"]["windows"]["5h"]["total_steps"], 0)
                    self.assertEqual(m_arch["valuation"]["windows"]["7d"]["total_steps"], 0)
                    self.assertAlmostEqual(m_arch["valuation"]["windows"]["total"]["cost_usd"], 1.50, places=6)
                finally:
                    engine.close()

                engine2 = MetricsEngine(brain_dir=brain, state_dir=state, as_of_date="2026-11-15")
                try:
                    m_all_24h = engine2.get_metrics(ALL_CONVERSATIONS_ID, window="24h")
                    v24 = m_all_24h["valuation"]
                    self.assertAlmostEqual(v24["windows"]["selected"]["cost_usd"], 4.23, places=6)
                    self.assertAlmostEqual(v24["windows"]["5h"]["cost_usd"], 4.23, places=6)
                    self.assertAlmostEqual(v24["windows"]["7d"]["cost_usd"], 7.43, places=6)

                    m_all_all = engine2.get_metrics(ALL_CONVERSATIONS_ID, window="all")
                    self.assertAlmostEqual(m_all_all["valuation"]["cost_usd"], 12.13, places=6)
                finally:
                    engine2.close()

    def test_match_any_specificity_independent_of_catalog_order(self) -> None:
        """Longer patterns like 'flash_lite' win over 'flash' regardless of JSON array order."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundled = root / "pricing.json"
            bundled.write_text(
                json.dumps(
                    {
                        "credits_per_usd": 100,
                        "models": [
                            {
                                "id": "gemini-flash",
                                "label": "Flash",
                                "pool": "gemini",
                                "match_any": ["flash"],
                                "rates": {"input": 1.0, "cache_read": 0.1, "output": 5.0},
                            },
                            {
                                "id": "gemini-flash-lite",
                                "label": "Flash-Lite",
                                "pool": "gemini",
                                "match_any": ["flash-lite", "flash_lite"],
                                "rates": {"input": 0.2, "cache_read": 0.02, "output": 1.0},
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            cat = PricingCatalog(state_dir=root / "state", bundled_path=bundled)
            res = cat.resolve_model("Subagent · flash_lite")
            self.assertEqual(res.status, "priced")
            assert res.entry is not None
            self.assertEqual(res.entry.catalog_id, "gemini-flash-lite")

    def test_custom_substring_overrides_orchestrator_labels(self) -> None:
        """Custom substring rules take precedence over the built-in Orchestrator rule."""
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            cat = PricingCatalog(state_dir=state, as_of_date="2026-11-15")
            cat.update_config(
                {
                    "action": "set_custom_model",
                    "pattern": "orchestrator (carbon) · planner",
                    "label": "Custom Planner Rate",
                    "match_mode": "substring",
                    "pool": "custom",
                    "rates": {"input": 9.0, "cache_read": 0.9, "output": 30.0},
                }
            )
            res = cat.resolve_model("Orchestrator (Carbon) · Planner")
            self.assertEqual(res.status, "priced")
            assert res.entry is not None
            self.assertEqual(res.entry.source, "custom")
            self.assertEqual(res.entry.label, "Custom Planner Rate")
            self.assertAlmostEqual(res.entry.rates["input"], 9.0, places=6)

    def test_catalog_override_preserves_upcoming_schedule_and_persists_after_effective_date(self) -> None:
        """Overriding current catalog rates keeps future schedule visible and never auto-reverts."""
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            cat_pre = PricingCatalog(state_dir=state, as_of_date="2026-11-15")
            cat_pre.update_config(
                {
                    "action": "set_catalog_override",
                    "id": "gemini-4-carbon",
                    "rates": {"input": 2.5, "cache_read": 0.25, "output": 12.5},
                }
            )
            entry_pre = cat_pre.resolve_model("Gemini 4 Carbon").entry
            assert entry_pre is not None
            self.assertEqual(entry_pre.source, "override")
            self.assertAlmostEqual(entry_pre.rates["input"], 2.5, places=6)
            self.assertTrue(entry_pre.has_future_change)
            self.assertEqual(entry_pre.future_effective_date, "2026-12-01")
            self.assertAlmostEqual(entry_pre.future_rates["input"], 4.0, places=6)

            # After effective_date, user's explicit override remains active for current rates.
            cat_post = PricingCatalog(state_dir=state, as_of_date="2026-12-05")
            entry_post = cat_post.resolve_model("Gemini 4 Carbon").entry
            assert entry_post is not None
            self.assertEqual(entry_post.source, "override")
            self.assertAlmostEqual(entry_post.rates["input"], 2.5, places=6)
            self.assertFalse(entry_post.has_future_change)

    def test_negative_orchestrator_savings_when_executor_exceeds_planner_rate(self) -> None:
        """If Executor rate > Planner rate, aggregate_valuation reports clean negative deltas."""
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            cat = PricingCatalog(state_dir=state, as_of_date="2026-11-15")
            cat.update_config(
                {
                    "action": "set_catalog_override",
                    "id": "gemini-4-hydrogen",
                    "rates": {"input": 5.0, "cache_read": 0.5, "output": 20.0},
                }
            )
            agg = cat.aggregate_valuation(
                [
                    {
                        "model_name": "Orchestrator (Carbon) · Planner",
                        "steps_count": 1,
                        "input_tokens": 1_000_000,
                        "cache_read_tokens": 0,
                        "output_tokens": 0,
                    },
                    {
                        "model_name": "Orchestrator (Carbon) · Executor",
                        "steps_count": 1,
                        "input_tokens": 1_000_000,
                        "cache_read_tokens": 0,
                        "output_tokens": 0,
                    },
                ]
            )
            orch = agg["orchestrator"]
            # Actual: Carbon $2 + Hydrogen $5 = $7 vs counterfactual $4 => -$3 (-75%)
            self.assertAlmostEqual(orch["savings_usd"], -3.0, places=6)
            self.assertAlmostEqual(orch["savings_pct"], -75.0, places=4)


if __name__ == "__main__":
    unittest.main()
