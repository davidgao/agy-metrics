"""Pure latency roofline fitting and step aggregation helpers."""

from __future__ import annotations

import statistics
from typing import Any

from .models import (
    StepMetrics,
    _FIT_MAX_RECENT_STEPS,
    _FIT_MIN_BELOW_WEIGHT,
    _FIT_MIN_PAIR_DX,
    _FIT_MIN_SLOPE_S_PER_TOK,
    _FIT_MIN_TOKEN_SPREAD,
    _FIT_MIN_TTFT_S,
    _FIT_MIN_WARM_STEPS,
    _FIT_RECENCY_HALF_LIFE,
    _FIT_SCALE_N0,
    _FIT_TARGET_TAU,
)


def empty_totals() -> dict[str, Any]:
    """Return a zeroed step totals payload matching engine output schemas."""
    return {
        "steps_count": 0,
        "compaction_steps_count": 0,
        "input_tokens": 0,
        "cache_read_tokens": 0,
        "gross_input_tokens": 0,
        "cache_hit_pct": 0.0,
        "output_tokens": 0,
        "est_thinking_tokens": 0,
        "est_visible_tokens": 0,
        "total_tokens": 0,
        "inference_time_s": 0.0,
        "model_time_s": 0.0,
        "wasted_time_s": 0.0,
        "retry_steps_count": 0,
        "failed_turns_count": 0,
        "timed_steps_count": 0,
        "cold_timed_steps_count": 0,
        "cold_model_time_s": 0.0,
        "timed_output_tokens": 0,
        "avg_effective_tps": None,
        "tool_time_s": 0.0,
        "compacted_count": 0,
        "warm_timed_steps_count": 0,
        "est_delay_steps_count": 0,
        "est_warm_delay_s": None,
        "est_total_delay_s": None,
        "avg_delay_s": None,
        "median_delay_s": None,
    }


def aggregate_steps(
    steps: list[StepMetrics],
    failed_turns_wasted_s: float = 0.0,
    failed_turns_count: int = 0,
) -> dict[str, Any]:
    """Aggregate token, cache hit, and timing counters across StepMetrics items."""
    sum_input = sum(s.input_tokens for s in steps)
    sum_cache = sum(s.cache_read_tokens for s in steps)
    gross_input = sum_input + sum_cache
    cache_hit_pct = (
        round(sum_cache / gross_input * 100.0, 1) if gross_input > 0 else 0.0
    )
    sum_output = sum(s.output_tokens for s in steps)
    sum_thinking = sum(s.est_thinking_tokens for s in steps)
    sum_visible = sum(s.est_visible_tokens for s in steps)

    timed_steps = [s for s in steps if s.duration_s is not None and s.duration_s > 0]
    inference_time_s = round(
        sum(float(s.duration_s) for s in timed_steps if s.duration_s), 2
    )
    retry_steps_count = sum(
        1 for s in steps if s.retry_wait_s > 0.0 or s.retry_count > 0
    )
    wasted_time_s = round(
        sum(float(s.retry_wait_s) for s in steps if s.retry_wait_s > 0.0)
        + float(failed_turns_wasted_s),
        2,
    )
    sum_model_time_s = round(inference_time_s + wasted_time_s, 2)
    cold_timed_steps = [
        s for s in timed_steps if s.step_kind != "checkpoint" and s.cold_cache_spike
    ]
    cold_model_time_s = round(
        sum(float(s.duration_s) for s in cold_timed_steps if s.duration_s), 2
    )
    timed_output_tokens = sum(s.output_tokens for s in timed_steps)
    avg_tps = (
        round(timed_output_tokens / inference_time_s, 1)
        if inference_time_s > 0.0
        else None
    )
    sum_tool_time_s = round(sum(s.tool_exec_s for s in steps), 2)

    return {
        "steps_count": len(steps),
        "compaction_steps_count": sum(
            1 for s in steps if s.step_kind == "checkpoint"
        ),
        "input_tokens": sum_input,
        "cache_read_tokens": sum_cache,
        "gross_input_tokens": gross_input,
        "cache_hit_pct": cache_hit_pct,
        "output_tokens": sum_output,
        "est_thinking_tokens": sum_thinking,
        "est_visible_tokens": sum_visible,
        "total_tokens": gross_input + sum_output,
        "inference_time_s": inference_time_s,
        "model_time_s": sum_model_time_s,
        "wasted_time_s": wasted_time_s,
        "retry_steps_count": retry_steps_count,
        "failed_turns_count": int(failed_turns_count),
        "timed_steps_count": len(timed_steps),
        "cold_timed_steps_count": len(cold_timed_steps),
        "cold_model_time_s": cold_model_time_s,
        "timed_output_tokens": timed_output_tokens,
        "avg_effective_tps": avg_tps,
        "tool_time_s": sum_tool_time_s,
    }


def step_roofline_delay(
    step: StepMetrics, fit: dict[str, Any] | None
) -> float | None:
    """Compute excess latency beyond the inferred roofline for one warm timed step."""
    if (
        not fit
        or not fit.get("available")
        or step.step_kind == "checkpoint"
        or step.cold_cache_spike
        or step.duration_s is None
        or step.duration_s <= 0
        or step.output_tokens <= 0
    ):
        return None
    expected = (
        float(fit["inferred_ttft_s"])
        + float(fit["slope_s_per_tok"]) * step.output_tokens
    )
    return max(0.0, float(step.duration_s) - expected)


def compute_roofline_fit(
    warm_points: tuple[tuple[int, float, str], ...],
) -> dict[str, Any]:
    """Fit a P95 speed roofline (duration_s = ttft + slope * output_tokens)."""
    n = len(warm_points)
    base: dict[str, Any] = {
        "available": False,
        "reason": "min_steps",
        "warm_count": n,
        "used_count": 0,
        "token_spread": 0,
        "quantile_pct": None,
        "inferred_ttft_s": None,
        "inferred_decode_tps": None,
        "slope_s_per_tok": None,
        "avg_delay_s": None,
        "median_delay_s": None,
    }
    if n < _FIT_MIN_WARM_STEPS:
        return base

    pts = list(warm_points[-_FIT_MAX_RECENT_STEPS:])
    m = len(pts)
    xs = [p[0] for p in pts]
    spread = max(xs) - min(xs)
    base["used_count"] = m
    base["token_spread"] = spread
    if spread < _FIT_MIN_TOKEN_SPREAD:
        base["reason"] = "min_spread"
        return base

    # Positive overall correlation guard using median pairwise slope
    s_xy = sorted((p[0], p[1]) for p in pts)
    slopes = [
        (s_xy[j][1] - s_xy[i][1]) / (s_xy[j][0] - s_xy[i][0])
        for i in range(m)
        for j in range(i + 1, m)
        if (s_xy[j][0] - s_xy[i][0]) >= _FIT_MIN_PAIR_DX
    ]
    if not slopes:
        slopes = [
            (s_xy[j][1] - s_xy[i][1]) / (s_xy[j][0] - s_xy[i][0])
            for i in range(m)
            for j in range(i + 1, m)
            if s_xy[j][0] > s_xy[i][0]
        ]
    if not slopes:
        base["reason"] = "min_spread"
        return base
    if statistics.median(slopes) <= _FIT_MIN_SLOPE_S_PER_TOK:
        base["reason"] = "non_positive_slope"
        return base

    # Weighted P95 speed frontier with exponential recency decay (tau + small-n guard)
    r_wts = [0.5 ** ((m - 1 - idx) / _FIT_RECENCY_HALF_LIFE) for idx in range(m)]
    sum_rw = sum(r_wts)
    sum_rw2 = sum(rw * rw for rw in r_wts)
    m_eff = (sum_rw * sum_rw) / sum_rw2 if sum_rw2 > 0 else float(m)
    tau = max(_FIT_TARGET_TAU, min(0.25, _FIT_MIN_BELOW_WEIGHT / m_eff))
    quantile_pct = int(round((1.0 - tau) * 100))

    weighted_pts = [
        (
            x,
            y,
            r_wts[idx]
            * (0.7 if src == "transcript_s" else 1.0)
            * x
            / (x + _FIT_SCALE_N0),
        )
        for idx, (x, y, src) in enumerate(pts)
        if x > 0
    ]
    total_w = sum(wt for _, _, wt in weighted_pts)
    if not weighted_pts or total_w <= 0:
        base["reason"] = "min_spread"
        return base

    def _eval_b(b: float) -> tuple[float, float, float]:
        # Minimizing sum_i wt_i * rho_tau((y_i - b)/x_i - s) for fixed b
        items = sorted(((y - b) / x, wt) for x, y, wt in weighted_pts)
        target = tau * total_w
        acc = 0.0
        s_raw = items[-1][0]
        for z, wt in items:
            acc += wt
            if acc >= target - 1e-12:
                s_raw = z
                break
        s_opt = max(_FIT_MIN_SLOPE_S_PER_TOK, s_raw)
        loss = 0.0
        for z, wt in items:
            u = z - s_opt
            loss += wt * (u * tau if u >= 0 else u * (tau - 1.0))
        return s_opt, s_raw, loss

    b_min = _FIT_MIN_TTFT_S
    b_max = max(b_min + 0.1, max(y for _, y, _ in weighted_pts))
    lo, hi = b_min, b_max
    gr = 0.6180339887498949
    c = hi - gr * (hi - lo)
    d = lo + gr * (hi - lo)
    _, _, fc = _eval_b(c)
    _, _, fd = _eval_b(d)
    for _ in range(45):
        if fc <= fd:
            hi = d
            d, fd = c, fc
            c = hi - gr * (hi - lo)
            _, _, fc = _eval_b(c)
        else:
            lo = c
            c, fc = d, fd
            d = lo + gr * (hi - lo)
            _, _, fd = _eval_b(d)

    b_best = 0.5 * (lo + hi)
    s_best, s_raw_best, best_loss = _eval_b(b_best)

    # Refine on exact boundary and nearest active vertex candidates
    s_bmin, s_raw_bmin, l_bmin = _eval_b(b_min)
    if l_bmin < best_loss:
        b_best, s_best, s_raw_best, best_loss = b_min, s_bmin, s_raw_bmin, l_bmin

    near_pts = sorted(
        weighted_pts,
        key=lambda p: abs((p[1] - b_best) / p[0] - s_best),
    )[:8]
    for x, y, _ in near_pts:
        b_cand = y - s_best * x
        if b_cand >= b_min:
            sc, sc_raw, lc = _eval_b(b_cand)
            if lc < best_loss:
                b_best, s_best, s_raw_best, best_loss = b_cand, sc, sc_raw, lc

    if s_raw_best <= _FIT_MIN_SLOPE_S_PER_TOK or s_best <= _FIT_MIN_SLOPE_S_PER_TOK:
        base["reason"] = "non_positive_slope"
        return base

    delays = [max(0.0, y - (b_best + s_best * x)) for x, y, _ in weighted_pts]
    avg_delay = sum(delays) / len(delays)
    median_delay = statistics.median(delays)

    return {
        "available": True,
        "reason": "ok",
        "warm_count": n,
        "used_count": m,
        "token_spread": spread,
        "quantile_pct": quantile_pct,
        "inferred_ttft_s": round(b_best, 2),
        "inferred_decode_tps": round(1.0 / s_best, 1),
        "slope_s_per_tok": round(s_best, 6),
        "avg_delay_s": round(avg_delay, 2),
        "median_delay_s": round(median_delay, 2),
    }
