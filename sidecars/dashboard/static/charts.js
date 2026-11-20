// Chart, latency roofline scatter, per-model table, and valuation card renderers.

import {
  computeStepRooflineParts,
  esc,
  fmtApproxDuration,
  fmtApproxSec,
  fmtAxisSec,
  fmtAxisTok,
  fmtBulkTps,
  fmtColdTimeNote,
  fmtCompact,
  fmtCredits,
  fmtDuration,
  fmtExactSecNote,
  fmtInt,
  fmtNomTps,
  fmtOrchSavingsPct,
  fmtRate,
  fmtShortDelay,
  fmtUsd,
  fmtWastedSummary,
  fmtWastedTimeNote,
  niceCeil,
  shortModelName,
  srcLabel
} from './format.js';
import {
  ALL_CONVERSATIONS_ID,
  HISTORY_AREA_THRESHOLD,
  PROJECT_SCOPE_PREFIX,
  RECENT_STEPS_LIMIT,
  SCATTER_MAX_STEPS,
  state,
  syncModeControls
} from './state.js';

function isRenderedViewAggregate() {
  const key = state.renderedViewKey || '';
  return (
    key.indexOf(ALL_CONVERSATIONS_ID + ':') === 0 ||
    key.indexOf(PROJECT_SCOPE_PREFIX) === 0
  );
}

function prefillGateNote(fit) {
  if (!fit) return '';
  if (fit.prefill_available) {
    return 'Prefill: ~' + fmtBulkTps(fit.inferred_prefill_tps) + ' tok/s';
  }
  if (fit.prefill_reason === 'unattributed') {
    return 'Prefill: disabled (unattributed)';
  }
  if (fit.prefill_reason === 'min_spread') {
    return 'Prefill: needs ≥40k uncached spread (' + fmtCompact(fit.prefill_spread || 0) + ' tok)';
  }
  if (fit.prefill_reason === 'min_high_steps') {
    return 'Prefill: needs ≥6 steps ≥25k uncached (' + (fit.high_prefill_count || 0) + '/6)';
  }
  if (fit.prefill_reason === 'non_positive_slope') {
    return 'Prefill: slope too flat/noisy';
  }
  return 'Prefill: needs ≥10 timed steps (' + (fit.high_prefill_count || 0) + '/6 ≥25k)';
}

function cacheGateNote(fit) {
  if (!fit) return '';
  if (fit.cache_available) {
    return 'Cache read: ~' + fmtBulkTps(fit.inferred_cache_tps) + ' tok/s';
  }
  if (fit.cache_reason === 'unattributed') {
    return 'Cache read: disabled (unattributed)';
  }
  if (fit.cache_reason === 'min_spread') {
    return 'Cache read: needs ≥80k cache spread on low-prefill steps (' + fmtCompact(fit.cache_spread || 0) + ' tok)';
  }
  if (fit.cache_reason === 'min_high_steps') {
    return 'Cache read: needs ≥6 low-prefill steps ≥80k cached (' + (fit.high_cache_count || 0) + '/6)';
  }
  if (fit.cache_reason === 'non_positive_slope') {
    return 'Cache read: slope too flat/noisy';
  }
  return 'Cache read: needs ≥15 low-prefill steps';
}

function buildStratifiedScatterSteps(allSteps, modelName, fit) {
  const timed = (allSteps || []).filter(function (s) {
    return (
      s.model_name === modelName &&
      s.step_kind !== 'checkpoint' &&
      s.duration_s != null &&
      s.duration_s > 0
    );
  });
  if (timed.length <= SCATTER_MAX_STEPS) return timed;
  if (!fit || (!fit.prefill_available && !fit.cache_available)) {
    return timed.slice(-SCATTER_MAX_STEPS);
  }
  const chosen = new Set();
  const hip = timed.filter(function (s) { return (s.input_tokens || 0) >= 25000; }).slice(-20);
  const orig = timed.filter(function (s) {
    return (s.input_tokens || 0) < 10000 && (s.cache_read_tokens || 0) < 40000;
  }).slice(-15);
  hip.forEach(function (s) { chosen.add(s); });
  orig.forEach(function (s) { chosen.add(s); });
  for (let i = timed.length - 1; i >= 0 && chosen.size < SCATTER_MAX_STEPS; i--) {
    chosen.add(timed[i]);
  }
  return timed.filter(function (s) { return chosen.has(s); });
}

export function renderByModel(byModel, totals, colorMap, callbacks) {
  const { onToggleModel, onClearFilter, isAllMode, subagentsCount } = callbacks || {};
  const tbody = document.getElementById('byModelTbody');
  const scopeLbl = document.getElementById('byModelScope');
  const subLbl = document.getElementById('byModelSub');
  tbody.innerHTML = '';

  if (subLbl) {
    subLbl.textContent = (!isAllMode && (subagentsCount || 0) > 0)
      ? ('incl. ' + subagentsCount + ' subagent' + (subagentsCount === 1 ? '' : 's'))
      : '';
  }

  const list = byModel || [];
  if (state.modelFilter) {
    scopeLbl.textContent = 'Filtered: ' + shortModelName(state.modelFilter) + ' (click to clear)';
    scopeLbl.style.cursor = 'pointer';
    scopeLbl.title = 'Click to clear model filter';
  } else {
    scopeLbl.textContent = 'Click a row for ~TTFT / TPS fit & filter';
    scopeLbl.style.cursor = '';
    scopeLbl.title = '';
  }

  if (list.length === 0) {
    const emptyRow = document.createElement('tr');
    emptyRow.innerHTML = '<td colspan="7" class="muted">No planner steps yet</td>';
    tbody.appendChild(emptyRow);
    return;
  }

  list.forEach(function (bm) {
    const color = colorMap[bm.model_name] || 'var(--m-muted)';
    const isSelected = state.modelFilter === bm.model_name;
    const fit = bm.latency_fit;
    const fitQ = (fit && fit.quantile_pct && fit.quantile_pct < 95) ? ('~P' + fit.quantile_pct) : 'P95';
    const has3Term = Boolean(fit && (fit.prefill_available || fit.cache_available));
    const estTtftVal = (fit && fit.available)
      ? Number(fit.est_ttft_s != null ? fit.est_ttft_s : fit.inferred_ttft_s)
      : null;
    const estDelayVal = (fit && fit.available)
      ? (fit.est_delay_s != null ? Number(fit.est_delay_s) : (fit.avg_delay_s != null ? Number(fit.avg_delay_s) : null))
      : null;
    const estP50DelayVal = (fit && fit.available)
      ? (fit.est_median_delay_s != null ? Number(fit.est_median_delay_s) : (fit.median_delay_s != null ? Number(fit.median_delay_s) : null))
      : null;
    const fitExtraRates = [];
    if (fit && fit.prefill_available) fitExtraRates.push('~' + fmtBulkTps(fit.inferred_prefill_tps) + ' tok/s prefill');
    if (fit && fit.cache_available) fitExtraRates.push('~' + fmtBulkTps(fit.inferred_cache_tps) + ' tok/s cache');
    const fitDelayTip = estDelayVal != null
      ? (', +' + estDelayVal.toFixed(2) + 's avg delay' +
         (estP50DelayVal != null ? (' (P50 +' + estP50DelayVal.toFixed(2) + 's)') : ''))
      : '';
    const fitTip = (fit && fit.available)
      ? (' · ' + fitQ + ' roofline: ~' + estTtftVal.toFixed(2) + 's avg TTFT, ~' + fit.inferred_decode_tps + ' tok/s decode' +
         (fitExtraRates.length ? (', ' + fitExtraRates.join(', ')) : '') + fitDelayTip)
      : '';

    const inputTitle = fmtInt(bm.gross_input_tokens) + ' gross input (' +
      fmtInt(bm.input_tokens) + ' uncached + ' + fmtInt(bm.cache_read_tokens) + ' cached read = ' + bm.cache_hit_pct + '% hit)';

    const bmWastedS = Number(bm.wasted_time_s || 0);
    const bmInfS = Number(bm.inference_time_s != null ? bm.inference_time_s : (bm.model_time_s || 0));
    const bmColdDelayCnt = bm.cold_delay_steps_count || 0;
    const bmWarmDelayCnt = Math.max(0, (bm.est_delay_steps_count || 0) - bmColdDelayCnt);
    const partialBmDelay = (bm.est_delay_steps_count || 0) < (bm.warm_timed_steps_count || 0) || ((bm.est_delay_steps_count || 0) === 0 && bmWastedS > 0);
    const bmDelayParts = [];
    if (bm.est_warm_delay_s != null) {
      bmDelayParts.push('~' + fmtDuration(bm.est_warm_delay_s) + fmtExactSecNote(bm.est_warm_delay_s) + ' warm roofline delay across ' + bmWarmDelayCnt + ' step(s)');
    }
    if (bm.est_cold_delay_s != null) {
      bmDelayParts.push('~' + fmtDuration(bm.est_cold_delay_s) + fmtExactSecNote(bm.est_cold_delay_s) + ' ❄ cold roofline delay across ' + bmColdDelayCnt + ' step(s)');
    }
    if (bmWastedS > 0) {
      const bmSum = fmtWastedSummary(bm);
      bmDelayParts.push(fmtDuration(bmWastedS) + fmtExactSecNote(bmWastedS) + ' retry/failed wait' + (bmSum ? (' (' + bmSum + ')') : ''));
    }
    const timeHtml = esc(fmtDuration(bm.model_time_s || 0)) +
      (bm.est_total_delay_s != null ? (' <span class="muted">(' + esc(fmtApproxDuration(bm.est_total_delay_s)) + (partialBmDelay ? '*' : '') + ')</span>') : '');
    const timeTitle = (bm.timed_steps_count || 0) + ' timed step(s): ' +
      fmtDuration(bmInfS) + fmtExactSecNote(bmInfS) + ' inference' + fmtColdTimeNote(bm) + fmtWastedTimeNote(bm) +
      (bm.est_total_delay_s != null
        ? (' · total delay ~' + fmtDuration(bm.est_total_delay_s) + fmtExactSecNote(bm.est_total_delay_s) +
           (bmDelayParts.length ? (' (' + bmDelayParts.join(' + ') + ')') : ''))
        : ' · Warm excess delay needs ≥4 warm timed steps');

    let nomTpsHtml;
    let nomTpsTitle;
    let ttftDelayHtml;
    let ttftDelayTitle;
    if (fit && fit.available) {
      const coldExclNote = fit.cold_excluded_count ? (', ' + fit.cold_excluded_count + ' cold excl.') : '';
      nomTpsHtml = '~' + esc(fmtNomTps(fit.inferred_decode_tps));
      nomTpsTitle = fitQ + ' speed roofline (n=' + fit.warm_count + ' warm' + coldExclNote + '): ~' +
        Number(fit.inferred_decode_tps).toFixed(1) + ' tok/s decode (' + fit.slope_s_per_tok + ' s/tok) · ' +
        prefillGateNote(fit) + ' · ' + cacheGateNote(fit);
      const ttftParts = [];
      if (has3Term) {
        ttftParts.push('~' + Number(fit.inferred_ttft_s).toFixed(2) + 's base');
        if (fit.prefill_available && fit.est_prefill_s != null) {
          ttftParts.push('~' + Number(fit.est_prefill_s).toFixed(2) + 's prefill');
        }
        if (fit.cache_available && fit.est_cache_s != null) {
          ttftParts.push('~' + Number(fit.est_cache_s).toFixed(2) + 's cache');
        }
      }
      const ttftDesc = has3Term
        ? ('avg step start (' + ttftParts.join(' + ') + ')')
        : ('avg warm TTFT (' + fitQ + ')');
      const sampleScopeNote = fit.est_steps_count > 0
        ? (' · last ' + fit.est_steps_count + (fit.prefill_available ? ' steps' : ' warm steps'))
        : '';
      if (estDelayVal != null) {
        const p50CellNote = estP50DelayVal != null
          ? (' · P50 delay: +' + estP50DelayVal.toFixed(2) + 's')
          : '';
        ttftDelayHtml = esc(fmtApproxSec(estTtftVal)) + ' <span class="muted">(' + esc(fmtShortDelay(estDelayVal)) + ')</span>';
        ttftDelayTitle = '~' + estTtftVal.toFixed(2) + 's ' + ttftDesc + ' + ' +
          estDelayVal.toFixed(2) + 's avg delay (~' +
          (estTtftVal + estDelayVal).toFixed(2) + 's expected before decode' +
          p50CellNote + sampleScopeNote + ')';
      } else {
        ttftDelayHtml = esc(fmtApproxSec(estTtftVal)) + ' <span class="muted">(—)</span>';
        ttftDelayTitle = '~' + estTtftVal.toFixed(2) + 's ' + ttftDesc + sampleScopeNote;
      }
    } else {
      const rawAvgNote = bm.avg_effective_tps != null ? (' · Raw avg: ' + bm.avg_effective_tps + ' tok/s') : '';
      let unfitReason;
      if (!fit || fit.reason === 'min_steps') {
        const warmN = (fit && fit.warm_count) || 0;
        nomTpsHtml = '— <span class="muted">(' + esc(warmN + '/4') + ')</span>';
        unfitReason = 'Needs ≥4 warm timed steps for roofline fit (' + warmN + '/4 recorded' +
          (fit && fit.cold_excluded_count ? (', ' + fit.cold_excluded_count + ' cold excl.') : '') + ')';
      } else if (fit.reason === 'min_spread') {
        nomTpsHtml = '— <span class="muted">(spread)</span>';
        unfitReason = 'Needs ≥200 tok output spread across warm steps (current: ' + fmtInt(fit.token_spread || 0) + ' tok)';
      } else if (fit.reason === 'unattributed') {
        nomTpsHtml = '—';
        unfitReason = 'Roofline speed fit disabled for unattributed ("unknown" / "auto") model steps';
      } else {
        nomTpsHtml = '—';
        unfitReason = 'Warm step timings have too much variance to fit a positive speed roofline yet';
      }
      nomTpsTitle = unfitReason + rawAvgNote;
      ttftDelayHtml = '—';
      ttftDelayTitle = unfitReason;
    }

    const tr = document.createElement('tr');
    const stepTitle = bm.steps_count + ' step(s)' + (bm.compaction_steps_count ? (' (incl. ' + bm.compaction_steps_count + ' compaction)') : '');
    const bmVal = bm.valuation;
    const costTip = (bmVal && bmVal.status === 'priced')
      ? (' · Paygo: ' + fmtUsd(bmVal.cost_usd) + ' (' + fmtCredits(bmVal.credits) + ')')
      : '';
    tr.className = 'model-row' + (isSelected ? ' active-filter' : '');
    tr.title = bm.model_name + (bm.is_active ? ' (active)' : '') + ' · ' + stepTitle + fitTip + costTip + ' · Click to filter';
    tr.innerHTML =
      '<td><span class="model-badge"><span class="model-dot" style="background:' + esc(color) + '"></span><span>' + esc(shortModelName(bm.model_name)) + '</span></span>' +
      (bm.is_active ? ' <span class="muted" title="Active model">★</span>' : '') + '</td>' +
      '<td class="mono" title="' + esc(stepTitle) + '">' + esc(bm.steps_count) + '</td>' +
      '<td class="mono" title="' + esc(inputTitle) + '">' + esc(fmtCompact(bm.gross_input_tokens)) + ' <span class="muted">(' + esc(bm.cache_hit_pct) + '%)</span></td>' +
      '<td class="mono" title="' + esc(fmtInt(bm.output_tokens) + ' out (~' + fmtInt(bm.est_thinking_tokens) + ' think, ~' + fmtInt(bm.est_visible_tokens) + ' visible)') + '">' + esc(fmtCompact(bm.output_tokens)) + ' <span class="muted">(~' + esc(fmtCompact(bm.est_thinking_tokens)) + ')</span></td>' +
      '<td class="mono" title="' + esc(timeTitle) + '">' + timeHtml + '</td>' +
      '<td class="mono" title="' + esc(nomTpsTitle) + '">' + nomTpsHtml + '</td>' +
      '<td class="mono" title="' + esc(ttftDelayTitle) + '">' + ttftDelayHtml + '</td>';
    tr.addEventListener('click', function () {
      if (onToggleModel) onToggleModel(bm.model_name);
    });
    tbody.appendChild(tr);
  });

  if (list.length > 1) {
    const totInputTitle = fmtInt(totals.gross_input_tokens) + ' gross input (' +
      fmtInt(totals.input_tokens) + ' uncached + ' + fmtInt(totals.cache_read_tokens) + ' cached read)';
    const totWastedS = Number(totals.wasted_time_s || 0);
    const totInfS = Number(totals.inference_time_s != null ? totals.inference_time_s : (totals.model_time_s || 0));
    const totColdDelayCnt = totals.cold_delay_steps_count || 0;
    const totWarmDelayCnt = Math.max(0, (totals.est_delay_steps_count || 0) - totColdDelayCnt);
    const partialTotDelay = (totals.est_delay_steps_count || 0) < (totals.warm_timed_steps_count || 0) || ((totals.est_delay_steps_count || 0) === 0 && totWastedS > 0);
    const totDelayParts = [];
    if (totals.est_warm_delay_s != null) {
      totDelayParts.push('~' + fmtDuration(totals.est_warm_delay_s) + fmtExactSecNote(totals.est_warm_delay_s) + ' warm roofline delay (' + totWarmDelayCnt + '/' + (totals.warm_timed_steps_count || 0) + ' warm steps)');
    }
    if (totals.est_cold_delay_s != null) {
      totDelayParts.push('~' + fmtDuration(totals.est_cold_delay_s) + fmtExactSecNote(totals.est_cold_delay_s) + ' ❄ cold roofline delay (' + totColdDelayCnt + ' cold step' + (totColdDelayCnt === 1 ? '' : 's') + ')');
    }
    if (totWastedS > 0) {
      const totSum = fmtWastedSummary(totals);
      totDelayParts.push(fmtDuration(totWastedS) + fmtExactSecNote(totWastedS) + ' retry/failed wait' + (totSum ? (' (' + totSum + ')') : ''));
    }
    const totTimeHtml = esc(fmtDuration(totals.model_time_s || 0)) +
      (totals.est_total_delay_s != null
        ? (' <span class="muted">(' + esc(fmtApproxDuration(totals.est_total_delay_s)) + (partialTotDelay ? '*' : '') + ')</span>')
        : '');
    const totTimeTitle = (totals.timed_steps_count || 0) + ' timed step(s): ' +
      fmtDuration(totInfS) + fmtExactSecNote(totInfS) + ' inference' + fmtColdTimeNote(totals) + fmtWastedTimeNote(totals) +
      (totals.est_total_delay_s != null
        ? (' · total delay ~' + fmtDuration(totals.est_total_delay_s) + fmtExactSecNote(totals.est_total_delay_s) +
           (totDelayParts.length ? (' (' + totDelayParts.join(' + ') + ')') : ''))
        : ' · Needs ≥4 warm timed steps on at least one model to estimate roofline delay');
    let totTtftDelayHtml;
    let totTtftDelayTitle;
    let wSum = 0;
    let wTtftSum = 0;
    let wDelaySum = 0;
    let fittedModelsCnt = 0;
    list.forEach(function (bm) {
      const f = bm.latency_fit;
      if (!f || !f.available) return;
      const tVal = f.est_ttft_s != null ? Number(f.est_ttft_s) : Number(f.inferred_ttft_s);
      const dVal = f.est_delay_s != null ? Number(f.est_delay_s) : (f.avg_delay_s != null ? Number(f.avg_delay_s) : null);
      if (tVal == null || dVal == null) return;
      const w = bm.timed_steps_count > 0 ? bm.timed_steps_count : (bm.steps_count || 0);
      if (w <= 0) return;
      wSum += w;
      wTtftSum += w * tVal;
      wDelaySum += w * dVal;
      fittedModelsCnt += 1;
    });
    if (wSum > 0) {
      const avgTotTtft = wTtftSum / wSum;
      const avgTotDelay = wDelaySum / wSum;
      totTtftDelayHtml = esc(fmtApproxSec(avgTotTtft)) + ' <span class="muted">(' + esc(fmtShortDelay(avgTotDelay)) + ')</span>';
      totTtftDelayTitle = 'Step-weighted avg across ' + fittedModelsCnt + '/' + list.length +
        ' fitted model(s): ~' + avgTotTtft.toFixed(2) + 's avg start + ' +
        avgTotDelay.toFixed(2) + 's avg delay (~' + (avgTotTtft + avgTotDelay).toFixed(2) + 's expected before decode)';
    } else if (totals.avg_delay_s != null) {
      const totP50Note = totals.median_delay_s != null
        ? (' · P50: +' + Number(totals.median_delay_s).toFixed(2) + 's')
        : '';
      totTtftDelayHtml = '— <span class="muted">(' + esc(fmtShortDelay(totals.avg_delay_s)) + ')</span>';
      totTtftDelayTitle = 'TTFT varies by model · Pooled avg excess delay: +' +
        Number(totals.avg_delay_s).toFixed(2) + 's' + totP50Note + ' (' +
        (totals.est_delay_steps_count || 0) + '/' + (totals.warm_timed_steps_count || 0) + ' warm steps covered)';
    } else {
      totTtftDelayHtml = '—';
      totTtftDelayTitle = 'Needs ≥4 warm timed steps on at least one model to estimate step start & delay';
    }
    const sumTr = document.createElement('tr');
    sumTr.className = 'model-summary-row';
    if (state.modelFilter) {
      sumTr.style.cursor = 'pointer';
      sumTr.title = 'Click to show all models';
      sumTr.addEventListener('click', function () {
        if (onClearFilter) onClearFilter();
      });
    }
    sumTr.innerHTML =
      '<td>All Models</td>' +
      '<td class="mono">' + esc(totals.steps_count || 0) + '</td>' +
      '<td class="mono" title="' + esc(totInputTitle) + '">' + esc(fmtCompact(totals.gross_input_tokens)) + ' (' + esc(totals.cache_hit_pct || 0) + '%)</td>' +
      '<td class="mono">' + esc(fmtCompact(totals.output_tokens)) + ' (~' + esc(fmtCompact(totals.est_thinking_tokens)) + ')</td>' +
      '<td class="mono" title="' + esc(totTimeTitle) + '">' + totTimeHtml + '</td>' +
      '<td class="mono" title="Nominal TPS varies by model">—</td>' +
      '<td class="mono" title="' + esc(totTtftDelayTitle) + '">' + totTtftDelayHtml + '</td>';
    tbody.appendChild(sumTr);
  }
}

export function renderChart(steps, colorMap, byModel) {
  const list = steps || [];
  const n = list.length;
  const lastS = n > 0 ? list[n - 1] : null;
  const lastSig = lastS
    ? [
        lastS.ordinal,
        lastS.total_context,
        lastS.cache_read_tokens,
        lastS.input_tokens,
        lastS.model_name,
        lastS.compacted ? 1 : 0,
        lastS.step_kind || ''
      ].join(':')
    : 'empty';
  const modelNamesSig = (byModel || []).map(function (bm) { return bm.model_name; }).join(',');
  const historyKey = [
    state.renderedConvId || '',
    n,
    state.modelFilter || '*',
    modelNamesSig,
    lastSig
  ].join('|');
  if (state.lastHistoryKey === historyKey) {
    return;
  }
  state.lastHistoryKey = historyKey;

  const svg = document.getElementById('historySvg');
  const hover = document.getElementById('chartHover');
  const leg = document.getElementById('chartLegend');
  svg.innerHTML = '';

  const hasCompaction = list.some(function (s) {
    return s.compacted || s.step_kind === 'checkpoint';
  });

  leg.innerHTML =
    '<span class="legend-item"><span class="swatch c-cached"></span>Cached Read</span>' +
    '<span class="legend-item"><span class="swatch c-uncached"></span>Uncached Prefill</span>' +
    (hasCompaction ? '<span class="legend-item" title="Context compaction boundary: prior history replaced by <CONTEXT_SUMMARY>"><span class="divider-swatch"></span>Compaction</span>' : '');
  const stepModelSet = new Set();
  list.forEach(function (s) {
    if (s && s.model_name) stepModelSet.add(s.model_name);
  });
  (byModel || []).forEach(function (bm) {
    if (!stepModelSet.has(bm.model_name)) return;
    const col = colorMap[bm.model_name] || 'var(--m-muted)';
    const span = document.createElement('span');
    span.className = 'legend-item';
    span.title = bm.model_name;
    span.innerHTML = '<span class="model-dot" style="background:' + esc(col) + '"></span><span>' + esc(shortModelName(bm.model_name)) + '</span>';
    leg.appendChild(span);
  });

  const NS = 'http://www.w3.org/2000/svg';
  const w = 600;
  const h = 156;

  if (n === 0) {
    hover.textContent = 'No planner steps yet';
    const emptyTxt = document.createElementNS(NS, 'text');
    emptyTxt.setAttribute('x', String(w / 2));
    emptyTxt.setAttribute('y', String(h / 2));
    emptyTxt.setAttribute('text-anchor', 'middle');
    emptyTxt.setAttribute('fill', 'var(--m-muted)');
    emptyTxt.setAttribute('font-size', '12');
    emptyTxt.textContent = 'No planner steps yet';
    svg.appendChild(emptyTxt);
    return;
  }

  let peakCtx = 0;
  list.forEach(function (s) {
    if (s.total_context > peakCtx) peakCtx = s.total_context;
  });
  hover.textContent =
    '#' + list[0].ordinal + (n > 1 ? ('–#' + list[n - 1].ordinal) : '') +
    ' · peak ' + fmtCompact(peakCtx) + ' tok';

  const padL = 46;
  const padR = 14;
  const padT = 16;
  const plotBottom = 122;
  const usableH = plotBottom - padT;
  const plotW = w - padL - padR;
  const stripY = 125.5;
  const stripH = 4.5;
  const maxCtx = niceCeil(Math.max(10, peakCtx) * 1.02);
  const slotW = plotW / n;

  function xLeft(idx) { return padL + idx * slotW; }
  function xRight(idx) { return padL + (idx + 1) * slotW; }
  function xCenter(idx) { return padL + (idx + 0.5) * slotW; }
  function yOf(tok) {
    const clamped = Math.min(maxCtx, Math.max(0, tok));
    return plotBottom - (clamped / maxCtx) * usableH;
  }

  const yUnit = document.createElementNS(NS, 'text');
  yUnit.setAttribute('x', String(padL - 6));
  yUnit.setAttribute('y', '10');
  yUnit.setAttribute('text-anchor', 'end');
  yUnit.setAttribute('fill', 'var(--m-muted)');
  yUnit.setAttribute('font-size', '9.5');
  yUnit.setAttribute('font-family', 'var(--m-mono)');
  yUnit.textContent = 'tok ↑';
  svg.appendChild(yUnit);

  [0, 0.5, 1].forEach(function (frac) {
    const val = maxCtx * frac;
    const y = yOf(val);
    const gl = document.createElementNS(NS, 'line');
    gl.setAttribute('x1', String(padL));
    gl.setAttribute('y1', y.toFixed(1));
    gl.setAttribute('x2', String(padL + plotW));
    gl.setAttribute('y2', y.toFixed(1));
    gl.setAttribute('stroke', 'var(--chart-grid)');
    gl.setAttribute('stroke-width', '1');
    if (frac > 0) gl.setAttribute('stroke-dasharray', '2,3');
    svg.appendChild(gl);

    const lbl = document.createElementNS(NS, 'text');
    lbl.setAttribute('x', String(padL - 6));
    lbl.setAttribute('y', (y + 3.5).toFixed(1));
    lbl.setAttribute('text-anchor', 'end');
    lbl.setAttribute('fill', 'var(--m-muted)');
    lbl.setAttribute('font-size', '10');
    lbl.setAttribute('font-family', 'var(--m-mono)');
    lbl.textContent = fmtAxisTok(val);
    svg.appendChild(lbl);
  });

  const yAxis = document.createElementNS(NS, 'line');
  yAxis.setAttribute('x1', String(padL));
  yAxis.setAttribute('y1', String(padT));
  yAxis.setAttribute('x2', String(padL));
  yAxis.setAttribute('y2', String(plotBottom));
  yAxis.setAttribute('stroke', 'var(--chart-grid)');
  yAxis.setAttribute('stroke-width', '1');
  svg.appendChild(yAxis);

  const tickIndices = [];
  if (n === 1) {
    tickIndices.push(0);
  } else {
    const strides = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000];
    let stride = Math.max(1, Math.ceil(n / 5));
    for (let si = 0; si < strides.length; si++) {
      if ((strides[si] / n) * plotW >= 72) {
        stride = strides[si];
        break;
      }
    }
    tickIndices.push(0);
    for (let ti = 1; ti < n - 1; ti++) {
      const txCand = xCenter(ti);
      const prevTx = xCenter(tickIndices[tickIndices.length - 1]);
      if (
        list[ti].ordinal % stride === 0 &&
        txCand - prevTx >= 38 &&
        xCenter(n - 1) - txCand >= 38
      ) {
        tickIndices.push(ti);
      }
    }
    const lastTickX = xCenter(tickIndices[tickIndices.length - 1]);
    if (xCenter(n - 1) - lastTickX >= 24 || tickIndices.length === 1) {
      tickIndices.push(n - 1);
    }
  }

  tickIndices.forEach(function (tIdx) {
    const tx = xCenter(tIdx);
    if (tIdx > 0 && tIdx < n - 1) {
      const vgl = document.createElementNS(NS, 'line');
      vgl.setAttribute('x1', tx.toFixed(1));
      vgl.setAttribute('y1', String(padT));
      vgl.setAttribute('x2', tx.toFixed(1));
      vgl.setAttribute('y2', String(plotBottom));
      vgl.setAttribute('stroke', 'var(--chart-grid)');
      vgl.setAttribute('stroke-width', '1');
      vgl.setAttribute('stroke-dasharray', '2,3');
      vgl.setAttribute('opacity', '0.65');
      svg.appendChild(vgl);
    }

    const tMark = document.createElementNS(NS, 'line');
    tMark.setAttribute('x1', tx.toFixed(1));
    tMark.setAttribute('y1', '131.5');
    tMark.setAttribute('x2', tx.toFixed(1));
    tMark.setAttribute('y2', '135');
    tMark.setAttribute('stroke', 'var(--chart-grid)');
    tMark.setAttribute('stroke-width', '1');
    svg.appendChild(tMark);

    let anchor = 'middle';
    let textX = tx;
    if (tx < padL + 12) {
      anchor = 'start';
      textX = padL;
    } else if (tx > padL + plotW - 14) {
      anchor = 'end';
      textX = padL + plotW;
    }
    const xLbl = document.createElementNS(NS, 'text');
    xLbl.setAttribute('x', textX.toFixed(1));
    xLbl.setAttribute('y', String(h - 7));
    xLbl.setAttribute('text-anchor', anchor);
    xLbl.setAttribute('fill', 'var(--m-muted)');
    xLbl.setAttribute('font-size', '10');
    xLbl.setAttribute('font-family', 'var(--m-mono)');
    xLbl.textContent = '#' + list[tIdx].ordinal;
    svg.appendChild(xLbl);
  });

  if (n <= HISTORY_AREA_THRESHOLD) {
    const barW = Math.min(24, Math.max(1.8, slotW * 0.74));
    const barRx = barW >= 4 ? '1.5' : '0.5';

    list.forEach(function (s, idx) {
      const bx = xLeft(idx) + (slotW - barW) / 2;
      const totalH = s.total_context > 0 ? Math.max(2, (s.total_context / maxCtx) * usableH) : 0;
      const cacheH = s.total_context > 0 ? (s.cache_read_tokens / s.total_context) * totalH : 0;
      const uncachedH = Math.max(0, totalH - cacheH);
      const yUncached = plotBottom - totalH;
      const yCached = yUncached + uncachedH;
      const modelCol = colorMap[s.model_name] || 'var(--m-muted)';
      const isDimmed = Boolean(state.modelFilter && s.model_name !== state.modelFilter);
      const isCheckpoint = s.step_kind === 'checkpoint';
      const hasBoth = s.input_tokens > 0 && s.cache_read_tokens > 0 && uncachedH >= 2.5 && cacheH >= 2.5;

      const g = document.createElementNS(NS, 'g');
      if (isDimmed) g.setAttribute('opacity', '0.22');

      if (s.input_tokens > 0) {
        const r1 = document.createElementNS(NS, 'rect');
        r1.setAttribute('x', bx.toFixed(1));
        r1.setAttribute('y', yUncached.toFixed(1));
        r1.setAttribute('width', barW.toFixed(1));
        r1.setAttribute('height', Math.max(hasBoth ? uncachedH - 1 : uncachedH, 1.5).toFixed(1));
        r1.setAttribute('fill', 'var(--chart-uncached)');
        r1.setAttribute('rx', barRx);
        g.appendChild(r1);
      }

      if (s.cache_read_tokens > 0) {
        const r2 = document.createElementNS(NS, 'rect');
        r2.setAttribute('x', bx.toFixed(1));
        r2.setAttribute('y', yCached.toFixed(1));
        r2.setAttribute('width', barW.toFixed(1));
        r2.setAttribute('height', Math.max(cacheH, 1.5).toFixed(1));
        r2.setAttribute('fill', 'var(--chart-cached)');
        r2.setAttribute('rx', barRx);
        g.appendChild(r2);
      }

      if (isCheckpoint && totalH > 0) {
        const rCap = document.createElementNS(NS, 'rect');
        rCap.setAttribute('x', bx.toFixed(1));
        rCap.setAttribute('y', yUncached.toFixed(1));
        rCap.setAttribute('width', barW.toFixed(1));
        rCap.setAttribute('height', '2.5');
        rCap.setAttribute('fill', 'var(--chart-compaction)');
        rCap.setAttribute('rx', barRx);
        g.appendChild(rCap);
      }

      const rModel = document.createElementNS(NS, 'rect');
      rModel.setAttribute('x', bx.toFixed(1));
      rModel.setAttribute('y', stripY.toFixed(1));
      rModel.setAttribute('width', barW.toFixed(1));
      rModel.setAttribute('height', stripH.toFixed(1));
      rModel.setAttribute('fill', modelCol);
      rModel.setAttribute('rx', '1');
      g.appendChild(rModel);

      svg.appendChild(g);
    });
  } else {
    const coords = list.map(function (s) {
      const totalH = s.total_context > 0 ? Math.max(1.8, (s.total_context / maxCtx) * usableH) : 0;
      const cacheH = s.total_context > 0 ? (s.cache_read_tokens / s.total_context) * totalH : 0;
      const yCacheTop = plotBottom - cacheH;
      let yTop = plotBottom - totalH;
      if (s.input_tokens > 0 && (yCacheTop - yTop) < 1.2) {
        yTop = Math.max(padT, yCacheTop - 1.2);
      }
      return {
        yTop: yTop,
        yCacheTop: yCacheTop,
        isDimmed: Boolean(state.modelFilter && s.model_name !== state.modelFilter)
      };
    });

    let runStart = 0;
    for (let idx = 1; idx <= n; idx++) {
      const shouldSplit =
        idx === n ||
        Boolean(list[idx].compacted) ||
        coords[idx].isDimmed !== coords[runStart].isDimmed;
      if (!shouldSplit) continue;

      const rEnd = idx - 1;
      const gRun = document.createElementNS(NS, 'g');
      if (coords[runStart].isDimmed) gRun.setAttribute('opacity', '0.22');

      const dCached = ['M', xLeft(runStart).toFixed(1), plotBottom.toFixed(1)];
      for (let k = runStart; k <= rEnd; k++) {
        dCached.push(
          'L', xLeft(k).toFixed(1), coords[k].yCacheTop.toFixed(1),
          'L', xRight(k).toFixed(1), coords[k].yCacheTop.toFixed(1)
        );
      }
      dCached.push('L', xRight(rEnd).toFixed(1), plotBottom.toFixed(1), 'Z');

      const pCached = document.createElementNS(NS, 'path');
      pCached.setAttribute('d', dCached.join(' '));
      pCached.setAttribute('fill', 'var(--chart-cached)');
      gRun.appendChild(pCached);

      const dUncached = ['M', xLeft(runStart).toFixed(1), coords[runStart].yCacheTop.toFixed(1)];
      for (let k = runStart; k <= rEnd; k++) {
        dUncached.push(
          'L', xLeft(k).toFixed(1), coords[k].yTop.toFixed(1),
          'L', xRight(k).toFixed(1), coords[k].yTop.toFixed(1)
        );
      }
      for (let k = rEnd; k >= runStart; k--) {
        dUncached.push(
          'L', xRight(k).toFixed(1), coords[k].yCacheTop.toFixed(1),
          'L', xLeft(k).toFixed(1), coords[k].yCacheTop.toFixed(1)
        );
      }
      dUncached.push('Z');

      const pUncached = document.createElementNS(NS, 'path');
      pUncached.setAttribute('d', dUncached.join(' '));
      pUncached.setAttribute('fill', 'var(--chart-uncached)');
      gRun.appendChild(pUncached);

      svg.appendChild(gRun);
      runStart = idx;
    }

    for (let ai = 0; ai < n; ai++) {
      const stepItem = list[ai];
      const ac = coords[ai];
      if (stepItem.cold_cache_spike) {
        const cw = Math.max(slotW, 1.8);
        const cRect = document.createElementNS(NS, 'rect');
        cRect.setAttribute('x', (xCenter(ai) - cw / 2).toFixed(1));
        cRect.setAttribute('y', ac.yTop.toFixed(1));
        cRect.setAttribute('width', cw.toFixed(1));
        cRect.setAttribute('height', Math.max(2, ac.yCacheTop - ac.yTop).toFixed(1));
        cRect.setAttribute('fill', 'var(--chart-uncached)');
        if (ac.isDimmed) cRect.setAttribute('opacity', '0.22');
        svg.appendChild(cRect);
      }
      if (stepItem.step_kind === 'checkpoint') {
        const kw = Math.max(slotW, 2.2);
        const kRect = document.createElementNS(NS, 'rect');
        kRect.setAttribute('x', (xCenter(ai) - kw / 2).toFixed(1));
        kRect.setAttribute('y', ac.yTop.toFixed(1));
        kRect.setAttribute('width', kw.toFixed(1));
        kRect.setAttribute('height', '2.5');
        kRect.setAttribute('fill', 'var(--chart-compaction)');
        if (ac.isDimmed) kRect.setAttribute('opacity', '0.22');
        svg.appendChild(kRect);
      }
    }

    let mStart = 0;
    for (let mi = 1; mi <= n; mi++) {
      if (mi < n && list[mi].model_name === list[mStart].model_name) continue;
      const mEnd = mi - 1;
      const mName = list[mStart].model_name;
      const mCol = colorMap[mName] || 'var(--m-muted)';
      const mDim = Boolean(state.modelFilter && mName !== state.modelFilter);
      const mRect = document.createElementNS(NS, 'rect');
      mRect.setAttribute('x', xLeft(mStart).toFixed(1));
      mRect.setAttribute('y', stripY.toFixed(1));
      mRect.setAttribute('width', (xRight(mEnd) - xLeft(mStart)).toFixed(1));
      mRect.setAttribute('height', stripH.toFixed(1));
      mRect.setAttribute('fill', mCol);
      if (mDim) mRect.setAttribute('opacity', '0.22');
      svg.appendChild(mRect);
      mStart = mi;
    }
  }

  function drawDivider(divX) {
    const xClamped = Math.max(padL + 0.5, Math.min(padL + plotW - 0.5, divX));
    const visLine = document.createElementNS(NS, 'line');
    visLine.setAttribute('x1', xClamped.toFixed(1));
    visLine.setAttribute('y1', String(padT - 2));
    visLine.setAttribute('x2', xClamped.toFixed(1));
    visLine.setAttribute('y2', (stripY + stripH + 1).toFixed(1));
    visLine.setAttribute('stroke', 'var(--chart-compaction)');
    visLine.setAttribute('stroke-width', '1.5');
    visLine.setAttribute('stroke-dasharray', '4,3');
    svg.appendChild(visLine);
  }

  list.forEach(function (s, idx) {
    if (s.compacted && idx > 0) {
      drawDivider(xLeft(idx));
    } else if (idx === n - 1 && s.step_kind === 'checkpoint') {
      drawDivider(xRight(idx));
    }
  });
}

export function renderLatencyScatter(selectedModelEntry, allSteps, colorMap, callbacks) {
  const { onRerender, onSwitchTarget } = callbacks || {};
  const card = document.getElementById('latencyFitCard');
  if (!selectedModelEntry) {
    card.classList.remove('visible');
    state.lastScatterKey = null;
    return;
  }
  card.classList.add('visible');

  const modelName = selectedModelEntry.model_name;
  const fit = selectedModelEntry.latency_fit || {
    available: false,
    reason: 'min_steps',
    warm_count: 0,
    cold_excluded_count: 0
  };
  const scatterMode = state.scatterMode || 'decode';
  const modeToggle = document.getElementById('scatterModeToggle');
  if (modeToggle) {
    modeToggle.querySelectorAll('[data-scatter-mode]').forEach(function (btn) {
      const mVal = btn.getAttribute('data-scatter-mode') || 'decode';
      const active = mVal === scatterMode;
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
  }

  const modelTimedSteps = Array.isArray(selectedModelEntry.scatter_steps)
    ? selectedModelEntry.scatter_steps
    : buildStratifiedScatterSteps(allSteps, modelName, fit);
  const lastStep = modelTimedSteps.length ? modelTimedSteps[modelTimedSteps.length - 1] : null;
  const lastSig = lastStep
    ? ((lastStep.conv_id || '') + ':' + lastStep.ordinal + ':' + lastStep.duration_s + ':' + lastStep.output_tokens + ':' + (lastStep.input_tokens || 0))
    : 'none';
  const fitSig = [
    fit.available ? 1 : 0,
    fit.reason || '',
    fit.warm_count || 0,
    fit.cold_excluded_count || 0,
    fit.quantile_pct || '',
    fit.inferred_ttft_s != null ? fit.inferred_ttft_s : '',
    fit.slope_s_per_tok != null ? fit.slope_s_per_tok : '',
    fit.prefill_available ? 1 : 0,
    fit.prefill_slope_s_per_tok != null ? fit.prefill_slope_s_per_tok : '',
    fit.high_prefill_count || 0,
    fit.cache_available ? 1 : 0,
    fit.cache_slope_s_per_tok != null ? fit.cache_slope_s_per_tok : '',
    fit.high_cache_count || 0,
    fit.avg_delay_s != null ? fit.avg_delay_s : '',
    fit.median_delay_s != null ? fit.median_delay_s : ''
  ].join(',');
  const scatterKey = [
    state.renderedViewKey || state.renderedConvId || '',
    modelName,
    scatterMode,
    state.showColdPrefills ? 1 : 0,
    modelTimedSteps.length,
    lastSig,
    fitSig
  ].join('|');

  if (state.lastScatterKey === scatterKey) {
    return;
  }
  state.lastScatterKey = scatterKey;

  const warmPts = modelTimedSteps.filter(function (s) { return !s.cold_cache_spike; });
  const coldPts = modelTimedSteps.filter(function (s) { return s.cold_cache_spike; });
  const modelCol = colorMap[modelName] || 'var(--m-primary)';

  const titlePrefix = scatterMode === 'prefill'
    ? 'Uncached Prefill vs. Time · '
    : (scatterMode === 'cache' ? 'Cached Read vs. Time · ' : 'Output vs. Time · ');
  document.getElementById('scatterTitle').textContent = titlePrefix + shortModelName(modelName);

  const coldBtn = document.getElementById('coldToggleBtn');
  if (scatterMode === 'prefill' || coldPts.length === 0) {
    coldBtn.style.display = 'none';
  } else {
    coldBtn.style.display = '';
    coldBtn.className = 'filter-pill' + (state.showColdPrefills ? ' active' : '');
    coldBtn.textContent = (state.showColdPrefills ? '❄ Cold shown (' : '❄ Show cold (') + coldPts.length + ')';
    coldBtn.title = fit.prefill_available
      ? 'Toggle visibility of cold-cache prefill steps (included in 3-term roofline)'
      : 'Toggle visibility of cold-cache prefill steps (currently excluded from speed roofline fit)';
  }

  const qLabel = (fit.quantile_pct && fit.quantile_pct < 95) ? ('~P' + fit.quantile_pct) : 'P95';
  const prefillQ = (fit.prefill_quantile_pct && fit.prefill_quantile_pct < 90) ? ('~P' + fit.prefill_quantile_pct) : 'P90';
  const cacheQ = (fit.cache_quantile_pct && fit.cache_quantile_pct < 90) ? ('~P' + fit.cache_quantile_pct) : 'P90';
  const lastModeled = fit.prefill_available
    ? (modelTimedSteps.length ? modelTimedSteps[modelTimedSteps.length - 1] : null)
    : (warmPts.length ? warmPts[warmPts.length - 1] : null);
  const statsRow = document.getElementById('scatterStatsRow');
  statsRow.innerHTML = '';
  if (fit.available) {
    const has3Term = Boolean(fit.prefill_available || fit.cache_available);
    const coldNote = fit.prefill_available
      ? ((fit.cold_count > 0) ? (' · ' + fit.cold_count + ' cold') : '')
      : (coldPts.length > 0 ? (' · ' + coldPts.length + ' cold excl.') : '');
    let latestDelayHtml = '';
    if (lastModeled) {
      const lmParts = computeStepRooflineParts(lastModeled, fit);
      const expectedLast = lmParts ? lmParts.expectedS : (fit.inferred_ttft_s + fit.slope_s_per_tok * lastModeled.output_tokens);
      const rawLastDelta = lastModeled.duration_s - expectedLast;
      const hasRecordedDelay = lastModeled.est_delay_s != null;
      const clampedLastDelta = hasRecordedDelay ? Number(lastModeled.est_delay_s) : Math.max(0, rawLastDelta);
      const breakdownStr = lmParts ? (' (' + lmParts.partsText + ')') : '';
      const latestTitle = hasRecordedDelay
        ? ('Latest modeled step #' + lastModeled.ordinal + (lastModeled.cold_cache_spike ? ' (❄ cold)' : '') + ': ' + lastModeled.duration_s + 's actual · +' +
           clampedLastDelta.toFixed(2) + 's recorded delay (roofline ~' + expectedLast.toFixed(2) + 's' + breakdownStr + ')')
        : ('Latest modeled step #' + lastModeled.ordinal + ': ' + lastModeled.duration_s + 's actual vs ~' +
           expectedLast.toFixed(2) + 's roofline' + breakdownStr + ' (' + (rawLastDelta >= 0 ? '+' : '') + rawLastDelta.toFixed(2) + 's)');
      latestDelayHtml =
        '<div class="scatter-kpi" title="' + esc(latestTitle) + '">' +
          '<span class="scatter-kpi-val mono">+' + esc(clampedLastDelta.toFixed(2)) + 's</span>' +
          '<span class="scatter-kpi-lbl">Latest Delay (#' + esc(lastModeled.ordinal) + ')</span>' +
        '</div>';
    }
    const warmCountLabel = (fit.used_count && fit.warm_count > fit.used_count)
      ? (fit.used_count + '/' + fit.warm_count)
      : String(fit.warm_count);
    const fitScopeNote = (fit.used_count && fit.warm_count > fit.used_count)
      ? ('most recent ' + fit.used_count + ' of ' + fit.warm_count + ' global warm steps (recency half-life 35)')
      : ('n=' + fit.warm_count + ' global warm steps');
    const noteTitle = (fit.quantile_pct && fit.quantile_pct < 95)
      ? ('Small-sample quantile relaxation (~P' + fit.quantile_pct + ' instead of P95) over ' + fitScopeNote)
      : ('P95 uncongested speed roofline over ' + fitScopeNote);
    const baseKpiLbl = has3Term ? '~Base Floor' : ('~Min TTFT (' + qLabel + ')');
    const baseKpiTitle = has3Term
      ? ('Inferred base dispatch/RTT overhead after peeling prefill & cache read time (' + qLabel + ' intercept)')
      : ('Inferred uncongested warm Time to First Token — X-intercept of the ' + qLabel + ' speed roofline (0 output tokens)');
    const preVal = fit.prefill_available ? ('~' + fmtBulkTps(fit.inferred_prefill_tps) + ' tok/s') : '—';
    const preLbl = fit.prefill_available ? ('~Prefill TPS (' + prefillQ + ')') : '~Prefill TPS';
    const preTitle = fit.prefill_available
      ? ('Inferred uncongested uncached prefill speed (' + prefillQ + ' frontier): ~' +
         fmtInt(fit.inferred_prefill_tps) + ' tok/s (' + (Number(fit.prefill_slope_s_per_tok) * 1e6).toFixed(1) + ' µs/tok)')
      : prefillGateNote(fit);
    const cacVal = fit.cache_available ? ('~' + fmtBulkTps(fit.inferred_cache_tps) + ' tok/s') : '—';
    const cacLbl = fit.cache_available ? ('~Cache TPS (' + cacheQ + ')') : '~Cache TPS';
    const cacTitle = fit.cache_available
      ? ('Inferred KV-cache recovery speed (' + cacheQ + ' frontier on low-prefill steps): ~' +
         fmtInt(fit.inferred_cache_tps) + ' tok/s (' + (Number(fit.cache_slope_s_per_tok) * 1e6).toFixed(1) + ' µs/tok)')
      : cacheGateNote(fit);
    const avgDelayDisp = fit.avg_delay_s != null ? ('+' + Number(fit.avg_delay_s).toFixed(2) + 's') : '—';
    const p50DelayDisp = fit.median_delay_s != null ? ('+' + Number(fit.median_delay_s).toFixed(2) + 's') : '—';
    statsRow.innerHTML =
      '<div class="scatter-kpi" title="' + esc(baseKpiTitle) + '">' +
        '<span class="scatter-kpi-val mono">~' + esc(fit.inferred_ttft_s.toFixed(2)) + 's</span>' +
        '<span class="scatter-kpi-lbl">' + esc(baseKpiLbl) + '</span>' +
      '</div>' +
      '<div class="scatter-kpi" title="Inferred uncongested decode throughput ceiling — slope of the ' + esc(qLabel) + ' speed roofline (' + esc(fit.slope_s_per_tok) + ' s/tok)">' +
        '<span class="scatter-kpi-val mono">~' + esc(fmtNomTps(fit.inferred_decode_tps)) + ' tok/s</span>' +
        '<span class="scatter-kpi-lbl">~Nominal TPS (' + esc(qLabel) + ')</span>' +
      '</div>' +
      '<div class="scatter-kpi" title="' + esc(preTitle) + '">' +
        '<span class="scatter-kpi-val mono">' + esc(preVal) + '</span>' +
        '<span class="scatter-kpi-lbl">' + esc(preLbl) + '</span>' +
      '</div>' +
      '<div class="scatter-kpi" title="' + esc(cacTitle) + '">' +
        '<span class="scatter-kpi-val mono">' + esc(cacVal) + '</span>' +
        '<span class="scatter-kpi-lbl">' + esc(cacLbl) + '</span>' +
      '</div>' +
      '<div class="scatter-kpi" title="Average (mean) horizontal excess latency beyond the uncongested speed roofline across modeled steps">' +
        '<span class="scatter-kpi-val mono">' + esc(avgDelayDisp) + '</span>' +
        '<span class="scatter-kpi-lbl">Avg Delay</span>' +
      '</div>' +
      '<div class="scatter-kpi" title="P50 (median) horizontal excess latency beyond the uncongested speed roofline across modeled steps — compare with Avg Delay to spot tail stalls">' +
        '<span class="scatter-kpi-val mono">' + esc(p50DelayDisp) + '</span>' +
        '<span class="scatter-kpi-lbl">P50 Delay</span>' +
      '</div>' +
      latestDelayHtml +
      '<span class="muted mono scatter-kpis-note" title="' + esc(noteTitle) + '">' + esc(qLabel) + ' roofline · n=' + esc(warmCountLabel) + ' warm' + esc(coldNote) + '</span>';
  } else {
    let msg = 'Warm step timings have too much variance to fit a positive speed roofline yet.';
    if (fit.reason === 'min_steps') {
      msg = 'Inferred ~Base Floor & nominal TPS need ≥4 warm timed steps (' +
        (fit.warm_count || 0) + '/4 recorded' +
        (coldPts.length ? (', ' + coldPts.length + ' cold excluded') : '') + ').';
    } else if (fit.reason === 'min_spread') {
      msg = 'Inferred ~Base Floor & nominal TPS need ≥200 tok output spread across warm steps (current spread: ' +
        fmtInt(fit.token_spread || 0) + ' tok).';
    } else if (fit.reason === 'unattributed') {
      msg = 'Roofline speed fit is disabled for unattributed ("unknown" / "auto") model steps.';
    }
    const msgSpan = document.createElement('span');
    msgSpan.className = 'muted';
    msgSpan.textContent = msg;
    statsRow.appendChild(msgSpan);
  }

  const svg = document.getElementById('scatterSvg');
  const hover = document.getElementById('scatterHover');
  const leg = document.getElementById('scatterLegend');
  svg.innerHTML = '';
  const isAllScatter = isRenderedViewAggregate();
  function isExternalPt(pt) {
    return Boolean(pt && pt.conv_id && (isAllScatter || pt.conv_id !== state.renderedConvId));
  }

  const NS = 'http://www.w3.org/2000/svg';
  const w = 600;
  const h = 184;
  const padL = 46;
  const padR = 16;
  const padT = 16;
  const padB = 24;
  const plotW = w - padL - padR;
  const plotH = h - padT - padB;

  const plottedPts = (scatterMode === 'prefill')
    ? modelTimedSteps
    : (state.showColdPrefills ? modelTimedSteps : warmPts);
  const hasExternalPts = plottedPts.some(isExternalPt);
  const hasLocalPts = plottedPts.some(function (pt) { return !isExternalPt(pt); });
  const baseHoverMsg = isAllScatter
    ? 'Hover a point to inspect · Click to open conversation'
    : (hasExternalPts && !hasLocalPts
        ? 'Hover a point to inspect · Click to open subagent'
        : (hasExternalPts
            ? 'Hover a point to inspect · Click to view step or open subagent'
            : 'Hover a point to inspect · Click to view step details'));
  if (scatterMode === 'prefill' && !fit.prefill_available) {
    hover.textContent = prefillGateNote(fit) + ' · ' + baseHoverMsg;
  } else if (scatterMode === 'cache' && !fit.cache_available) {
    hover.textContent = cacheGateNote(fit) + ' · ' + baseHoverMsg;
  } else {
    hover.textContent = baseHoverMsg;
  }
  if (plottedPts.length === 0) {
    const emptyTxt = document.createElementNS(NS, 'text');
    emptyTxt.setAttribute('x', String(w / 2));
    emptyTxt.setAttribute('y', String(h / 2));
    emptyTxt.setAttribute('text-anchor', 'middle');
    emptyTxt.setAttribute('fill', 'var(--m-muted)');
    emptyTxt.setAttribute('font-size', '12');
    emptyTxt.textContent = coldPts.length > 0
      ? 'Only cold-prefill timed steps recorded — click "❄ Show cold" above'
      : 'No timed steps recorded for this model yet';
    svg.appendChild(emptyTxt);
    leg.innerHTML = '';
    return;
  }

  const tBase = fit.available ? Number(fit.inferred_ttft_s || 0) : 0;
  const sDec = fit.available ? Number(fit.slope_s_per_tok || 0) : 0;
  const sPre = (fit.available && fit.prefill_available) ? Number(fit.prefill_slope_s_per_tok || 0) : 0;
  const sCac = (fit.available && fit.cache_available) ? Number(fit.cache_slope_s_per_tok || 0) : 0;

  const projPts = plottedPts.map(function (s) {
    const inTok = Number(s.input_tokens || 0);
    const cacTok = Number(s.cache_read_tokens || 0);
    const outTok = Number(s.output_tokens || 0);
    const dur = Number(s.duration_s || 0);
    let xSec = dur;
    let yTok = outTok;
    if (scatterMode === 'decode') {
      yTok = outTok;
      xSec = Math.max(0.02, dur - (sPre * inTok + sCac * cacTok));
    } else if (scatterMode === 'prefill') {
      yTok = inTok;
      xSec = Math.max(0.0, dur - (sDec * outTok + sCac * cacTok));
    } else {
      yTok = cacTok;
      xSec = Math.max(0.0, dur - (sDec * outTok + sPre * inTok));
    }
    const decEstS = sDec * outTok;
    const wClean = (sDec > 0) ? (1.0 / (1.0 + Math.pow(decEstS / 5.0, 2))) : 1.0;
    return { step: s, xSec: xSec, yTok: yTok, wClean: wClean };
  });

  let lineAvailable = false;
  let modeSlope = 0;
  let lineLabel = qLabel + ' speed roofline';
  let modeQLabel = qLabel;
  if (scatterMode === 'decode') {
    lineAvailable = Boolean(fit.available && sDec > 0);
    modeSlope = sDec;
    lineLabel = qLabel + ' speed roofline';
    modeQLabel = qLabel;
  } else if (scatterMode === 'prefill') {
    lineAvailable = Boolean(fit.available && fit.prefill_available && sPre > 0);
    modeSlope = sPre;
    lineLabel = prefillQ + ' prefill roofline';
    modeQLabel = prefillQ;
  } else {
    lineAvailable = Boolean(fit.available && fit.cache_available && sCac > 0);
    modeSlope = sCac;
    lineLabel = cacheQ + ' cache roofline';
    modeQLabel = cacheQ;
  }

  let rawMaxX = (fit.available && lineAvailable) ? Math.max(1.0, tBase * 1.25) : 1.0;
  let rawMaxY = (scatterMode === 'decode') ? 100 : 1000;
  projPts.forEach(function (p) {
    if (p.xSec > rawMaxX) rawMaxX = p.xSec;
    if (p.yTok > rawMaxY) rawMaxY = p.yTok;
  });
  const maxX = niceCeil(rawMaxX * 1.08);
  const maxY = niceCeil(rawMaxY * 1.08);

  function xOf(sec) { return padL + (sec / maxX) * plotW; }
  function yOf(tok) { return padT + plotH - (tok / maxY) * plotH; }

  const yUnit = document.createElementNS(NS, 'text');
  yUnit.setAttribute('x', String(padL - 6));
  yUnit.setAttribute('y', '10');
  yUnit.setAttribute('text-anchor', 'end');
  yUnit.setAttribute('fill', 'var(--m-muted)');
  yUnit.setAttribute('font-size', '9.5');
  yUnit.setAttribute('font-family', 'var(--m-mono)');
  yUnit.textContent = scatterMode === 'prefill' ? 'in ↑' : (scatterMode === 'cache' ? 'cac ↑' : 'tok ↑');
  svg.appendChild(yUnit);

  [0, 0.5, 1].forEach(function (frac) {
    const val = Math.round(maxY * frac);
    const y = yOf(val);
    const gl = document.createElementNS(NS, 'line');
    gl.setAttribute('x1', String(padL));
    gl.setAttribute('y1', y.toFixed(1));
    gl.setAttribute('x2', String(padL + plotW));
    gl.setAttribute('y2', y.toFixed(1));
    gl.setAttribute('stroke', 'var(--chart-grid)');
    gl.setAttribute('stroke-width', '1');
    if (frac > 0) gl.setAttribute('stroke-dasharray', '2,3');
    svg.appendChild(gl);

    const lbl = document.createElementNS(NS, 'text');
    lbl.setAttribute('x', String(padL - 6));
    lbl.setAttribute('y', (y + 3.5).toFixed(1));
    lbl.setAttribute('text-anchor', 'end');
    lbl.setAttribute('fill', 'var(--m-muted)');
    lbl.setAttribute('font-size', '10');
    lbl.setAttribute('font-family', 'var(--m-mono)');
    lbl.textContent = fmtAxisTok(val);
    svg.appendChild(lbl);
  });

  const yAxis = document.createElementNS(NS, 'line');
  yAxis.setAttribute('x1', String(padL));
  yAxis.setAttribute('y1', String(padT));
  yAxis.setAttribute('x2', String(padL));
  yAxis.setAttribute('y2', String(padT + plotH));
  yAxis.setAttribute('stroke', 'var(--chart-grid)');
  yAxis.setAttribute('stroke-width', '1');
  svg.appendChild(yAxis);

  [0, 0.5, 1].forEach(function (frac) {
    const val = maxX * frac;
    const x = xOf(val);
    if (frac > 0) {
      const vgl = document.createElementNS(NS, 'line');
      vgl.setAttribute('x1', x.toFixed(1));
      vgl.setAttribute('y1', String(padT));
      vgl.setAttribute('x2', x.toFixed(1));
      vgl.setAttribute('y2', String(padT + plotH));
      vgl.setAttribute('stroke', 'var(--chart-grid)');
      vgl.setAttribute('stroke-width', '1');
      vgl.setAttribute('stroke-dasharray', '2,3');
      svg.appendChild(vgl);
    }

    const lbl = document.createElementNS(NS, 'text');
    lbl.setAttribute('x', x.toFixed(1));
    lbl.setAttribute('y', String(h - 6));
    lbl.setAttribute('text-anchor', frac === 0 ? 'start' : (frac === 1 ? 'end' : 'middle'));
    lbl.setAttribute('fill', 'var(--m-muted)');
    lbl.setAttribute('font-size', '10');
    lbl.setAttribute('font-family', 'var(--m-mono)');
    lbl.textContent = fmtAxisSec(val);
    svg.appendChild(lbl);
  });

  if (lineAvailable && tBase < maxX) {
    const xStart = tBase;
    let xEnd = maxX;
    let yEnd = (maxX - xStart) / modeSlope;
    if (yEnd > maxY) {
      xEnd = Math.min(maxX, xStart + modeSlope * maxY);
      yEnd = maxY;
    }
    if (yEnd > 0) {
      const tLine = document.createElementNS(NS, 'line');
      tLine.setAttribute('x1', xOf(xStart).toFixed(1));
      tLine.setAttribute('y1', yOf(0).toFixed(1));
      tLine.setAttribute('x2', xOf(xEnd).toFixed(1));
      tLine.setAttribute('y2', yOf(yEnd).toFixed(1));
      tLine.setAttribute('stroke', modelCol);
      tLine.setAttribute('stroke-width', '1.8');
      tLine.setAttribute('opacity', '0.9');
      svg.appendChild(tLine);

      const interceptDot = document.createElementNS(NS, 'circle');
      interceptDot.setAttribute('cx', xOf(xStart).toFixed(1));
      interceptDot.setAttribute('cy', yOf(0).toFixed(1));
      interceptDot.setAttribute('r', '2.8');
      interceptDot.setAttribute('fill', modelCol);
      const interceptTitle = document.createElementNS(NS, 'title');
      interceptTitle.textContent = ((fit.prefill_available || fit.cache_available)
        ? 'Inferred base floor ('
        : 'Inferred min warm TTFT (') + modeQLabel + ' X-intercept): ~' + fit.inferred_ttft_s + 's';
      interceptDot.appendChild(interceptTitle);
      svg.appendChild(interceptDot);
    }
  }

  const warmRankMap = new WeakMap();
  warmPts.forEach(function (ws, idx) {
    warmRankMap.set(ws, idx);
  });
  const latestStepObj = modelTimedSteps.length
    ? modelTimedSteps[modelTimedSteps.length - 1]
    : null;
  const recentCutoffSet = new Set(modelTimedSteps.slice(-5));

  projPts.forEach(function (p) {
    const s = p.step;
    const cx = xOf(Math.min(maxX, Math.max(0, p.xSec)));
    const cy = yOf(Math.min(maxY, Math.max(0, p.yTok)));
    const isLatest = (s === latestStepObj);

    const g = document.createElementNS(NS, 'g');
    g.setAttribute('class', 'scatter-pt');

    let residualNote = '';
    const sParts = computeStepRooflineParts(s, fit);
    if (sParts) {
      const delta = s.duration_s - sParts.expectedS;
      residualNote = ' · ' + (delta >= 0 ? '+' : '') + delta.toFixed(1) + 's vs roofline';
    }
    const ptConvPrefix = (isExternalPt(s) && s.conv_title) ? ('[' + s.conv_title + '] ') : '';
    const peelNote = (scatterMode !== 'decode' || sPre > 0 || sCac > 0)
      ? (' · peeled ' + p.xSec.toFixed(2) + 's')
      : '';
    let ptMetricDesc;
    if (scatterMode === 'prefill') {
      ptMetricDesc = fmtInt(s.input_tokens || 0) + ' uncached in (' + fmtInt(s.output_tokens || 0) + ' out) in ' + s.duration_s + 's' + peelNote;
    } else if (scatterMode === 'cache') {
      ptMetricDesc = fmtInt(s.cache_read_tokens || 0) + ' cached (' + fmtInt(s.input_tokens || 0) + ' uncached) in ' + s.duration_s + 's' + peelNote;
    } else {
      ptMetricDesc = fmtInt(s.output_tokens) + ' tok in ' + s.duration_s + 's' + peelNote + ' (' +
        (s.effective_tps != null ? (s.effective_tps + ' tok/s, ') : '') +
        srcLabel(s.duration_source) + ')';
    }
    const ptSummary =
      ptConvPrefix +
      '#' + s.ordinal +
      (s.cold_cache_spike ? ' ❄ cold' : '') +
      ': ' + ptMetricDesc + residualNote;

    const titleEl = document.createElementNS(NS, 'title');
    titleEl.textContent = ptSummary;
    g.appendChild(titleEl);

    const hit = document.createElementNS(NS, 'circle');
    hit.setAttribute('class', 'scatter-hit');
    hit.setAttribute('cx', cx.toFixed(1));
    hit.setAttribute('cy', cy.toFixed(1));
    hit.setAttribute('r', '8');
    hit.setAttribute('fill', 'transparent');
    g.appendChild(hit);

    const cleanAlpha = Math.max(0.20, Math.min(0.95, 0.22 + 0.73 * p.wClean));

    if (s.cold_cache_spike) {
      const d = 4.5;
      const poly = document.createElementNS(NS, 'polygon');
      poly.setAttribute(
        'points',
        [
          cx.toFixed(1) + ',' + (cy - d).toFixed(1),
          (cx + d).toFixed(1) + ',' + cy.toFixed(1),
          cx.toFixed(1) + ',' + (cy + d).toFixed(1),
          (cx - d).toFixed(1) + ',' + cy.toFixed(1)
        ].join(' ')
      );
      poly.setAttribute('fill', 'var(--m-card)');
      poly.setAttribute('stroke', 'var(--chart-uncached)');
      poly.setAttribute('stroke-width', '1.6');
      const coldOp = (scatterMode === 'decode')
        ? (recentCutoffSet.has(s) ? 0.95 : 0.62)
        : (isLatest ? 1.0 : cleanAlpha);
      poly.setAttribute('opacity', coldOp.toFixed(2));
      g.appendChild(poly);
    } else {
      const wIdx = warmRankMap.get(s) || 0;
      const isRecent = wIdx >= warmPts.length - 5;
      const dot = document.createElementNS(NS, 'circle');
      dot.setAttribute('cx', cx.toFixed(1));
      dot.setAttribute('cy', cy.toFixed(1));
      dot.setAttribute('r', isLatest ? '4.3' : (isRecent ? '3.9' : '2.9'));
      dot.setAttribute('fill', modelCol);
      const dotAlpha = isLatest
        ? 1.0
        : (scatterMode === 'decode' ? (isRecent ? 0.88 : 0.34) : cleanAlpha);
      dot.setAttribute('fill-opacity', dotAlpha.toFixed(2));
      if (isRecent || isLatest) {
        dot.setAttribute('stroke', 'var(--m-card)');
        dot.setAttribute('stroke-width', '0.9');
      }
      g.appendChild(dot);
    }

    if (isLatest) {
      const halo = document.createElementNS(NS, 'circle');
      halo.setAttribute('cx', cx.toFixed(1));
      halo.setAttribute('cy', cy.toFixed(1));
      halo.setAttribute('r', '7');
      halo.setAttribute('fill', 'none');
      halo.setAttribute('stroke', modelCol);
      halo.setAttribute('stroke-width', '1.5');
      halo.setAttribute('opacity', '0.95');
      g.appendChild(halo);
    }

    g.addEventListener('mouseenter', function () {
      hover.textContent = ptSummary;
    });
    g.addEventListener('click', function () {
      if (isExternalPt(s)) {
        state.viewMode = 'conversation';
        state.followActive = false;
        state.selectedProjectScopeId = null;
        state.selectedConvId = s.conv_id;
        state.pendingJump = { convId: s.conv_id, stepIndex: s.step_index, modelName: modelName };
        const ft = document.getElementById('followToggle');
        if (ft) ft.checked = false;
        syncModeControls();
        if (onSwitchTarget) onSwitchTarget(s.conv_id);
        return;
      }
      const nextExpanded = (state.expandedStep === s.step_index) ? null : s.step_index;
      state.expandedStep = nextExpanded;
      if (nextExpanded !== null && !state.showAllSteps) {
        const payloadSteps = (state.lastPayload && state.lastPayload.steps) ? state.lastPayload.steps : allSteps;
        const allModelSteps = (payloadSteps || []).filter(function (x) { return x.model_name === modelName; });
        if (allModelSteps.length > RECENT_STEPS_LIMIT) {
          const shownIdx = allModelSteps.findIndex(function (x) { return x.step_index === s.step_index; });
          if (shownIdx !== -1 && shownIdx < allModelSteps.length - RECENT_STEPS_LIMIT) {
            state.showAllSteps = true;
          }
        }
      }
      if (onRerender) onRerender();
    });
    svg.appendChild(g);
  });

  let legHtml =
    '<span class="legend-item"><span class="model-dot" style="background:' + esc(modelCol) + ';opacity:0.38"></span>' +
    '<span class="model-dot" style="background:' + esc(modelCol) + '"></span><span>Older → Recent · ◎ Latest</span></span>';
  if (lineAvailable) {
    legHtml += '<span class="legend-item"><span class="trend-swatch" style="border-color:' + esc(modelCol) + '"></span><span>' + esc(lineLabel) + '</span></span>';
  }
  if (plottedPts.some(function (p) { return p.cold_cache_spike; })) {
    legHtml += '<span class="legend-item"><span class="diamond-swatch"></span><span>' + (fit.prefill_available ? 'Cold prefill' : 'Cold prefill (excl.)') + '</span></span>';
  }
  leg.innerHTML = legHtml;
}

function addCalloutRow(container, cls, leftHtml, rightHtml, titleText) {
  const row = document.createElement('div');
  row.className = 'val-note' + (cls ? (' ' + cls) : '');
  if (titleText) row.title = titleText;
  row.innerHTML = '<span>' + leftHtml + '</span><span class="mono">' + rightHtml + '</span>';
  container.appendChild(row);
}

function renderValBox(card) {
  const box = document.createElement('div');
  box.className = 'val-box';

  const head = document.createElement('div');
  head.className = 'val-box-head';
  const lbl = document.createElement('span');
  lbl.className = 'val-box-label';
  lbl.textContent = card.label;
  const stSpan = document.createElement('span');
  stSpan.className = 'muted mono';
  stSpan.textContent = fmtInt(card.stepsCount) + ' step' + (card.stepsCount === 1 ? '' : 's');
  head.appendChild(lbl);
  head.appendChild(stSpan);
  box.appendChild(head);

  const hero = document.createElement('div');
  hero.className = 'val-hero';
  const costSpan = document.createElement('span');
  costSpan.className = 'big-num mono';
  costSpan.textContent = card.costText;
  if (card.costTitle) costSpan.title = card.costTitle;
  const credSpan = document.createElement('span');
  credSpan.className = 'val-credits-eq mono';
  credSpan.textContent = card.creditsText;
  if (card.creditsTitle) credSpan.title = card.creditsTitle;
  hero.appendChild(costSpan);
  hero.appendChild(credSpan);
  box.appendChild(hero);

  if (card.hasFuture) {
    const fRow = document.createElement('div');
    fRow.className = 'kv-line';
    const fLbl = document.createElement('span');
    fLbl.className = 'muted';
    fLbl.textContent = card.futureLbl;
    if (card.futureTitle) fLbl.title = card.futureTitle;
    const fVal = document.createElement('span');
    fVal.className = 'mono';
    fVal.textContent = card.futureText;
    if (card.futureValTitle) fVal.title = card.futureValTitle;
    fRow.appendChild(fLbl);
    fRow.appendChild(fVal);
    box.appendChild(fRow);
  }

  [
    { name: 'Uncached In', text: card.uncachedText, title: card.uncachedTitle },
    { name: 'Cached Read', text: card.cacheReadText, title: card.cacheReadTitle },
    { name: 'Output (incl. think)', text: card.outputText, title: card.outputTitle }
  ].forEach(function (kv) {
    const row = document.createElement('div');
    row.className = 'kv-line';
    const k = document.createElement('span');
    k.className = 'muted';
    k.textContent = kv.name;
    const v = document.createElement('span');
    v.className = 'mono';
    v.textContent = kv.text;
    if (kv.title) v.title = kv.title;
    row.appendChild(k);
    row.appendChild(v);
    box.appendChild(row);
  });

  return box;
}

export function renderValuationCard(m, selectedModelEntry, isAllMode) {
  const val = m.valuation || {};
  const windows = val.windows || {};
  const subs = m.subagents || { count: 0, items: [], totals: {} };
  const cpu = Number(val.credits_per_usd || 100);
  const perCrUsd = cpu > 0 ? (1.0 / cpu) : 0.01;
  const cpuLabel = Number.isInteger(cpu) ? String(cpu) : String(Number(cpu.toFixed(4)));
  const rateText = cpuLabel + ' cr / $1 (' + fmtUsd(perCrUsd) + '/cr)';

  const creditsBadge = document.getElementById('valCreditsRateBadge');
  if (creditsBadge) {
    if (Math.abs(cpu - 100) > 1e-9) {
      creditsBadge.style.display = '';
      creditsBadge.textContent = cpuLabel + ' cr/$1';
      creditsBadge.title = 'AI Credit rate: ' + rateText;
    } else {
      creditsBadge.style.display = 'none';
    }
  }

  const titleEl = document.getElementById('valCardTitle');
  const subEl = document.getElementById('valCardSub');
  const poolToggleEl = document.getElementById('valPoolToggle');
  const modelPillEl = document.getElementById('valModelOverridePill');
  const valGridEl = document.getElementById('valGrid');
  const unpricedBtn = document.getElementById('valUnpricedBtn');
  const customBadge = document.getElementById('valCustomBadge');
  const calloutsEl = document.getElementById('valCallouts');
  calloutsEl.innerHTML = '';

  const ratesBtn = document.getElementById('ratesToggleBtn');
  if (ratesBtn) {
    ratesBtn.classList.toggle('active', Boolean(state.ratesOpen));
  }

  const encompassing = isAllMode ? (windows.selected || val) : (windows.total || val);
  const byPool = encompassing.by_pool || {};
  function hasPoolUsage(p) {
    return Boolean(p && (((p.total_steps || 0) > 0) || ((p.total_tokens || 0) > 0)));
  }
  const geminiActive = hasPoolUsage(byPool.gemini);
  const partnerActive = hasPoolUsage(byPool.partner);

  if (!selectedModelEntry) {
    if ((state.valPool === 'gemini' && !geminiActive) || (state.valPool === 'partner' && !partnerActive)) {
      state.valPool = 'all';
    }
  }

  if (poolToggleEl) {
    poolToggleEl.classList.toggle('overridden', Boolean(selectedModelEntry));
    const poolLabels = { all: 'All', gemini: 'Google', partner: '3rd-Party' };
    poolToggleEl.querySelectorAll('[data-pool]').forEach(function (btn) {
      const pool = btn.getAttribute('data-pool') || 'all';
      const pLbl = poolLabels[pool] || pool;
      const isDisabled = (pool === 'gemini' && !geminiActive) || (pool === 'partner' && !partnerActive);
      btn.disabled = isDisabled;
      btn.classList.toggle('disabled', isDisabled);
      const isActive = !selectedModelEntry && !isDisabled && state.valPool === pool;
      btn.classList.toggle('active', isActive);
      btn.setAttribute('aria-pressed', isActive ? 'true' : 'false');
      if (isDisabled) {
        btn.title = 'No ' + pLbl + ' pool usage in ' + (isAllMode ? 'selected timeframe' : 'this conversation');
      } else if (selectedModelEntry) {
        btn.title = 'Overridden by selected model — click to clear model filter and show ' + pLbl + ' pool';
      } else {
        btn.title = pool === 'all' ? 'Show combined cost across all models' : ('Filter cost cards to ' + pLbl + ' quota pool');
      }
    });
  }

  if (modelPillEl) {
    if (selectedModelEntry) {
      modelPillEl.style.display = '';
      modelPillEl.textContent = 'Model: ' + shortModelName(selectedModelEntry.model_name) + ' ×';
    } else {
      modelPillEl.style.display = 'none';
    }
  }

  function extractCardSpec(winObj, label) {
    if (selectedModelEntry) {
      const mName = selectedModelEntry.model_name;
      const mv = (winObj && winObj.by_model && winObj.by_model[mName]) || null;
      const baseMv = selectedModelEntry.valuation || {};
      const stepsCount = mv ? (mv.steps_count || 0) : 0;
      const totTok = mv ? (mv.total_tokens || 0) : 0;
      const hasUsage = stepsCount > 0 || totTok > 0;
      if (baseMv.status !== 'priced') {
        return {
          label: label,
          stepsCount: stepsCount,
          hasUsage: hasUsage,
          costText: '—',
          costTitle: baseMv.status === 'unattributed'
            ? 'Unattributed ("unknown" / "auto") model steps cannot be priced'
            : 'Model has no rate in local pricing catalog — click "⚙ Rates" to add one',
          creditsText: '',
          creditsTitle: '',
          uncachedText: '—',
          uncachedTitle: '',
          cacheReadText: '—',
          cacheReadTitle: '',
          outputText: '—',
          outputTitle: '',
          hasFuture: false,
          futureLbl: '',
          futureTitle: '',
          futureText: '',
          futureValTitle: ''
        };
      }
      const ratesObj = (mv && mv.rates) || baseMv.rates || {};
      const cUsd = mv ? mv.cost_usd : 0;
      const cCred = mv ? mv.credits : 0;
      const uCost = mv ? mv.uncached_input_cost_usd : 0;
      const cCost = mv ? mv.cache_read_cost_usd : 0;
      const oCost = mv ? mv.output_cost_usd : 0;
      const hasFut = Boolean(mv && mv.has_future_change && mv.future_effective_date && stepsCount > 0);
      let futLbl = '';
      let futText = '';
      let futValTitle = '';
      if (hasFut) {
        let mPctTag = '';
        if (cUsd > 0 && mv.future_cost_usd !== cUsd) {
          const mDiff = Math.round(((mv.future_cost_usd - cUsd) / cUsd) * 100);
          mPctTag = ' (' + (mDiff >= 0 ? '+' : '') + mDiff + '%)';
        }
        futLbl = 'From ' + mv.future_effective_date;
        futText = fmtUsd(mv.future_cost_usd) + mPctTag;
        futValTitle = 'Scheduled rates (' + mv.future_effective_date + '): ' + fmtUsd(mv.future_cost_usd) + ' · ' + fmtCredits(mv.future_credits) + mPctTag;
      }
      return {
        label: label,
        stepsCount: stepsCount,
        hasUsage: hasUsage,
        costText: fmtUsd(cUsd),
        costTitle: 'Paygo equivalent using ' + (baseMv.matched_label || mName) + ' rates (' + label + ')',
        creditsText: '· ' + fmtCredits(cCred),
        creditsTitle: 'AI Credits equivalent (' + rateText + ')',
        uncachedText: fmtUsd(uCost),
        uncachedTitle: 'Uncached input rate: ' + fmtRate(ratesObj.input) + '/1M · ' + fmtCredits((uCost || 0) * cpu),
        cacheReadText: fmtUsd(cCost),
        cacheReadTitle: 'Cached read rate: ' + fmtRate(ratesObj.cache_read) + '/1M · ' + fmtCredits((cCost || 0) * cpu),
        outputText: fmtUsd(oCost),
        outputTitle: 'Output rate: ' + fmtRate(ratesObj.output) + '/1M (includes thinking tokens) · ' + fmtCredits((oCost || 0) * cpu),
        hasFuture: hasFut,
        futureLbl: futLbl,
        futureTitle: futLbl ? ('Scheduled rate change: ' + mv.future_effective_date) : '',
        futureText: futText,
        futureValTitle: futValTitle
      };
    }

    const pv = (winObj && winObj.by_pool && winObj.by_pool[state.valPool]) || winObj || {};
    const cov = pv.coverage || {};
    const pricedSteps = cov.priced_steps || 0;
    const unpricedSteps = cov.unpriced_steps || 0;
    const unattrSteps = cov.unattributed_steps || 0;
    const pStepsCount = pv.total_steps != null ? pv.total_steps : (pricedSteps + unpricedSteps + unattrSteps);
    const pHasUsage = pStepsCount > 0 || ((pv.total_tokens || 0) > 0) || ((pv.cost_usd || 0) > 0);
    const hasAnyPriced = pricedSteps > 0;
    const hasGaps = (unpricedSteps + unattrSteps) > 0;
    const noDataOrZero = !hasAnyPriced && !hasGaps;

    const pHasFut = Boolean(hasAnyPriced && pv.has_future_change);
    let pFutLbl = '';
    let pFutTitle = '';
    let pFutText = '';
    let pFutValTitle = '';
    if (pHasFut) {
      const dates = pv.future_effective_dates || [];
      pFutLbl = dates.length === 1 ? ('From ' + dates[0]) : 'Scheduled std.';
      pFutTitle = dates.length ? ('Scheduled rate change(s): ' + dates.join(', ')) : '';
      let futPctTag = '';
      if (pv.cost_usd > 0 && pv.future_cost_usd !== pv.cost_usd) {
        const gDiff = Math.round(((pv.future_cost_usd - pv.cost_usd) / pv.cost_usd) * 100);
        futPctTag = ' (' + (gDiff >= 0 ? '+' : '') + gDiff + '%)';
      }
      pFutText = fmtUsd(pv.future_cost_usd) + futPctTag;
      pFutValTitle = 'Scheduled rates' + (dates.length ? (' (' + dates.join(', ') + ')') : '') + ': ' + fmtUsd(pv.future_cost_usd) + ' · ' + fmtCredits(pv.future_credits) + futPctTag;
    }

    return {
      label: label,
      stepsCount: pStepsCount,
      hasUsage: pHasUsage,
      costText: noDataOrZero ? '$0.00' : (hasAnyPriced ? (fmtUsd(pv.cost_usd) + (hasGaps ? '*' : '')) : '—'),
      costTitle: hasGaps
        ? ('Partial coverage: ' + pricedSteps + ' priced step(s), ' + unpricedSteps + ' unpriced, ' + unattrSteps + ' unattributed')
        : ('Paygo equivalent (' + label + ')'),
      creditsText: noDataOrZero ? '· 0 cr' : (hasAnyPriced ? ('· ' + fmtCredits(pv.credits) + (hasGaps ? '*' : '')) : ''),
      creditsTitle: 'AI Credits equivalent (' + rateText + ')',
      uncachedText: hasAnyPriced ? fmtUsd(pv.uncached_input_cost_usd) : (hasGaps ? '—' : '$0.00'),
      uncachedTitle: hasAnyPriced
        ? ('Uncached input tokens cost · ' + fmtCredits((pv.uncached_input_cost_usd || 0) * cpu))
        : 'Uncached input tokens cost',
      cacheReadText: hasAnyPriced ? fmtUsd(pv.cache_read_cost_usd) : (hasGaps ? '—' : '$0.00'),
      cacheReadTitle: hasAnyPriced
        ? ('Cached prompt read tokens cost · ' + fmtCredits((pv.cache_read_cost_usd || 0) * cpu))
        : 'Cached prompt read tokens cost',
      outputText: hasAnyPriced ? fmtUsd(pv.output_cost_usd) : (hasGaps ? '—' : '$0.00'),
      outputTitle: hasAnyPriced
        ? ('Includes response, tool call, and thinking tokens · ' + fmtCredits((pv.output_cost_usd || 0) * cpu))
        : 'Includes response, tool call, and thinking tokens',
      hasFuture: pHasFut,
      futureLbl: pFutLbl,
      futureTitle: pFutTitle,
      futureText: pFutText,
      futureValTitle: pFutValTitle
    };
  }

  const cards = [];
  if (isAllMode) {
    const win = m.window || state.timeWindow || 'all';
    const selLblMap = { '5h': 'Last 5h', '24h': 'Last 24h', '7d': 'Last 7d', '30d': 'Last 30d', 'all': 'All Time' };
    if (win === '24h' || win === '7d' || win === '30d' || win === 'all') {
      cards.push(extractCardSpec(windows['5h'], 'Last 5h'));
    }
    if (win === '30d' || win === 'all') {
      cards.push(extractCardSpec(windows['7d'], 'Last 7d'));
    }
    cards.push(extractCardSpec(windows.selected || val, selLblMap[win] || 'All Time'));
  } else {
    const ageS = Number(val.conversation_age_s || 0);
    const c5 = extractCardSpec(windows['5h'], 'Last 5h');
    const c7 = extractCardSpec(windows['7d'], 'Last 7d');
    const cTot = extractCardSpec(windows.total || val, 'Total');
    if (ageS >= 5 * 3600 && c5.hasUsage) {
      cards.push(c5);
    }
    if (ageS >= 7 * 86400 && c7.hasUsage) {
      cards.push(c7);
    }
    cards.push(cTot);
  }

  if (valGridEl) {
    valGridEl.innerHTML = '';
    valGridEl.setAttribute('data-count', String(cards.length));
    cards.forEach(function (card) {
      valGridEl.appendChild(renderValBox(card));
    });
  }

  if (selectedModelEntry) {
    const mvMeta = selectedModelEntry.valuation || {};
    titleEl.textContent = 'Paygo & AI Credits · ' + shortModelName(selectedModelEntry.model_name);
    subEl.textContent = mvMeta.matched_label
      ? ('Matched: ' + mvMeta.matched_label + ((mvMeta.source === 'override' || mvMeta.source === 'custom') ? (' (' + mvMeta.source + ')') : ''))
      : '';
    customBadge.style.display = (mvMeta.source === 'override' || mvMeta.source === 'custom') ? '' : 'none';

    if (mvMeta.status !== 'priced') {
      if (mvMeta.status === 'unpriced') {
        unpricedBtn.style.display = '';
        unpricedBtn.textContent = '⚠ 1 unpriced — set rate';
        unpricedBtn.title = 'Open local pricing editor to set a rate for ' + selectedModelEntry.model_name;
        addCalloutRow(
          calloutsEl,
          'warn',
          'Unpriced model: <span class="mono">' + esc(selectedModelEntry.model_name) + '</span>',
          esc(fmtInt(selectedModelEntry.steps_count) + ' step(s)'),
          'No matching pricing rule found'
        );
      } else {
        unpricedBtn.style.display = 'none';
        addCalloutRow(
          calloutsEl,
          '',
          'Unattributed steps (<span class="mono">' + esc(selectedModelEntry.model_name) + '</span>) are excluded from cost',
          esc(fmtInt(selectedModelEntry.steps_count) + ' step(s)'),
          ''
        );
      }
    } else {
      unpricedBtn.style.display = 'none';
      if (mvMeta.orchestrator_role === 'executor' && mvMeta.orchestrator_savings_usd != null) {
        const futSavNote = (mvMeta.orchestrator_future_savings_usd != null && Math.abs(mvMeta.orchestrator_future_savings_usd - mvMeta.orchestrator_savings_usd) > 1e-9)
          ? (' · Future: ' + fmtUsd(mvMeta.orchestrator_future_savings_usd))
          : '';
        const cfCost = Number(mvMeta.cost_usd || 0) + Number(mvMeta.orchestrator_savings_usd || 0);
        const exSavPct = cfCost > 0 ? Math.round((mvMeta.orchestrator_savings_usd / cfCost) * 1000) / 10 : 0;
        addCalloutRow(
          calloutsEl,
          'orch',
          'Orchestrator Saved <span class="muted">(vs running Executor on ' + esc(mvMeta.counterfactual_label || 'Planner') + ')</span>',
          esc(fmtUsd(mvMeta.orchestrator_savings_usd) + ' (' + fmtCredits(mvMeta.orchestrator_savings_usd * cpu) + (exSavPct ? (' · ' + fmtOrchSavingsPct(exSavPct)) : '') + ')' + futSavNote),
          'Savings from delegating Executor steps to ' + (mvMeta.matched_label || 'Hydrogen')
        );
      }
    }
  } else {
    titleEl.textContent = 'Paygo & AI Credits';
    const subNoteParts = [];
    if (state.valPool === 'gemini') subNoteParts.push('Google Pool');
    else if (state.valPool === 'partner') subNoteParts.push('3rd-Party Pool');
    if (!isAllMode && subs.count > 0) {
      subNoteParts.push('incl. ' + subs.count + ' subagent' + (subs.count === 1 ? '' : 's'));
    }
    subEl.textContent = subNoteParts.join(' · ');
    customBadge.style.display = val.override_active ? '' : 'none';

    const overallCov = encompassing.coverage || {};
    const overallUnpriced = overallCov.unpriced_models || [];
    if (overallUnpriced.length > 0) {
      unpricedBtn.style.display = '';
      unpricedBtn.textContent = '⚠ ' + overallUnpriced.length + ' unpriced — set rate';
      unpricedBtn.title = 'Unpriced model(s): ' + overallUnpriced.map(function (u) { return u.model_name; }).join(', ');
    } else {
      unpricedBtn.style.display = 'none';
    }

    const activeEnc = (byPool && byPool[state.valPool]) || encompassing;
    const cov = activeEnc.coverage || {};
    const unpricedSteps = cov.unpriced_steps || 0;
    const unattrSteps = cov.unattributed_steps || 0;
    const unpricedList = cov.unpriced_models || [];

    const orch = activeEnc.orchestrator || {};
    if (orch.present) {
      if ((orch.executor_steps || 0) > 0) {
        const plannerStr = (orch.planner_labels && orch.planner_labels.length)
          ? orch.planner_labels.join(' / ')
          : 'Planner';
        const orchFutNote = (orch.has_future_change && Math.abs((orch.future_savings_usd || 0) - (orch.savings_usd || 0)) > 1e-9)
          ? (' · Future: ' + fmtUsd(orch.future_savings_usd))
          : '';
        addCalloutRow(
          calloutsEl,
          'orch',
          'Orchestrator Saved <span class="muted">(vs running ' + esc(orch.executor_steps) + ' Executor step' + (orch.executor_steps === 1 ? '' : 's') + ' on ' + esc(plannerStr) + ')</span>',
          esc(fmtUsd(orch.savings_usd) + ' (' + fmtCredits(orch.savings_credits) + ' · ' + fmtOrchSavingsPct(orch.savings_pct) + ')' + orchFutNote),
          'Actual Orchestrator cost ' + fmtUsd(orch.actual_cost_usd) + ' vs counterfactual ' + fmtUsd(orch.counterfactual_cost_usd)
        );
      } else {
        addCalloutRow(
          calloutsEl,
          'orch',
          'Orchestrator Saved <span class="muted">(' + esc(orch.planner_steps) + ' Planner step' + (orch.planner_steps === 1 ? '' : 's') + ')</span>',
          '$0.00 (no Executor steps yet)',
          'Savings accrue when Orchestrator hands off execution steps to ' + (orch.executor_label || 'Hydrogen')
        );
      }
    }

    if (!isAllMode && subs.count > 0) {
      const ownV = activeEnc.own || {};
      const subV = activeEnc.subagents || {};
      const ownHasPriced = ((ownV.coverage && ownV.coverage.priced_steps) || 0) > 0;
      const subHasPriced = ((subV.coverage && subV.coverage.priced_steps) || 0) > 0;
      const ownText = ownHasPriced
        ? (fmtUsd(ownV.cost_usd) + ' (' + fmtCredits(ownV.credits) + ')')
        : (((ownV.coverage && (ownV.coverage.unpriced_steps + ownV.coverage.unattributed_steps)) || 0) > 0 ? '—' : '$0.00 (0 cr)');
      const subText = subHasPriced
        ? (fmtUsd(subV.cost_usd) + ' (' + fmtCredits(subV.credits) + ')')
        : (((subV.coverage && (subV.coverage.unpriced_steps + subV.coverage.unattributed_steps)) || 0) > 0 ? '—' : '$0.00 (0 cr)');
      addCalloutRow(
        calloutsEl,
        '',
        'Session Breakdown',
        esc('Main: ' + ownText + ' · Subagents (' + subs.count + '): ' + subText),
        'Combined session cost includes parent conversation and child subagents'
      );
    }

    if ((unpricedSteps + unattrSteps) > 0) {
      const gapParts = [];
      if (unpricedSteps > 0) {
        gapParts.push(unpricedSteps + ' unpriced step(s): ' + unpricedList.map(function (u) { return u.model_name; }).join(', '));
      }
      if (unattrSteps > 0) {
        gapParts.push(unattrSteps + ' unattributed ("unknown"/"auto") step(s)');
      }
      addCalloutRow(
        calloutsEl,
        'warn',
        'Partial coverage — excluded from cost',
        esc(gapParts.join(' · ')),
        'Click "⚙ Rates" to add rates for unpriced models'
      );
    }
  }

  calloutsEl.classList.toggle('visible', calloutsEl.children.length > 0);
}
