// Shared dashboard client state, URL helpers, and demand-driven generation timer.

export const ALL_CONVERSATIONS_ID = '__all__';
export const RECENT_STEPS_LIMIT = 5;
export const HISTORY_AREA_THRESHOLD = 60;
export const SCATTER_MAX_STEPS = 100;

const initParams = new URLSearchParams(window.location.search);
const initConvParam = initParams.get('conversation_id');
const initWinParam = initParams.get('window');

export const state = {
  viewMode: initConvParam === ALL_CONVERSATIONS_ID ? 'all' : 'conversation',
  timeWindow: (initWinParam === '5h' || initWinParam === '24h' || initWinParam === '7d' || initWinParam === '30d' || initWinParam === 'all') ? initWinParam : 'all',
  valPool: 'all',
  followActive: !(initConvParam && initConvParam !== ALL_CONVERSATIONS_ID),
  selectedConvId: (initConvParam && initConvParam !== ALL_CONVERSATIONS_ID) ? initConvParam : null,
  activeConvId: null,
  renderedConvId: null,
  renderedViewKey: null,
  generatingSinceMs: null,
  status: 'idle',
  expandedStep: null,
  showAllSteps: false,
  showColdPrefills: false,
  lastScatterKey: null,
  lastHistoryKey: null,
  modelFilter: null,
  pendingJump: null,
  es: null,
  sseSubId: null,
  fetchAbort: null,
  lastSseActivityMs: 0,
  lastSnapshotAtMs: 0,
  healthFailCount: 0,
  sseRetryTimer: null,
  sseWatchdogTimer: null,
  sseRetryCount: 0,
  lastPayload: null,
  ratesOpen: false,
  pricingConfig: null
};

let genTimerId = null;

export function currentRequestedConvId() {
  if (state.viewMode === 'all') return ALL_CONVERSATIONS_ID;
  return state.followActive ? null : state.selectedConvId;
}

export function syncModeControls() {
  const isAll = state.viewMode === 'all';
  const convBtn = document.getElementById('modeConvBtn');
  const allBtn = document.getElementById('modeAllBtn');
  const convCtrl = document.getElementById('convControls');
  const allCtrl = document.getElementById('allControls');
  if (convBtn) {
    convBtn.classList.toggle('active', !isAll);
    convBtn.setAttribute('aria-pressed', !isAll ? 'true' : 'false');
  }
  if (allBtn) {
    allBtn.classList.toggle('active', isAll);
    allBtn.setAttribute('aria-pressed', isAll ? 'true' : 'false');
  }
  if (convCtrl) convCtrl.classList.toggle('hidden', isAll);
  if (allCtrl) allCtrl.classList.toggle('hidden', !isAll);
  const winBtns = document.querySelectorAll('.filter-pill[data-window]');
  winBtns.forEach(function (btn) {
    const active = btn.getAttribute('data-window') === state.timeWindow;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
}

export function sidecarFetch(url, init) {
  const f = (typeof window !== 'undefined' && window.sidecar && typeof window.sidecar.fetch === 'function')
    ? window.sidecar.fetch.bind(window.sidecar)
    : fetch;
  return f(url, init);
}

export function buildUrl(path, convId, win, subId) {
  const params = new URLSearchParams(window.location.search);
  if (convId) {
    params.set('conversation_id', convId);
  } else {
    params.delete('conversation_id');
  }
  if (convId === ALL_CONVERSATIONS_ID) {
    params.set('window', win || state.timeWindow || 'all');
  } else {
    params.delete('window');
  }
  if (subId) {
    params.set('sub_id', subId);
  } else {
    params.delete('sub_id');
  }
  let basePath = '';
  const rawLocPath = (typeof window !== 'undefined' && window.location && window.location.protocol !== 'about:')
    ? (window.location.pathname || '')
    : '';
  if (rawLocPath && rawLocPath !== '/') {
    basePath = rawLocPath.replace(/\/index\.html$/, '').replace(/\/+$/, '');
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
    dot.className = 'dot gen';
    const sec = state.generatingSinceMs ? Math.max(0, (Date.now() - state.generatingSinceMs) / 1000) : 0;
    lbl.textContent = 'Generating ' + sec.toFixed(1) + 's';
  } else {
    pill.classList.remove('generating');
    dot.className = 'dot';
    lbl.textContent = 'Idle';
  }
}

export function syncGeneratingTimer() {
  updateGeneratingTimer();
  const isHidden = typeof document !== 'undefined' && Boolean(document.hidden);
  const shouldRun = state.status === 'generating' && !isHidden;
  if (shouldRun) {
    if (genTimerId === null) {
      genTimerId = setInterval(updateGeneratingTimer, 100);
    }
  } else if (genTimerId !== null) {
    clearInterval(genTimerId);
    genTimerId = null;
  }
}
