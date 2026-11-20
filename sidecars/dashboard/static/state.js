// Shared dashboard client state, URL helpers, and demand-driven generation timer.

import { fmtDuration } from './format.js';

export const ALL_CONVERSATIONS_ID = '__all__';
export const PROJECT_SCOPE_PREFIX = 'project:';
export const UNASSIGNED_PROJECT_ID = '__unassigned__';
export const RECENT_STEPS_LIMIT = 5;
export const HISTORY_AREA_THRESHOLD = 60;
export const SCATTER_MAX_STEPS = 100;

export function isProjectScopeId(id) {
  return Boolean(typeof id === 'string' && id.indexOf(PROJECT_SCOPE_PREFIX) === 0);
}

export function isAggregateScopeId(id) {
  return id === ALL_CONVERSATIONS_ID || isProjectScopeId(id);
}

export function isAggregateMode(mode) {
  return mode === 'all' || mode === 'project';
}

const initParams = new URLSearchParams(window.location.search);
const initConvParam = initParams.get('conversation_id');
const initWinParam = initParams.get('window');

export const state = {
  viewMode: initConvParam === ALL_CONVERSATIONS_ID
    ? 'all'
    : (isProjectScopeId(initConvParam) ? 'project' : 'conversation'),
  timeWindow: (initWinParam === '5h' || initWinParam === '24h' || initWinParam === '7d' || initWinParam === '30d' || initWinParam === 'all') ? initWinParam : 'all',
  valPool: 'all',
  followActive: !initConvParam,
  selectedConvId: (initConvParam && !isAggregateScopeId(initConvParam)) ? initConvParam : null,
  selectedProjectScopeId: isProjectScopeId(initConvParam) ? initConvParam : null,
  activeConvId: null,
  renderedConvId: null,
  renderedViewKey: null,
  generatingSinceMs: null,
  status: 'idle',
  expandedStep: null,
  showAllSteps: false,
  showColdPrefills: false,
  scatterMode: 'decode',
  lastScatterKey: null,
  lastHistoryKey: null,
  lastStepsSig: null,
  scrollToStepIndex: null,
  modelFilter: null,
  pendingJump: null,
  es: null,
  sseSubId: null,
  fetchAbort: null,
  lastEtag: null,
  lastSseActivityMs: 0,
  lastSnapshotAtMs: 0,
  healthFailCount: 0,
  sseFailCount: 0,
  sseDisabled: false,
  sseRetryTimer: null,
  sseWatchdogTimer: null,
  pollTimer: null,
  initFallbackTimer: null,
  sseRetryCount: 0,
  lastPayload: null,
  ratesOpen: false,
  pricingConfig: null,
  scopeMenuOpen: false,
  scopeFilterQuery: '',
  collapsedProjects: new Set(),
  convsDataCache: null,
  lastScopeTreeSig: null
};

let genTimerId = null;

export function currentRequestedConvId() {
  if (state.viewMode === 'all') return ALL_CONVERSATIONS_ID;
  if (state.viewMode === 'project') return state.selectedProjectScopeId || ALL_CONVERSATIONS_ID;
  return state.followActive ? null : state.selectedConvId;
}

export function syncModeControls() {
  const isAgg = isAggregateMode(state.viewMode);
  const allCtrl = document.getElementById('allControls');
  const followEl = document.getElementById('followToggle');
  if (allCtrl) allCtrl.classList.toggle('hidden', !isAgg);
  if (followEl) followEl.checked = Boolean(state.followActive);
  ['ctxCard', 'historyCard', 'stepsCard', 'subagentCard'].forEach(function (id) {
    const el = document.getElementById(id);
    if (el && isAgg) el.style.display = 'none';
  });
  const winBtns = document.querySelectorAll('.filter-pill[data-window]');
  winBtns.forEach(function (btn) {
    const active = btn.getAttribute('data-window') === state.timeWindow;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
}

export function hasSidecarFetch() {
  return Boolean(typeof window !== 'undefined' && window.sidecar && typeof window.sidecar.fetch === 'function');
}

export function canUseEventSource() {
  if (hasSidecarFetch()) return false;
  if (typeof EventSource !== 'function') return false;
  const proto = (typeof window !== 'undefined' && window.location) ? (window.location.protocol || '') : '';
  if (proto === 'about:' || proto === 'blob:' || proto === 'data:') return false;
  return true;
}

export function sidecarFetch(url, init) {
  const f = hasSidecarFetch()
    ? window.sidecar.fetch.bind(window.sidecar)
    : fetch;
  return f(url, init);
}

export function buildUrl(path, convId, win, subId, etag, forBrowserDirect) {
  const rawSearch = (typeof window !== 'undefined' && window.location && window.location.search) || '';
  const params = new URLSearchParams(rawSearch);
  if (convId) {
    params.set('conversation_id', convId);
  } else {
    params.delete('conversation_id');
  }
  if (isAggregateScopeId(convId)) {
    params.set('window', win || state.timeWindow || 'all');
  } else {
    params.delete('window');
  }
  if (subId) {
    params.set('sub_id', subId);
  } else {
    params.delete('sub_id');
  }
  if (etag) {
    params.set('etag', etag);
  } else {
    params.delete('etag');
  }
  let basePath = '';
  const loc = (typeof window !== 'undefined' && window.location) ? window.location : null;
  const proto = loc ? (loc.protocol || '') : '';
  const isHttpLikeProto = (
    proto === 'http:' ||
    proto === 'https:' ||
    proto === 'sidecar:' ||
    proto === 'vscode-webview:' ||
    proto === 'app:'
  );
  if ((forBrowserDirect || !hasSidecarFetch()) && isHttpLikeProto) {
    const rawLocPath = loc.pathname || '';
    if (rawLocPath && rawLocPath !== '/') {
      basePath = rawLocPath.replace(/\/index\.html$/, '').replace(/\/+$/, '');
    }
  }
  const rel = path.charAt(0) === '/' ? path : ('/' + path);
  const fullPath = basePath ? (basePath + rel) : rel;
  const qs = params.toString();
  return qs ? (fullPath + '?' + qs) : fullPath;
}

export function updateGeneratingTimer() {
  const pill = document.getElementById('genPill');
  const dot = document.getElementById('genDot');
  const lbl = document.getElementById('genLabel');
  if (!pill || !dot || !lbl) return;
  if (state.status === 'generating') {
    pill.classList.add('generating');
    if (state.healthFailCount > 0) {
      dot.className = 'dot err';
      lbl.textContent = 'Generating (stale)';
      pill.title = 'Connection interrupted — generation timer paused until reconnected';
      return;
    }
    pill.title = '';
    dot.className = 'dot gen';
    const sec = state.generatingSinceMs ? Math.max(0, (Date.now() - state.generatingSinceMs) / 1000) : 0;
    lbl.textContent = 'Generating ' + (sec >= 60 ? fmtDuration(sec) : (sec.toFixed(1) + 's'));
  } else {
    pill.classList.remove('generating');
    pill.title = '';
    dot.className = 'dot';
    lbl.textContent = 'Idle';
  }
}

export function syncGeneratingTimer() {
  updateGeneratingTimer();
  const isHidden = typeof document !== 'undefined' && Boolean(document.hidden);
  const shouldRun = state.status === 'generating' && !isHidden && state.healthFailCount === 0;
  if (shouldRun) {
    if (genTimerId === null) {
      genTimerId = setInterval(updateGeneratingTimer, 100);
    }
  } else if (genTimerId !== null) {
    clearInterval(genTimerId);
    genTimerId = null;
  }
}
