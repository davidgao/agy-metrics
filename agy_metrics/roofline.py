"""Pure latency roofline fitting and step aggregation helpers."""

from __future__ import annotations

import math
import statistics
from typing import Any

from .models import (
    StepMetrics,
    _FIT_CACHE_HIGH_TOK,
    _FIT_CACHE_MAX_IN_TOK,
    _FIT_CACHE_MIN_HIGH_STEPS,
    _FIT_CACHE_MIN_SPREAD,
    _FIT_CACHE_MIN_STEPS,
    _FIT_DECODE_NOISE_SCALE_S,
    _FIT_FRONTIER_TAU,
    _FIT_MAX_RECENT_STEPS,
    _FIT_MIN_BELOW_WEIGHT,
    _FIT_MIN_CACHE_SLOPE_S_PER_TOK,
    _FIT_MIN_PAIR_DX,
    _FIT_MIN_PREFILL_SLOPE_S_PER_TOK,
    _FIT_MIN_SLOPE_S_PER_TOK,
    _FIT_MIN_TOKEN_SPREAD,
    _FIT_MIN_TTFT_S,
    _FIT_MIN_WARM_STEPS,
    _FIT_PREFILL_HIGH_TOK,
    _FIT_PREFILL_MIN_HIGH_STEPS,
    _FIT_PREFILL_MIN_SPREAD,
    _FIT_PREFILL_MIN_STEPS,
    _FIT_RECENCY_HALF_LIFE,
    _FIT_SCALE_N0,
    _FIT_TARGET_TAU,
    _FIT_WARM_DECODE_MAX_IN_TOK,
)

_C_BINS = (0, 20_000, 60_000, 120_000, 220_000)
_P_BINS = (0, 5_000, 15_000, 40_000, 100_000, 220_000)


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
        "cold_delay_steps_count": 0,
        "est_warm_delay_s": None,
        "est_cold_delay_s": None,
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
    """Compute excess latency beyond the inferred roofline for one timed step."""
    if (
        not fit
        or not fit.get("available")
        or step.step_kind == "checkpoint"
        or step.duration_s is None
        or step.duration_s <= 0
        or step.output_tokens <= 0
    ):
        return None
    if step.cold_cache_spike and not fit.get("prefill_available"):
        return None
    expected = (
        float(fit["inferred_ttft_s"])
        + float(fit.get("prefill_slope_s_per_tok") or 0.0) * step.input_tokens
        + float(fit.get("cache_slope_s_per_tok") or 0.0) * step.cache_read_tokens
        + float(fit["slope_s_per_tok"]) * step.output_tokens
    )
    return max(0.0, float(step.duration_s) - expected)


def _normalize_fit_points(
    points: tuple[tuple[Any, ...], ...],
) -> list[tuple[int, int, int, float, str, int]]:
    out: list[tuple[int, int, int, float, str, int]] = []
    for p in points:
        if len(p) == 3:
            out.append((0, 0, int(p[0]), float(p[1]), str(p[2] or ""), 0))
        else:
            out.append(
                (
                    int(p[0]),
                    int(p[1]),
                    int(p[2]),
                    float(p[3]),
                    str(p[4] or ""),
                    int(p[5]),
                )
            )
    return out


def _bin_balance_weights(
    vals: list[int], edges: tuple[int, ...]
) -> list[float]:
    if not vals:
        return []
    last_bin = len(edges) - 1
    counts: dict[int, int] = {}
    bins: list[int] = []
    for v in vals:
        bi = last_bin
        for idx in range(len(edges) - 1):
            if edges[idx] <= v < edges[idx + 1]:
                bi = idx
                break
        bins.append(bi)
        counts[bi] = counts.get(bi, 0) + 1
    return [1.0 / math.sqrt(counts[bi]) for bi in bins]


def _fit_decode_stage(
    dec_pts: list[tuple[int, int, int, float, str, int]],
    s_pre: float,
    s_cac: float,
) -> tuple[tuple[float, float, int] | None, str]:
    """Fit P95 decode frontier (y_adj = b + s_dec * out_tok) over warm steps."""
    m = len(dec_pts)
    if m < _FIT_MIN_WARM_STEPS:
        return None, "min_steps"

    xs = [p[2] for p in dec_pts]
    ys = [
        max(_FIT_MIN_TTFT_S, p[3] - s_pre * p[0] - s_cac * p[1])
        for p in dec_pts
    ]
    srcs = [p[4] for p in dec_pts]
    if (max(xs) - min(xs)) < _FIT_MIN_TOKEN_SPREAD:
        return None, "min_spread"

    s_xy = sorted((xs[i], ys[i]) for i in range(m))
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
        return None, "min_spread"
    if statistics.median(slopes) <= _FIT_MIN_SLOPE_S_PER_TOK:
        return None, "non_positive_slope"

    r_wts = [0.5 ** ((m - 1 - idx) / _FIT_RECENCY_HALF_LIFE) for idx in range(m)]
    sum_rw = sum(r_wts)
    sum_rw2 = sum(rw * rw for rw in r_wts)
    m_eff = (sum_rw * sum_rw) / sum_rw2 if sum_rw2 > 0 else float(m)
    tau = max(_FIT_TARGET_TAU, min(0.25, _FIT_MIN_BELOW_WEIGHT / m_eff))
    quantile_pct = int(round((1.0 - tau) * 100))

    weighted_pts = [
        (
            xs[idx],
            ys[idx],
            r_wts[idx]
            * (0.7 if srcs[idx] == "transcript_s" else 1.0)
            * xs[idx]
            / (xs[idx] + _FIT_SCALE_N0),
        )
        for idx in range(m)
        if xs[idx] > 0
    ]
    total_w = sum(wt for _, _, wt in weighted_pts)
    if not weighted_pts or total_w <= 0:
        return None, "min_spread"

    def _eval_b(b: float) -> tuple[float, float, float]:
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
        return None, "non_positive_slope"

    return (b_best, s_best, quantile_pct), "ok"


def _fit_peeled_line(
    xs: list[int] | list[float],
    ys: list[float],
    wts: list[float],
    *,
    tau_target: float = _FIT_FRONTIER_TAU,
    b_min: float = _FIT_MIN_TTFT_S,
    s_min: float = 0.0,
) -> tuple[float, float, float, int] | None:
    """Solve min_{b>=b_min, s>=s_min} sum w_i * rho_tau(y_i - (b + s * x_i))."""
    pts = [
        (float(x), float(y), float(w))
        for x, y, w in zip(xs, ys, wts)
        if w > 0.0
    ]
    if len(pts) < 4:
        return None
    sum_w = sum(w for _, _, w in pts)
    sum_w2 = sum(w * w for _, _, w in pts)
    m_eff = (sum_w * sum_w) / sum_w2 if sum_w2 > 0 else float(len(pts))
    tau = max(tau_target, min(0.25, _FIT_MIN_BELOW_WEIGHT / m_eff))

    pos_pts = [(x, y, w) for x, y, w in pts if x > 0.0]
    if not pos_pts:
        return None

    def _eval_b(b: float) -> tuple[float, float, float]:
        items = sorted(((y - b) / x, w * x) for x, y, w in pos_pts)
        tot_wx = sum(wx for _, wx in items)
        target = tau * tot_wx
        acc = 0.0
        s_raw = items[-1][0]
        for z, wx in items:
            acc += wx
            if acc >= target - 1e-12:
                s_raw = z
                break
        s_opt = max(s_min, s_raw)
        loss = 0.0
        for x, y, w in pts:
            u = y - (b + s_opt * x)
            loss += w * (u * tau if u >= 0.0 else u * (tau - 1.0))
        return s_opt, s_raw, loss

    pos_ys = [y for _, y, _ in pts if y > b_min]
    b_max = max(
        b_min + 0.5,
        statistics.median(pos_ys) * 2.0 if pos_ys else 10.0,
    )
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
    s_best, s_raw, best_loss = _eval_b(b_best)
    sb0, sr0, l0 = _eval_b(b_min)
    if l0 < best_loss:
        b_best, s_best, s_raw, best_loss = b_min, sb0, sr0, l0

    return b_best, s_best, s_raw, int(round((1.0 - tau) * 100))


def compute_roofline_fit(
    warm_points: tuple[tuple[Any, ...], ...],
    all_points: tuple[tuple[int, int, int, float, str, int], ...] | None = None,
    recent_points: tuple[tuple[int, int, int, float, str, int], ...] | None = None,
) -> dict[str, Any]:
    """Fit staged 3-speed roofline (decode, cache recovery, true prefill + base floor)."""
    norm_warm = _normalize_fit_points(warm_points)
    pool = (
        _normalize_fit_points(all_points)
        if all_points is not None
        else list(norm_warm)
    )

    low_p = [r for r in pool if r[0] < _FIT_CACHE_MAX_IN_TOK]
    c_spread = (
        (max(r[1] for r in low_p) - min(r[1] for r in low_p)) if low_p else 0
    )
    n_hi_c = sum(1 for r in low_p if r[1] >= _FIT_CACHE_HIGH_TOK)
    cac_eligible = True
    cache_reason = "ok"
    if len(low_p) < _FIT_CACHE_MIN_STEPS:
        cac_eligible = False
        cache_reason = "min_steps"
    elif c_spread < _FIT_CACHE_MIN_SPREAD:
        cac_eligible = False
        cache_reason = "min_spread"
    elif n_hi_c < _FIT_CACHE_MIN_HIGH_STEPS:
        cac_eligible = False
        cache_reason = "min_high_steps"

    p_spread = (
        (max(r[0] for r in pool) - min(r[0] for r in pool)) if pool else 0
    )
    n_hi_p = sum(1 for r in pool if r[0] >= _FIT_PREFILL_HIGH_TOK)
    pre_eligible = True
    prefill_reason = "ok"
    if len(pool) < _FIT_PREFILL_MIN_STEPS:
        pre_eligible = False
        prefill_reason = "min_steps"
    elif p_spread < _FIT_PREFILL_MIN_SPREAD:
        pre_eligible = False
        prefill_reason = "min_spread"
    elif n_hi_p < _FIT_PREFILL_MIN_HIGH_STEPS:
        pre_eligible = False
        prefill_reason = "min_high_steps"

    n = len(norm_warm)
    base: dict[str, Any] = {
        "available": False,
        "reason": "min_steps",
        "warm_count": n,
        "used_count": 0,
        "token_spread": 0,
        "quantile_pct": None,
        "inferred_ttft_s": None,
        "inferred_base_s": None,
        "inferred_decode_tps": None,
        "slope_s_per_tok": None,
        "avg_delay_s": None,
        "median_delay_s": None,
        "est_ttft_s": None,
        "est_prefill_s": None,
        "est_cache_s": None,
        "est_delay_s": None,
        "est_median_delay_s": None,
        "est_steps_count": 0,
        "prefill_available": False,
        "prefill_reason": prefill_reason,
        "inferred_prefill_tps": None,
        "prefill_slope_s_per_tok": None,
        "prefill_quantile_pct": None,
        "prefill_spread": p_spread,
        "high_prefill_count": n_hi_p,
        "cache_available": False,
        "cache_reason": cache_reason,
        "inferred_cache_tps": None,
        "cache_slope_s_per_tok": None,
        "cache_quantile_pct": None,
        "cache_spread": c_spread,
        "high_cache_count": n_hi_c,
    }
    if n < _FIT_MIN_WARM_STEPS:
        return base

    pts = norm_warm[-_FIT_MAX_RECENT_STEPS:]
    m = len(pts)
    xs_all = [p[2] for p in pts]
    spread = max(xs_all) - min(xs_all)
    base["used_count"] = m
    base["token_spread"] = spread
    if spread < _FIT_MIN_TOKEN_SPREAD:
        base["reason"] = "min_spread"
        return base

    low_in_pts = [p for p in pts if p[0] < _FIT_WARM_DECODE_MAX_IN_TOK]
    if (
        len(low_in_pts) >= _FIT_MIN_WARM_STEPS
        and (
            max(p[2] for p in low_in_pts) - min(p[2] for p in low_in_pts)
        )
        >= _FIT_MIN_TOKEN_SPREAD
    ):
        dec_pts = low_in_pts
    else:
        dec_pts = pts

    dec_res, dec_err = _fit_decode_stage(dec_pts, 0.0, 0.0)
    if dec_res is None:
        base["reason"] = dec_err
        return base
    b_dec, s_dec, q_dec = dec_res

    s_cac = 0.0
    s_pre = 0.0
    cache_available = False
    prefill_available = False
    q_cac: int | None = None
    q_pre: int | None = None

    if cac_eligible or pre_eligible:
        bw_c = (
            _bin_balance_weights([r[1] for r in low_p], _C_BINS)
            if cac_eligible
            else []
        )
        bw_p = (
            _bin_balance_weights([r[0] for r in pool], _P_BINS)
            if pre_eligible
            else []
        )

        for it in range(2):
            if it > 0:
                dec_res_it, _ = _fit_decode_stage(dec_pts, s_pre, s_cac)
                if dec_res_it is not None:
                    b_dec, s_dec, q_dec = dec_res_it

            if cac_eligible:
                wts_c = [
                    (0.7 if r[4] == "transcript_s" else 1.0)
                    / (1.0 + ((s_dec * r[2]) / _FIT_DECODE_NOISE_SCALE_S) ** 2)
                    * bw_c[idx]
                    for idx, r in enumerate(low_p)
                ]
                res_c = _fit_peeled_line(
                    [r[1] for r in low_p],
                    [r[3] - s_dec * r[2] - s_pre * r[0] for r in low_p],
                    wts_c,
                    tau_target=_FIT_FRONTIER_TAU,
                    b_min=_FIT_MIN_TTFT_S,
                    s_min=0.0,
                )
                if res_c is not None and res_c[2] > _FIT_MIN_CACHE_SLOPE_S_PER_TOK:
                    _, s_cac, _, q_cac = res_c
                    cache_available = True
                    cache_reason = "ok"
                else:
                    s_cac = 0.0
                    cache_available = False
                    cache_reason = "non_positive_slope"

            if pre_eligible:
                wts_p = [
                    (0.7 if r[4] == "transcript_s" else 1.0)
                    / (1.0 + ((s_dec * r[2]) / _FIT_DECODE_NOISE_SCALE_S) ** 2)
                    * bw_p[idx]
                    for idx, r in enumerate(pool)
                ]
                res_p = _fit_peeled_line(
                    [r[0] for r in pool],
                    [r[3] - s_dec * r[2] - s_cac * r[1] for r in pool],
                    wts_p,
                    tau_target=_FIT_FRONTIER_TAU,
                    b_min=_FIT_MIN_TTFT_S,
                    s_min=s_cac,
                )
                if res_p is not None and res_p[2] > _FIT_MIN_PREFILL_SLOPE_S_PER_TOK:
                    _, s_pre, _, q_pre = res_p
                    prefill_available = True
                    prefill_reason = "ok"
                else:
                    s_pre = 0.0
                    prefill_available = False
                    prefill_reason = "non_positive_slope"

        dec_res_fin, _ = _fit_decode_stage(dec_pts, s_pre, s_cac)
        if dec_res_fin is not None:
            b_dec, s_dec, q_dec = dec_res_fin

    eval_pts = pool if prefill_available else pts
    delays = [
        max(0.0, r[3] - (b_dec + s_pre * r[0] + s_cac * r[1] + s_dec * r[2]))
        for r in eval_pts
    ]
    avg_delay = sum(delays) / len(delays)
    median_delay = statistics.median(delays)

    if prefill_available:
        norm_recent = (
            _normalize_fit_points(recent_points)[-_FIT_MAX_RECENT_STEPS:]
            if recent_points is not None
            else []
        )
        recent_eval_pts = norm_recent if norm_recent else pool
    else:
        recent_eval_pts = pts
    n_recent = len(recent_eval_pts)
    avg_in_tok = sum(r[0] for r in recent_eval_pts) / n_recent
    avg_cac_tok = sum(r[1] for r in recent_eval_pts) / n_recent
    est_prefill = s_pre * avg_in_tok if prefill_available else 0.0
    est_cache = s_cac * avg_cac_tok if cache_available else 0.0
    est_ttft = b_dec + est_prefill + est_cache
    recent_delays = [
        max(0.0, r[3] - (b_dec + s_pre * r[0] + s_cac * r[1] + s_dec * r[2]))
        for r in recent_eval_pts
    ]
    est_delay = sum(recent_delays) / len(recent_delays)
    est_median_delay = statistics.median(recent_delays)

    return {
        "available": True,
        "reason": "ok",
        "warm_count": n,
        "used_count": m,
        "token_spread": spread,
        "quantile_pct": q_dec,
        "inferred_ttft_s": round(b_dec, 2),
        "inferred_base_s": round(b_dec, 2),
        "inferred_decode_tps": round(1.0 / s_dec, 1),
        "slope_s_per_tok": round(s_dec, 6),
        "avg_delay_s": round(avg_delay, 2),
        "median_delay_s": round(median_delay, 2),
        "est_ttft_s": round(est_ttft, 2),
        "est_prefill_s": round(est_prefill, 2) if prefill_available else None,
        "est_cache_s": round(est_cache, 2) if cache_available else None,
        "est_delay_s": round(est_delay, 2),
        "est_median_delay_s": round(est_median_delay, 2),
        "est_steps_count": n_recent,
        "prefill_available": prefill_available,
        "prefill_reason": prefill_reason,
        "inferred_prefill_tps": round(1.0 / s_pre) if prefill_available else None,
        "prefill_slope_s_per_tok": round(s_pre, 9) if prefill_available else None,
        "prefill_quantile_pct": q_pre if prefill_available else None,
        "prefill_spread": p_spread,
        "high_prefill_count": n_hi_p,
        "cache_available": cache_available,
        "cache_reason": cache_reason,
        "inferred_cache_tps": round(1.0 / s_cac) if cache_available else None,
        "cache_slope_s_per_tok": round(s_cac, 9) if cache_available else None,
        "cache_quantile_pct": q_cac if cache_available else None,
        "cache_spread": c_spread,
        "high_cache_count": n_hi_c,
    }
