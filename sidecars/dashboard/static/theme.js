// Host theme detection, contrast adaptation, and Auto/Dark/Light cycling.

export const THEME_STORAGE_KEY = 'agy_metrics.theme';
const VALID_TOKEN_NAME = /^--[a-z0-9-]{1,40}$/;

let hostPostScheme = null;
let colorProbeCtx = null;
let colorProbeReady = false;
let onThemeChangedCb = null;

export function lsGet(k) {
  try { return localStorage.getItem(k); } catch (e) { return null; }
}

export function lsSet(k, v) {
  try {
    if (v == null) localStorage.removeItem(k);
    else localStorage.setItem(k, v);
  } catch (e) {}
}

export function getSavedThemeMode() {
  const saved = lsGet(THEME_STORAGE_KEY);
  return (saved === 'dark' || saved === 'light' || saved === 'auto') ? saved : 'auto';
}

function getColorProbeCtx() {
  if (!colorProbeReady) {
    colorProbeReady = true;
    try {
      const canvas = document.createElement('canvas');
      canvas.width = 1;
      canvas.height = 1;
      colorProbeCtx = canvas.getContext('2d', { willReadFrequently: true });
    } catch (e) {
      colorProbeCtx = null;
    }
  }
  return colorProbeCtx;
}

function parseFallbackRgb(computed) {
  if (!computed || computed === 'transparent') return { r: 0, g: 0, b: 0, a: 0, css: 'transparent' };
  const nums = (computed.match(/[-+]?[\d.]+/g) || []).map(Number);
  if (nums.length < 3) return null;
  const scale = computed.indexOf('color(') === 0 ? 1 : 255;
  const alpha = nums.length >= 4 ? nums[3] : 1;
  return {
    r: Math.max(0, Math.min(255, Math.round(nums[0] * (255 / scale)))),
    g: Math.max(0, Math.min(255, Math.round(nums[1] * (255 / scale)))),
    b: Math.max(0, Math.min(255, Math.round(nums[2] * (255 / scale)))),
    a: Math.max(0, Math.min(1, alpha)),
    css: computed
  };
}

function resolveCssVarColor(varName) {
  const root = document.documentElement;
  const raw = getComputedStyle(root).getPropertyValue(varName).trim();
  if (!raw) return null;
  const probe = document.createElement('span');
  probe.style.cssText = 'position:absolute;visibility:hidden;pointer-events:none;background-color:rgba(0,0,0,0)';
  probe.style.backgroundColor = 'var(' + varName + ')';
  (document.body || root).appendChild(probe);
  const computed = getComputedStyle(probe).backgroundColor;
  probe.remove();
  if (!computed || computed === 'transparent' || computed === 'rgba(0, 0, 0, 0)') {
    return { r: 0, g: 0, b: 0, a: 0, css: computed || 'transparent' };
  }
  const ctx = getColorProbeCtx();
  if (!ctx) return parseFallbackRgb(computed);
  ctx.clearRect(0, 0, 1, 1);
  ctx.fillStyle = 'rgba(0, 0, 0, 0)';
  ctx.fillStyle = computed;
  ctx.fillRect(0, 0, 1, 1);
  const d = ctx.getImageData(0, 0, 1, 1).data;
  return { r: d[0], g: d[1], b: d[2], a: d[3] / 255, css: computed };
}

function gammaLuma(rgb) {
  return (0.2126 * rgb.r + 0.7152 * rgb.g + 0.0722 * rgb.b) / 255;
}

function relLuminance(rgb) {
  const ch = [rgb.r / 255, rgb.g / 255, rgb.b / 255];
  for (let i = 0; i < 3; i++) {
    ch[i] = ch[i] <= 0.03928 ? ch[i] / 12.92 : Math.pow((ch[i] + 0.055) / 1.055, 2.4);
  }
  return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2];
}

export function wcagContrast(c1, c2) {
  const l1 = relLuminance(c1);
  const l2 = relLuminance(c2);
  const hi = Math.max(l1, l2);
  const lo = Math.min(l1, l2);
  return (hi + 0.05) / (lo + 0.05);
}

function setBodyVar(body, name, val) {
  if (!val) {
    if (body.style.getPropertyValue(name)) body.style.removeProperty(name);
  } else if (body.style.getPropertyValue(name) !== val) {
    body.style.setProperty(name, val);
  }
}

function syncSelectColors(mode, resolved) {
  const body = document.body;
  if (!body) return;
  const ctx = getColorProbeCtx();
  if (mode !== 'auto' || !ctx) {
    setBodyVar(body, '--m-select-bg', '');
    setBodyVar(body, '--m-select-fg', '');
    return;
  }
  const bg = resolveCssVarColor('--background');
  const card = resolveCssVarColor('--card');
  const content = resolveCssVarColor('--content');
  const fg = resolveCssVarColor('--foreground');
  if (!fg || fg.a < 0.5 || (!bg && !card && !content)) {
    setBodyVar(body, '--m-select-bg', '');
    setBodyVar(body, '--m-select-fg', '');
    return;
  }
  ctx.clearRect(0, 0, 1, 1);
  ctx.fillStyle = resolved === 'dark' ? '#0f1117' : '#f8fafc';
  ctx.fillRect(0, 0, 1, 1);
  if (bg && bg.a > 0) {
    ctx.fillStyle = bg.css;
    ctx.fillRect(0, 0, 1, 1);
  }
  const surfaceCss = (card && card.a > 0) ? card.css : (content && content.a > 0 ? content.css : null);
  if (surfaceCss) {
    ctx.fillStyle = surfaceCss;
    ctx.fillRect(0, 0, 1, 1);
  }
  const baseData = ctx.getImageData(0, 0, 1, 1).data;
  const baseRgb = { r: baseData[0], g: baseData[1], b: baseData[2] };

  ctx.fillStyle = fg.css;
  ctx.fillRect(0, 0, 1, 1);
  const fgData = ctx.getImageData(0, 0, 1, 1).data;
  const selFg = { r: fgData[0], g: fgData[1], b: fgData[2] };

  const selBg = {
    r: Math.round(baseRgb.r * 0.94 + selFg.r * 0.06),
    g: Math.round(baseRgb.g * 0.94 + selFg.g * 0.06),
    b: Math.round(baseRgb.b * 0.94 + selFg.b * 0.06)
  };
  const polarityOk = resolved === 'dark' ? gammaLuma(selBg) < 0.5 : gammaLuma(selBg) >= 0.5;
  if (!polarityOk || wcagContrast(selBg, selFg) < 4.5) {
    setBodyVar(body, '--m-select-bg', '');
    setBodyVar(body, '--m-select-fg', '');
    return;
  }
  setBodyVar(body, '--m-select-bg', 'rgb(' + selBg.r + ', ' + selBg.g + ', ' + selBg.b + ')');
  setBodyVar(body, '--m-select-fg', 'rgb(' + selFg.r + ', ' + selFg.g + ', ' + selFg.b + ')');
}

function detectHostBgScheme() {
  const bg = resolveCssVarColor('--background');
  if (bg && bg.a >= 0.2) return gammaLuma(bg) < 0.5 ? 'dark' : 'light';
  const card = resolveCssVarColor('--card');
  if (card && card.a >= 0.5) return gammaLuma(card) < 0.5 ? 'dark' : 'light';
  const fg = resolveCssVarColor('--foreground');
  if (fg && fg.a >= 0.5) return gammaLuma(fg) >= 0.5 ? 'dark' : 'light';
  return null;
}

function detectUrlScheme() {
  const qp = new URLSearchParams(window.location.search);
  const keys = ['theme', 'colorScheme', 'scheme'];
  for (let i = 0; i < keys.length; i++) {
    const v = (qp.get(keys[i]) || '').toLowerCase();
    if (v.indexOf('dark') !== -1) return 'dark';
    if (v.indexOf('light') !== -1) return 'light';
  }
  return null;
}

function detectMediaScheme() {
  if (!window.matchMedia) return null;
  if (window.matchMedia('(prefers-color-scheme: dark)').matches) return 'dark';
  if (window.matchMedia('(prefers-color-scheme: light)').matches) return 'light';
  return null;
}

function resolveAutoScheme() {
  return (
    detectHostBgScheme() ||
    hostPostScheme ||
    detectUrlScheme() ||
    detectMediaScheme() ||
    'dark'
  );
}

function updateThemeButton(mode, resolved) {
  const btn = document.getElementById('themeBtn');
  if (!btn) return;
  const svgAuto = '<svg class="theme-icon" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="5.5" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M8 2.5a5.5 5.5 0 0 1 0 11Z" fill="currentColor"/></svg>';
  const svgDark = '<svg class="theme-icon" viewBox="0 0 16 16" aria-hidden="true"><path d="M13 9.8A5.5 5.5 0 0 1 6.2 3a5.5 5.5 0 1 0 6.8 6.8Z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>';
  const svgLight = '<svg class="theme-icon" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="2.8" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M8 1.5v1.8M8 12.7v1.8M1.5 8h1.8M12.7 8h1.8M3.4 3.4l1.3 1.3M11.3 11.3l1.3 1.3M12.6 3.4l-1.3 1.3M4.7 11.3l-1.3 1.3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>';
  if (mode === 'auto') {
    btn.innerHTML = svgAuto + '<span>Auto</span>';
    btn.title = 'Theme: Auto (' + resolved + ') — click for Dark';
  } else if (mode === 'dark') {
    btn.innerHTML = svgDark + '<span>Dark</span>';
    btn.title = 'Theme: Dark — click for Light';
  } else {
    btn.innerHTML = svgLight + '<span>Light</span>';
    btn.title = 'Theme: Light — click for Auto';
  }
}

export function syncTheme() {
  const root = document.documentElement;
  const mode = getSavedThemeMode();
  let resolved;
  if (mode === 'dark' || mode === 'light') {
    resolved = mode;
    root.dataset.forceTheme = mode;
  } else {
    delete root.dataset.forceTheme;
    resolved = resolveAutoScheme();
  }
  root.dataset.theme = resolved;
  syncSelectColors(mode, resolved);
  updateThemeButton(mode, resolved);
  if (onThemeChangedCb) onThemeChangedCb();
}

export function applyHostMessage(raw) {
  if (!raw || typeof raw !== 'object') return;
  const msg = (raw.payload && typeof raw.payload === 'object') ? raw.payload : raw;
  const tokens = msg.tokens || msg.variables || msg.cssVariables;
  const root = document.documentElement;
  if (tokens && typeof tokens === 'object') {
    Object.keys(tokens).forEach(function (k) {
      const v = tokens[k];
      if (
        VALID_TOKEN_NAME.test(k) &&
        typeof v === 'string' &&
        v.length > 0 &&
        v.length <= 200 &&
        !/[;{}<>\\]|url\(|expression/i.test(v)
      ) {
        root.style.setProperty(k, v);
      }
    });
  }
  const rawScheme = msg.scheme || msg.theme || msg.colorScheme || msg.mode;
  if (typeof rawScheme === 'string') {
    const lower = rawScheme.toLowerCase();
    if (lower.indexOf('dark') !== -1) hostPostScheme = 'dark';
    else if (lower.indexOf('light') !== -1) hostPostScheme = 'light';
  }
  syncTheme();
}

export function initThemeControls(onThemeChanged) {
  onThemeChangedCb = onThemeChanged || null;
  syncTheme();

  if (typeof MutationObserver === 'function') {
    new MutationObserver(function () {
      syncTheme();
    }).observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['style', 'class']
    });
  }

  if (window.matchMedia) {
    ['(prefers-color-scheme: dark)', '(prefers-color-scheme: light)'].forEach(function (q) {
      const mq = window.matchMedia(q);
      if (typeof mq.addEventListener === 'function') {
        mq.addEventListener('change', syncTheme);
      } else if (typeof mq.addListener === 'function') {
        mq.addListener(syncTheme);
      }
    });
  }

  window.addEventListener('message', function (e) {
    if (window.parent && window.parent !== window && e.source !== window.parent) return;
    applyHostMessage(e.data);
  });

  if (window.parent && window.parent !== window) {
    try {
      window.parent.postMessage({ type: 'READY' }, '*');
      window.parent.postMessage({ type: 'sidecar:ready' }, '*');
    } catch (e) {}
  }

  const themeBtn = document.getElementById('themeBtn');
  if (themeBtn) {
    themeBtn.addEventListener('click', function () {
      const cur = getSavedThemeMode();
      const next = cur === 'auto' ? 'dark' : (cur === 'dark' ? 'light' : 'auto');
      lsSet(THEME_STORAGE_KEY, next);
      syncTheme();
    });
  }
}
