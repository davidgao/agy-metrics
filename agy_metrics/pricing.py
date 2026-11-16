"""Offline model pricing, AI credits, and Orchestrator valuation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import threading
from typing import Any

_DEFAULT_BUNDLED_PATH = Path(__file__).resolve().parent / "pricing.json"
_ORCH_PAREN_RE = re.compile(r"orchestrator\s*\(([^)]+)\)", re.IGNORECASE)
_EXECUTOR_PHASE_RE = re.compile(r"(?:·|\b)executor\b", re.IGNORECASE)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_MAX_STR_LEN = 120
_MAX_RATE = 100_000.0
_MIN_CREDITS_PER_USD = 0.0001
_MAX_CREDITS_PER_USD = 1_000_000.0
_VALID_MATCH_MODES = ("exact", "substring")
_VALID_POOLS = ("gemini", "partner", "custom")


def _validate_rate(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Invalid rate for {field_name}: expected number")
    num = float(value)
    if not math.isfinite(num) or num < 0.0 or num > _MAX_RATE:
        raise ValueError(f"Rate for {field_name} must be between 0 and {_MAX_RATE:g}")
    return num


def _validate_rates_dict(raw: Any) -> dict[str, float]:
    if not isinstance(raw, dict):
        raise ValueError("Invalid rates object")
    return {
        "input": _validate_rate(raw.get("input"), "input"),
        "cache_read": _validate_rate(raw.get("cache_read"), "cache_read"),
        "output": _validate_rate(raw.get("output"), "output"),
    }


def _validate_credits_per_usd(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Invalid credits_per_usd: expected number")
    num = float(value)
    if not math.isfinite(num) or num < _MIN_CREDITS_PER_USD or num > _MAX_CREDITS_PER_USD:
        raise ValueError(
            f"credits_per_usd must be between {_MIN_CREDITS_PER_USD:g} and {_MAX_CREDITS_PER_USD:g}"
        )
    return num


def _validate_text(value: Any, field_name: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Invalid {field_name}: expected string")
    text = value.strip()
    if not allow_empty and not text:
        raise ValueError(f"{field_name} cannot be empty")
    if len(text) > _MAX_STR_LEN:
        raise ValueError(f"{field_name} exceeds {_MAX_STR_LEN} characters")
    return text


def _stat_sig(path: Path | None) -> tuple[str, int, int, int]:
    if path is None:
        return ("", 0, 0, 0)
    try:
        st = path.stat()
    except OSError:
        return ("", 0, 0, 0)
    return (str(path), int(st.st_ino), int(st.st_size), int(st.st_mtime_ns))


def cost_for_tokens(
    input_tokens: int,
    cache_read_tokens: int,
    output_tokens: int,
    rates: dict[str, float],
) -> tuple[float, float, float, float]:
    """Compute (uncached_input_usd, cache_read_usd, output_usd, total_usd)."""
    uncached = max(0, int(input_tokens)) * rates["input"] / 1_000_000.0
    cached = max(0, int(cache_read_tokens)) * rates["cache_read"] / 1_000_000.0
    output = max(0, int(output_tokens)) * rates["output"] / 1_000_000.0
    return uncached, cached, output, uncached + cached + output


@dataclass(frozen=True)
class EffectiveEntry:
    catalog_id: str | None
    label: str
    pool: str
    source: str
    rates: dict[str, float]
    future_rates: dict[str, float]
    future_effective_date: str | None
    has_future_change: bool
    match_any: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelResolution:
    status: str  # "priced" | "unattributed" | "unpriced"
    entry: EffectiveEntry | None
    orchestrator_role: str | None  # "planner" | "executor" | None
    counterfactual_planner: EffectiveEntry | None


def _resolve_schedule(
    base_rates: dict[str, float],
    upcoming: dict[str, Any] | None,
    as_of_date: str,
) -> tuple[dict[str, float], dict[str, float], str | None, bool]:
    if not isinstance(upcoming, dict):
        return dict(base_rates), dict(base_rates), None, False
    eff_date = str(upcoming.get("effective_date") or "").strip()
    if not _DATE_RE.fullmatch(eff_date):
        return dict(base_rates), dict(base_rates), None, False
    try:
        up_rates = _validate_rates_dict(upcoming)
    except ValueError:
        return dict(base_rates), dict(base_rates), None, False

    if as_of_date >= eff_date:
        return up_rates, dict(up_rates), None, False
    changed = up_rates != base_rates
    return dict(base_rates), up_rates, (eff_date if changed else None), changed


class PricingCatalog:
    """Thread-safe loader and resolver for bundled + local override pricing."""

    def __init__(
        self,
        state_dir: Path | None = None,
        bundled_path: Path | None = None,
        as_of_date: str | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self.bundled_path = (
            Path(bundled_path) if bundled_path is not None else _DEFAULT_BUNDLED_PATH
        )
        self.override_path = (
            (Path(state_dir) / "pricing.json") if state_dir is not None else None
        )
        if as_of_date is not None and not _DATE_RE.fullmatch(as_of_date):
            raise ValueError(f"Invalid as_of_date: {as_of_date!r}")
        self._pinned_as_of_date = as_of_date

        self._bundled_raw: dict[str, Any] = {}
        self._default_credits_per_usd: float = 100.0
        self._credits_per_usd: float = 100.0
        self._override_credits_per_usd: float | None = None
        self._catalog_overrides: dict[str, dict[str, Any]] = {}
        self._custom_raw: dict[str, dict[str, Any]] = {}

        self._bundled_entries: list[EffectiveEntry] = []
        self._bundled_by_id: dict[str, EffectiveEntry] = {}
        self._bundled_match_table: list[tuple[str, EffectiveEntry]] = []
        self._custom_exact: dict[str, EffectiveEntry] = {}
        self._custom_substring: list[tuple[str, EffectiveEntry]] = []
        self._default_planner_id: str = "gemini-4-carbon"
        self._executor_id: str = "gemini-4-hydrogen"
        self._loaded_sig: tuple[tuple[str, int, int, int], tuple[str, int, int, int], str] | None = None

        self._reload()

    def _active_date(self) -> str:
        if self._pinned_as_of_date is not None:
            return self._pinned_as_of_date
        return datetime.now().date().isoformat()

    def _current_sig(
        self,
    ) -> tuple[tuple[str, int, int, int], tuple[str, int, int, int], str]:
        return (
            _stat_sig(self.bundled_path),
            _stat_sig(self.override_path),
            self._active_date(),
        )

    def _load_override_raw(self) -> dict[str, Any]:
        if self.override_path is None or not self.override_path.is_file():
            return {}
        try:
            data = json.loads(self.override_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _reload(self) -> None:
        with self._lock:
            sig = self._current_sig()
            as_of = sig[2]

            try:
                bundled = json.loads(self.bundled_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                bundled = {}
            if not isinstance(bundled, dict):
                bundled = {}
            self._bundled_raw = bundled

            try:
                self._default_credits_per_usd = _validate_credits_per_usd(
                    bundled.get("credits_per_usd", 100.0)
                )
            except ValueError:
                self._default_credits_per_usd = 100.0

            orch_cfg = bundled.get("orchestrator")
            if isinstance(orch_cfg, dict):
                self._default_planner_id = str(
                    orch_cfg.get("default_planner_id") or "gemini-4-carbon"
                )
                self._executor_id = str(
                    orch_cfg.get("executor_id") or "gemini-4-hydrogen"
                )
            else:
                self._default_planner_id = "gemini-4-carbon"
                self._executor_id = "gemini-4-hydrogen"

            ov_raw = self._load_override_raw()
            ov_credits: float | None = None
            if "credits_per_usd" in ov_raw and ov_raw["credits_per_usd"] is not None:
                try:
                    ov_credits = _validate_credits_per_usd(ov_raw["credits_per_usd"])
                except ValueError:
                    ov_credits = None
            self._override_credits_per_usd = ov_credits
            self._credits_per_usd = (
                ov_credits if ov_credits is not None else self._default_credits_per_usd
            )

            cat_ov_raw = ov_raw.get("catalog_overrides")
            clean_cat_ov: dict[str, dict[str, Any]] = {}
            if isinstance(cat_ov_raw, dict):
                for cid, entry_ov in cat_ov_raw.items():
                    if not isinstance(cid, str) or not isinstance(entry_ov, dict):
                        continue
                    try:
                        rates = _validate_rates_dict(entry_ov.get("rates"))
                    except ValueError:
                        continue
                    clean_item: dict[str, Any] = {"rates": rates}
                    if isinstance(entry_ov.get("upcoming_rates"), dict):
                        clean_item["upcoming_rates"] = entry_ov["upcoming_rates"]
                    clean_cat_ov[cid] = clean_item
            self._catalog_overrides = clean_cat_ov

            entries: list[EffectiveEntry] = []
            by_id: dict[str, EffectiveEntry] = {}
            raw_models = bundled.get("models")
            if isinstance(raw_models, list):
                for item in raw_models:
                    if not isinstance(item, dict):
                        continue
                    cid = str(item.get("id") or "").strip()
                    if not cid:
                        continue
                    try:
                        base_rates = _validate_rates_dict(item.get("rates"))
                    except ValueError:
                        continue
                    label = str(item.get("label") or cid).strip()
                    pool = str(item.get("pool") or "gemini").strip().lower()
                    raw_match = item.get("match_any") or []
                    match_any = tuple(
                        str(p).strip().lower()
                        for p in raw_match
                        if isinstance(p, str) and p.strip()
                    )

                    ov = clean_cat_ov.get(cid)
                    if ov is not None:
                        if "upcoming_rates" in ov:
                            cur_r, fut_r, eff_d, has_fut = _resolve_schedule(
                                ov["rates"], ov.get("upcoming_rates"), as_of
                            )
                        else:
                            cur_r = dict(ov["rates"])
                            bundled_up = item.get("upcoming_rates")
                            fut_r, eff_d, has_fut = dict(cur_r), None, False
                            if isinstance(bundled_up, dict):
                                b_eff_date = str(
                                    bundled_up.get("effective_date") or ""
                                ).strip()
                                if _DATE_RE.fullmatch(b_eff_date) and as_of < b_eff_date:
                                    try:
                                        b_up_rates = _validate_rates_dict(bundled_up)
                                    except ValueError:
                                        b_up_rates = None
                                    if b_up_rates is not None and b_up_rates != cur_r:
                                        fut_r = b_up_rates
                                        eff_d = b_eff_date
                                        has_fut = True
                        source = "override"
                    else:
                        cur_r, fut_r, eff_d, has_fut = _resolve_schedule(
                            base_rates, item.get("upcoming_rates"), as_of
                        )
                        source = "bundled"

                    eff = EffectiveEntry(
                        catalog_id=cid,
                        label=label,
                        pool=pool,
                        source=source,
                        rates=cur_r,
                        future_rates=fut_r,
                        future_effective_date=eff_d,
                        has_future_change=has_fut,
                        match_any=match_any,
                    )
                    entries.append(eff)
                    by_id[cid] = eff

            match_table: list[tuple[str, int, int, EffectiveEntry]] = []
            for entry_idx, eff in enumerate(entries):
                for pat in eff.match_any:
                    match_table.append((pat, -len(pat), entry_idx, eff))
            match_table.sort(key=lambda item: (item[1], item[2]))

            self._bundled_entries = entries
            self._bundled_by_id = by_id
            self._bundled_match_table = [(item[0], item[3]) for item in match_table]

            custom_raw = ov_raw.get("custom_models")
            clean_custom: dict[str, dict[str, Any]] = {}
            custom_exact: dict[str, EffectiveEntry] = {}
            custom_sub: list[tuple[str, EffectiveEntry]] = []
            if isinstance(custom_raw, dict):
                for key, item in custom_raw.items():
                    if not isinstance(key, str) or not isinstance(item, dict):
                        continue
                    try:
                        pattern = _validate_text(item.get("pattern") or key, "pattern")
                        label = _validate_text(item.get("label") or pattern, "label")
                        rates = _validate_rates_dict(item.get("rates"))
                    except ValueError:
                        continue
                    match_mode = str(item.get("match_mode") or "exact").strip().lower()
                    if match_mode not in _VALID_MATCH_MODES:
                        match_mode = "exact"
                    pool = str(item.get("pool") or "custom").strip().lower()
                    if pool not in _VALID_POOLS:
                        pool = "custom"

                    clean_custom[key] = {
                        "pattern": pattern,
                        "label": label,
                        "match_mode": match_mode,
                        "pool": pool,
                        "rates": rates,
                    }
                    eff = EffectiveEntry(
                        catalog_id=None,
                        label=label,
                        pool=pool,
                        source="custom",
                        rates=dict(rates),
                        future_rates=dict(rates),
                        future_effective_date=None,
                        has_future_change=False,
                        match_any=(pattern.lower(),),
                    )
                    if match_mode == "exact":
                        custom_exact[pattern.lower()] = eff
                    else:
                        custom_sub.append((pattern.lower(), eff))

            custom_sub.sort(key=lambda item: -len(item[0]))
            self._custom_raw = clean_custom
            self._custom_exact = custom_exact
            self._custom_substring = custom_sub
            self._loaded_sig = sig

    def refresh_if_changed(self) -> bool:
        with self._lock:
            sig = self._current_sig()
            if sig == self._loaded_sig:
                return False
            self._reload()
            return True

    @property
    def credits_per_usd(self) -> float:
        with self._lock:
            return self._credits_per_usd

    @property
    def override_active(self) -> bool:
        with self._lock:
            return bool(
                self._override_credits_per_usd is not None
                or self._catalog_overrides
                or self._custom_raw
            )

    def _resolve_base_hint(self, hint: str) -> EffectiveEntry | None:
        hint_lower = hint.strip().lower()
        if not hint_lower:
            return None
        if hint_lower in self._custom_exact:
            return self._custom_exact[hint_lower]
        for pat, eff in self._custom_substring:
            if pat in hint_lower:
                return eff
        for pat, eff in self._bundled_match_table:
            if pat in hint_lower:
                return eff
        return None

    def resolve_model(self, raw_model_name: str) -> ModelResolution:
        """Resolve a transcript/dashboard model label to pricing and Orchestrator roles."""
        with self._lock:
            raw = (raw_model_name or "").strip()
            lower = raw.lower()

            if lower in self._custom_exact:
                return ModelResolution(
                    status="priced",
                    entry=self._custom_exact[lower],
                    orchestrator_role=None,
                    counterfactual_planner=None,
                )

            if not lower or lower in ("unknown", "auto"):
                return ModelResolution(
                    status="unattributed",
                    entry=None,
                    orchestrator_role=None,
                    counterfactual_planner=None,
                )

            for pat, eff in self._custom_substring:
                if pat in lower:
                    return ModelResolution(
                        status="priced",
                        entry=eff,
                        orchestrator_role=None,
                        counterfactual_planner=None,
                    )

            if "orchestrator" in lower:
                m_hint = _ORCH_PAREN_RE.search(raw)
                base_hint = m_hint.group(1).strip() if m_hint else ""
                phase = "executor" if _EXECUTOR_PHASE_RE.search(lower) else "planner"
                if base_hint:
                    planner_entry = self._resolve_base_hint(base_hint)
                else:
                    planner_entry = self._bundled_by_id.get(self._default_planner_id)

                executor_entry = self._bundled_by_id.get(self._executor_id)
                if phase == "executor":
                    if executor_entry is None:
                        return ModelResolution(
                            status="unpriced",
                            entry=None,
                            orchestrator_role="executor",
                            counterfactual_planner=planner_entry,
                        )
                    return ModelResolution(
                        status="priced",
                        entry=executor_entry,
                        orchestrator_role="executor",
                        counterfactual_planner=planner_entry,
                    )
                if planner_entry is None:
                    return ModelResolution(
                        status="unpriced",
                        entry=None,
                        orchestrator_role="planner",
                        counterfactual_planner=None,
                    )
                return ModelResolution(
                    status="priced",
                    entry=planner_entry,
                    orchestrator_role="planner",
                    counterfactual_planner=planner_entry,
                )

            for pat, eff in self._bundled_match_table:
                if pat in lower:
                    return ModelResolution(
                        status="priced",
                        entry=eff,
                        orchestrator_role=None,
                        counterfactual_planner=None,
                    )

            return ModelResolution(
                status="unpriced",
                entry=None,
                orchestrator_role=None,
                counterfactual_planner=None,
            )

    def value_step(
        self,
        model_name: str,
        input_tokens: int,
        cache_read_tokens: int,
        output_tokens: int,
    ) -> dict[str, Any]:
        with self._lock:
            res = self.resolve_model(model_name)
            if res.status != "priced" or res.entry is None:
                return {
                    "pricing_status": res.status,
                    "cost_usd": None,
                    "credits": None,
                    "future_cost_usd": None,
                    "future_credits": None,
                }
            _, _, _, cur_tot = cost_for_tokens(
                input_tokens, cache_read_tokens, output_tokens, res.entry.rates
            )
            _, _, _, fut_tot = cost_for_tokens(
                input_tokens, cache_read_tokens, output_tokens, res.entry.future_rates
            )
            cpu = self._credits_per_usd
            return {
                "pricing_status": "priced",
                "cost_usd": round(cur_tot, 6),
                "credits": round(cur_tot * cpu, 4),
                "future_cost_usd": round(fut_tot, 6),
                "future_credits": round(fut_tot * cpu, 4),
            }

    def value_model_group(
        self,
        model_name: str,
        steps_count: int,
        input_tokens: int,
        cache_read_tokens: int,
        output_tokens: int,
    ) -> dict[str, Any]:
        with self._lock:
            res = self.resolve_model(model_name)
            if res.status != "priced" or res.entry is None:
                return {
                    "status": res.status,
                    "catalog_id": None,
                    "matched_label": None,
                    "pool": None,
                    "source": None,
                    "rates": None,
                    "future_rates": None,
                    "future_effective_date": None,
                    "has_future_change": False,
                    "input_cost_usd": None,
                    "uncached_input_cost_usd": None,
                    "cache_read_cost_usd": None,
                    "output_cost_usd": None,
                    "cost_usd": None,
                    "credits": None,
                    "future_cost_usd": None,
                    "future_credits": None,
                    "future_uncached_input_cost_usd": None,
                    "future_cache_read_cost_usd": None,
                    "orchestrator_role": res.orchestrator_role,
                    "orchestrator_savings_usd": None,
                    "orchestrator_future_savings_usd": None,
                    "counterfactual_label": (
                        res.counterfactual_planner.label
                        if res.counterfactual_planner is not None
                        else None
                    ),
                }

            eff = res.entry
            u_in, c_in, out_c, tot_c = cost_for_tokens(
                input_tokens, cache_read_tokens, output_tokens, eff.rates
            )
            f_u_in, f_c_in, _, fut_tot_c = cost_for_tokens(
                input_tokens, cache_read_tokens, output_tokens, eff.future_rates
            )
            cpu = self._credits_per_usd
            orch_sav: float | None = None
            orch_fut_sav: float | None = None
            cf_label: str | None = None
            if (
                res.orchestrator_role == "executor"
                and res.counterfactual_planner is not None
            ):
                cf = res.counterfactual_planner
                cf_label = cf.label
                _, _, _, cf_tot = cost_for_tokens(
                    input_tokens, cache_read_tokens, output_tokens, cf.rates
                )
                _, _, _, cf_fut_tot = cost_for_tokens(
                    input_tokens, cache_read_tokens, output_tokens, cf.future_rates
                )
                orch_sav = round(cf_tot - tot_c, 6)
                orch_fut_sav = round(cf_fut_tot - fut_tot_c, 6)

            return {
                "status": "priced",
                "catalog_id": eff.catalog_id,
                "matched_label": eff.label,
                "pool": eff.pool,
                "source": eff.source,
                "rates": dict(eff.rates),
                "future_rates": dict(eff.future_rates),
                "future_effective_date": eff.future_effective_date,
                "has_future_change": eff.has_future_change,
                "input_cost_usd": round(u_in + c_in, 6),
                "uncached_input_cost_usd": round(u_in, 6),
                "cache_read_cost_usd": round(c_in, 6),
                "output_cost_usd": round(out_c, 6),
                "cost_usd": round(tot_c, 6),
                "credits": round(tot_c * cpu, 4),
                "future_cost_usd": round(fut_tot_c, 6),
                "future_credits": round(fut_tot_c * cpu, 4),
                "future_uncached_input_cost_usd": round(f_u_in, 6),
                "future_cache_read_cost_usd": round(f_c_in, 6),
                "orchestrator_role": res.orchestrator_role,
                "orchestrator_savings_usd": orch_sav,
                "orchestrator_future_savings_usd": orch_fut_sav,
                "counterfactual_label": cf_label,
            }

    def empty_valuation(self) -> dict[str, Any]:
        with self._lock:
            return {
                "credits_per_usd": self._credits_per_usd,
                "override_active": self.override_active,
                "total_steps": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "credits": 0.0,
                "input_cost_usd": 0.0,
                "uncached_input_cost_usd": 0.0,
                "cache_read_cost_usd": 0.0,
                "output_cost_usd": 0.0,
                "has_future_change": False,
                "future_effective_dates": [],
                "future_cost_usd": 0.0,
                "future_credits": 0.0,
                "future_input_cost_usd": 0.0,
                "future_uncached_input_cost_usd": 0.0,
                "future_cache_read_cost_usd": 0.0,
                "future_output_cost_usd": 0.0,
                "orchestrator": {
                    "present": False,
                    "planner_steps": 0,
                    "executor_steps": 0,
                    "actual_cost_usd": 0.0,
                    "counterfactual_cost_usd": 0.0,
                    "savings_usd": 0.0,
                    "savings_credits": 0.0,
                    "savings_pct": 0.0,
                    "has_future_change": False,
                    "future_actual_cost_usd": 0.0,
                    "future_counterfactual_cost_usd": 0.0,
                    "future_savings_usd": 0.0,
                    "future_savings_credits": 0.0,
                    "future_savings_pct": 0.0,
                    "planner_labels": [],
                    "executor_label": None,
                },
                "coverage": {
                    "priced_steps": 0,
                    "priced_tokens": 0,
                    "unpriced_steps": 0,
                    "unpriced_tokens": 0,
                    "unpriced_models": [],
                    "unattributed_steps": 0,
                    "unattributed_tokens": 0,
                },
            }

    def aggregate_valuation(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Aggregate Paygo, Credits, Orchestrator savings, and coverage across model rows."""
        with self._lock:
            cpu = self._credits_per_usd
            cur_u_in = 0.0
            cur_c_in = 0.0
            cur_out = 0.0
            cur_tot = 0.0
            fut_u_in = 0.0
            fut_c_in = 0.0
            fut_in = 0.0
            fut_out = 0.0
            fut_tot = 0.0
            fut_dates: set[str] = set()
            has_fut = False

            orch_planner_steps = 0
            orch_executor_steps = 0
            orch_actual = 0.0
            orch_counterfactual = 0.0
            orch_fut_actual = 0.0
            orch_fut_counterfactual = 0.0
            orch_fut_changed = False
            planner_labels: list[str] = []
            executor_label: str | None = None

            priced_steps = 0
            priced_tokens = 0
            unpriced_steps = 0
            unpriced_tokens = 0
            unattributed_steps = 0
            unattributed_tokens = 0
            unpriced_by_name: dict[str, dict[str, Any]] = {}

            for row in rows:
                m_name = str(row.get("model_name") or "")
                steps_n = int(row.get("steps_count") or 0)
                in_tok = int(row.get("input_tokens") or 0)
                cache_tok = int(row.get("cache_read_tokens") or 0)
                out_tok = int(row.get("output_tokens") or 0)
                tok_tot = in_tok + cache_tok + out_tok

                res = self.resolve_model(m_name)
                if res.status == "unattributed":
                    unattributed_steps += steps_n
                    unattributed_tokens += tok_tot
                    continue
                if res.status != "priced" or res.entry is None:
                    unpriced_steps += steps_n
                    unpriced_tokens += tok_tot
                    bucket = unpriced_by_name.get(m_name)
                    if bucket is None:
                        unpriced_by_name[m_name] = {
                            "model_name": m_name,
                            "steps_count": steps_n,
                            "total_tokens": tok_tot,
                        }
                    else:
                        bucket["steps_count"] += steps_n
                        bucket["total_tokens"] += tok_tot
                    continue

                eff = res.entry
                priced_steps += steps_n
                priced_tokens += tok_tot

                u_in, c_in, out_c, tot_c = cost_for_tokens(
                    in_tok, cache_tok, out_tok, eff.rates
                )
                f_u_in, f_c_in, f_out_c, f_tot_c = cost_for_tokens(
                    in_tok, cache_tok, out_tok, eff.future_rates
                )
                cur_u_in += u_in
                cur_c_in += c_in
                cur_out += out_c
                cur_tot += tot_c
                fut_u_in += f_u_in
                fut_c_in += f_c_in
                fut_in += f_u_in + f_c_in
                fut_out += f_out_c
                fut_tot += f_tot_c
                if eff.has_future_change:
                    has_fut = True
                    if eff.future_effective_date:
                        fut_dates.add(eff.future_effective_date)

                if res.orchestrator_role == "planner":
                    orch_planner_steps += steps_n
                    orch_actual += tot_c
                    orch_counterfactual += tot_c
                    orch_fut_actual += f_tot_c
                    orch_fut_counterfactual += f_tot_c
                    if eff.label not in planner_labels:
                        planner_labels.append(eff.label)
                    if eff.has_future_change:
                        orch_fut_changed = True
                elif res.orchestrator_role == "executor":
                    orch_executor_steps += steps_n
                    executor_label = eff.label
                    if res.counterfactual_planner is not None:
                        cf = res.counterfactual_planner
                        _, _, _, cf_tot = cost_for_tokens(
                            in_tok, cache_tok, out_tok, cf.rates
                        )
                        _, _, _, cf_fut_tot = cost_for_tokens(
                            in_tok, cache_tok, out_tok, cf.future_rates
                        )
                        orch_actual += tot_c
                        orch_counterfactual += cf_tot
                        orch_fut_actual += f_tot_c
                        orch_fut_counterfactual += cf_fut_tot
                        if cf.label not in planner_labels:
                            planner_labels.append(cf.label)
                        if eff.has_future_change or cf.has_future_change:
                            orch_fut_changed = True
                    else:
                        orch_actual += tot_c
                        orch_counterfactual += tot_c
                        orch_fut_actual += f_tot_c
                        orch_fut_counterfactual += f_tot_c
                        if eff.has_future_change:
                            orch_fut_changed = True

            orch_present = (orch_planner_steps > 0) or (orch_executor_steps > 0)
            orch_sav_pct = (
                round((orch_counterfactual - orch_actual) / orch_counterfactual * 100.0, 1)
                if orch_present and orch_counterfactual > 0.0
                else 0.0
            )
            orch_fut_sav_pct = (
                round(
                    (orch_fut_counterfactual - orch_fut_actual)
                    / orch_fut_counterfactual
                    * 100.0,
                    1,
                )
                if orch_present and orch_fut_counterfactual > 0.0
                else 0.0
            )

            unpriced_list = sorted(
                unpriced_by_name.values(),
                key=lambda item: (-item["total_tokens"], item["model_name"]),
            )

            return {
                "credits_per_usd": cpu,
                "override_active": self.override_active,
                "total_steps": priced_steps + unpriced_steps + unattributed_steps,
                "total_tokens": priced_tokens + unpriced_tokens + unattributed_tokens,
                "cost_usd": round(cur_tot, 6),
                "credits": round(cur_tot * cpu, 4),
                "input_cost_usd": round(cur_u_in + cur_c_in, 6),
                "uncached_input_cost_usd": round(cur_u_in, 6),
                "cache_read_cost_usd": round(cur_c_in, 6),
                "output_cost_usd": round(cur_out, 6),
                "has_future_change": has_fut,
                "future_effective_dates": sorted(fut_dates),
                "future_cost_usd": round(fut_tot, 6),
                "future_credits": round(fut_tot * cpu, 4),
                "future_input_cost_usd": round(fut_in, 6),
                "future_uncached_input_cost_usd": round(fut_u_in, 6),
                "future_cache_read_cost_usd": round(fut_c_in, 6),
                "future_output_cost_usd": round(fut_out, 6),
                "orchestrator": {
                    "present": orch_present,
                    "planner_steps": orch_planner_steps,
                    "executor_steps": orch_executor_steps,
                    "actual_cost_usd": round(orch_actual, 6),
                    "counterfactual_cost_usd": round(orch_counterfactual, 6),
                    "savings_usd": round(orch_counterfactual - orch_actual, 6)
                    if orch_present
                    else 0.0,
                    "savings_credits": round((orch_counterfactual - orch_actual) * cpu, 4)
                    if orch_present
                    else 0.0,
                    "savings_pct": orch_sav_pct,
                    "has_future_change": orch_present and orch_fut_changed,
                    "future_actual_cost_usd": round(orch_fut_actual, 6),
                    "future_counterfactual_cost_usd": round(orch_fut_counterfactual, 6),
                    "future_savings_usd": round(
                        orch_fut_counterfactual - orch_fut_actual, 6
                    )
                    if orch_present
                    else 0.0,
                    "future_savings_credits": round(
                        (orch_fut_counterfactual - orch_fut_actual) * cpu, 4
                    )
                    if orch_present
                    else 0.0,
                    "future_savings_pct": orch_fut_sav_pct,
                    "planner_labels": planner_labels,
                    "executor_label": executor_label,
                },
                "coverage": {
                    "priced_steps": priced_steps,
                    "priced_tokens": priced_tokens,
                    "unpriced_steps": unpriced_steps,
                    "unpriced_tokens": unpriced_tokens,
                    "unpriced_models": unpriced_list,
                    "unattributed_steps": unattributed_steps,
                    "unattributed_tokens": unattributed_tokens,
                },
            }

    def _rows_for_pool(
        self, rows: list[dict[str, Any]], pool_name: str
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in rows:
            res = self.resolve_model(str(row.get("model_name") or ""))
            if (
                res.status == "priced"
                and res.entry is not None
                and res.entry.pool == pool_name
            ):
                out.append(row)
        return out

    def build_scoped_valuation(
        self,
        rows: list[dict[str, Any]],
        *,
        own_rows: list[dict[str, Any]] | None = None,
        sub_rows: list[dict[str, Any]] | None = None,
        model_rows: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Compute aggregate valuation plus per-pool (all/gemini/partner) and per-model rollups."""
        with self._lock:
            include_split = own_rows is not None and sub_rows is not None
            base = self.aggregate_valuation(rows)
            if include_split:
                assert own_rows is not None and sub_rows is not None
                base["own"] = self.aggregate_valuation(own_rows)
                base["subagents"] = self.aggregate_valuation(sub_rows)

            all_pool_val = dict(base)
            by_pool: dict[str, dict[str, Any]] = {"all": all_pool_val}
            for pool_name in ("gemini", "partner"):
                p_val = self.aggregate_valuation(self._rows_for_pool(rows, pool_name))
                if include_split:
                    assert own_rows is not None and sub_rows is not None
                    p_val["own"] = self.aggregate_valuation(
                        self._rows_for_pool(own_rows, pool_name)
                    )
                    p_val["subagents"] = self.aggregate_valuation(
                        self._rows_for_pool(sub_rows, pool_name)
                    )
                by_pool[pool_name] = p_val

            target_model_rows = model_rows if model_rows is not None else rows
            grouped_model_totals: dict[str, list[int]] = {}
            for row in target_model_rows:
                m_name = str(row.get("model_name") or "")
                s_n = int(row.get("steps_count") or 0)
                i_t = int(row.get("input_tokens") or 0)
                c_t = int(row.get("cache_read_tokens") or 0)
                o_t = int(row.get("output_tokens") or 0)
                acc = grouped_model_totals.get(m_name)
                if acc is None:
                    grouped_model_totals[m_name] = [s_n, i_t, c_t, o_t]
                else:
                    acc[0] += s_n
                    acc[1] += i_t
                    acc[2] += c_t
                    acc[3] += o_t

            by_model: dict[str, dict[str, Any]] = {}
            for m_name, (s_n, i_t, c_t, o_t) in grouped_model_totals.items():
                by_model[m_name] = {
                    "model_name": m_name,
                    "steps_count": s_n,
                    "input_tokens": i_t,
                    "cache_read_tokens": c_t,
                    "output_tokens": o_t,
                    "total_tokens": i_t + c_t + o_t,
                    **self.value_model_group(m_name, s_n, i_t, c_t, o_t),
                }

            base["by_pool"] = by_pool
            base["by_model"] = by_model
            return base

    def empty_scoped_valuation(
        self, *, include_session_split: bool = False
    ) -> dict[str, Any]:
        """Return an empty scoped valuation payload with zeroed pool and model maps."""
        with self._lock:
            return self.build_scoped_valuation(
                [],
                own_rows=[] if include_session_split else None,
                sub_rows=[] if include_session_split else None,
                model_rows=[],
            )

    def get_config(
        self, seen_model_names: list[str] | set[str] | None = None
    ) -> dict[str, Any]:
        with self._lock:
            self.refresh_if_changed()
            as_of = self._active_date()
            raw_models = self._bundled_raw.get("models")
            catalog_out: list[dict[str, Any]] = []
            if isinstance(raw_models, list):
                for item in raw_models:
                    if not isinstance(item, dict):
                        continue
                    cid = str(item.get("id") or "").strip()
                    eff = self._bundled_by_id.get(cid)
                    if eff is None:
                        continue
                    default_rates = _validate_rates_dict(item.get("rates"))
                    upcoming = item.get("upcoming_rates")
                    upcoming_out: dict[str, Any] | None = None
                    if isinstance(upcoming, dict):
                        try:
                            up_r = _validate_rates_dict(upcoming)
                            eff_d = str(upcoming.get("effective_date") or "").strip()
                            if _DATE_RE.fullmatch(eff_d):
                                upcoming_out = {
                                    "effective_date": eff_d,
                                    "label": str(upcoming.get("label") or "Standard rate"),
                                    **up_r,
                                }
                        except ValueError:
                            upcoming_out = None
                    catalog_out.append(
                        {
                            "id": cid,
                            "label": eff.label,
                            "pool": eff.pool,
                            "match_any": list(eff.match_any),
                            "default_rates": default_rates,
                            "rates": dict(eff.rates),
                            "future_rates": dict(eff.future_rates),
                            "future_effective_date": eff.future_effective_date,
                            "has_future_change": eff.has_future_change,
                            "upcoming_rates": upcoming_out,
                            "source": eff.source,
                        }
                    )

            custom_out = [
                {
                    "key": k,
                    "pattern": v["pattern"],
                    "label": v["label"],
                    "match_mode": v["match_mode"],
                    "pool": v["pool"],
                    "rates": dict(v["rates"]),
                }
                for k, v in sorted(self._custom_raw.items())
            ]

            seen_unpriced: list[str] = []
            if seen_model_names:
                for name in sorted(set(seen_model_names)):
                    if self.resolve_model(name).status == "unpriced":
                        seen_unpriced.append(name)

            return {
                "schema_version": int(self._bundled_raw.get("schema_version") or 1),
                "updated_at": str(self._bundled_raw.get("updated_at") or ""),
                "as_of_date": as_of,
                "credits_per_usd": self._credits_per_usd,
                "default_credits_per_usd": self._default_credits_per_usd,
                "has_overrides": self.override_active,
                "override_path": str(self.override_path) if self.override_path else None,
                "catalog": catalog_out,
                "custom_models": custom_out,
                "seen_unpriced_models": seen_unpriced,
            }

    def _write_override_file(self) -> None:
        if self.override_path is None:
            raise ValueError("No state_dir configured for pricing overrides")
        has_any = bool(
            self._override_credits_per_usd is not None
            or self._catalog_overrides
            or self._custom_raw
        )
        if not has_any:
            try:
                self.override_path.unlink()
            except FileNotFoundError:
                pass
            return

        payload: dict[str, Any] = {"schema_version": 1}
        if self._override_credits_per_usd is not None:
            payload["credits_per_usd"] = self._override_credits_per_usd
        if self._catalog_overrides:
            payload["catalog_overrides"] = self._catalog_overrides
        if self._custom_raw:
            payload["custom_models"] = self._custom_raw

        self.override_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.override_path.with_name(
            f"{self.override_path.name}.tmp.{os.getpid()}"
        )
        data = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        os.replace(tmp_path, self.override_path)

    def update_config(self, payload: dict[str, Any]) -> None:
        """Apply a validated local override mutation and persist $PLUGIN_DATA/pricing.json."""
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object")
        action = str(payload.get("action") or "").strip()

        with self._lock:
            self.refresh_if_changed()
            if action == "set_catalog_override":
                cid = _validate_text(payload.get("id"), "id")
                if cid not in self._bundled_by_id:
                    raise ValueError(f"Unknown catalog model id: {cid!r}")
                rates = _validate_rates_dict(payload.get("rates"))
                self._catalog_overrides[cid] = {"rates": rates}
            elif action == "reset_catalog_override":
                cid = _validate_text(payload.get("id"), "id")
                if cid not in self._bundled_by_id:
                    raise ValueError(f"Unknown catalog model id: {cid!r}")
                self._catalog_overrides.pop(cid, None)
            elif action == "set_custom_model":
                pattern = _validate_text(payload.get("pattern"), "pattern")
                raw_label = payload.get("label")
                label = (
                    _validate_text(raw_label, "label")
                    if raw_label is not None and str(raw_label).strip()
                    else pattern
                )
                match_mode = str(payload.get("match_mode") or "exact").strip().lower()
                if match_mode not in _VALID_MATCH_MODES:
                    raise ValueError(
                        f"match_mode must be one of {_VALID_MATCH_MODES}"
                    )
                pool = str(payload.get("pool") or "custom").strip().lower()
                if pool not in _VALID_POOLS:
                    raise ValueError(f"pool must be one of {_VALID_POOLS}")
                rates = _validate_rates_dict(payload.get("rates"))
                key = pattern.lower() if match_mode == "exact" else f"sub:{pattern.lower()}"
                self._custom_raw[key] = {
                    "pattern": pattern,
                    "label": label,
                    "match_mode": match_mode,
                    "pool": pool,
                    "rates": rates,
                }
            elif action == "delete_custom_model":
                key = _validate_text(payload.get("key"), "key")
                if key in self._custom_raw:
                    self._custom_raw.pop(key, None)
                else:
                    key_lower = key.lower()
                    matched_key = next(
                        (k for k in self._custom_raw if k.lower() == key_lower), None
                    )
                    if matched_key is not None:
                        self._custom_raw.pop(matched_key, None)
            elif action == "set_credits_per_usd":
                cpu = _validate_credits_per_usd(payload.get("credits_per_usd"))
                if abs(cpu - self._default_credits_per_usd) <= 1e-12:
                    self._override_credits_per_usd = None
                else:
                    self._override_credits_per_usd = cpu
            elif action == "reset_all":
                self._override_credits_per_usd = None
                self._catalog_overrides.clear()
                self._custom_raw.clear()
            else:
                raise ValueError(f"Unknown pricing action: {action!r}")

            self._write_override_file()
            self._reload()
