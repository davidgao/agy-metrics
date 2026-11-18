// Main dashboard coordinator: DOM updates, SSE live subscription, and UI events.

import {
  renderByModel,
  renderChart,
  renderLatencyScatter,
  renderValuationCard
} from './charts.js';
import {
  computeStepRooflineParts,
  esc,
  fmtApproxDuration,
  fmtColdTimeNote,
  fmtCompact,
  fmtCredits,
  fmtDuration,
  fmtExactSecNote,
  fmtInt,
  fmtUsd,
  fmtWastedSummary,
  fmtWastedTimeNote,
  getModelColorMap,
  shortModelName,
  srcLabel
} from './format.js';
import { initPricingModal } from './pricing-modal.js';
import { initScopeSelector, renderScopeSelector } from './selector.js';
import {
  ALL_CONVERSATIONS_ID,
  RECENT_STEPS_LIMIT,
  buildUrl,
  canUseEventSource,
  currentRequestedConvId,
  hasSidecarFetch,
  isAggregateMode,
  isProjectScopeId,
  sidecarFetch,
  state,
  syncGeneratingTimer,
  syncModeControls
} from './state.js';
import { initThemeControls } from './theme.js';

const SSE_WATCHDOG_MS = 35000;
const POLL_IDLE_MS = 1500;
const POLL_GENERATING_MS = 600;
const INIT_SNAPSHOT_FALLBACK_MS = 900;

function selectScope(scopeId) {
  if (!scopeId) return;
  state.modelFilter = null;
  state.expandedStep = null;
  state.showAllSteps = false;
  state.showColdPrefills = false;
  state.lastScatterKey = null;
  state.followActive = false;

  if (scopeId === ALL_CONVERSATIONS_ID) {
    state.viewMode = 'all';
    state.selectedProjectScopeId = null;
    state.selectedConvId = null;
  } else if (isProjectScopeId(scopeId)) {
    state.viewMode = 'project';
    state.selectedProjectScopeId = scopeId;
    state.selectedConvId = null;
  } else {
    state.viewMode = 'conversation';
    state.selectedProjectScopeId = null;
    state.selectedConvId = scopeId;
  }

  const followEl = document.getElementById('followToggle');
  if (followEl) followEl.checked = false;
  syncModeControls();
  if (state.convsDataCache) renderScopeSelector(state.convsDataCache, selectScope);
  switchTarget(scopeId);
}

function toggleModelFilter(modelName) {
  state.modelFilter = (state.modelFilter === modelName) ? null : modelName;
  if (state.lastPayload) renderMetrics(state.lastPayload);
}

function clearModelFilter() {
  state.modelFilter = null;
  state.lastScatterKey = null;
  if (state.lastPayload) renderMetrics(state.lastPayload);
}

function renderMetrics(m) {
  if (!m) return;
  const mMode = (m.mode === 'all' || m.mode === 'project') ? m.mode : 'conversation';
  const isAllMode = isAggregateMode(mMode);
  const viewKey = isAllMode
    ? ((m.conversation_id || ALL_CONVERSATIONS_ID) + ':' + (m.window || state.timeWindow || 'all'))
    : (m.conversation_id || '');
  if (viewKey && viewKey !== state.renderedViewKey) {
    const prevScopePrefix = state.renderedViewKey
      ? state.renderedViewKey.split(':').slice(0, -1).join(':')
      : '';
    const curScopePrefix = isAllMode ? (m.conversation_id || ALL_CONVERSATIONS_ID) : '';
    const prevWasSameAgg = Boolean(isAllMode && prevScopePrefix && prevScopePrefix === curScopePrefix);
    state.renderedViewKey = viewKey;
    state.renderedConvId = m.conversation_id || null;
    state.showColdPrefills = false;
    state.lastScatterKey = null;
    state.lastHistoryKey = null;
    if (state.pendingJump && !isAllMode && state.pendingJump.convId === m.conversation_id) {
      state.expandedStep = state.pendingJump.stepIndex;
      state.showAllSteps = true;
      state.modelFilter = state.pendingJump.modelName || null;
      state.pendingJump = null;
    } else {
      state.showAllSteps = false;
      state.expandedStep = null;
      if (!prevWasSameAgg) {
        state.modelFilter = null;
      }
    }
  } else if (state.pendingJump && !isAllMode && state.pendingJump.convId === m.conversation_id) {
    state.expandedStep = state.pendingJump.stepIndex;
    state.showAllSteps = true;
    state.modelFilter = state.pendingJump.modelName || state.modelFilter;
    state.pendingJump = null;
  }
  syncModeControls();

  const parentBtn = document.getElementById('parentConvBtn');
  if (parentBtn) {
    if (!isAllMode && m.is_subagent && m.parent_id) {
      parentBtn.style.display = '';
      parentBtn.textContent = '↑ ' + (m.parent_title || 'Parent');
      parentBtn.title = 'Back to parent conversation: ' + (m.parent_title || m.parent_id);
      parentBtn.setAttribute('data-parent-id', m.parent_id);
    } else {
      parentBtn.style.display = 'none';
      parentBtn.removeAttribute('data-parent-id');
    }
  }

  const ctxCard = document.getElementById('ctxCard');
  const histCard = document.getElementById('historyCard');
  const stepsCard = document.getElementById('stepsCard');
  if (ctxCard) ctxCard.style.display = isAllMode ? 'none' : '';
  const subCardTop = document.getElementById('subagentCard');
  if (subCardTop) subCardTop.style.display = isAllMode ? 'none' : '';
  if (histCard) histCard.style.display = isAllMode ? 'none' : '';
  if (stepsCard) stepsCard.style.display = isAllMode ? 'none' : '';

  const allScopeSummary = document.getElementById('allScopeSummary');
  if (allScopeSummary) {
    if (isAllMode) {
      const nConv = m.conversations_count || 0;
      const nSub = m.subagents_count || 0;
      allScopeSummary.textContent = nConv + ' conv' + (nConv === 1 ? '' : 's') +
        (nSub ? (' · ' + nSub + ' subagent' + (nSub === 1 ? '' : 's')) : '');
    } else {
      allScopeSummary.textContent = '';
    }
  }
  state.status = m.status || 'idle';
  state.generatingSinceMs = m.generating_since_ms || null;
  syncGeneratingTimer();

  const byModel = m.by_model || [];
  if (state.modelFilter && !byModel.some(function (bm) { return bm.model_name === state.modelFilter; })) {
    state.modelFilter = null;
  }
  const colorMap = getModelColorMap(byModel);

  const modelPill = document.getElementById('modelPill');
  modelPill.textContent = m.model_name || 'unknown';
  modelPill.title = m.model_name || 'unknown';

  if (!isAllMode) {
    const ctx = m.context || {};
    const brk = ctx.breakdown || {};
    const cur = ctx.current_tokens || 0;
    document.getElementById('ctxTotal').textContent = fmtCompact(cur) + ' tok';
    document.getElementById('ctxHitText').textContent = (ctx.cache_hit_pct || 0) + '% cached (' + fmtCompact(ctx.cache_read_tokens) + ' / ' + fmtCompact(cur) + ')';

    const badgesEl = document.getElementById('contextBadges');
    badgesEl.innerHTML = '';
    if (ctx.cold_cache_spike) {
      const b1 = document.createElement('span');
      b1.className = 'pill warn';
      b1.textContent = ctx.model_switched ? 'Model Switch Prefill' : 'Cold Prefill';
      b1.title = ctx.model_switched
        ? 'Expected cold KV-cache prefill caused by switching models on this step'
        : 'Cold KV-cache prefill (>=25k uncached tokens with <20% cache hit)';
      badgesEl.appendChild(b1);
    }
    if (ctx.compacted) {
      const b2 = document.createElement('span');
      b2.className = 'pill compact';
      b2.textContent = 'Compacted';
      b2.title = 'Active context includes a compacted summary (<CONTEXT_SUMMARY>)';
      badgesEl.appendChild(b2);
    }

    const showSummarySeg = (brk.summary_tokens || 0) > 0 || ((m.totals && m.totals.compacted_count) || 0) > 0;
    const segs = [
      { cls: 'c-sys', label: 'System & Tools', val: brk.system_tokens || 0, hidden: false },
      { cls: 'c-summary', label: 'Compacted Summary', val: brk.summary_tokens || 0, hidden: !showSummarySeg },
      { cls: 'c-tool', label: 'Tool Outputs', val: brk.tool_tokens || 0, hidden: false },
      { cls: 'c-model', label: 'Model', val: brk.model_tokens || 0, hidden: false },
      { cls: 'c-user', label: 'User', val: brk.user_tokens || 0, hidden: false }
    ];
    const bar = document.getElementById('ctxBar');
    const leg = document.getElementById('ctxLegend');
    bar.innerHTML = '';
    leg.innerHTML = '';
    segs.forEach(function (s) {
      if (s.hidden && s.val === 0) return;
      const pct = cur > 0 ? (s.val / cur) * 100 : 0;
      if (s.val > 0) {
        const segDiv = document.createElement('div');
        segDiv.className = 'seg ' + s.cls;
        segDiv.style.width = pct.toFixed(2) + '%';
        segDiv.title = s.label + ': ' + fmtInt(s.val) + ' tok (' + pct.toFixed(1) + '%)';
        bar.appendChild(segDiv);
      }

      const lItem = document.createElement('span');
      lItem.className = 'legend-item';
      lItem.title = s.label + ': ' + fmtInt(s.val) + ' tok (' + pct.toFixed(1) + '%)';
      lItem.innerHTML = '<span class="swatch ' + s.cls + '"></span><span>' + esc(s.label) + ': <span class="mono">' + esc(fmtCompact(s.val)) + '</span></span>';
      leg.appendChild(lItem);
    });

    const chipsEl = document.getElementById('topToolsChips');
    chipsEl.innerHTML = '';
    (ctx.top_tools || []).forEach(function (t) {
      const ch = document.createElement('span');
      ch.className = 'chip mono';
      ch.textContent = t.name + ' ' + fmtCompact(t.tokens) + ' (' + t.pct + '%)';
      ch.title = t.name + ': ~' + fmtInt(t.tokens) + ' tokens (' + t.pct + '% of active context)';
      chipsEl.appendChild(ch);
    });
  }

  const globalTotals = m.totals || {};
  renderByModel(byModel, globalTotals, colorMap, {
    onToggleModel: toggleModelFilter,
    onClearFilter: clearModelFilter
  });

  const selectedModelEntry = state.modelFilter
    ? byModel.find(function (bm) { return bm.model_name === state.modelFilter; })
    : null;
  const allSteps = m.steps || [];
  renderLatencyScatter(selectedModelEntry, allSteps, colorMap, {
    onRerender: function () {
      if (state.lastPayload) renderMetrics(state.lastPayload);
    },
    onSwitchTarget: function (cid) {
      switchTarget(cid);
    }
  });
  renderValuationCard(m, selectedModelEntry, isAllMode);

  const scopedTot = selectedModelEntry || globalTotals;
  const scopedLast = selectedModelEntry ? selectedModelEntry.latest_step : m.latest_step;
  const scopedStepsCount = selectedModelEntry ? selectedModelEntry.steps_count : (m.planner_steps_count || 0);
  const scopeTag = selectedModelEntry ? (' · ' + shortModelName(selectedModelEntry.model_name)) : '';

  const stepTag = scopedLast ? (' #' + scopedLast.ordinal) : '';
  const lastConvNote = (isAllMode && scopedLast && scopedLast.conv_title) ? ('[' + scopedLast.conv_title + '] ') : '';
  document.getElementById('kpiInTitle').textContent = 'Input Tokens' + scopeTag;
  document.getElementById('kpiOutTitle').textContent = 'Output Tokens' + scopeTag;
  const kpiLastTitleEl = document.getElementById('kpiLastTitle');
  kpiLastTitleEl.textContent = 'Latest Step' + stepTag + scopeTag;
  kpiLastTitleEl.title = lastConvNote ? ('Global latest step in ' + scopedLast.conv_title) : '';
  document.getElementById('kpiTimingTitle').textContent = (selectedModelEntry ? 'Model Timing' : (m.mode === 'project' ? 'Project Timing' : (isAllMode ? 'Total Timing' : 'Session Timing'))) + scopeTag;

  document.getElementById('totGrossIn').textContent = fmtCompact(scopedTot.gross_input_tokens);
  document.getElementById('totIn').textContent = fmtInt(scopedTot.input_tokens);
  document.getElementById('totCache').textContent = fmtInt(scopedTot.cache_read_tokens);
  document.getElementById('totHitPct').textContent = (scopedTot.cache_hit_pct || 0) + '%';

  document.getElementById('totOut').textContent = fmtCompact(scopedTot.output_tokens);
  document.getElementById('totThink').textContent = fmtInt(scopedTot.est_thinking_tokens);
  document.getElementById('totVis').textContent = fmtInt(scopedTot.est_visible_tokens);

  const lastDurEl = document.getElementById('lastDur');
  lastDurEl.textContent = (scopedLast && scopedLast.duration_s != null) ? (scopedLast.duration_s + 's') : '—';
  if (!scopedLast) {
    lastDurEl.title = '';
  } else if (scopedLast.step_kind === 'checkpoint') {
    lastDurEl.title = lastConvNote + 'System compaction call (CHECKPOINT; untimed)';
  } else if (scopedLast.duration_s != null) {
    const lastRetryNote = scopedLast.retry_wait_s > 0
      ? (' · +' + fmtDuration(scopedLast.retry_wait_s) + fmtExactSecNote(scopedLast.retry_wait_s) + ' retry wait')
      : '';
    lastDurEl.title = lastConvNote + 'Model inference: ' + scopedLast.duration_s + 's (' + srcLabel(scopedLast.duration_source) + ')' + lastRetryNote;
  } else {
    lastDurEl.title = lastConvNote + 'Model inference duration unavailable';
  }

  const lastDelayEl = document.getElementById('lastDelay');
  const lastModelEntry = selectedModelEntry || (scopedLast ? byModel.find(function (bm) { return bm.model_name === scopedLast.model_name; }) : null);
  const lastFit = lastModelEntry && lastModelEntry.latency_fit;
  const lastRetryS = Number((scopedLast && scopedLast.retry_wait_s) || 0);
  if (!scopedLast) {
    lastDelayEl.textContent = '—';
    lastDelayEl.title = '';
  } else if (scopedLast.est_delay_s != null) {
    const combinedLastDelay = Number(scopedLast.est_delay_s) + lastRetryS;
    const lastTagParts = [];
    if (scopedLast.cold_cache_spike) lastTagParts.push('❄');
    if (lastRetryS > 0) lastTagParts.push('↻');
    lastDelayEl.textContent = '~' + combinedLastDelay.toFixed(2) + 's' + (lastTagParts.length ? (' (' + lastTagParts.join(' ') + ')') : '');
    const retryPart = lastRetryS > 0
      ? (' (~' + Number(scopedLast.est_delay_s).toFixed(2) + 's roofline + ' + fmtDuration(lastRetryS) + fmtExactSecNote(lastRetryS) + ' retry wait)')
      : '';
    const lastParts = computeStepRooflineParts(scopedLast, lastFit);
    if (lastParts) {
      lastDelayEl.title = 'Actual ' + scopedLast.duration_s + 's vs ~' + lastParts.expectedS.toFixed(2) + 's speed roofline (' + lastParts.partsText + ')' + retryPart;
    } else {
      lastDelayEl.title = 'Estimated excess latency above speed roofline' + retryPart;
    }
  } else if (lastRetryS > 0) {
    lastDelayEl.textContent = '+' + fmtDuration(lastRetryS) + ' (↻)';
    lastDelayEl.title = 'Retry wait before winning attempt: ' + fmtDuration(lastRetryS) + fmtExactSecNote(lastRetryS) +
      (scopedLast.cold_cache_spike ? ' (❄ cold prefill; prefill roofline gated)' : '');
  } else if (scopedLast.step_kind === 'checkpoint') {
    lastDelayEl.textContent = '—';
    lastDelayEl.title = 'System compaction call (excluded from roofline delay)';
  } else if (scopedLast.cold_cache_spike) {
    lastDelayEl.textContent = '— (❄ cold)';
    lastDelayEl.title = 'Cold KV-cache prefill step; needs ≥6 steps ≥25k uncached tokens to unlock prefill roofline';
  } else if (scopedLast.duration_s == null) {
    lastDelayEl.textContent = '—';
    lastDelayEl.title = 'Step duration unavailable';
  } else if (!lastFit || lastFit.reason === 'min_steps') {
    lastDelayEl.textContent = '— (' + ((lastFit && lastFit.warm_count) || 0) + '/4)';
    lastDelayEl.title = 'Needs ≥4 warm timed steps on ' + shortModelName(scopedLast.model_name) + ' to estimate delay';
  } else if (lastFit.reason === 'unattributed') {
    lastDelayEl.textContent = '—';
    lastDelayEl.title = 'Roofline delay disabled for unattributed ("unknown" / "auto") model steps';
  } else {
    lastDelayEl.textContent = '—';
    lastDelayEl.title = 'Speed roofline not yet available for ' + shortModelName(scopedLast.model_name);
  }

  const lastOutEl = document.getElementById('lastOut');
  lastOutEl.textContent = scopedLast ? (fmtInt(scopedLast.output_tokens) + ' tok') : '—';
  lastOutEl.title = scopedLast
    ? (lastConvNote + fmtInt(scopedLast.output_tokens) + ' output (~' + fmtInt(scopedLast.est_thinking_tokens) + ' thinking, ~' + fmtInt(scopedLast.est_visible_tokens) + ' visible + tools)')
    : '';

  const sumModelEl = document.getElementById('sumModelTime');
  const sumModelTimeS = scopedTot.model_time_s || 0;
  const sumInfTimeS = scopedTot.inference_time_s != null ? scopedTot.inference_time_s : sumModelTimeS;
  sumModelEl.textContent = fmtDuration(sumModelTimeS);
  sumModelEl.title = (scopedTot.timed_steps_count || 0) + ' timed step(s): ' +
    fmtDuration(sumInfTimeS) + fmtExactSecNote(sumInfTimeS) + ' inference' +
    fmtColdTimeNote(scopedTot) + fmtWastedTimeNote(scopedTot);

  const sumDelayEl = document.getElementById('sumEstDelay');
  const scopedWastedS = Number(scopedTot.wasted_time_s || 0);
  if (scopedTot.est_total_delay_s != null) {
    const totDelay = Number(scopedTot.est_total_delay_s);
    const totColdDelaySteps = scopedTot.cold_delay_steps_count || 0;
    const totWarmDelaySteps = Math.max(0, (scopedTot.est_delay_steps_count || 0) - totColdDelaySteps);
    const partialScoped = (scopedTot.est_delay_steps_count || 0) < (scopedTot.warm_timed_steps_count || 0) || ((scopedTot.est_delay_steps_count || 0) === 0 && scopedWastedS > 0);
    sumDelayEl.textContent = fmtApproxDuration(totDelay) + (partialScoped ? '*' : '');
    const dParts = [];
    if (scopedTot.est_warm_delay_s != null) {
      dParts.push('~' + fmtDuration(scopedTot.est_warm_delay_s) + fmtExactSecNote(scopedTot.est_warm_delay_s) + ' warm roofline across ' + totWarmDelaySteps + '/' + (scopedTot.warm_timed_steps_count || 0) + ' warm step(s)');
    }
    if (scopedTot.est_cold_delay_s != null) {
      dParts.push('~' + fmtDuration(scopedTot.est_cold_delay_s) + fmtExactSecNote(scopedTot.est_cold_delay_s) + ' ❄ cold roofline across ' + totColdDelaySteps + ' step(s)');
    }
    if (scopedWastedS > 0) {
      const wSum = fmtWastedSummary(scopedTot);
      dParts.push(fmtDuration(scopedWastedS) + fmtExactSecNote(scopedWastedS) + ' retry/failed wait' + (wSum ? (' (' + wSum + ')') : ''));
    }
    sumDelayEl.title = 'Estimated total delay included in model time: ' +
      fmtApproxDuration(totDelay) + fmtExactSecNote(totDelay) +
      (dParts.length ? (' (' + dParts.join(' + ') + ')') : '') +
      (sumModelTimeS > 0 ? (' · ' + Math.min(100, Math.round((totDelay / sumModelTimeS) * 100)) + '% of model time') : '');
  } else if (selectedModelEntry && selectedModelEntry.latency_fit && selectedModelEntry.latency_fit.reason === 'min_steps') {
    sumDelayEl.textContent = '— (' + (selectedModelEntry.latency_fit.warm_count || 0) + '/4)';
    sumDelayEl.title = 'Needs ≥4 warm timed steps on ' + shortModelName(selectedModelEntry.model_name) + ' to estimate cumulative delay';
  } else if (selectedModelEntry && selectedModelEntry.latency_fit && selectedModelEntry.latency_fit.reason === 'unattributed') {
    sumDelayEl.textContent = '—';
    sumDelayEl.title = 'Roofline delay disabled for unattributed ("unknown" / "auto") model steps';
  } else {
    sumDelayEl.textContent = '—';
    sumDelayEl.title = 'Needs ≥4 warm timed steps on a model to fit its speed roofline';
  }

  const sumWastedRowEl = document.getElementById('sumWastedRow');
  const sumWastedEl = document.getElementById('sumWastedTime');
  if (sumWastedRowEl && sumWastedEl) {
    if (scopedWastedS > 0) {
      const wSum = fmtWastedSummary(scopedTot);
      sumWastedRowEl.style.display = '';
      sumWastedEl.textContent = fmtDuration(scopedWastedS);
      sumWastedEl.title = 'Included in Model Time and ~Est. Total Delay: ' +
        fmtDuration(scopedWastedS) + fmtExactSecNote(scopedWastedS) +
        (wSum ? (' (' + wSum + ')') : '');
    } else {
      sumWastedRowEl.style.display = 'none';
    }
  }

  const sumToolEl = document.getElementById('sumToolTime');
  const sumToolTimeS = scopedTot.tool_time_s || 0;
  sumToolEl.textContent = fmtDuration(sumToolTimeS);
  sumToolEl.title = 'Cumulative tool execution time: ' + fmtDuration(sumToolTimeS) + fmtExactSecNote(sumToolTimeS);
  const stepCountLblEl = document.getElementById('stepCountLbl');
  if (stepCountLblEl) stepCountLblEl.textContent = isAllMode ? 'Total Steps' : 'Planner Steps';
  document.getElementById('stepCount').textContent = String(scopedStepsCount);

  const subs = m.subagents || { count: 0, items: [], totals: {} };
  const subCard = document.getElementById('subagentCard');
  if (!isAllMode && subs.count > 0) {
    subCard.classList.add('visible');
    const subVal = subs.valuation || {};
    const subCostNote = (subVal.coverage && subVal.coverage.priced_steps > 0) ? (' · ' + fmtUsd(subVal.cost_usd)) : '';
    document.getElementById('subSummary').textContent = subs.count + ' subagent(s) · ' + fmtCompact(subs.totals.total_tokens) + ' tok' + subCostNote;
    document.getElementById('grandTotalTok').textContent = fmtInt((m.grand_totals || {}).total_tokens || 0) + ' tok';
    const subChips = document.getElementById('subChips');
    subChips.innerHTML = '';
    subs.items.forEach(function (it) {
      const ch = document.createElement('button');
      ch.type = 'button';
      ch.className = 'chip chip-btn';
      const agentNote = it.agent_name && (!it.role || it.role.toLowerCase().indexOf(it.agent_name.toLowerCase()) === -1)
        ? (' · ' + it.agent_name)
        : '';
      const cleanSubModel = it.model_name ? String(it.model_name).replace(/^Subagent\s*·\s*/i, '') : '';
      const modelNote = cleanSubModel ? (' [' + cleanSubModel + ']') : '';
      ch.textContent = (it.role || 'Subagent') + agentNote + modelNote + ' (' + it.planner_steps_count + ' steps · ' + fmtCompact((it.input_tokens || 0) + (it.cache_read_tokens || 0) + (it.output_tokens || 0)) + ' tok)';
      const tipParts = ['Click to inspect subagent'];
      if (it.cost_usd != null) {
        tipParts.push('Paygo: ' + fmtUsd(it.cost_usd) + ' (' + fmtCredits(it.credits) + ')');
      }
      ch.title = tipParts.join(' · ');
      ch.addEventListener('click', function () {
        if (it.conversation_id) selectScope(it.conversation_id);
      });
      subChips.appendChild(ch);
    });
  } else {
    subCard.classList.remove('visible');
  }
  if (isAllMode) {
    subCard.style.display = 'none';
    return;
  }
  subCard.style.display = '';

  renderChart(allSteps, colorMap, byModel);

  const shownSteps = state.modelFilter
    ? allSteps.filter(function (s) { return s.model_name === state.modelFilter; })
    : allSteps;
  const orderedSteps = shownSteps.slice().reverse();
  const canFold = orderedSteps.length > RECENT_STEPS_LIMIT;
  const isFolded = canFold && !state.showAllSteps;
  const visibleSteps = isFolded ? orderedSteps.slice(0, RECENT_STEPS_LIMIT) : orderedSteps;
  const hiddenCount = orderedSteps.length - visibleSteps.length;

  const stepsMetaEl = document.getElementById('stepsMeta');
  if (state.modelFilter) {
    stepsMetaEl.textContent = isFolded
      ? ('Filtered: ' + shortModelName(state.modelFilter) + ' (latest ' + visibleSteps.length + ' of ' + orderedSteps.length + ') · Click row for details')
      : ('Filtered: ' + shortModelName(state.modelFilter) + ' (' + orderedSteps.length + ' / ' + allSteps.length + ' steps) · Click row for details');
  } else {
    stepsMetaEl.textContent = isFolded
      ? ('Latest ' + visibleSteps.length + ' of ' + orderedSteps.length + ' steps · Click row for details')
      : ('All Models · ' + orderedSteps.length + ' steps · Click row for details');
  }

  const toggleBtn = document.getElementById('stepsToggleBtn');
  if (!canFold) {
    toggleBtn.style.display = 'none';
  } else if (isFolded) {
    toggleBtn.style.display = '';
    toggleBtn.textContent = 'Show all (' + orderedSteps.length + ') ▾';
    toggleBtn.title = 'Expand table to view all ' + orderedSteps.length + ' steps (' + hiddenCount + ' older hidden)';
  } else {
    toggleBtn.style.display = '';
    toggleBtn.textContent = 'Recent (' + RECENT_STEPS_LIMIT + ') ▴';
    toggleBtn.title = 'Fold table back to the ' + RECENT_STEPS_LIMIT + ' most recent steps';
  }

  const tbody = document.getElementById('stepsTbody');
  tbody.innerHTML = '';
  if (orderedSteps.length === 0) {
    const noStepsTr = document.createElement('tr');
    noStepsTr.className = 'muted';
    noStepsTr.innerHTML = '<td colspan="7" class="muted">No steps recorded yet</td>';
    tbody.appendChild(noStepsTr);
  }

  const fitByModel = Object.create(null);
  byModel.forEach(function (bm) {
    if (bm && bm.model_name) fitByModel[bm.model_name] = bm.latency_fit || null;
  });

  visibleSteps.forEach(function (s) {
    const tr = document.createElement('tr');
    tr.className = 'step-row';
    const isCheckpoint = s.step_kind === 'checkpoint';
    let flagsHtml = '';
    if (s.cold_cache_spike) {
      const coldTip = 'Cold KV-cache prefill: >=25k uncached tokens with <20% cache hit (' + fmtInt(s.input_tokens) + ' uncached of ' + fmtInt(s.total_context) + ' total)';
      flagsHtml += ' <span class="step-flag" title="' + esc(coldTip) + '">❄</span>';
    }
    if (isCheckpoint) {
      flagsHtml += ' <span class="step-flag" title="System compaction call (CHECKPOINT): summarized history into <CONTEXT_SUMMARY>">⇄</span>';
    } else if (s.compacted) {
      flagsHtml += ' <span class="step-flag" title="Context compacted: resumed from <CONTEXT_SUMMARY> after CHECKPOINT compaction">⇄</span>';
    }
    if ((s.retry_wait_s || 0) > 0 || (s.retry_count || 0) > 0) {
      const rCount = s.retry_count || 1;
      const rTip = s.retry_wait_s > 0
        ? ('Retried (' + rCount + '): +' + fmtDuration(s.retry_wait_s) + fmtExactSecNote(s.retry_wait_s) + ' failed-attempt wait before winning step')
        : ('Retried after ' + rCount + ' failed attempt(s)');
      flagsHtml += ' <span class="step-flag" title="' + esc(rTip) + '">↻</span>';
    }

    const mCol = colorMap[s.model_name] || 'var(--m-muted)';
    const switchTag = s.model_switched
      ? '<span class="step-flag" title="Model switched from previous step">↱ </span>'
      : '';

    const stepTitle = 'Ordinal #' + s.ordinal + ', raw step_index ' + s.step_index +
      (s.created_at ? (', recorded at ' + s.created_at) : '') + '. Click row to expand details.';
    const modelTitle = s.model_name + (isCheckpoint ? ' (system compaction call)' : '');
    const ctxTitle = fmtInt(s.total_context) + ' total context tokens (' +
      fmtInt(s.cache_read_tokens) + ' cached read = ' + s.cache_hit_pct + '% hit, ' +
      fmtInt(s.input_tokens) + ' uncached prefill)';
    const outTitle = fmtInt(s.output_tokens) + ' output tokens (~' +
      fmtInt(s.est_thinking_tokens) + ' est. thinking, ~' +
      fmtInt(s.est_visible_tokens) + (isCheckpoint ? ' est. summary)' : ' est. visible + tools)');

    const retryS = Number(s.retry_wait_s || 0);
    const stepFit = fitByModel[s.model_name] || null;
    let durCellHtml;
    let durTitle;
    if (isCheckpoint) {
      durCellHtml = '—';
      durTitle = 'System compaction call (CHECKPOINT; untimed and excluded from roofline delay)';
    } else if (s.duration_s == null) {
      if (retryS > 0) {
        durCellHtml = '— <span class="muted">(+' + esc(fmtDuration(retryS)) + ')</span>';
        durTitle = 'Duration unavailable · +' + fmtDuration(retryS) + fmtExactSecNote(retryS) + ' retry wait · Tool exec: ' + s.tool_exec_s + 's';
      } else {
        durCellHtml = '—';
        durTitle = 'Duration unavailable (historical step without lifecycle hooks or tool-start timestamps)';
      }
    } else {
      const infS = Number(s.duration_s);
      const totalWaitS = infS + retryS;
      const retryPart = retryS > 0
        ? (' · +' + fmtDuration(retryS) + fmtExactSecNote(retryS) + ' retry wait')
        : '';
      const sParts = computeStepRooflineParts(s, stepFit);
      if (s.est_delay_s != null) {
        const roofDelayS = Number(s.est_delay_s);
        const stepDelayS = roofDelayS + retryS;
        const coldTag = s.cold_cache_spike ? ' ❄' : '';
        durCellHtml = esc(fmtDuration(totalWaitS)) + ' <span class="muted">(' + esc(fmtApproxDuration(stepDelayS)) + coldTag + ')</span>';
        let roofNote = '~' + roofDelayS.toFixed(2) + 's roofline delay';
        if (sParts) {
          roofNote = '~' + roofDelayS.toFixed(2) + 's roofline delay (vs ~' + sParts.expectedS.toFixed(2) + 's: ' + sParts.partsText + ')';
        }
        durTitle = 'Model inference: ' + infS + 's (' + srcLabel(s.duration_source) + ')' +
          retryPart + ' · ' + roofNote + ' · Tool exec: ' + s.tool_exec_s + 's';
      } else {
        let noDelayNote = 'speed roofline not yet available';
        if (s.cold_cache_spike) {
          noDelayNote = 'cold prefill (needs ≥6 steps ≥25k uncached to unlock prefill roofline)';
        } else if (!stepFit || stepFit.reason === 'min_steps') {
          noDelayNote = 'needs ≥4 warm steps for roofline (' + ((stepFit && stepFit.warm_count) || 0) + '/4)';
        } else if (stepFit.reason === 'min_spread') {
          noDelayNote = 'needs ≥200 tok output spread for roofline';
        } else if (stepFit.reason === 'unattributed') {
          noDelayNote = 'roofline disabled for unattributed model';
        }
        if (retryS > 0) {
          durCellHtml = esc(fmtDuration(totalWaitS)) + ' <span class="muted">(' + esc(fmtApproxDuration(retryS)) + '*' + (s.cold_cache_spike ? ' ❄' : '') + ')</span>';
        } else if (s.cold_cache_spike) {
          durCellHtml = esc(fmtDuration(infS)) + ' <span class="muted">(❄)</span>';
        } else {
          durCellHtml = esc(fmtDuration(infS));
        }
        durTitle = 'Model inference: ' + infS + 's (' + srcLabel(s.duration_source) + ')' +
          retryPart + ' · ' + noDelayNote + ' · Tool exec: ' + s.tool_exec_s + 's';
      }
    }

    let paygoCellHtml;
    let paygoTitle;
    if (s.pricing_status === 'priced' && s.cost_usd != null) {
      paygoCellHtml = esc(fmtUsd(s.cost_usd));
      const futStepNote = (s.future_cost_usd != null && Math.abs(s.future_cost_usd - s.cost_usd) > 1e-9)
        ? (' · Scheduled std.: ' + fmtUsd(s.future_cost_usd))
        : '';
      paygoTitle = 'Step Paygo (current rates): ' + fmtUsd(s.cost_usd) + ' (' + fmtCredits(s.credits) + ')' + futStepNote;
    } else if (s.pricing_status === 'unattributed') {
      paygoCellHtml = '—';
      paygoTitle = 'Unattributed ("unknown" / "auto") step — excluded from Paygo';
    } else {
      paygoCellHtml = '—';
      paygoTitle = 'Unpriced model (' + (s.model_name || 'unknown') + ') — click "⚙ Rates" to set a rate';
    }

    const toolsList = s.tool_names || [];
    let toolsCellHtml;
    let toolsTitle;
    if (isCheckpoint) {
      toolsTitle = 'System context compaction call (CHECKPOINT): summarized history into <CONTEXT_SUMMARY>';
      toolsCellHtml = '<span class="pill compact" title="' + esc(toolsTitle) + '">⇄ compaction</span>';
    } else if (toolsList.length > 0) {
      toolsTitle = toolsList.length + ' tool call(s): ' + toolsList.join(', ') + ' (tool exec: ' + s.tool_exec_s + 's)';
      toolsCellHtml = esc(toolsList.join(', '));
    } else {
      toolsTitle = 'Direct model response (no tool calls invoked)';
      toolsCellHtml = 'reply';
    }

    tr.innerHTML =
      '<td class="mono" title="' + esc(stepTitle) + '">#' + esc(s.ordinal) + flagsHtml + '</td>' +
      '<td title="' + esc(modelTitle) + '"><span class="model-badge" title="' + esc(modelTitle) + '"><span class="model-dot" style="background:' + esc(mCol) + '"></span>' + switchTag + '<span>' + esc(shortModelName(s.model_name)) + '</span></span></td>' +
      '<td class="mono" title="' + esc(ctxTitle) + '">' + esc(fmtCompact(s.total_context)) + ' <span class="muted">(' + esc(s.cache_hit_pct) + '%)</span></td>' +
      '<td class="mono" title="' + esc(outTitle) + '">' + esc(fmtInt(s.output_tokens)) + ' <span class="muted">(~' + esc(fmtInt(s.est_thinking_tokens)) + ')</span></td>' +
      '<td class="mono" title="' + esc(durTitle) + '">' + durCellHtml + '</td>' +
      '<td class="mono" title="' + esc(paygoTitle) + '">' + paygoCellHtml + '</td>' +
      '<td class="muted tools-cell" title="' + esc(toolsTitle) + '">' + toolsCellHtml + '</td>';

    tr.addEventListener('click', function () {
      state.expandedStep = (state.expandedStep === s.step_index) ? null : s.step_index;
      renderMetrics(state.lastPayload);
    });
    tbody.appendChild(tr);

    if (state.expandedStep === s.step_index) {
      const dtr = document.createElement('tr');
      dtr.className = 'detail-row';
      const detailParts = (s.duration_s != null) ? computeStepRooflineParts(s, stepFit) : null;
      dtr.innerHTML =
        '<td colspan="7">' +
        '<b>Step ' + esc(s.step_index) + '</b>' +
        (isCheckpoint ? ' · Kind: <span class="mono">Compaction (CHECKPOINT)</span>' : '') +
        ' · Model: <span class="mono">' + esc(s.model_name) + '</span>' +
        (s.model_switched ? ' <span class="pill info">Model Switch</span>' : '') +
        (s.cold_cache_spike ? ' <span class="pill warn">❄ Cold Prefill</span>' : '') + ' · ' +
        (s.cost_usd != null ? ('Paygo: <span class="mono">' + esc(fmtUsd(s.cost_usd)) + ' (' + esc(fmtCredits(s.credits)) + ')</span> · ') : '') +
        (s.duration_s != null ? ('Inference: <span class="mono">' + esc(s.duration_s) + 's</span> · ') : '') +
        (detailParts ? ('Roofline: <span class="mono">~' + esc(detailParts.expectedS.toFixed(2)) + 's (' + esc(detailParts.partsText) + ')</span> · ') : '') +
        (s.est_delay_s != null ? ('Est. delay: <span class="mono">~' + esc(Number(s.est_delay_s).toFixed(2)) + 's</span> · ') : '') +
        'Uncached: <span class="mono">' + esc(fmtInt(s.input_tokens)) + '</span> · ' +
        'Cached: <span class="mono">' + esc(fmtInt(s.cache_read_tokens)) + '</span> · ' +
        (isCheckpoint ? 'Summary est: ' : 'Visible est: ') + '<span class="mono">' + esc(fmtInt(s.est_visible_tokens)) + '</span> · ' +
        (s.retry_wait_s > 0 ? ('Retry wait: <span class="mono">' + esc(fmtDuration(s.retry_wait_s)) + '</span> · ') : '') +
        'Tool exec: <span class="mono">' + esc(s.tool_exec_s) + 's</span> · ' +
        'Source: <span class="mono">' + esc(srcLabel(s.duration_source)) + '</span> · ' +
        'At: <span class="mono">' + esc(s.created_at) + '</span>' +
        '</td>';
      tbody.appendChild(dtr);
    }
  });

  if (canFold) {
    const foldTr = document.createElement('tr');
    foldTr.className = 'steps-fold-row';
    const foldTd = document.createElement('td');
    foldTd.setAttribute('colspan', '7');
    foldTd.textContent = isFolded
      ? ('Show ' + hiddenCount + ' older step' + (hiddenCount === 1 ? '' : 's') + ' (' + orderedSteps.length + ' total) ▾')
      : ('Fold to ' + RECENT_STEPS_LIMIT + ' most recent steps ▴');
    foldTr.appendChild(foldTd);
    foldTr.addEventListener('click', function () {
      state.showAllSteps = !state.showAllSteps;
      renderMetrics(state.lastPayload);
    });
    tbody.appendChild(foldTr);
  }
}

function abortPendingFetch() {
  if (state.fetchAbort) {
    try { state.fetchAbort.abort(); } catch (e) {}
    state.fetchAbort = null;
  }
}

function applyCombined(data) {
  if (!data) return false;
  if (data.sub_id) state.sseSubId = data.sub_id;
  if (data.etag) state.lastEtag = data.etag;
  if (data.unchanged) {
    state.lastSnapshotAtMs = Date.now();
    return true;
  }
  if (data.conversations) renderScopeSelector(data.conversations, selectScope);
  let matched = true;
  if (data.metrics) {
    const m = data.metrics;
    const mMode = (m.mode === 'all' || m.mode === 'project') ? m.mode : 'conversation';
    if (mMode !== state.viewMode) {
      matched = false;
    } else if (isAggregateMode(mMode) && m.window && m.window !== state.timeWindow) {
      matched = false;
    } else if (
      mMode === 'project' &&
      state.selectedProjectScopeId &&
      m.conversation_id &&
      m.conversation_id !== state.selectedProjectScopeId
    ) {
      matched = false;
    } else if (
      mMode === 'conversation' &&
      !state.followActive &&
      state.selectedConvId &&
      m.conversation_id &&
      m.conversation_id !== state.selectedConvId
    ) {
      matched = false;
    } else {
      state.lastPayload = m;
      state.lastSnapshotAtMs = Date.now();
      renderMetrics(m);
    }
  }
  return matched;
}

function stopPolling() {
  if (state.pollTimer) {
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
  }
}

function clearInitFallback() {
  if (state.initFallbackTimer) {
    clearTimeout(state.initFallbackTimer);
    state.initFallbackTimer = null;
  }
}

function schedulePoll(customDelayMs) {
  stopPolling();
  if (typeof document !== 'undefined' && document.hidden) return;
  const delay = customDelayMs != null
    ? customDelayMs
    : (state.status === 'generating' ? POLL_GENERATING_MS : POLL_IDLE_MS);
  state.pollTimer = setTimeout(function () {
    state.pollTimer = null;
    pollSnapshot();
  }, delay);
}

function fetchOnce(convId, win, useEtag) {
  const reqConv = convId !== undefined ? convId : currentRequestedConvId();
  const reqWin = win !== undefined ? win : state.timeWindow;
  const expectedMode = reqConv === ALL_CONVERSATIONS_ID
    ? 'all'
    : (isProjectScopeId(reqConv) ? 'project' : 'conversation');
  const subId = (state.es && state.es.readyState === 1 && state.sseSubId) ? state.sseSubId : null;
  abortPendingFetch();
  stopPolling();
  const ctrl = typeof AbortController === 'function' ? new AbortController() : null;
  state.fetchAbort = ctrl;
  const etagArg = useEtag ? state.lastEtag : null;
  const url = buildUrl('/api/snapshot', reqConv, reqWin, subId, etagArg);
  return sidecarFetch(url, ctrl ? { signal: ctrl.signal } : undefined)
    .then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function (data) {
      if (ctrl && state.fetchAbort === ctrl) state.fetchAbort = null;
      if (state.viewMode !== expectedMode) return;
      if (isAggregateMode(expectedMode) && state.timeWindow !== reqWin) return;
      if (
        expectedMode === 'project' &&
        state.selectedProjectScopeId &&
        reqConv !== state.selectedProjectScopeId
      ) {
        return;
      }
      if (
        expectedMode === 'conversation' &&
        !state.followActive &&
        state.selectedConvId &&
        reqConv !== state.selectedConvId
      ) {
        return;
      }
      state.healthFailCount = 0;
      markSseHealthy();
      applyCombined(data);
      if (subId && data && data.retargeted === false && !state.sseDisabled && !state.sseRetryTimer) {
        state.sseSubId = null;
        connectSSE();
      }
      if (state.sseDisabled || !state.es || state.es.readyState !== 1 || !state.sseSubId) {
        schedulePoll();
      }
    })
    .catch(function (err) {
      if (ctrl && state.fetchAbort === ctrl) state.fetchAbort = null;
      if (err && err.name === 'AbortError') return;
      state.healthFailCount += 1;
      if (!state.es || state.es.readyState !== 1) {
        showSseReconnecting();
      }
      if (state.sseDisabled || !state.es || state.es.readyState !== 1 || !state.sseSubId) {
        const errDelay = Math.min(5000, 1000 * Math.pow(2, Math.min(state.healthFailCount, 3)));
        schedulePoll(errDelay);
      }
    });
}

function pollSnapshot() {
  if (state.es && state.es.readyState === 1 && state.sseSubId) {
    stopPolling();
    return;
  }
  fetchOnce(undefined, undefined, true);
}

function switchTarget(convId, win) {
  state.lastEtag = null;
  if (
    (state.es && state.es.readyState === 1 && state.sseSubId) ||
    state.sseDisabled ||
    !canUseEventSource() ||
    state.pollTimer !== null
  ) {
    fetchOnce(convId, win, false);
  } else {
    abortPendingFetch();
    connectSSE();
  }
}

function clearSseTimers() {
  clearInitFallback();
  if (state.sseRetryTimer) {
    clearTimeout(state.sseRetryTimer);
    state.sseRetryTimer = null;
  }
  if (state.sseWatchdogTimer) {
    clearTimeout(state.sseWatchdogTimer);
    state.sseWatchdogTimer = null;
  }
}

function markSseHealthy() {
  state.sseRetryCount = 0;
  state.healthFailCount = 0;
  state.lastSseActivityMs = Date.now();
  const pill = document.getElementById('ssePill');
  const dot = document.getElementById('sseDot');
  const lbl = document.getElementById('sseLabel');
  if (dot) dot.className = 'dot live';
  if (lbl) lbl.textContent = 'Live';
  if (pill) pill.style.display = 'none';
}

function showSseReconnecting() {
  const pill = document.getElementById('ssePill');
  const dot = document.getElementById('sseDot');
  const lbl = document.getElementById('sseLabel');
  if (dot) dot.className = 'dot err';
  if (lbl) {
    lbl.textContent = state.healthFailCount >= 4
      ? 'Server unreachable — reopen panel if restarted'
      : 'Reconnecting';
  }
  if (pill) pill.style.display = '';
}

function scheduleSseRetry() {
  const delayMs = Math.min(5000, 1000 * Math.pow(2, Math.min(state.sseRetryCount, 3)));
  state.sseRetryCount += 1;
  state.sseRetryTimer = setTimeout(function () {
    state.sseRetryTimer = null;
    connectSSE();
  }, delayMs);
}

function handleSseDisconnect(es) {
  if (state.es !== es) return;
  clearSseTimers();
  try { es.close(); } catch (e) {}
  state.es = null;
  state.sseSubId = null;
  state.sseFailCount += 1;
  if (state.sseFailCount >= 2 && state.lastSnapshotAtMs > 0) {
    state.sseDisabled = true;
    fetchOnce(undefined, undefined, true);
    return;
  }
  if (state.lastSnapshotAtMs === 0) {
    showSseReconnecting();
  }
  fetchOnce(undefined, undefined, Boolean(state.lastEtag));
  scheduleSseRetry();
}

function armSseWatchdog(es) {
  if (state.sseWatchdogTimer) {
    clearTimeout(state.sseWatchdogTimer);
    state.sseWatchdogTimer = null;
  }
  if (state.es !== es) return;
  state.sseWatchdogTimer = setTimeout(function () {
    state.sseWatchdogTimer = null;
    handleSseDisconnect(es);
  }, SSE_WATCHDOG_MS);
}

function connectSSE() {
  clearSseTimers();
  state.sseSubId = null;
  if (state.es) {
    try { state.es.close(); } catch (e) {}
    state.es = null;
  }
  if (state.sseDisabled || !canUseEventSource()) {
    state.sseDisabled = true;
    fetchOnce(undefined, undefined, false);
    return;
  }
  const targetConv = currentRequestedConvId();
  const targetWin = state.timeWindow;
  let es;
  try {
    es = new EventSource(buildUrl('/events', targetConv, targetWin, null, null, true));
  } catch (e) {
    state.es = null;
    state.sseFailCount += 1;
    if (state.sseFailCount >= 2 && state.lastSnapshotAtMs > 0) {
      state.sseDisabled = true;
      fetchOnce(undefined, undefined, true);
      return;
    }
    showSseReconnecting();
    fetchOnce(undefined, undefined, Boolean(state.lastEtag));
    scheduleSseRetry();
    return;
  }
  state.es = es;
  armSseWatchdog(es);
  state.initFallbackTimer = setTimeout(function () {
    state.initFallbackTimer = null;
    if (state.es === es && !state.sseSubId) {
      fetchOnce(undefined, undefined, false);
    }
  }, INIT_SNAPSHOT_FALLBACK_MS);

  function onData(ev) {
    if (state.es !== es) return;
    state.sseFailCount = 0;
    clearInitFallback();
    stopPolling();
    markSseHealthy();
    armSseWatchdog(es);
    try {
      const parsed = JSON.parse(ev.data);
      const matched = applyCombined(parsed);
      if (ev.type === 'snapshot' && !matched && state.sseSubId && es.readyState === 1) {
        fetchOnce(undefined, undefined, false);
      }
    } catch (e) {}
  }

  es.onopen = function () {
    if (state.es !== es) return;
    markSseHealthy();
    armSseWatchdog(es);
  };
  es.addEventListener('snapshot', onData);
  es.addEventListener('update', onData);
  es.addEventListener('ping', function () {
    if (state.es !== es) return;
    state.sseFailCount = 0;
    clearInitFallback();
    stopPolling();
    markSseHealthy();
    armSseWatchdog(es);
    if (
      !document.hidden &&
      isAggregateMode(state.viewMode) &&
      (state.timeWindow === '5h' || state.timeWindow === '24h') &&
      (Date.now() - (state.lastSnapshotAtMs || 0)) >= 60000
    ) {
      fetchOnce(undefined, undefined, false);
    }
  });
  es.onerror = function () {
    handleSseDisconnect(es);
  };
}

function ensureSseAlive() {
  if (state.sseDisabled || !canUseEventSource()) {
    fetchOnce(undefined, undefined, Boolean(state.lastEtag));
    return;
  }
  const stale = state.lastSseActivityMs > 0 && (Date.now() - state.lastSseActivityMs) > 20000;
  if (!state.es || state.es.readyState !== 1 || state.sseRetryTimer !== null || stale) {
    state.sseRetryCount = 0;
    connectSSE();
  }
}

function initDashboard() {
  initThemeControls(function () {
    state.lastScatterKey = null;
    state.lastHistoryKey = null;
  });

  initPricingModal(function () {
    state.lastEtag = null;
    fetchOnce(undefined, undefined, false);
  });

  document.getElementById('ssePill').addEventListener('click', function () {
    stopPolling();
    if (!hasSidecarFetch()) {
      state.sseDisabled = false;
      state.sseFailCount = 0;
    }
    state.sseRetryCount = 0;
    state.healthFailCount = 0;
    connectSSE();
  });

  window.addEventListener('online', function () {
    ensureSseAlive();
  });

  document.addEventListener('visibilitychange', function () {
    syncGeneratingTimer();
    if (document.hidden) {
      stopPolling();
    } else {
      ensureSseAlive();
    }
  });

  initScopeSelector(selectScope);

  const parentConvBtn = document.getElementById('parentConvBtn');
  if (parentConvBtn) {
    parentConvBtn.addEventListener('click', function () {
      const pid = parentConvBtn.getAttribute('data-parent-id');
      if (pid) selectScope(pid);
    });
  }

  document.querySelectorAll('.filter-pill[data-window]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      const win = btn.getAttribute('data-window') || 'all';
      if (isAggregateMode(state.viewMode) && state.timeWindow === win) return;
      if (!isAggregateMode(state.viewMode)) {
        state.viewMode = 'all';
      }
      state.timeWindow = win;
      state.lastScatterKey = null;
      syncModeControls();
      switchTarget();
    });
  });

  document.getElementById('stepsToggleBtn').addEventListener('click', function () {
    state.showAllSteps = !state.showAllSteps;
    if (state.lastPayload) renderMetrics(state.lastPayload);
  });

  document.getElementById('coldToggleBtn').addEventListener('click', function () {
    state.showColdPrefills = !state.showColdPrefills;
    state.lastScatterKey = null;
    if (state.lastPayload) renderMetrics(state.lastPayload);
  });

  document.querySelectorAll('#scatterModeToggle [data-scatter-mode]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      const mode = btn.getAttribute('data-scatter-mode') || 'decode';
      if (state.scatterMode === mode) return;
      state.scatterMode = mode;
      state.lastScatterKey = null;
      if (state.lastPayload) renderMetrics(state.lastPayload);
    });
  });

  document.getElementById('scatterCloseBtn').addEventListener('click', function () {
    clearModelFilter();
  });

  document.getElementById('byModelScope').addEventListener('click', function () {
    if (state.modelFilter === null) return;
    clearModelFilter();
  });

  document.querySelectorAll('#valPoolToggle [data-pool]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      if (btn.disabled) return;
      const pool = btn.getAttribute('data-pool') || 'all';
      state.valPool = pool;
      if (state.modelFilter !== null) {
        state.modelFilter = null;
        state.lastScatterKey = null;
      }
      if (state.lastPayload) renderMetrics(state.lastPayload);
    });
  });

  document.getElementById('valModelOverridePill').addEventListener('click', function () {
    if (state.modelFilter === null) return;
    clearModelFilter();
  });

  document.getElementById('followToggle').addEventListener('change', function (e) {
    state.viewMode = 'conversation';
    state.selectedProjectScopeId = null;
    state.followActive = Boolean(e.target.checked);
    if (state.followActive) {
      state.selectedConvId = null;
      state.modelFilter = null;
      state.expandedStep = null;
      state.showAllSteps = false;
      state.showColdPrefills = false;
      state.lastScatterKey = null;
      if (state.convsDataCache) renderScopeSelector(state.convsDataCache, selectScope);
      syncModeControls();
      switchTarget(null);
    } else {
      if (!state.selectedConvId && state.activeConvId) {
        state.selectedConvId = state.activeConvId;
      }
      if (state.convsDataCache) renderScopeSelector(state.convsDataCache, selectScope);
      syncModeControls();
      if (!(state.es && state.es.readyState === 1 && state.sseSubId) && !(state.sseDisabled && state.pollTimer !== null)) {
        connectSSE();
      }
    }
  });

  syncModeControls();
  syncGeneratingTimer();
  connectSSE();
}

initDashboard();
