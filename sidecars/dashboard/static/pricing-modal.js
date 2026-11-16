// Local pricing catalog override and custom model drawer UI.

import { esc, fmtRate } from './format.js';
import { buildUrl, sidecarFetch, state } from './state.js';

function getCsrfToken() {
  if (typeof document === 'undefined') return '';
  const meta = document.querySelector('meta[name="agy-csrf-token"]');
  return meta ? (meta.getAttribute('content') || '') : '';
}

function showRatesFeedback(msg, isError) {
  const el = document.getElementById('ratesFeedback');
  if (!el) return;
  if (!msg) {
    el.style.display = 'none';
    el.textContent = '';
    return;
  }
  el.style.display = '';
  el.style.color = isError ? 'var(--fb-warn)' : 'var(--fb-ok)';
  el.textContent = msg;
}

function parseRateValue(strVal, label) {
  const n = Number(strVal);
  if (strVal === '' || !isFinite(n) || n < 0 || n > 100000) {
    throw new Error('Invalid ' + label + ' rate (must be 0 – 100000)');
  }
  return n;
}

function createPoolSelect(selectedPool) {
  const sel = document.createElement('select');
  sel.className = 'rate-select';
  sel.setAttribute('aria-label', 'Quota Pool');
  [
    { val: 'custom', lbl: 'Custom' },
    { val: 'gemini', lbl: 'Google' },
    { val: 'partner', lbl: '3rd-Party' }
  ].forEach(function (optInfo) {
    const opt = document.createElement('option');
    opt.value = optInfo.val;
    opt.textContent = optInfo.lbl;
    if ((selectedPool || 'custom') === optInfo.val) opt.selected = true;
    sel.appendChild(opt);
  });
  return sel;
}

export function postPricingAction(payload, onUpdated) {
  showRatesFeedback('', false);
  const csrf = getCsrfToken();
  const bodyPayload = csrf ? Object.assign({}, payload, { _csrf_token: csrf }) : payload;
  return sidecarFetch(buildUrl('/api/pricing', null), {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    body: JSON.stringify(bodyPayload)
  }).then(function (r) {
    return r.json().then(function (data) {
      if (!r.ok) {
        throw new Error((data && data.error) || ('HTTP ' + r.status));
      }
      return data;
    });
  }).then(function (cfg) {
    state.pricingConfig = cfg;
    renderRatesDrawer(cfg, false, onUpdated);
    if (onUpdated) onUpdated();
    return cfg;
  }).catch(function (err) {
    showRatesFeedback(err && err.message ? err.message : 'Failed to update pricing', true);
  });
}

export function fetchPricingConfig(focusUnpriced, onUpdated) {
  return sidecarFetch(buildUrl('/api/pricing', null))
    .then(function (r) { return r.json(); })
    .then(function (cfg) {
      state.pricingConfig = cfg;
      renderRatesDrawer(cfg, Boolean(focusUnpriced), onUpdated);
      return cfg;
    })
    .catch(function () {
      showRatesFeedback('Unable to load /api/pricing', true);
    });
}

export function renderRatesDrawer(cfg, focusUnpriced, onUpdated) {
  if (!cfg) return;
  const drawer = document.getElementById('ratesDrawer');
  if (!drawer) return;
  drawer.classList.toggle('open', Boolean(state.ratesOpen));

  const cpuIn = document.getElementById('cfgCreditsInput');
  if (cpuIn) cpuIn.value = String(cfg.credits_per_usd != null ? cfg.credits_per_usd : 100);
  const resetAllBtn = document.getElementById('cfgResetAllBtn');
  if (resetAllBtn) resetAllBtn.style.display = cfg.has_overrides ? '' : 'none';

  const body = document.getElementById('ratesDrawerBody');
  body.innerHTML = '';

  const customModels = cfg.custom_models || [];
  const customExactSet = new Set();
  customModels.forEach(function (cm) {
    if (cm.match_mode === 'exact') customExactSet.add(String(cm.pattern || '').toLowerCase());
  });

  const unpricedSeen = [];
  const seenSet = new Set();
  function pushUnpriced(name) {
    if (!name) return;
    const low = String(name).toLowerCase();
    if (low === 'unknown' || low === 'auto' || seenSet.has(low) || customExactSet.has(low)) return;
    seenSet.add(low);
    unpricedSeen.push(String(name));
  }
  (cfg.seen_unpriced_models || []).forEach(pushUnpriced);
  if (state.lastPayload && state.lastPayload.valuation && state.lastPayload.valuation.coverage) {
    (state.lastPayload.valuation.coverage.unpriced_models || []).forEach(function (u) {
      pushUnpriced(u.model_name);
    });
  }

  let firstUnpricedInput = null;
  if (unpricedSeen.length > 0) {
    const uh = document.createElement('div');
    uh.className = 'rates-subhead';
    uh.textContent = 'Unpriced Models Seen';
    body.appendChild(uh);

    const utWrap = document.createElement('div');
    utWrap.className = 'table-wrap';
    const ut = document.createElement('table');
    ut.innerHTML = '<thead><tr><th>Model</th><th>Pool</th><th>In /1M</th><th>Cache /1M</th><th>Out /1M</th><th>Action</th></tr></thead>';
    const utb = document.createElement('tbody');
    unpricedSeen.forEach(function (mName) {
      const tr = document.createElement('tr');
      const tdName = document.createElement('td');
      tdName.className = 'mono';
      tdName.textContent = mName;

      const tdPool = document.createElement('td');
      const unPoolSel = createPoolSelect('custom');
      tdPool.appendChild(unPoolSel);

      const tdIn = document.createElement('td');
      const inEl = document.createElement('input');
      inEl.type = 'number'; inEl.step = 'any'; inEl.min = '0'; inEl.placeholder = '1.0';
      inEl.className = 'rate-input';
      if (!firstUnpricedInput) firstUnpricedInput = inEl;
      tdIn.appendChild(inEl);

      const tdCa = document.createElement('td');
      const caEl = document.createElement('input');
      caEl.type = 'number'; caEl.step = 'any'; caEl.min = '0'; caEl.placeholder = '0.1';
      caEl.className = 'rate-input';
      tdCa.appendChild(caEl);

      const tdOut = document.createElement('td');
      const outEl = document.createElement('input');
      outEl.type = 'number'; outEl.step = 'any'; outEl.min = '0'; outEl.placeholder = '5.0';
      outEl.className = 'rate-input';
      tdOut.appendChild(outEl);

      const tdAct = document.createElement('td');
      const saveBtn = document.createElement('button');
      saveBtn.type = 'button';
      saveBtn.className = 'filter-pill btn-sm';
      saveBtn.textContent = 'Save';
      saveBtn.addEventListener('click', function () {
        try {
          const rates = {
            input: parseRateValue(inEl.value, 'input'),
            cache_read: parseRateValue(caEl.value === '' ? '0' : caEl.value, 'cache_read'),
            output: parseRateValue(outEl.value, 'output')
          };
          postPricingAction({
            action: 'set_custom_model',
            pattern: mName,
            label: mName,
            match_mode: 'exact',
            pool: unPoolSel.value || 'custom',
            rates: rates
          }, onUpdated);
        } catch (e) {
          showRatesFeedback(e.message, true);
        }
      });
      tdAct.appendChild(saveBtn);
      tr.appendChild(tdName);
      tr.appendChild(tdPool);
      tr.appendChild(tdIn);
      tr.appendChild(tdCa);
      tr.appendChild(tdOut);
      tr.appendChild(tdAct);
      utb.appendChild(tr);
    });
    ut.appendChild(utb);
    utWrap.appendChild(ut);
    body.appendChild(utWrap);
  }

  const ch = document.createElement('div');
  ch.className = 'rates-subhead';
  ch.textContent = 'Catalog & Custom Rules';
  body.appendChild(ch);

  const tWrap = document.createElement('div');
  tWrap.className = 'table-wrap';
  const tbl = document.createElement('table');
  tbl.innerHTML = '<thead><tr><th>Model</th><th>Pool</th><th>In /1M</th><th>Cache /1M</th><th>Out /1M</th><th>Actions</th></tr></thead>';
  const tb = document.createElement('tbody');

  customModels.forEach(function (cm) {
    const tr = document.createElement('tr');
    const tdName = document.createElement('td');
    tdName.innerHTML = '<span class="mono">' + esc(cm.pattern) + '</span> <span class="muted">(' + esc(cm.match_mode) + ')</span>';

    const tdPool = document.createElement('td');
    const cmPoolSel = createPoolSelect(cm.pool || 'custom');
    tdPool.appendChild(cmPoolSel);

    const tdIn = document.createElement('td');
    const inEl = document.createElement('input');
    inEl.type = 'number'; inEl.step = 'any'; inEl.min = '0'; inEl.className = 'rate-input';
    inEl.value = String(cm.rates.input);
    tdIn.appendChild(inEl);

    const tdCa = document.createElement('td');
    const caEl = document.createElement('input');
    caEl.type = 'number'; caEl.step = 'any'; caEl.min = '0'; caEl.className = 'rate-input';
    caEl.value = String(cm.rates.cache_read);
    tdCa.appendChild(caEl);

    const tdOut = document.createElement('td');
    const outEl = document.createElement('input');
    outEl.type = 'number'; outEl.step = 'any'; outEl.min = '0'; outEl.className = 'rate-input';
    outEl.value = String(cm.rates.output);
    tdOut.appendChild(outEl);

    const tdAct = document.createElement('td');
    tdAct.className = 'rates-group';
    const saveBtn = document.createElement('button');
    saveBtn.type = 'button'; saveBtn.className = 'filter-pill btn-sm'; saveBtn.textContent = 'Save';
    saveBtn.addEventListener('click', function () {
      try {
        postPricingAction({
          action: 'set_custom_model',
          pattern: cm.pattern,
          label: cm.label,
          match_mode: cm.match_mode,
          pool: cmPoolSel.value || 'custom',
          rates: {
            input: parseRateValue(inEl.value, 'input'),
            cache_read: parseRateValue(caEl.value, 'cache_read'),
            output: parseRateValue(outEl.value, 'output')
          }
        }, onUpdated);
      } catch (e) {
        showRatesFeedback(e.message, true);
      }
    });
    const delBtn = document.createElement('button');
    delBtn.type = 'button'; delBtn.className = 'filter-pill btn-sm'; delBtn.textContent = 'Delete';
    delBtn.addEventListener('click', function () {
      postPricingAction({ action: 'delete_custom_model', key: cm.key }, onUpdated);
    });
    tdAct.appendChild(saveBtn);
    tdAct.appendChild(delBtn);

    tr.appendChild(tdName); tr.appendChild(tdPool); tr.appendChild(tdIn); tr.appendChild(tdCa); tr.appendChild(tdOut); tr.appendChild(tdAct);
    tb.appendChild(tr);
  });

  (cfg.catalog || []).forEach(function (item) {
    const tr = document.createElement('tr');
    const tdName = document.createElement('td');
    const futBadge = item.has_future_change && item.future_effective_date
      ? (' <span class="muted" title="Scheduled standard rates from ' + esc(item.future_effective_date) + ': ' + esc(fmtRate(item.future_rates.input) + ' / ' + fmtRate(item.future_rates.cache_read) + ' / ' + fmtRate(item.future_rates.output)) + '">→ ' + esc(item.future_effective_date) + '</span>')
      : '';
    const ovBadge = item.source === 'override' ? ' <span class="pill info">override</span>' : '';
    tdName.innerHTML = '<span>' + esc(item.label) + '</span>' + futBadge + ovBadge;

    const tdPool = document.createElement('td');
    tdPool.innerHTML = '<span class="muted">' + esc(item.pool === 'partner' ? '3rd-Party' : (item.pool === 'gemini' ? 'Google' : 'Custom')) + '</span>';

    const tdIn = document.createElement('td');
    const inEl = document.createElement('input');
    inEl.type = 'number'; inEl.step = 'any'; inEl.min = '0'; inEl.className = 'rate-input';
    inEl.value = String(item.rates.input);
    tdIn.appendChild(inEl);

    const tdCa = document.createElement('td');
    const caEl = document.createElement('input');
    caEl.type = 'number'; caEl.step = 'any'; caEl.min = '0'; caEl.className = 'rate-input';
    caEl.value = String(item.rates.cache_read);
    tdCa.appendChild(caEl);

    const tdOut = document.createElement('td');
    const outEl = document.createElement('input');
    outEl.type = 'number'; outEl.step = 'any'; outEl.min = '0'; outEl.className = 'rate-input';
    outEl.value = String(item.rates.output);
    tdOut.appendChild(outEl);

    const tdAct = document.createElement('td');
    tdAct.className = 'rates-group';
    const saveBtn = document.createElement('button');
    saveBtn.type = 'button'; saveBtn.className = 'filter-pill btn-sm'; saveBtn.textContent = 'Save';
    saveBtn.addEventListener('click', function () {
      try {
        postPricingAction({
          action: 'set_catalog_override',
          id: item.id,
          rates: {
            input: parseRateValue(inEl.value, 'input'),
            cache_read: parseRateValue(caEl.value, 'cache_read'),
            output: parseRateValue(outEl.value, 'output')
          }
        }, onUpdated);
      } catch (e) {
        showRatesFeedback(e.message, true);
      }
    });
    tdAct.appendChild(saveBtn);
    if (item.source === 'override') {
      const rstBtn = document.createElement('button');
      rstBtn.type = 'button'; rstBtn.className = 'filter-pill btn-sm'; rstBtn.textContent = 'Reset';
      rstBtn.addEventListener('click', function () {
        postPricingAction({ action: 'reset_catalog_override', id: item.id }, onUpdated);
      });
      tdAct.appendChild(rstBtn);
    }

    tr.appendChild(tdName); tr.appendChild(tdPool); tr.appendChild(tdIn); tr.appendChild(tdCa); tr.appendChild(tdOut); tr.appendChild(tdAct);
    tb.appendChild(tr);
  });

  tbl.appendChild(tb);
  tWrap.appendChild(tbl);
  body.appendChild(tWrap);

  const ah = document.createElement('div');
  ah.className = 'rates-subhead';
  ah.textContent = 'Add Custom Rule';
  body.appendChild(ah);

  const addRow = document.createElement('div');
  addRow.className = 'rates-group';
  const patIn = document.createElement('input');
  patIn.type = 'text'; patIn.placeholder = 'Model / Substring'; patIn.className = 'rate-input wide';
  const modeSel = document.createElement('select');
  modeSel.className = 'rate-select';
  modeSel.innerHTML = '<option value="exact">Exact</option><option value="substring">Substring</option>';
  const addPoolSel = createPoolSelect('custom');
  const addIn = document.createElement('input');
  addIn.type = 'number'; addIn.step = 'any'; addIn.min = '0'; addIn.placeholder = 'In'; addIn.className = 'rate-input';
  const addCa = document.createElement('input');
  addCa.type = 'number'; addCa.step = 'any'; addCa.min = '0'; addCa.placeholder = 'Cache'; addCa.className = 'rate-input';
  const addOut = document.createElement('input');
  addOut.type = 'number'; addOut.step = 'any'; addOut.min = '0'; addOut.placeholder = 'Out'; addOut.className = 'rate-input';
  const addBtn = document.createElement('button');
  addBtn.type = 'button'; addBtn.className = 'filter-pill btn-sm'; addBtn.textContent = 'Add';
  addBtn.addEventListener('click', function () {
    try {
      const pat = (patIn.value || '').trim();
      if (!pat) throw new Error('Model pattern cannot be empty');
      postPricingAction({
        action: 'set_custom_model',
        pattern: pat,
        label: pat,
        match_mode: modeSel.value,
        pool: addPoolSel.value || 'custom',
        rates: {
          input: parseRateValue(addIn.value, 'input'),
          cache_read: parseRateValue(addCa.value === '' ? '0' : addCa.value, 'cache_read'),
          output: parseRateValue(addOut.value, 'output')
        }
      }, onUpdated);
    } catch (e) {
      showRatesFeedback(e.message, true);
    }
  });
  addRow.appendChild(patIn);
  addRow.appendChild(modeSel);
  addRow.appendChild(addPoolSel);
  addRow.appendChild(addIn);
  addRow.appendChild(addCa);
  addRow.appendChild(addOut);
  addRow.appendChild(addBtn);
  body.appendChild(addRow);

  if (focusUnpriced && firstUnpricedInput) {
    try { firstUnpricedInput.focus(); } catch (e) {}
  }
}

export function initPricingModal(onUpdated) {
  document.getElementById('ratesToggleBtn').addEventListener('click', function () {
    state.ratesOpen = !state.ratesOpen;
    const drawer = document.getElementById('ratesDrawer');
    if (drawer) drawer.classList.toggle('open', state.ratesOpen);
    const btn = document.getElementById('ratesToggleBtn');
    if (btn) btn.classList.toggle('active', state.ratesOpen);
    if (state.ratesOpen) {
      fetchPricingConfig(false, onUpdated);
    }
  });

  document.getElementById('valUnpricedBtn').addEventListener('click', function () {
    state.ratesOpen = true;
    const drawer = document.getElementById('ratesDrawer');
    if (drawer) drawer.classList.add('open');
    const btn = document.getElementById('ratesToggleBtn');
    if (btn) btn.classList.add('active');
    fetchPricingConfig(true, onUpdated);
  });

  document.getElementById('cfgCreditsSaveBtn').addEventListener('click', function () {
    const inp = document.getElementById('cfgCreditsInput');
    const val = Number(inp ? inp.value : 0);
    if (!inp || inp.value === '' || !isFinite(val) || val < 0.0001 || val > 1000000) {
      showRatesFeedback('Credits / $1 must be between 0.0001 and 1000000', true);
      return;
    }
    postPricingAction({ action: 'set_credits_per_usd', credits_per_usd: val }, onUpdated);
  });

  document.getElementById('cfgResetAllBtn').addEventListener('click', function () {
    postPricingAction({ action: 'reset_all' }, onUpdated);
  });
}
