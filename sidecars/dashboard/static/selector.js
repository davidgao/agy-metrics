// Sidebar-style Project / Conversation / Subagent tree popover selector.

import { esc, fmtCompact } from './format.js';
import {
  ALL_CONVERSATIONS_ID,
  PROJECT_SCOPE_PREFIX,
  UNASSIGNED_PROJECT_ID,
  state
} from './state.js';

let scopeKbIdx = -1;
let scopeOnSelectCb = null;
const scopeParentOverrides = new Map();

function scopeCurrentTargetId() {
  if (state.viewMode === 'all') return ALL_CONVERSATIONS_ID;
  if (state.viewMode === 'project') return state.selectedProjectScopeId || ALL_CONVERSATIONS_ID;
  return state.followActive ? state.activeConvId : state.selectedConvId;
}

function scopeBuildIndex(convsData) {
  const projects = (convsData && convsData.projects) || [];
  const convs = (convsData && convsData.conversations) || [];
  const byId = Object.create(null);
  const childrenByParent = Object.create(null);

  convs.forEach(function (c) {
    byId[c.conversation_id] = c;
  });
  convs.forEach(function (c) {
    if (c.parent_id && byId[c.parent_id]) {
      if (!childrenByParent[c.parent_id]) childrenByParent[c.parent_id] = [];
      childrenByParent[c.parent_id].push(c);
    }
  });

  const hasNamedProjects = projects.some(function (p) {
    return !p.is_unassigned;
  });
  const totalTopConvs = convs.filter(function (c) {
    return !c.is_subagent || !byId[c.parent_id];
  }).length;

  return {
    projects: projects,
    convs: convs,
    byId: byId,
    childrenByParent: childrenByParent,
    hasNamedProjects: hasNamedProjects,
    totalTopConvs: totalTopConvs
  };
}

function scopeUpdateTrigger(idxData) {
  const btn = document.getElementById('scopeTriggerBtn');
  const txtEl = document.getElementById('scopeTriggerText');
  const metaEl = document.getElementById('scopeTriggerMeta');
  if (!btn || !txtEl || !metaEl) return;

  if (state.viewMode === 'all') {
    const n = idxData.totalTopConvs;
    txtEl.textContent = 'All Conversations';
    metaEl.textContent = n + ' conv' + (n === 1 ? '' : 's');
    btn.title = 'All Conversations (' + n + ' conversation' + (n === 1 ? '' : 's') + ')';
    return;
  }

  if (state.viewMode === 'project') {
    const scopeId = state.selectedProjectScopeId || '';
    const pid = scopeId.indexOf(PROJECT_SCOPE_PREFIX) === 0
      ? scopeId.slice(PROJECT_SCOPE_PREFIX.length)
      : scopeId;
    const proj = idxData.projects.find(function (p) { return p.project_id === pid; }) || null;
    const pName = proj ? proj.name : (pid === UNASSIGNED_PROJECT_ID ? 'No Project' : (pid || 'Project'));
    const pCount = proj ? (proj.conversations_count || 0) : 0;
    txtEl.textContent = pName + ' · All';
    metaEl.textContent = pCount + ' conv' + (pCount === 1 ? '' : 's');
    btn.title = 'Project: ' + pName + ' (' + pCount + ' conversation' + (pCount === 1 ? '' : 's') + ')';
    return;
  }

  if (idxData.convs.length === 0) {
    txtEl.textContent = 'No conversations found';
    metaEl.textContent = '';
    btn.title = 'No conversations found';
    return;
  }

  const targetId = scopeCurrentTargetId();
  let c = (targetId && idxData.byId[targetId]) || null;
  if (!c) {
    c = idxData.convs.find(function (item) { return !item.is_subagent; }) || idxData.convs[0];
  }
  const title = c.title || c.conversation_id.slice(0, 8);
  const isActive = c.conversation_id === state.activeConvId;
  const showProj = idxData.hasNamedProjects && c.project_id !== UNASSIGNED_PROJECT_ID && c.project_name;
  const projPrefix = showProj
    ? ('<span class="scope-trigger-proj">' + esc(c.project_name) + ' / </span>')
    : '';
  const activeMark = isActive ? '★ ' : '';
  const subPrefix = c.is_subagent ? '↳ ' : '';
  txtEl.innerHTML = projPrefix + activeMark + subPrefix + esc(title);
  metaEl.textContent = fmtCompact(c.current_tokens);

  const tipParts = [];
  if (showProj) tipParts.push(c.project_name);
  tipParts.push((c.is_subagent ? '↳ ' : '') + title + (c.is_subagent && c.agent_name ? (' [' + c.agent_name + ']') : ''));
  let tip = tipParts.join(' / ');
  if (c.prompt_preview && c.prompt_preview !== title) {
    tip += '\nPrompt: ' + c.prompt_preview;
  }
  btn.title = tip;
}

function scopeMatchesNode(c, q) {
  if (!q) return true;
  const hay = [
    c.title || '',
    c.prompt_preview || '',
    c.agent_name || '',
    c.model_name || '',
    c.project_name || '',
    c.conversation_id || ''
  ].join('\n').toLowerCase();
  return hay.indexOf(q) !== -1;
}

function scopeCollectDescendants(parentId, childrenByParent, outList, visited) {
  const kids = childrenByParent[parentId] || [];
  kids.forEach(function (k) {
    if (visited.has(k.conversation_id)) return;
    visited.add(k.conversation_id);
    outList.push(k);
    scopeCollectDescendants(k.conversation_id, childrenByParent, outList, visited);
  });
}

function scopeSubtreeMatches(c, q, childrenByParent, visited) {
  if (scopeMatchesNode(c, q)) return true;
  if (visited.has(c.conversation_id)) return false;
  visited.add(c.conversation_id);
  const kids = childrenByParent[c.conversation_id] || [];
  for (let i = 0; i < kids.length; i++) {
    if (scopeSubtreeMatches(kids[i], q, childrenByParent, visited)) return true;
  }
  return false;
}

function scopeAttachSelectHandler(el, scopeId) {
  el.addEventListener('click', function (e) {
    e.stopPropagation();
    scopeSetMenuOpen(false);
    if (scopeOnSelectCb) scopeOnSelectCb(scopeId);
  });
}

function scopeMakeConvButton(c, isSubagent, currentTargetId) {
  const isSelected = state.viewMode === 'conversation' && c.conversation_id === currentTargetId;
  const isActive = c.conversation_id === state.activeConvId;
  const title = c.title || c.conversation_id.slice(0, 8);

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'scope-item-btn';
  btn.setAttribute('data-scope', c.conversation_id);
  btn.setAttribute('role', 'option');
  btn.setAttribute('aria-selected', isSelected ? 'true' : 'false');

  let tip = (isSubagent ? '↳ ' : '') + title;
  if (isSubagent && c.agent_name) tip += ' [' + c.agent_name + ']';
  if (c.model_name) tip += ' · ' + c.model_name;
  if (c.prompt_preview && c.prompt_preview !== title) tip += '\nPrompt: ' + c.prompt_preview;
  btn.title = tip;

  let prefixHtml = '';
  if (isSubagent) prefixHtml += '<span class="scope-branch">↳</span>';
  if (c.status === 'generating') {
    prefixHtml += '<span class="dot gen" title="Generating"></span>';
  } else if (isActive) {
    prefixHtml += '<span class="scope-star" title="Active conversation">★</span>';
  }
  const agentHtml = (isSubagent && c.agent_name)
    ? ('<span class="scope-agent-tag">' + esc(c.agent_name) + '</span>')
    : '';

  btn.innerHTML =
    '<span class="scope-item-main">' +
      prefixHtml +
      '<span class="scope-item-title">' + esc(title) + '</span>' +
      agentHtml +
    '</span>' +
    '<span class="scope-item-meta mono">' + esc(fmtCompact(c.current_tokens)) + '</span>';

  scopeAttachSelectHandler(btn, c.conversation_id);
  return btn;
}

function scopeRenderConversationBranch(container, conv, idxData, q, currentTargetId, showTwistyCol) {
  const desc = [];
  scopeCollectDescendants(conv.conversation_id, idxData.childrenByParent, desc, new Set([conv.conversation_id]));
  const hasKids = desc.length > 0;
  const hasRelevantChild = desc.some(function (d) {
    return d.conversation_id === currentTargetId || d.conversation_id === state.activeConvId;
  });
  const defaultExpanded = desc.length <= 5 || hasRelevantChild;
  const isExpanded = q
    ? true
    : (scopeParentOverrides.has(conv.conversation_id)
        ? Boolean(scopeParentOverrides.get(conv.conversation_id))
        : defaultExpanded);

  const row = document.createElement('div');
  const isSelected = state.viewMode === 'conversation' && conv.conversation_id === currentTargetId;
  row.className = 'scope-row scope-conv-row' + (isSelected ? ' active' : '');

  if (hasKids) {
    const tw = document.createElement('button');
    tw.type = 'button';
    tw.className = 'scope-twisty sub';
    tw.setAttribute('aria-label', isExpanded ? 'Collapse subagents' : 'Expand subagents');
    tw.title = isExpanded ? ('Hide ' + desc.length + ' subagent(s)') : ('Show ' + desc.length + ' subagent(s)');
    tw.textContent = isExpanded ? '▾' : '▸';
    tw.addEventListener('click', function (e) {
      e.stopPropagation();
      scopeParentOverrides.set(conv.conversation_id, !isExpanded);
      state.lastScopeTreeSig = null;
      scopeRenderTreeList();
    });
    row.appendChild(tw);
  } else if (showTwistyCol) {
    const sp = document.createElement('span');
    sp.className = 'scope-twisty-spacer';
    row.appendChild(sp);
  }

  row.appendChild(scopeMakeConvButton(conv, false, currentTargetId));
  container.appendChild(row);

  if (hasKids && isExpanded) {
    const directMatched = q ? scopeMatchesNode(conv, q) : true;
    const renderChildren = function (parentId, depthLevel, seenSet) {
      const kids = idxData.childrenByParent[parentId] || [];
      kids.forEach(function (child) {
        if (seenSet.has(child.conversation_id)) return;
        seenSet.add(child.conversation_id);
        const childMatches = !q || directMatched || scopeSubtreeMatches(child, q, idxData.childrenByParent, new Set());
        if (!childMatches) return;
        const subRow = document.createElement('div');
        const subSelected = state.viewMode === 'conversation' && child.conversation_id === currentTargetId;
        subRow.className = 'scope-row scope-sub-row' + (subSelected ? ' active' : '');
        const indentPx = 14 + Math.min(Math.max(1, depthLevel), 3) * 12;
        subRow.style.paddingLeft = indentPx + 'px';
        subRow.appendChild(scopeMakeConvButton(child, true, currentTargetId));
        container.appendChild(subRow);
        renderChildren(child.conversation_id, depthLevel + 1, seenSet);
      });
    };
    renderChildren(conv.conversation_id, 1, new Set([conv.conversation_id]));
  }
}

function scopeRenderTreeList() {
  const treeEl = document.getElementById('scopeTreeList');
  if (!treeEl || !state.scopeMenuOpen) return;

  const convsData = state.convsDataCache || { projects: [], conversations: [] };
  const idxData = scopeBuildIndex(convsData);
  const q = (state.scopeFilterQuery || '').trim().toLowerCase();
  const currentTargetId = scopeCurrentTargetId();

  const overridesSig = Array.from(scopeParentOverrides.entries())
    .map(function (kv) { return kv[0] + ':' + (kv[1] ? '1' : '0'); })
    .sort()
    .join(',');
  const projSig = idxData.projects.map(function (p) {
    return p.project_id + ':' + p.name + ':' + (p.conversations_count || 0);
  }).join('|');
  const listSig = idxData.convs.map(function (c) {
    return [
      c.conversation_id,
      c.title || '',
      c.status || '',
      c.current_tokens || 0,
      c.project_id || '',
      c.parent_id || '',
      c.agent_name || ''
    ].join(':');
  }).join('|');
  const treeSig = [
    q,
    state.viewMode,
    currentTargetId || '',
    state.activeConvId || '',
    Array.from(state.collapsedProjects).sort().join(','),
    overridesSig,
    projSig,
    listSig
  ].join('||');

  if (state.lastScopeTreeSig === treeSig) {
    return;
  }
  state.lastScopeTreeSig = treeSig;

  const prevScroll = treeEl.scrollTop;
  treeEl.innerHTML = '';
  scopeKbIdx = -1;

  // 1. Top-level All Conversations row
  const allBtn = document.createElement('button');
  allBtn.type = 'button';
  allBtn.className = 'scope-row scope-all-row' + (state.viewMode === 'all' ? ' active' : '');
  allBtn.setAttribute('data-scope', ALL_CONVERSATIONS_ID);
  allBtn.setAttribute('role', 'option');
  allBtn.setAttribute('aria-selected', state.viewMode === 'all' ? 'true' : 'false');
  allBtn.innerHTML =
    '<span class="scope-item-main"><span class="scope-item-title">All Conversations</span></span>' +
    '<span class="scope-item-meta mono">' + esc(idxData.totalTopConvs + ' conv' + (idxData.totalTopConvs === 1 ? '' : 's')) + '</span>';
  scopeAttachSelectHandler(allBtn, ALL_CONVERSATIONS_ID);
  treeEl.appendChild(allBtn);

  if (idxData.convs.length === 0) {
    const emptyDiv = document.createElement('div');
    emptyDiv.className = 'scope-empty muted';
    emptyDiv.textContent = 'No conversations recorded yet';
    treeEl.appendChild(emptyDiv);
    return;
  }

  const anySubagents = idxData.convs.some(function (c) {
    return Boolean(idxData.childrenByParent[c.conversation_id] && idxData.childrenByParent[c.conversation_id].length);
  });
  let renderedCount = 0;

  if (!idxData.hasNamedProjects) {
    const topConvs = idxData.convs.filter(function (c) {
      return !c.is_subagent || !idxData.byId[c.parent_id];
    });
    topConvs.forEach(function (conv) {
      if (q && !scopeSubtreeMatches(conv, q, idxData.childrenByParent, new Set())) return;
      scopeRenderConversationBranch(treeEl, conv, idxData, q, currentTargetId, anySubagents);
      renderedCount += 1;
    });
  } else {
    idxData.projects.forEach(function (proj) {
      const projScopeId = PROJECT_SCOPE_PREFIX + proj.project_id;
      const projMatches = !q || proj.name.toLowerCase().indexOf(q) !== -1;
      const projTopConvs = idxData.convs.filter(function (c) {
        const isTop = !c.is_subagent || !idxData.byId[c.parent_id];
        return isTop && (c.project_id || UNASSIGNED_PROJECT_ID) === proj.project_id;
      });
      const visibleTops = projMatches
        ? projTopConvs
        : projTopConvs.filter(function (c) {
            return scopeSubtreeMatches(c, q, idxData.childrenByParent, new Set());
          });

      if (q && !projMatches && visibleTops.length === 0) {
        return;
      }

      const isProjCollapsed = !q && state.collapsedProjects.has(proj.project_id);
      const isProjSelected = state.viewMode === 'project' && state.selectedProjectScopeId === projScopeId;

      const pRow = document.createElement('div');
      pRow.className = 'scope-row scope-project-row' + (isProjSelected ? ' active' : '');

      const pTwisty = document.createElement('button');
      pTwisty.type = 'button';
      pTwisty.className = 'scope-twisty';
      pTwisty.setAttribute('aria-label', isProjCollapsed ? ('Expand ' + proj.name) : ('Collapse ' + proj.name));
      pTwisty.textContent = isProjCollapsed ? '▸' : '▾';
      pTwisty.addEventListener('click', function (e) {
        e.stopPropagation();
        if (state.collapsedProjects.has(proj.project_id)) {
          state.collapsedProjects.delete(proj.project_id);
        } else {
          state.collapsedProjects.add(proj.project_id);
        }
        state.lastScopeTreeSig = null;
        scopeRenderTreeList();
      });
      pRow.appendChild(pTwisty);

      const pSel = document.createElement('button');
      pSel.type = 'button';
      pSel.className = 'scope-project-select';
      pSel.setAttribute('data-scope', projScopeId);
      pSel.setAttribute('role', 'option');
      pSel.setAttribute('aria-selected', isProjSelected ? 'true' : 'false');
      pSel.title = 'Filter aggregate view to project: ' + proj.name;
      const folderSvg =
        '<svg class="scope-folder-icon" viewBox="0 0 16 16" aria-hidden="true">' +
        '<path d="M1.8 3.2A1.2 1.2 0 0 1 3 2h3.1l1.2 1.4H13a1.2 1.2 0 0 1 1.2 1.2v7.2A1.2 1.2 0 0 1 13 13H3a1.2 1.2 0 0 1-1.2-1.2V3.2Z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>';
      pSel.innerHTML =
        '<span class="scope-item-main">' +
          folderSvg +
          '<span class="scope-project-name">' + esc(proj.name) + '</span>' +
        '</span>' +
        '<span class="scope-item-meta mono">' + esc(proj.conversations_count || 0) + '</span>';
      scopeAttachSelectHandler(pSel, projScopeId);
      pRow.appendChild(pSel);
      treeEl.appendChild(pRow);
      renderedCount += 1;

      if (!isProjCollapsed) {
        visibleTops.forEach(function (conv) {
          scopeRenderConversationBranch(treeEl, conv, idxData, q, currentTargetId, true);
        });
      }
    });
  }

  if (renderedCount === 0 && q) {
    const noMatch = document.createElement('div');
    noMatch.className = 'scope-empty muted';
    noMatch.textContent = 'No matching projects or conversations';
    treeEl.appendChild(noMatch);
  }

  treeEl.scrollTop = prevScroll;
}

function scopeApplyKbFocus(items) {
  items.forEach(function (el, idx) {
    el.classList.toggle('kb-focus', idx === scopeKbIdx);
  });
  if (scopeKbIdx >= 0 && items[scopeKbIdx] && typeof items[scopeKbIdx].scrollIntoView === 'function') {
    items[scopeKbIdx].scrollIntoView({ block: 'nearest' });
  }
}

function scopeSetMenuOpen(open) {
  const menu = document.getElementById('scopeMenu');
  const trigger = document.getElementById('scopeTriggerBtn');
  const search = document.getElementById('scopeSearchInput');
  state.scopeMenuOpen = Boolean(open);
  if (menu) menu.classList.toggle('hidden', !state.scopeMenuOpen);
  if (trigger) trigger.setAttribute('aria-expanded', state.scopeMenuOpen ? 'true' : 'false');
  if (state.scopeMenuOpen) {
    state.lastScopeTreeSig = null;
    scopeRenderTreeList();
    if (search) {
      search.focus();
      search.select();
    }
  } else {
    scopeKbIdx = -1;
    if (state.scopeFilterQuery) {
      state.scopeFilterQuery = '';
      if (search) search.value = '';
    }
  }
}

export function renderScopeSelector(convsData, onSelectScope) {
  if (onSelectScope) scopeOnSelectCb = onSelectScope;
  if (!convsData) return;
  state.convsDataCache = convsData;
  state.activeConvId = convsData.active_conversation_id || null;

  const idxData = scopeBuildIndex(convsData);
  scopeUpdateTrigger(idxData);
  if (state.scopeMenuOpen) {
    scopeRenderTreeList();
  }
}

export function initScopeSelector(onSelectScope) {
  scopeOnSelectCb = onSelectScope || null;
  const picker = document.getElementById('scopePicker');
  const trigger = document.getElementById('scopeTriggerBtn');
  const menu = document.getElementById('scopeMenu');
  const search = document.getElementById('scopeSearchInput');

  if (trigger) {
    trigger.addEventListener('click', function (e) {
      e.stopPropagation();
      scopeSetMenuOpen(!state.scopeMenuOpen);
    });
  }

  if (search) {
    search.addEventListener('input', function (e) {
      state.scopeFilterQuery = e.target.value || '';
      state.lastScopeTreeSig = null;
      scopeRenderTreeList();
    });
  }

  if (menu) {
    menu.addEventListener('keydown', function (e) {
      const items = Array.from(menu.querySelectorAll('[data-scope]'));
      if (e.key === 'Escape') {
        e.preventDefault();
        scopeSetMenuOpen(false);
        if (trigger) trigger.focus();
        return;
      }
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        if (items.length === 0) return;
        scopeKbIdx = (scopeKbIdx + 1) % items.length;
        scopeApplyKbFocus(items);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        if (items.length === 0) return;
        scopeKbIdx = scopeKbIdx <= 0 ? (items.length - 1) : (scopeKbIdx - 1);
        scopeApplyKbFocus(items);
      } else if (e.key === 'Enter') {
        if (e.target && e.target.classList && e.target.classList.contains('scope-twisty')) {
          return;
        }
        const picked = (scopeKbIdx >= 0 && items[scopeKbIdx])
          ? items[scopeKbIdx]
          : (e.target === search ? items[0] : null);
        if (picked) {
          e.preventDefault();
          const scopeId = picked.getAttribute('data-scope');
          scopeSetMenuOpen(false);
          if (scopeId && scopeOnSelectCb) scopeOnSelectCb(scopeId);
        }
      }
    });
  }

  document.addEventListener('click', function (e) {
    if (!state.scopeMenuOpen) return;
    if (picker && !picker.contains(e.target)) {
      scopeSetMenuOpen(false);
    }
  });
}
