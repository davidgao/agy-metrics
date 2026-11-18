"""Tests for install.py (copy install, --upgrade, --uninstall, manifest validation) and frontend UI."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import unittest

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from helpers import REPO_ROOT  # noqa: E402
import install as installer  # noqa: E402

STATIC_DIR = REPO_ROOT / "sidecars" / "dashboard" / "static"


class TestInstallAndFrontend(unittest.TestCase):
    def test_copy_install_abort_if_exists_upgrade_and_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_repo = tmp_path / "repo"
            for entry in (
                *installer.RUNTIME_ENTRIES,
                "tests",
                "scripts",
                "TODO.md",
                "pyproject.toml",
                "install.py",
            ):
                src = REPO_ROOT / entry
                dst = fake_repo / entry
                if src.is_dir():
                    shutil.copytree(src, dst)
                elif src.is_file():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
            pycache_dir = fake_repo / "agy_metrics" / "__pycache__"
            pycache_dir.mkdir(parents=True, exist_ok=True)
            (pycache_dir / "dummy.cpython-313.pyc").write_bytes(b"pyc")

            plugins_dir = tmp_path / "plugins"
            target = plugins_dir / installer.PLUGIN_NAME

            # 1. Status when not installed succeeds
            self.assertEqual(
                installer.cmd_status(repo_root=fake_repo, plugins_dir=plugins_dir),
                0,
            )

            # 2. Fresh install copies runtime files directly without dev clutter or symlinks
            rc = installer.cmd_install(
                upgrade=False, repo_root=fake_repo, plugins_dir=plugins_dir
            )
            self.assertEqual(rc, 0)
            self.assertFalse(target.is_symlink())
            self.assertTrue(target.is_dir())

            for rel_str in (
                "plugin.json",
                "hooks.json",
                "README.md",
                "LICENSE",
                "agy_metrics/engine.py",
                "sidecars/dashboard/static/app.js",
            ):
                copied = target / rel_str
                self.assertTrue(copied.is_file(), rel_str)
                self.assertFalse(copied.is_symlink(), rel_str)

            for forbidden in ("tests", "scripts", "TODO.md", "pyproject.toml", "install.py"):
                self.assertFalse((target / forbidden).exists(), forbidden)
            for p in target.rglob("*"):
                self.assertNotIn("__pycache__", p.parts)
                self.assertFalse(p.name.endswith(".pyc"))
            self.assertEqual(
                [p for p in plugins_dir.iterdir() if p.name.startswith(".")],
                [],
            )

            umask = os.umask(0)
            os.umask(umask)
            self.assertEqual(
                stat.S_IMODE(target.stat().st_mode),
                0o777 & ~umask,
            )

            # Runtime __pycache__ inside installed directory still counts as in-sync
            runtime_pycache = target / "agy_metrics" / "__pycache__"
            runtime_pycache.mkdir(parents=True, exist_ok=True)
            (runtime_pycache / "engine.cpython-313.pyc").write_bytes(b"runtime-pyc")
            self.assertTrue(installer._is_installed_in_sync(fake_repo, target))
            self.assertEqual(
                installer.cmd_status(repo_root=fake_repo, plugins_dir=plugins_dir),
                0,
            )

            # 3. Re-running without --upgrade aborts and leaves existing install untouched
            sentinel = target / "README.md"
            sentinel_text = "keep-local-sentinel\n"
            sentinel.write_text(sentinel_text, encoding="utf-8")
            self.assertEqual(
                installer.cmd_install(
                    upgrade=False, repo_root=fake_repo, plugins_dir=plugins_dir
                ),
                1,
            )
            self.assertEqual(sentinel.read_text(encoding="utf-8"), sentinel_text)

            # 4. Source changes and stale files trigger out-of-sync; --upgrade replaces cleanly
            (fake_repo / "README.md").write_text("# Updated README\n", encoding="utf-8")
            stale_file = target / "agy_metrics" / "obsolete_module.py"
            stale_file.write_text("# obsolete\n", encoding="utf-8")
            self.assertFalse(installer._is_installed_in_sync(fake_repo, target))
            self.assertEqual(
                installer.cmd_status(repo_root=fake_repo, plugins_dir=plugins_dir),
                0,
            )

            self.assertEqual(
                installer.cmd_install(
                    upgrade=True, repo_root=fake_repo, plugins_dir=plugins_dir
                ),
                0,
            )
            self.assertFalse(stale_file.exists())
            self.assertEqual(
                (target / "README.md").read_text(encoding="utf-8"),
                "# Updated README\n",
            )
            self.assertTrue(installer._is_installed_in_sync(fake_repo, target))

            # 5. Failed --upgrade from broken repo preserves existing valid installation
            broken_repo = tmp_path / "broken_repo"
            shutil.copytree(fake_repo, broken_repo)
            (broken_repo / "plugin.json").write_text("{broken", encoding="utf-8")
            self.assertEqual(
                installer.cmd_install(
                    upgrade=True, repo_root=broken_repo, plugins_dir=plugins_dir
                ),
                1,
            )
            self.assertEqual(
                (target / "README.md").read_text(encoding="utf-8"),
                "# Updated README\n",
            )

            # 6. Deleting source checkout leaves installed plugin copy intact
            shutil.rmtree(fake_repo)
            self.assertTrue((target / "plugin.json").is_file())
            self.assertTrue((target / "hooks" / "run_hook.py").is_file())
            self.assertEqual(
                json.loads((target / "plugin.json").read_text(encoding="utf-8"))["name"],
                "agy-metrics",
            )

            # 7. CLI flags: mutually exclusive flags rejected
            with self.assertRaises(SystemExit):
                installer.main(["--upgrade", "--uninstall"])

            # 8. Uninstall removes installed directory and is idempotent
            self.assertEqual(installer.cmd_uninstall(plugins_dir=plugins_dir), 0)
            self.assertFalse(os.path.lexists(target))
            self.assertEqual(installer.cmd_uninstall(plugins_dir=plugins_dir), 0)

    def test_validate_manifests_catches_missing_assets_and_bad_pricing(self) -> None:
        self.assertEqual(installer._validate_manifests(REPO_ROOT), [])

        with tempfile.TemporaryDirectory() as tmp:
            fake_repo = Path(tmp) / "repo"
            for entry in ("plugin.json", "hooks.json", "assets", "hooks", "sidecars", "agy_metrics"):
                src = REPO_ROOT / entry
                dst = fake_repo / entry
                if src.is_dir():
                    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                else:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)

            self.assertEqual(installer._validate_manifests(fake_repo), [])

            # Missing logo asset
            (fake_repo / "assets" / "logo.svg").unlink()
            errs_logo = installer._validate_manifests(fake_repo)
            self.assertTrue(any("logo" in e.lower() for e in errs_logo), errs_logo)
            shutil.copy2(REPO_ROOT / "assets" / "logo.svg", fake_repo / "assets" / "logo.svg")

            # Missing hook script referenced in hooks.json
            (fake_repo / "hooks" / "run_hook.py").unlink()
            errs_hook = installer._validate_manifests(fake_repo)
            self.assertTrue(any("hook script" in e.lower() for e in errs_hook), errs_hook)
            shutil.copy2(REPO_ROOT / "hooks" / "run_hook.py", fake_repo / "hooks" / "run_hook.py")

            # Empty/invalid pricing models
            (fake_repo / "agy_metrics" / "pricing.json").write_text('{"credits_per_usd":100,"models":[]}', encoding="utf-8")
            errs_pricing = installer._validate_manifests(fake_repo)
            self.assertTrue(any("pricing" in e.lower() for e in errs_pricing), errs_pricing)

            # Corrupt JSON
            (fake_repo / "plugin.json").write_text("{broken", encoding="utf-8")
            errs_json = installer._validate_manifests(fake_repo)
            self.assertTrue(any("invalid json" in e.lower() for e in errs_json), errs_json)

    def test_dashboard_theme_palettes_contrast_and_controls(self) -> None:
        css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        js_bundle = "\n".join(p.read_text(encoding="utf-8") for p in sorted(STATIC_DIR.glob("*.js")))
        combined = "\n".join([css, html, js_bundle])

        def parse_block(pattern: str) -> dict[str, str]:
            m = re.search(pattern, css, re.DOTALL)
            self.assertIsNotNone(m, f"Missing CSS block: {pattern}")
            return dict(re.findall(r"(--[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{6})\b", m.group(1)))

        dark_vars = parse_block(r":root\s*\{(.*?)\n\}")
        light_vars = parse_block(r':root\[data-theme="light"\]\s*\{(.*?)\n\}')

        tokens = [
            "--ctx-sys",
            "--ctx-summary",
            "--ctx-tool",
            "--ctx-model",
            "--ctx-user",
            "--chart-cached",
            "--chart-uncached",
            "--chart-compaction",
            "--model-planner",
            "--model-executor",
            "--model-1",
            "--model-2",
            "--model-3",
            "--model-4",
            "--model-5",
        ]

        def rel_lum(hex_color: str) -> float:
            rgb = [int(hex_color[i : i + 2], 16) / 255.0 for i in (1, 3, 5)]
            lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
            return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

        def contrast(c1: str, c2: str) -> float:
            l1, l2 = rel_lum(c1), rel_lum(c2)
            hi, lo = max(l1, l2), min(l1, l2)
            return (hi + 0.05) / (lo + 0.05)

        for tok in tokens:
            self.assertIn(tok, dark_vars)
            self.assertIn(tok, light_vars)
            self.assertNotEqual(dark_vars[tok].lower(), light_vars[tok].lower())
            for bg_key in ("--fb-card", "--fb-surface"):
                self.assertGreaterEqual(contrast(dark_vars[tok], dark_vars[bg_key]), 3.0)
                self.assertGreaterEqual(contrast(light_vars[tok], light_vars[bg_key]), 3.0)

        self.assertGreaterEqual(contrast(dark_vars["--fb-fg"], dark_vars["--fb-surface"]), 4.5)
        self.assertGreaterEqual(contrast(light_vars["--fb-fg"], light_vars["--fb-surface"]), 4.5)

        for required in (
            "--m-select-bg",
            "--m-select-fg",
            "select option",
            "syncSelectColors",
            "wcagContrast",
            "RECENT_STEPS_LIMIT = 5",
            "stepsToggleBtn",
            "showAllSteps",
            "steps-fold-row",
            "latencyFitCard",
            "scatterSvg",
            "coldToggleBtn",
            "scatterCloseBtn",
            "lastScatterKey",
            "HISTORY_AREA_THRESHOLD = 60",
            "lastHistoryKey",
            "steps10 = [10, 12, 14",
            "Nominal TPS",
            "speed roofline",
            "Time (~Delay)",
            "~Dec. TPS",
            "~Base (+Delay)",
            "scatterModeToggle",
            'data-scatter-mode="prefill"',
            'data-scatter-mode="cache"',
            "~Prefill TPS",
            "~Cache TPS",
            "computeStepRooflineParts",
            "lastDelay",
            "sumEstDelay",
            "sumWastedRow",
            "sumWastedTime",
            "fmtWastedTimeNote",
            "fmtWastedSummary",
            "retry_wait_s",
            "scopePicker",
            "scopeTriggerBtn",
            "scopeMenu",
            "scopeSearchInput",
            "scopeTreeList",
            "PROJECT_SCOPE_PREFIX",
            "parentConvBtn",
            "allControls",
            'data-window="24h"',
            "ctxCard",
            "historyCard",
            "stepsCard",
            "ALL_CONVERSATIONS_ID",
            "fmtDuration",
            "SCATTER_MAX_STEPS = 100",
            "sseRetryTimer",
            "sseWatchdogTimer",
            "Avg Delay",
            "P50 Delay",
            "theme-icon",
            "valuationCard",
            "ratesDrawer",
            "Paygo &amp; AI Credits",
            "valPoolToggle",
            'data-pool="gemini"',
            'data-pool="partner"',
            "valModelOverridePill",
            "valGrid",
            "Uncached In",
            "Cached Read",
            "Output (incl. think)",
        ):
            self.assertIn(required, combined, required)

        for forbidden in (
            "var(--secondary",
            'preserveAspectRatio="none"',
            "Hover a bar to inspect",
            "Time (+Delay)",
            "~TPS (+Delay)",
            "modelFilterBar",
            "Uncached / Cached",
            "Timing Source",
            "lastTps",
            "avgTps",
            "Theil",
            "#6366f1",
            "Med Traffic Delay",
            "Combined In+Out",
            "totAll",
            "cache savings",
            "kv savings",
            "valCreditsFutureRow",
        ):
            self.assertNotIn(forbidden, combined.lower() if forbidden.islower() else combined, forbidden)

    def test_frontend_freeze_race_guards_and_demand_timer(self) -> None:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        js_bundle = "\n".join(p.read_text(encoding="utf-8") for p in sorted(STATIC_DIR.glob("*.js")))
        state_js = (STATIC_DIR / "state.js").read_text(encoding="utf-8")
        format_js = (STATIC_DIR / "format.js").read_text(encoding="utf-8")
        pricing_js = (STATIC_DIR / "pricing-modal.js").read_text(encoding="utf-8")
        charts_js = (STATIC_DIR / "charts.js").read_text(encoding="utf-8")

        self.assertIn('<script src="preload.js"></script>', html)
        self.assertNotIn("state.timeWindow = m.window", js_bundle)
        for marker in (
            "/api/snapshot",
            "AbortController",
            "retargeted",
            "switchTarget",
            "hasSidecarFetch",
            "canUseEventSource",
            "sseDisabled",
            "pollTimer",
        ):
            self.assertIn(marker, js_bundle)

        # Generation timer is demand-driven (only runs while generating & visible)
        self.assertIn("syncGeneratingTimer", state_js)
        self.assertIn("clearInterval(genTimerId)", state_js)
        self.assertIn("document.hidden", state_js)
        self.assertRegex(
            state_js,
            r"shouldRun\s*=\s*state\.status\s*===\s*'generating'\s*&&\s*!isHidden",
        )

        # Negative savings formatter prevents '--X%' rendering
        self.assertIn("fmtOrchSavingsPct", format_js)
        self.assertIn("fmtOrchSavingsPct", charts_js)
        self.assertNotIn("' · -' + orch.savings_pct", charts_js)

        # Per-process CSRF token meta/body wiring on POST /api/pricing
        self.assertIn("agy-csrf-token", pricing_js)
        self.assertIn("_csrf_token", pricing_js)

        # Per-Step Telemetry upgraded from tok/s to Time (~Delay) + Paygo
        steps_thead_match = re.search(
            r'<div class="card" id="stepsCard">.*?<thead>(.*?)</thead>',
            html,
            re.DOTALL,
        )
        self.assertIsNotNone(steps_thead_match)
        steps_thead = steps_thead_match.group(1) if steps_thead_match else ""
        self.assertIn(">Time (~Delay)<", steps_thead)
        self.assertIn(">Paygo<", steps_thead)
        self.assertNotIn(">tok/s<", steps_thead)

        # Scheduled future price row keeps USD + % inline and puts future credits in tooltip
        self.assertIn("futureValTitle", charts_js)
        self.assertIn("futText = fmtUsd(mv.future_cost_usd) + mPctTag;", charts_js)
        self.assertIn("pFutText = fmtUsd(pv.future_cost_usd) + futPctTag;", charts_js)
        self.assertIn(
            "futValTitle = 'Scheduled rates (' + mv.future_effective_date + '): ' + fmtUsd(mv.future_cost_usd) + ' · ' + fmtCredits(mv.future_credits) + mPctTag;",
            charts_js,
        )
        self.assertIn(
            "pFutValTitle = 'Scheduled rates' + (dates.length ? (' (' + dates.join(', ') + ')') : '') + ': ' + fmtUsd(pv.future_cost_usd) + ' · ' + fmtCredits(pv.future_credits) + futPctTag;",
            charts_js,
        )
        self.assertNotIn("futText = fmtUsd(mv.future_cost_usd) + ' · '", charts_js)
        self.assertNotIn("pFutText = fmtUsd(pv.future_cost_usd) + ' · '", charts_js)


if __name__ == "__main__":
    unittest.main()
