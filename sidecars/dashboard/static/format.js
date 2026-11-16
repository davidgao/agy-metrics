// Pure formatting, escaping, and color-mapping helpers for agy-metrics.

const EXTRA_MODEL_COLORS = [
  'var(--model-1)',
  'var(--model-2)',
  'var(--model-3)',
  'var(--model-4)',
  'var(--model-5)'
];

export function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

export function fmtInt(n) {
  return Number(n || 0).toLocaleString();
}

export function fmtCompact(n) {
  const v = Number(n || 0);
  if (v >= 1000000) return (v / 1000000).toFixed(2) + 'M';
  if (v >= 1000) return (v / 1000).toFixed(1) + 'k';
  return String(v);
}

export function fmtUsd(val) {
  if (val == null || isNaN(val)) return '—';
  const n = Number(val);
  if (Math.abs(n) < 1e-12) return '$0.00';
  const sign = n < 0 ? '-' : '';
  const a = Math.abs(n);
  if (a < 0.0001) return sign + '<$0.0001';
  if (a < 0.01) return sign + '$' + a.toFixed(4);
  if (a < 1.0) return sign + '$' + a.toFixed(3);
  return sign + '$' + a.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export function fmtCredits(val) {
  if (val == null || isNaN(val)) return '—';
  const n = Number(val);
  if (Math.abs(n) < 1e-9) return '0 cr';
  const sign = n < 0 ? '-' : '';
  const a = Math.abs(n);
  if (a < 0.05) return sign + '<0.1 cr';
  if (a < 10) return sign + a.toFixed(1) + ' cr';
  return sign + Math.round(a).toLocaleString() + ' cr';
}

export function fmtRate(val) {
  if (val == null || isNaN(val)) return '—';
  return '$' + String(Number(Number(val).toFixed(4)));
}

export function fmtOrchSavingsPct(pct) {
  const n = Number(pct || 0);
  return n >= 0 ? ('-' + n + '%') : ('+' + Math.abs(n) + '%');
}

export function shortModelName(name) {
  if (!name) return 'unknown';
  return String(name).replace(/^Orchestrator\s*\(([^)]+)\)\s*·\s*/i, '$1 · ');
}

export function getModelColorMap(byModel) {
  const map = {};
  let usedPlanner = false;
  let usedExecutor = false;
  let extraIdx = 0;
  (byModel || []).forEach(function (bm) {
    const name = bm.model_name || 'unknown';
    const lower = name.toLowerCase();
    if (lower.indexOf('planner') !== -1 && !usedPlanner) {
      map[name] = 'var(--model-planner)';
      usedPlanner = true;
    } else if (lower.indexOf('executor') !== -1 && !usedExecutor) {
      map[name] = 'var(--model-executor)';
      usedExecutor = true;
    } else {
      map[name] = EXTRA_MODEL_COLORS[extraIdx % EXTRA_MODEL_COLORS.length];
      extraIdx++;
    }
  });
  return map;
}

export function srcLabel(src) {
  if (src === 'hook_ms') return 'hook ms';
  if (src === 'watcher_ms') return 'live';
  if (src === 'transcript_s') return 'log 1s';
  return '—';
}

export function fmtNomTps(tps) {
  const v = Number(tps || 0);
  return v >= 100 ? String(Math.round(v)) : v.toFixed(1);
}

export function fmtDuration(sec) {
  const v = Math.max(0, Number(sec || 0));
  const s10 = Math.round(v * 10);
  if (s10 < 600) {
    return (s10 / 10).toFixed(1) + 's';
  }
  const wholeS = Math.round(v);
  if (wholeS < 3600) {
    const m = Math.floor(wholeS / 60);
    const remS = wholeS % 60;
    return m + 'm ' + remS + 's';
  }
  const wholeM = Math.round(v / 60);
  if (wholeM < 1440) {
    const h = Math.floor(wholeM / 60);
    const remM = wholeM % 60;
    return h + 'h ' + remM + 'm';
  }
  const days = v / 86400;
  return days >= 100 ? (Math.round(days) + 'd') : (days.toFixed(1) + 'd');
}

export function fmtApproxDuration(sec) {
  return '~' + fmtDuration(sec);
}

export function fmtExactSecNote(sec) {
  const v = Number(sec || 0);
  return v >= 60 ? (' [' + v.toFixed(1) + 's]') : '';
}

export function fmtShortDelay(sec) {
  return '+' + Number(sec || 0).toFixed(1) + 's';
}

export function fmtApproxSec(sec) {
  return '~' + Number(sec || 0).toFixed(1) + 's';
}

export function fmtColdTimeNote(obj) {
  const n = (obj && obj.cold_timed_steps_count) || 0;
  if (n <= 0) return '';
  const coldS = Number(obj.cold_model_time_s || 0);
  return ' (incl. ' + n + ' cold prefill' + (n === 1 ? '' : 's') + ': ' + fmtDuration(coldS) + fmtExactSecNote(coldS) + ')';
}

export function fmtWastedSummary(obj) {
  if (!obj) return '';
  const parts = [];
  const retries = obj.retry_steps_count || 0;
  const failed = obj.failed_turns_count || 0;
  if (retries > 0) parts.push(retries + ' retried step' + (retries === 1 ? '' : 's'));
  if (failed > 0) parts.push(failed + ' failed turn' + (failed === 1 ? '' : 's'));
  return parts.join(', ');
}

export function fmtWastedTimeNote(obj) {
  const w = Number((obj && obj.wasted_time_s) || 0);
  if (!(w > 0)) return '';
  const summary = fmtWastedSummary(obj);
  return ' · incl. ' + fmtDuration(w) + fmtExactSecNote(w) + ' retry/failed wait' + (summary ? (' (' + summary + ')') : '');
}

export function fmtAxisTok(v) {
  const n = Number(v || 0);
  if (n === 0) return '0';
  if (n >= 1000000) return Number((n / 1000000).toFixed(2)) + 'M';
  if (n >= 1000) return Number((n / 1000).toFixed(1)) + 'k';
  return String(Math.round(n));
}

export function niceCeil(val) {
  if (!(val > 0)) return 1;
  const exp = Math.pow(10, Math.floor(Math.log10(val)));
  const norm10 = (val / exp) * 10;
  const steps10 = [10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30, 32, 36, 40, 44, 48, 50, 56, 60, 66, 72, 80, 90, 100];
  let pick10 = 100;
  for (let i = 0; i < steps10.length; i++) {
    if (norm10 <= steps10[i] + 1e-9) {
      pick10 = steps10[i];
      break;
    }
  }
  return Number(((pick10 * exp) / 10).toPrecision(10));
}

export function fmtAxisSec(sec) {
  if (!sec) return '0s';
  if (sec >= 100) return Math.round(sec) + 's';
  if (sec >= 10) return Number(sec.toFixed(1)) + 's';
  return Number(sec.toFixed(2)) + 's';
}
