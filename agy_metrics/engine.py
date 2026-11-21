"""Incremental transcript and hook telemetry coordinator."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import statistics
import threading
import time
from typing import Any

from .metadata import MetadataIndex
from .models import (
    ALL_CONVERSATIONS_ID,
    ConversationState,
    PROJECT_SCOPE_PREFIX,
    StepMetrics,
    UNASSIGNED_PROJECT_ID,
    UNASSIGNED_PROJECT_NAME,
    VALID_WINDOWS,
    _FIT_MAX_RECENT_STEPS,
    _FIT_ORIGIN_MAX_CACHE_TOK,
    _FIT_ORIGIN_MAX_IN_TOK,
    _FIT_PREFILL_HIGH_TOK,
    _LIVE_GENERATING_STALE_MS,
    _MAX_HYDRATED_CONVS,
    _SAFE_CONV_RE,
    _WINDOW_SECONDS,
    _is_unattributed_model,
    _safe_int,
    normalize_step_model,
)
from . import parser as _parser
from .paths import get_app_data_dir, get_brain_dir, get_projects_dir, get_state_dir
from .pricing import PricingCatalog
from .roofline import aggregate_steps, empty_totals
from .storage import MetricsStore

__all__ = [
    "ALL_CONVERSATIONS_ID",
    "PROJECT_SCOPE_PREFIX",
    "UNASSIGNED_PROJECT_ID",
    "UNASSIGNED_PROJECT_NAME",
    "VALID_WINDOWS",
    "_MAX_HYDRATED_CONVS",
    "ConversationState",
    "MetricsEngine",
    "StepMetrics",
]


class MetricsEngine:
    """Thread-safe incremental telemetry aggregator over Antigravity local state."""

    def __init__(
        self,
        brain_dir: Path | None = None,
        state_dir: Path | None = None,
        as_of_date: str | None = None,
        orphan_hook_grace_s: float = 300.0,
        app_data_dir: Path | None = None,
        projects_dir: Path | None = None,
    ) -> None:
        self.brain_dir = Path(brain_dir) if brain_dir is not None else get_brain_dir()
        self.state_dir = Path(state_dir) if state_dir is not None else get_state_dir()
        if app_data_dir is not None:
            self.app_data_dir = Path(app_data_dir)
        elif brain_dir is not None:
            self.app_data_dir = self.brain_dir.parent
        else:
            self.app_data_dir = get_app_data_dir()
        if projects_dir is not None:
            self.projects_dir = Path(projects_dir)
        elif brain_dir is not None and "AGY_METRICS_PROJECTS_DIR" not in os.environ:
            self.projects_dir = self.app_data_dir / "config" / "projects"
        else:
            self.projects_dir = get_projects_dir(self.app_data_dir)
        self._orphan_hook_grace_s = max(0.0, float(orphan_hook_grace_s))
        self._lock = threading.RLock()
        self._conversations: dict[str, ConversationState] = {}
        self._latest_json_sig: tuple[int, int] | None = None
        self._latest_record: dict[str, Any] | None = None
        self._initial_scan_done = False
        self._fit_compute_count: int = 0
        self._transcript_open_count: int = 0
        self._db_closed = False
        self._hot_conv_ids: dict[str, float] = {}
        self._last_full_scan_mono: float = 0.0
        self._dir_mtimes: tuple[int, int] = (0, 0)
        self._state_rev: int = 0
        self._convs_cache: tuple[int, dict[str, Any]] | None = None
        self._metrics_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._metadata = MetadataIndex(self.app_data_dir, self.projects_dir)
        self._pricing = PricingCatalog(state_dir=self.state_dir, as_of_date=as_of_date)
        self._store = MetricsStore(self.state_dir)
        try:
            self._conversations = self._store.load_persisted_summaries()
        except sqlite3.Error:
            self._conversations.clear()
            try:
                self._store.reset_fresh_disk_db()
                self._conversations = self._store.load_persisted_summaries()
            except sqlite3.Error:
                self._conversations.clear()
                self._recover_db_to_memory()

    @property
    def _db(self) -> sqlite3.Connection:
        return self._store.db

    @_db.setter
    def _db(self, conn: sqlite3.Connection) -> None:
        self._store.db = conn

    def close(self) -> None:
        with self._lock:
            if getattr(self, "_db_closed", False):
                return
            store = getattr(self, "_store", None)
            if store is not None:
                store.close()
            self._db_closed = True

    def __del__(self) -> None:
        try:
            if not getattr(self, "_db_closed", False):
                store = getattr(self, "_store", None)
                if store is not None:
                    store.close()
                self._db_closed = True
        except Exception:
            pass

    def _bump_state_rev(self) -> None:
        self._state_rev += 1
        self._convs_cache = None
        self._metrics_cache.clear()

    @staticmethod
    def _safe_dir_mtime_ns(path: Path) -> int:
        try:
            return int(path.stat().st_mtime_ns)
        except OSError:
            return 0

    @staticmethod
    def _stat_sig(path: Path | None) -> tuple[str, int, int, int]:
        if path is None:
            return ("", 0, 0, 0)
        try:
            st = path.stat()
        except OSError:
            return ("", 0, 0, 0)
        return (str(path), int(st.st_ino), int(st.st_size), int(st.st_mtime_ns))

    def _current_file_sig(
        self, conv: ConversationState
    ) -> tuple[str, int, int, int, str, int, int, int]:
        t_sig = self._stat_sig(self._resolve_transcript_path(conv))
        h_sig = self._stat_sig(self._resolve_hook_path(conv.conversation_id))
        return (*t_sig, *h_sig)

    def _recover_db_to_memory(self) -> None:
        self._store.reset_to_memory_db()
        self._db_closed = False
        self._initial_scan_done = False
        self._last_full_scan_mono = 0.0
        self._bump_state_rev()
        for conv in self._conversations.values():
            conv.persisted_sig = None
            conv.persisted_steps_fp = None
        for conv in list(self._conversations.values()):
            if conv.hydrated:
                self._persist_conversation_rollup(conv, _allow_recover=False)

    def _delete_conversation_rollup(self, cid: str) -> None:
        self._store.delete_conversation_rollup(
            cid, on_error=self._recover_db_to_memory
        )

    def _persist_conversation_rollup(
        self, conv: ConversationState, _allow_recover: bool = True
    ) -> None:
        file_sig = self._current_file_sig(conv)

        def _on_recover() -> None:
            self._recover_db_to_memory()
            self._persist_conversation_rollup(conv, _allow_recover=False)

        self._store.persist_conversation_rollup(
            conv,
            file_sig,
            on_recover=_on_recover if _allow_recover else None,
            _allow_recover=_allow_recover,
        )

    def _ensure_hydrated(self, conv: ConversationState) -> None:
        if not conv.hydrated:
            had_sig = conv.persisted_sig
            conv.hydrated = True
            self._ingest_hooks(conv)
            self._ingest_transcript(conv)
            self._recompute_conversation(conv)
            if self._current_file_sig(conv) != had_sig:
                self._persist_conversation_rollup(conv)
        conv.last_accessed_mono = time.monotonic()

    def _evict_idle_hydrated_convs(self, protected_ids: set[str]) -> None:
        hydrated = [c for c in self._conversations.values() if c.hydrated]
        if len(hydrated) <= _MAX_HYDRATED_CONVS:
            return
        candidates = [
            c
            for c in hydrated
            if c.conversation_id not in protected_ids
            and c.status != "generating"
            and c.persisted_sig is not None
        ]
        candidates.sort(key=lambda c: (c.last_accessed_mono, c.updated_at_ms))
        excess = len(hydrated) - _MAX_HYDRATED_CONVS
        for c in candidates[:excess]:
            c.dehydrate()

    def _get_or_create_conv(self, conv_id: str) -> ConversationState:
        conv = self._conversations.get(conv_id)
        if conv is None:
            conv = ConversationState(conversation_id=conv_id)
            self._conversations[conv_id] = conv
        return conv

    def _resolve_transcript_path(self, conv: ConversationState) -> Path | None:
        logs_dir = (
            self.brain_dir / conv.conversation_id / ".system_generated" / "logs"
        )
        full_path = logs_dir / "transcript_full.jsonl"
        if full_path.is_file():
            return full_path
        compact_path = logs_dir / "transcript.jsonl"
        if compact_path.is_file():
            return compact_path
        if conv.hook_reported_transcript:
            reported = Path(conv.hook_reported_transcript)
            if reported.name == "transcript.jsonl":
                sibling_full = reported.with_name("transcript_full.jsonl")
                if sibling_full.is_file():
                    return sibling_full
            if reported.is_file():
                return reported
        return None

    def _resolve_hook_path(self, conv_id: str) -> Path | None:
        p = self.state_dir / "conversations" / f"{conv_id}.hooks.jsonl"
        return p if p.is_file() else None

    def _read_latest_json(self) -> bool:
        latest_path = self.state_dir / "latest.json"
        try:
            st = latest_path.stat()
        except OSError:
            changed = self._latest_record is not None
            self._latest_json_sig = None
            self._latest_record = None
            return changed

        sig = (st.st_mtime_ns, st.st_size)
        if sig == self._latest_json_sig:
            return False

        try:
            data = json.loads(latest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False

        if isinstance(data, dict):
            self._latest_json_sig = sig
            self._latest_record = data
            return True
        return False

    def is_generating(self) -> bool:
        with self._lock:
            now_ms = int(time.time() * 1000)
            if self._latest_record and self._latest_record.get("status") == "generating":
                rec_cid = str(self._latest_record.get("conversation_id") or "")
                rec_ts = _safe_int(self._latest_record.get("ts_ms"))
                if 0 <= (now_ms - rec_ts) <= _LIVE_GENERATING_STALE_MS:
                    conv_obj = self._conversations.get(rec_cid) if rec_cid else None
                    if conv_obj is None or not conv_obj.hydrated:
                        return True
                    if conv_obj.status == "generating":
                        return True
            return any(c.status == "generating" for c in self._conversations.values())

    def _is_live_generating_latest(self, cid: str, now_ms: int) -> bool:
        rec = self._latest_record
        if not rec or rec.get("conversation_id") != cid:
            return False
        if rec.get("status") != "generating":
            return False
        rec_ts = _safe_int(rec.get("ts_ms"))
        return 0 <= (now_ms - rec_ts) <= _LIVE_GENERATING_STALE_MS

    def _discover_conversation_ids(
        self, cleanup_orphans: bool = False
    ) -> tuple[set[str], list[str]]:
        """Discover active conversation IDs and optionally prune expired orphan hooks."""
        now_s = time.time()
        now_ms = int(now_s * 1000)
        brain_ids: set[str] = set()
        if self.brain_dir.is_dir():
            try:
                for entry in self.brain_dir.iterdir():
                    if entry.is_dir() and _SAFE_CONV_RE.fullmatch(entry.name):
                        logs_dir = entry / ".system_generated" / "logs"
                        if (logs_dir / "transcript_full.jsonl").is_file() or (
                            logs_dir / "transcript.jsonl"
                        ).is_file():
                            brain_ids.add(entry.name)
            except OSError:
                pass

        ids: set[str] = set(brain_ids)
        pruned_orphans: list[str] = []
        hook_active_ids: set[str] = set()

        hooks_dir = self.state_dir / "conversations"
        if hooks_dir.is_dir():
            try:
                for entry in hooks_dir.iterdir():
                    if not (entry.is_file() and entry.name.endswith(".hooks.jsonl")):
                        continue
                    cid = entry.name[: -len(".hooks.jsonl")]
                    if not _SAFE_CONV_RE.fullmatch(cid):
                        continue
                    if cid in brain_ids or (self.brain_dir / cid).is_dir():
                        ids.add(cid)
                        hook_active_ids.add(cid)
                        continue
                    if self._is_live_generating_latest(cid, now_ms):
                        ids.add(cid)
                        hook_active_ids.add(cid)
                        continue
                    try:
                        st = entry.stat()
                    except OSError:
                        continue
                    age_s = now_s - st.st_mtime
                    if age_s <= self._orphan_hook_grace_s:
                        ids.add(cid)
                        hook_active_ids.add(cid)
                    elif cleanup_orphans:
                        try:
                            entry.unlink(missing_ok=True)
                        except OSError:
                            pass
                        pruned_orphans.append(cid)
            except OSError:
                pass

        if self._latest_record:
            latest_cid = str(self._latest_record.get("conversation_id") or "")
            if (
                latest_cid
                and _SAFE_CONV_RE.fullmatch(latest_cid)
                and (
                    latest_cid in ids
                    or latest_cid in hook_active_ids
                    or (self.brain_dir / latest_cid).is_dir()
                    or self._is_live_generating_latest(latest_cid, now_ms)
                )
            ):
                ids.add(latest_cid)

        return ids, pruned_orphans

    def _ingest_hooks(self, conv: ConversationState) -> bool:
        return _parser.ingest_hooks(conv, self._resolve_hook_path(conv.conversation_id))

    def _ingest_transcript(self, conv: ConversationState) -> bool:
        now_ms = int(time.time() * 1000)

        def _on_open() -> None:
            self._transcript_open_count += 1

        return _parser.ingest_transcript(
            conv,
            self._resolve_transcript_path(conv),
            initial_scan_done=self._initial_scan_done,
            now_ms=now_ms,
            on_open=_on_open,
        )

    def _recompute_conversation(self, conv: ConversationState) -> None:
        now_ms = int(time.time() * 1000)
        _parser.recompute_conversation(conv, self._latest_record, now_ms)

    def _link_subagents(self) -> None:
        def _on_hint_changed(child: ConversationState) -> None:
            if not child.hydrated:
                self._ensure_hydrated(child)
            else:
                self._recompute_conversation(child)
            self._persist_conversation_rollup(child)

        _parser.link_subagents(self._conversations, _on_hint_changed)

    def _expand_related_ids(self, seed_id: str | None) -> set[str]:
        return _parser.expand_related_ids(self._conversations, seed_id)

    def _sync_conversations_metadata(self) -> bool:
        ext_map = self._metadata.conversations
        ext_by_lower = {k.lower(): v for k, v in ext_map.items()}
        prev_snap = {
            cid: (
                c.title,
                c.parent_id,
                c.project_id,
                c.project_name,
                c.agent_name,
                c.nesting_depth,
            )
            for cid, c in self._conversations.items()
        }

        for conv in self._conversations.values():
            ext = ext_map.get(conv.conversation_id) or ext_by_lower.get(
                conv.conversation_id.lower()
            )
            if ext is not None:
                conv.external_title = ext.title
                conv.agent_name = ext.agent_name
                conv.external_parent_id = ext.parent_id
                conv.external_project_id = ext.project_id
                conv.external_workspace_paths = list(ext.workspace_paths)
            else:
                conv.external_title = ""
                conv.agent_name = ""
                conv.external_parent_id = ""
                conv.external_project_id = ""
                conv.external_workspace_paths = []

        self._link_subagents()

        def _resolve_own_project(c: ConversationState) -> tuple[str, str]:
            if c.external_project_id:
                proj = self._metadata.projects.get(c.external_project_id)
                if proj is not None:
                    return (proj.project_id, proj.name)
            all_paths = [*c.external_workspace_paths, *c.hook_workspace_paths]
            pid, pname = self._metadata.resolve_project_for_paths(all_paths)
            if pid:
                return (pid, pname)
            if c.external_project_id:
                return (c.external_project_id, c.external_project_id[:8])
            return ("", "")

        for conv in self._conversations.values():
            if conv.external_title:
                conv.title = conv.external_title
            elif conv.parent_id:
                parent = self._conversations.get(conv.parent_id)
                role = ""
                if parent is not None:
                    role = parent.child_subagents.get(conv.conversation_id, "")
                    if not role:
                        low_cid = conv.conversation_id.lower()
                        for k, v in parent.child_subagents.items():
                            if k.lower() == low_cid:
                                role = v
                                break
                if role and role != "Subagent":
                    conv.title = role
                elif conv.prompt_title:
                    conv.title = conv.prompt_title
                elif conv.agent_name:
                    conv.title = f"Subagent · {conv.agent_name}"
                else:
                    conv.title = conv.conversation_id[:8]
            else:
                conv.title = conv.prompt_title or conv.conversation_id[:8]

        for conv in self._conversations.values():
            if conv.parent_id is None:
                conv.project_id, conv.project_name = _resolve_own_project(conv)

        for conv in self._conversations.values():
            if conv.parent_id is not None:
                root_id = _parser.root_ancestor_id(
                    self._conversations, conv.conversation_id
                )
                root_conv = self._conversations.get(root_id)
                if root_conv is not None and root_conv.project_id:
                    conv.project_id = root_conv.project_id
                    conv.project_name = root_conv.project_name
                else:
                    conv.project_id, conv.project_name = _resolve_own_project(conv)

        meta_changed = False
        title_updates: list[tuple[str, str]] = []
        for cid, conv in self._conversations.items():
            cur = (
                conv.title,
                conv.parent_id,
                conv.project_id,
                conv.project_name,
                conv.agent_name,
                conv.nesting_depth,
            )
            old = prev_snap.get(cid)
            if old != cur:
                meta_changed = True
                if old is not None and old[0] != conv.title and conv.persisted_sig is not None:
                    title_updates.append((conv.title, cid))

        if title_updates:
            self._store.update_persisted_titles(title_updates)
        return meta_changed

    def refresh_if_changed(
        self,
        target_id: str | None = None,
        fast_poll: bool = False,
    ) -> bool:
        with self._lock:
            latest_changed = self._read_latest_json()
            changed = latest_changed
            if self._pricing.refresh_if_changed():
                changed = True
            if self._metadata.refresh_if_changed():
                changed = True
            latest_cid = (
                str(self._latest_record.get("conversation_id") or "")
                if self._latest_record
                else ""
            )
            is_aggregate_target = (
                not target_id
                or target_id == ALL_CONVERSATIONS_ID
                or target_id.startswith(PROJECT_SCOPE_PREFIX)
            )
            real_target_id = None if is_aggregate_target else target_id
            now_mono = time.monotonic()
            now_ms = int(time.time() * 1000)

            cur_dir_mtimes = (
                self._safe_dir_mtime_ns(self.brain_dir),
                self._safe_dir_mtime_ns(self.state_dir / "conversations"),
            )
            sweep_interval = 2.5 if fast_poll else 2.0
            do_full_sweep = (
                not self._initial_scan_done
                or cur_dir_mtimes != self._dir_mtimes
                or (now_mono - self._last_full_scan_mono) >= sweep_interval
            )

            stale_hot = [
                cid
                for cid, ts in self._hot_conv_ids.items()
                if (now_mono - ts) > 120.0
                and cid in self._conversations
                and self._conversations[cid].status != "generating"
            ]
            for cid in stale_hot:
                self._hot_conv_ids.pop(cid, None)

            if do_full_sweep:
                conv_ids, pruned_orphans = self._discover_conversation_ids(
                    cleanup_orphans=True
                )
                removed = [
                    cid
                    for cid in self._conversations.keys()
                    if cid not in conv_ids
                ]
                removed_set = set(removed) | set(pruned_orphans)
                for cid in removed_set:
                    self._conversations.pop(cid, None)
                    self._hot_conv_ids.pop(cid, None)
                    self._delete_conversation_rollup(cid)
                    changed = True
                candidate_ids = conv_ids
                self._dir_mtimes = cur_dir_mtimes
                self._last_full_scan_mono = now_mono
            else:
                candidate_ids = set(self._hot_conv_ids.keys())
                candidate_ids.update(self._expand_related_ids(latest_cid))
                candidate_ids.update(self._expand_related_ids(real_target_id))

            def _cand_sort_key(cid: str) -> tuple[int, int, str]:
                c_obj = self._conversations.get(cid)
                if c_obj is not None and c_obj.persisted_sig is not None:
                    return (0, 0, cid)
                probe = (
                    c_obj
                    if c_obj is not None
                    else ConversationState(conversation_id=cid)
                )
                t_mtime = self._stat_sig(self._resolve_transcript_path(probe))[3]
                if t_mtime == 0:
                    t_mtime = self._stat_sig(self._resolve_hook_path(cid))[3]
                return (1, t_mtime, cid)

            for cid in sorted(candidate_ids, key=_cand_sort_key):
                conv = self._get_or_create_conv(cid)
                if not conv.hydrated and conv.persisted_sig is not None:
                    if self._current_file_sig(conv) == conv.persisted_sig:
                        if cid == latest_cid and (
                            latest_changed or not self._initial_scan_done
                        ):
                            rec = self._latest_record
                            rec_ts = _safe_int(rec.get("ts_ms")) if rec else 0
                            if (
                                rec
                                and rec.get("status") == "generating"
                                and 0 <= (now_ms - rec_ts) <= _LIVE_GENERATING_STALE_MS
                            ):
                                conv.status = "generating"
                                conv.generating_since_ms = rec_ts
                                self._hot_conv_ids[cid] = now_mono
                            elif conv.status == "generating":
                                conv.status = "idle"
                                conv.generating_since_ms = None
                        continue
                    conv.hydrated = True

                hook_changed = self._ingest_hooks(conv)
                tr_changed = self._ingest_transcript(conv)
                if (
                    hook_changed
                    or tr_changed
                    or (latest_changed and cid == latest_cid)
                    or conv.persisted_sig is None
                ):
                    self._recompute_conversation(conv)
                    self._persist_conversation_rollup(conv)
                    self._hot_conv_ids[cid] = now_mono
                    changed = True

            if changed or not self._initial_scan_done:
                if self._sync_conversations_metadata():
                    changed = True
                for conv in self._conversations.values():
                    if conv.parent_id is not None:
                        continue
                    for cid, _, _ in _parser.collect_descendant_subagents(
                        self._conversations, conv.conversation_id
                    ):
                        child = self._conversations.get(cid)
                        if child is not None:
                            conv.updated_at_ms = max(
                                conv.updated_at_ms, child.updated_at_ms
                            )
                self._initial_scan_done = True

            protected_ids = self._expand_related_ids(latest_cid)
            protected_ids.update(self._expand_related_ids(real_target_id))
            self._evict_idle_hydrated_convs(protected_ids)

            if changed:
                self._bump_state_rev()

            return changed

    def _select_active_conversation_id(self) -> str | None:
        if not self._conversations:
            return None

        if self._latest_record:
            cid = str(self._latest_record.get("conversation_id") or "")
            if cid in self._conversations:
                return _parser.root_ancestor_id(self._conversations, cid)

        top_level = [
            c for c in self._conversations.values() if c.parent_id is None
        ]
        pool = top_level if top_level else list(self._conversations.values())
        pool.sort(
            key=lambda c: (
                c.updated_at_ms,
                1 if (c.steps if c.hydrated else c.summary_steps_count) else 0,
                c.conversation_id,
            ),
            reverse=True,
        )
        return pool[0].conversation_id if pool else None

    def list_conversations(self, refresh: bool = True) -> dict[str, Any]:
        with self._lock:
            if refresh:
                self.refresh_if_changed()
            if (
                self._convs_cache is not None
                and self._convs_cache[0] == self._state_rev
            ):
                return dict(self._convs_cache[1])
            active_id = self._select_active_conversation_id()
            active_conv = self._conversations.get(active_id) if active_id else None
            active_project_id = (
                (active_conv.project_id or UNASSIGNED_PROJECT_ID)
                if active_conv is not None
                else None
            )

            proj_stats: dict[str, dict[str, Any]] = {}
            top_by_proj: dict[str, list[ConversationState]] = {}
            children_by_parent: dict[str, list[ConversationState]] = {}

            for c in self._conversations.values():
                eff_pid = c.project_id or UNASSIGNED_PROJECT_ID
                eff_pname = c.project_name or UNASSIGNED_PROJECT_NAME
                bucket = proj_stats.get(eff_pid)
                if bucket is None:
                    bucket = {
                        "project_id": eff_pid,
                        "name": eff_pname,
                        "conversations_count": 0,
                        "subagents_count": 0,
                        "updated_at_ms": c.updated_at_ms,
                        "is_unassigned": eff_pid == UNASSIGNED_PROJECT_ID,
                    }
                    proj_stats[eff_pid] = bucket
                else:
                    bucket["updated_at_ms"] = max(
                        int(bucket["updated_at_ms"]), c.updated_at_ms
                    )
                if c.parent_id is None or c.parent_id not in self._conversations:
                    bucket["conversations_count"] += 1
                    top_by_proj.setdefault(eff_pid, []).append(c)
                else:
                    bucket["subagents_count"] += 1
                    children_by_parent.setdefault(c.parent_id, []).append(c)

            projects_list = sorted(
                proj_stats.values(),
                key=lambda p: (
                    1 if p["is_unassigned"] else 0,
                    -int(p["updated_at_ms"]),
                    str(p["name"]).lower(),
                    str(p["project_id"]),
                ),
            )
            for child_list in children_by_parent.values():
                child_list.sort(key=lambda c: (-c.updated_at_ms, c.conversation_id))

            ordered_items: list[ConversationState] = []
            visited_cids: set[str] = set()

            def _emit_subtree(node: ConversationState) -> None:
                if node.conversation_id in visited_cids:
                    return
                visited_cids.add(node.conversation_id)
                ordered_items.append(node)
                for child in children_by_parent.get(node.conversation_id, ()):
                    _emit_subtree(child)

            for proj in projects_list:
                tops = top_by_proj.get(str(proj["project_id"]), [])
                tops.sort(key=lambda c: (-c.updated_at_ms, c.conversation_id))
                for root_conv in tops:
                    _emit_subtree(root_conv)

            for c in sorted(
                self._conversations.values(),
                key=lambda x: (-x.updated_at_ms, x.conversation_id),
            ):
                _emit_subtree(c)

            db_steps_by_cid = {
                str(r[0]): int(r[1] or 0)
                for r in self._db.execute(
                    "SELECT conv_id, SUM(steps_count) FROM conv_model_totals GROUP BY conv_id"
                ).fetchall()
            }
            payload = {
                "active_conversation_id": active_id,
                "active_project_id": active_project_id,
                "projects": projects_list,
                "conversations": [
                    {
                        "conversation_id": c.conversation_id,
                        "title": c.title,
                        "prompt_preview": c.prompt_title,
                        "agent_name": c.agent_name,
                        "project_id": c.project_id or UNASSIGNED_PROJECT_ID,
                        "project_name": c.project_name or UNASSIGNED_PROJECT_NAME,
                        "model_name": c.current_model_name,
                        "status": c.status,
                        "planner_steps_count": (
                            len(c.steps)
                            if c.hydrated
                            else (
                                db_steps_by_cid.get(c.conversation_id)
                                or c.summary_steps_count
                            )
                        ),
                        "current_tokens": c.current_tokens,
                        "updated_at_ms": c.updated_at_ms,
                        "is_subagent": c.parent_id is not None,
                        "parent_id": c.parent_id,
                        "root_id": _parser.root_ancestor_id(
                            self._conversations, c.conversation_id
                        ),
                        "nesting_depth": c.nesting_depth,
                    }
                    for c in ordered_items
                ],
            }
            self._convs_cache = (self._state_rev, payload)
            return dict(payload)

    def _get_global_model_fit_and_sync_delays(
        self, model_name: str
    ) -> dict[str, Any]:
        def _on_fit() -> None:
            self._fit_compute_count += 1

        return self._store.get_global_model_fit_and_sync_delays(
            model_name, on_fit_computed=_on_fit
        )

    def _step_lite_row_to_dict(self, row: tuple[Any, ...]) -> dict[str, Any]:
        (
            conv_id,
            ordinal,
            step_index,
            created_at,
            _epoch_s,
            model_name,
            is_checkpoint,
            in_tok,
            cache_tok,
            out_tok,
            think_tok,
            vis_tok,
            dur_s,
            dur_src,
            tool_exec_s,
            cold,
            compacted,
            raw_est_delay,
            retry_wait_s,
            retry_count,
        ) = row
        total_ctx = int(in_tok) + int(cache_tok)
        cache_hit = (
            round(int(cache_tok) / total_ctx * 100.0, 1) if total_ctx > 0 else 0.0
        )
        eff_tps = (
            round(int(out_tok) / float(dur_s), 1)
            if (not is_checkpoint and dur_s is not None and float(dur_s) > 0.0)
            else None
        )
        est_delay_s = (
            round(float(raw_est_delay), 2) if raw_est_delay is not None else None
        )

        conv = self._conversations.get(str(conv_id))
        step_val = self._pricing.value_step(
            str(model_name), int(in_tok), int(cache_tok), int(out_tok)
        )
        return {
            "ordinal": int(ordinal),
            "step_index": int(step_index),
            "created_at": str(created_at),
            "model_name": str(model_name),
            "input_tokens": int(in_tok),
            "cache_read_tokens": int(cache_tok),
            "total_context": total_ctx,
            "cache_hit_pct": cache_hit,
            "output_tokens": int(out_tok),
            "est_thinking_tokens": int(think_tok),
            "est_visible_tokens": int(vis_tok),
            "duration_s": float(dur_s) if dur_s is not None else None,
            "duration_source": dur_src,
            "effective_tps": eff_tps,
            "tool_exec_s": float(tool_exec_s),
            "tool_names": [],
            "compacted": bool(compacted),
            "cold_cache_spike": bool(cold),
            "model_switched": False,
            "step_kind": "checkpoint" if is_checkpoint else "planner",
            "retry_wait_s": round(float(retry_wait_s or 0.0), 2),
            "retry_count": int(retry_count or 0),
            "est_delay_s": est_delay_s,
            "conv_id": str(conv_id),
            "conv_title": conv.title if conv is not None else str(conv_id)[:8],
            **step_val,
        }

    def _query_model_scatter_steps(
        self,
        m_name: str,
        global_fit: dict[str, Any],
        *,
        win_sql: str = "",
        win_args: tuple[Any, ...] = (),
        scope_sql: str = "",
        scope_args: tuple[Any, ...] = (),
        est_delay_count: int = 0,
    ) -> tuple[list[dict[str, Any]], list[float]]:
        """Query stratified timed steps for roofline scatter plotting and median delay."""
        scatter_rows_desc = self._db.execute(
            f"""
            SELECT conv_id, ordinal, step_index, epoch_s,
                   input_tokens, cache_read_tokens, output_tokens, duration_s,
                   duration_source, cold_cache_spike, est_delay_s,
                   COALESCE(retry_wait_s, 0.0)
            FROM step_lite
            WHERE model_name = ?
              AND is_checkpoint = 0
              AND duration_s IS NOT NULL
              AND duration_s > 0
              {win_sql}
              {scope_sql}
            ORDER BY epoch_s DESC, conv_id DESC, ordinal DESC
            LIMIT ?
            """,
            (m_name, *win_args, *scope_args, _FIT_MAX_RECENT_STEPS),
        ).fetchall()
        if len(scatter_rows_desc) == _FIT_MAX_RECENT_STEPS and (
            global_fit.get("prefill_available")
            or global_fit.get("cache_available")
        ):
            anchor_hip = self._db.execute(
                f"""
                SELECT conv_id, ordinal, step_index, epoch_s,
                       input_tokens, cache_read_tokens, output_tokens, duration_s,
                       duration_source, cold_cache_spike, est_delay_s,
                       COALESCE(retry_wait_s, 0.0)
                FROM step_lite
                WHERE model_name = ?
                  AND is_checkpoint = 0
                  AND duration_s IS NOT NULL
                  AND duration_s > 0
                  AND input_tokens >= ?
                  {win_sql}
                  {scope_sql}
                ORDER BY epoch_s DESC, conv_id DESC, ordinal DESC
                LIMIT 20
                """,
                (m_name, _FIT_PREFILL_HIGH_TOK, *win_args, *scope_args),
            ).fetchall()
            anchor_orig = self._db.execute(
                f"""
                SELECT conv_id, ordinal, step_index, epoch_s,
                       input_tokens, cache_read_tokens, output_tokens, duration_s,
                       duration_source, cold_cache_spike, est_delay_s,
                       COALESCE(retry_wait_s, 0.0)
                FROM step_lite
                WHERE model_name = ?
                  AND is_checkpoint = 0
                  AND duration_s IS NOT NULL
                  AND duration_s > 0
                  AND input_tokens < ?
                  AND cache_read_tokens < ?
                  {win_sql}
                  {scope_sql}
                ORDER BY epoch_s DESC, conv_id DESC, ordinal DESC
                LIMIT 15
                """,
                (
                    m_name,
                    _FIT_ORIGIN_MAX_IN_TOK,
                    _FIT_ORIGIN_MAX_CACHE_TOK,
                    *win_args,
                    *scope_args,
                ),
            ).fetchall()
            selected_map: dict[tuple[str, int], tuple[Any, ...]] = {}
            for ar in (*anchor_hip, *anchor_orig):
                selected_map[(str(ar[0]), int(ar[1]))] = ar
            for sr in scatter_rows_desc:
                if len(selected_map) >= _FIT_MAX_RECENT_STEPS:
                    break
                k = (str(sr[0]), int(sr[1]))
                if k not in selected_map:
                    selected_map[k] = sr
            scatter_rows = sorted(
                selected_map.values(),
                key=lambda r: (float(r[3]), str(r[0]), int(r[1])),
            )
        else:
            scatter_rows = list(reversed(scatter_rows_desc))

        scatter_steps: list[dict[str, Any]] = []
        modeled_delays: list[float] = []
        for seq_i, sr in enumerate(scatter_rows, start=1):
            (
                s_cid,
                s_ord,
                s_idx,
                _s_ep,
                s_in,
                s_cac,
                s_out,
                s_dur,
                s_src,
                s_cold,
                s_delay,
                s_retry,
            ) = sr
            c_obj = self._conversations.get(str(s_cid))
            s_dur_f = float(s_dur)
            s_out_i = int(s_out)
            s_retry_f = max(0.0, float(s_retry or 0.0))
            s_delay_f = round(float(s_delay), 2) if s_delay is not None else None
            if s_out_i > 0 and s_delay_f is not None:
                modeled_delays.append(round(s_delay_f + s_retry_f, 2))
            scatter_steps.append(
                {
                    "seq": seq_i,
                    "ordinal": int(s_ord),
                    "step_index": int(s_idx),
                    "model_name": m_name,
                    "input_tokens": int(s_in),
                    "cache_read_tokens": int(s_cac),
                    "output_tokens": s_out_i,
                    "duration_s": s_dur_f,
                    "duration_source": s_src,
                    "effective_tps": round(s_out_i / s_dur_f, 1),
                    "cold_cache_spike": bool(s_cold),
                    "step_kind": "planner",
                    "est_delay_s": s_delay_f,
                    "retry_wait_s": round(s_retry_f, 2),
                    "conv_id": str(s_cid),
                    "conv_title": (
                        c_obj.title if c_obj is not None else str(s_cid)[:8]
                    ),
                }
            )

        if not modeled_delays and est_delay_count > 0:
            fb_rows = self._db.execute(
                f"""
                SELECT est_delay_s, COALESCE(retry_wait_s, 0.0)
                FROM step_lite
                WHERE model_name = ?
                  AND is_checkpoint = 0
                  AND est_delay_s IS NOT NULL
                  {win_sql}
                  {scope_sql}
                ORDER BY epoch_s DESC, conv_id DESC, ordinal DESC
                LIMIT ?
                """,
                (m_name, *win_args, *scope_args, _FIT_MAX_RECENT_STEPS),
            ).fetchall()
            modeled_delays = [
                round(float(r[0]) + max(0.0, float(r[1] or 0.0)), 2) for r in fb_rows
            ]

        return scatter_steps, modeled_delays

    def _get_all_conversations_metrics(
        self, window: str = "all", project_id: str | None = None
    ) -> dict[str, Any]:
        norm_win = window if window in VALID_WINDOWS else "all"
        now_s = time.time()
        cutoff_epoch: float | None = (
            now_s - _WINDOW_SECONDS[norm_win] if norm_win != "all" else None
        )

        if project_id is None:
            scoped_cids: list[str] | None = None
            scoped_convs = list(self._conversations.values())
            mode = "all"
            resp_conv_id = ALL_CONVERSATIONS_ID
            resp_title = "All Conversations"
            project_name = ""
        else:
            scoped_convs = [
                c
                for c in self._conversations.values()
                if (c.project_id or UNASSIGNED_PROJECT_ID) == project_id
            ]
            scoped_cids = [c.conversation_id for c in scoped_convs]
            mode = "project"
            resp_conv_id = f"{PROJECT_SCOPE_PREFIX}{project_id}"
            proj_meta = self._metadata.projects.get(project_id)
            if proj_meta is not None:
                project_name = proj_meta.name
            elif scoped_convs:
                project_name = next(
                    (c.project_name for c in scoped_convs if c.project_name),
                    UNASSIGNED_PROJECT_NAME
                    if project_id == UNASSIGNED_PROJECT_ID
                    else project_id,
                )
            elif project_id == UNASSIGNED_PROJECT_ID:
                project_name = UNASSIGNED_PROJECT_NAME
            else:
                project_name = "Unknown Project"
            resp_title = project_name

        def _build_all_valuation(
            selected_val_rows: list[dict[str, Any]],
        ) -> dict[str, Any]:
            sel_val = self._pricing.build_scoped_valuation(selected_val_rows)
            val_5h = (
                dict(sel_val)
                if norm_win == "5h"
                else self._pricing.build_scoped_valuation(
                    self._store.query_window_val_rows(
                        now_s - _WINDOW_SECONDS["5h"], conv_ids=scoped_cids
                    )
                )
            )
            val_7d = (
                dict(sel_val)
                if norm_win == "7d"
                else self._pricing.build_scoped_valuation(
                    self._store.query_window_val_rows(
                        now_s - _WINDOW_SECONDS["7d"], conv_ids=scoped_cids
                    )
                )
            )
            out = dict(sel_val)
            out["windows"] = {
                "5h": val_5h,
                "7d": val_7d,
                "selected": dict(sel_val),
            }
            return out

        eff_status = "idle"
        eff_gen_since: int | None = None
        max_updated_ms = 0
        for conv in scoped_convs:
            if conv.updated_at_ms > max_updated_ms:
                max_updated_ms = conv.updated_at_ms
            if conv.status == "generating":
                if eff_status != "generating":
                    eff_status = "generating"
                    eff_gen_since = conv.generating_since_ms
                elif conv.generating_since_ms is not None and (
                    eff_gen_since is None
                    or conv.generating_since_ms < eff_gen_since
                ):
                    eff_gen_since = conv.generating_since_ms

        active_conv_id = self._select_active_conversation_id()
        active_conv = (
            self._conversations.get(active_conv_id) if active_conv_id else None
        )
        if project_id is not None and (
            active_conv is None
            or (active_conv.project_id or UNASSIGNED_PROJECT_ID) != project_id
        ):
            scoped_sorted = sorted(
                scoped_convs,
                key=lambda c: (c.updated_at_ms, c.conversation_id),
                reverse=True,
            )
            active_model_name = (
                scoped_sorted[0].current_model_name if scoped_sorted else ""
            )
        else:
            active_model_name = active_conv.current_model_name if active_conv else ""

        win_models = self._store.query_window_models(
            cutoff_epoch, conv_ids=scoped_cids
        )
        global_fits: dict[str, dict[str, Any]] = {
            m_name: self._get_global_model_fit_and_sync_delays(m_name)
            for m_name in win_models
        }
        agg_rows, win_conv_ids = self._store.query_all_model_aggregates(
            cutoff_epoch, conv_ids=scoped_cids
        )

        if cutoff_epoch is None:
            compacted_count = sum(c.compacted_count for c in scoped_convs)
            conversations_count = 0
            subagents_count = 0
            if scoped_cids is None:
                active_cids = {
                    str(r[0])
                    for r in self._db.execute(
                        "SELECT DISTINCT conv_id FROM conv_model_totals"
                    ).fetchall()
                }
            elif scoped_cids:
                ph = ",".join("?" for _ in scoped_cids)
                active_cids = {
                    str(r[0])
                    for r in self._db.execute(
                        f"SELECT DISTINCT conv_id FROM conv_model_totals WHERE conv_id IN ({ph})",
                        tuple(scoped_cids),
                    ).fetchall()
                }
            else:
                active_cids = set()
            for c in scoped_convs:
                step_n = len(c.steps) if c.hydrated else c.summary_steps_count
                if step_n <= 0 and c.conversation_id not in active_cids:
                    continue
                if c.parent_id is None:
                    conversations_count += 1
                else:
                    subagents_count += 1
        else:
            compacted_count = sum(int(row[18] or 0) for row in agg_rows)
            conversations_count = 0
            subagents_count = 0
            for cid_val in win_conv_ids or []:
                conv_obj = self._conversations.get(str(cid_val))
                if conv_obj is not None and conv_obj.parent_id is not None:
                    subagents_count += 1
                else:
                    conversations_count += 1

        extra_project_fields: dict[str, Any] = (
            {"project_id": project_id, "project_name": project_name}
            if project_id is not None
            else {}
        )

        if not agg_rows:
            return {
                "mode": mode,
                "window": norm_win,
                "conversation_id": resp_conv_id,
                "title": resp_title,
                **extra_project_fields,
                "model_name": active_model_name,
                "status": eff_status,
                "generating_since_ms": eff_gen_since,
                "updated_at_ms": max_updated_ms,
                "conversations_count": conversations_count,
                "subagents_count": subagents_count,
                "planner_steps_count": 0,
                "latest_step": None,
                "totals": empty_totals(),
                "by_model": [],
                "valuation": _build_all_valuation([]),
                "steps": [],
            }

        win_sql = " AND epoch_s >= ?" if cutoff_epoch is not None else ""
        win_args: tuple[Any, ...] = (
            (cutoff_epoch,) if cutoff_epoch is not None else ()
        )
        if scoped_cids is None:
            scope_sql = ""
            scope_args: tuple[Any, ...] = ()
        elif not scoped_cids:
            scope_sql = " AND 1 = 0"
            scope_args = ()
        else:
            ph = ",".join("?" for _ in scoped_cids)
            scope_sql = f" AND conv_id IN ({ph})"
            scope_args = tuple(scoped_cids)

        pooled_sample_delays: list[float] = []
        by_model: list[dict[str, Any]] = []

        totals = empty_totals()
        total_steps = 0
        total_comp_steps = 0
        total_in = 0
        total_cache = 0
        total_out = 0
        total_think = 0
        total_vis = 0
        total_inf_time = 0.0
        total_wasted_time = 0.0
        total_retry_steps = 0
        total_failed_turns = 0
        total_timed_steps = 0
        total_cold_timed = 0
        total_cold_time = 0.0
        total_timed_out = 0
        total_tool_time = 0.0
        total_warm_timed = 0
        total_est_delay_steps = 0
        total_est_delay_sum = 0.0
        total_cold_delay_steps = 0
        total_cold_delay_sum = 0.0

        for row in agg_rows:
            m_name = str(row[0])
            m_steps = int(row[1] or 0)
            m_comp_steps = int(row[2] or 0)
            m_in = int(row[3] or 0)
            m_cache = int(row[4] or 0)
            m_out = int(row[5] or 0)
            m_think = int(row[6] or 0)
            m_vis = int(row[7] or 0)
            m_inf_time_s = float(row[8] or 0.0)
            m_timed_steps = int(row[9] or 0)
            m_cold_timed = int(row[10] or 0)
            m_cold_time_s = float(row[11] or 0.0)
            m_timed_out = int(row[12] or 0)
            m_tool_s = float(row[13] or 0.0)
            m_warm_timed = int(row[14] or 0)
            m_cold_excl = int(row[15] or 0)
            m_est_delay_count = int(row[16] or 0)
            m_delay_sum = float(row[17] or 0.0)
            m_wasted_s = float(row[19] or 0.0)
            m_retry_steps = int(row[20] or 0)
            m_failed_turns = int(row[21] or 0)
            m_cold_delay_count = int(row[22] or 0)
            m_cold_delay_sum = float(row[23] or 0.0)
            m_warm_delay_count = max(0, m_est_delay_count - m_cold_delay_count)
            m_warm_delay_sum = max(0.0, m_delay_sum - m_cold_delay_sum)

            total_steps += m_steps
            total_comp_steps += m_comp_steps
            total_in += m_in
            total_cache += m_cache
            total_out += m_out
            total_think += m_think
            total_vis += m_vis
            total_inf_time += m_inf_time_s
            total_wasted_time += m_wasted_s
            total_retry_steps += m_retry_steps
            total_failed_turns += m_failed_turns
            total_timed_steps += m_timed_steps
            total_cold_timed += m_cold_timed
            total_cold_time += m_cold_time_s
            total_timed_out += m_timed_out
            total_tool_time += m_tool_s
            total_warm_timed += m_warm_timed
            total_est_delay_steps += m_est_delay_count
            if m_est_delay_count > 0:
                total_est_delay_sum += m_delay_sum
            total_cold_delay_steps += m_cold_delay_count
            if m_cold_delay_count > 0:
                total_cold_delay_sum += m_cold_delay_sum

            m_gross_in = m_in + m_cache
            m_hit_pct = (
                round(m_cache / m_gross_in * 100.0, 1) if m_gross_in > 0 else 0.0
            )
            m_inf_round = round(m_inf_time_s, 2)
            m_wasted_round = round(m_wasted_s, 2)
            m_time_round = round(m_inf_time_s + m_wasted_s, 2)
            m_avg_tps = (
                round(m_timed_out / m_inf_round, 1) if m_inf_round > 0.0 else None
            )

            global_fit = global_fits.get(
                m_name
            ) or self._get_global_model_fit_and_sync_delays(m_name)

            scatter_steps, win_modeled_delays = self._query_model_scatter_steps(
                m_name,
                global_fit,
                win_sql=win_sql,
                win_args=win_args,
                scope_sql=scope_sql,
                scope_args=scope_args,
                est_delay_count=m_est_delay_count,
            )

            if global_fit["available"]:
                pooled_sample_delays.extend(win_modeled_delays)
            fit: dict[str, Any] = dict(global_fit)
            m_avg_delay_s: float | None = (
                fit.get("avg_delay_s") if fit.get("available") else None
            )

            m_est_warm_delay: float | None = (
                round(m_warm_delay_sum, 2) if m_warm_delay_count > 0 else None
            )
            m_est_cold_delay: float | None = (
                round(m_cold_delay_sum, 2) if m_cold_delay_count > 0 else None
            )
            m_est_total_delay: float | None = (
                round(
                    (m_delay_sum if m_est_delay_count > 0 else 0.0) + m_wasted_s, 2
                )
                if (m_est_delay_count > 0 or m_wasted_round > 0.0)
                else None
            )

            latest_m_row = self._db.execute(
                f"""
                SELECT conv_id, ordinal, step_index, created_at, epoch_s, model_name,
                       is_checkpoint, input_tokens, cache_read_tokens, output_tokens,
                       est_thinking_tokens, est_visible_tokens, duration_s,
                       duration_source, tool_exec_s, cold_cache_spike, compacted,
                       est_delay_s, retry_wait_s, retry_count
                FROM step_lite
                WHERE model_name = ? {win_sql} {scope_sql}
                ORDER BY epoch_s DESC, conv_id DESC, ordinal DESC
                LIMIT 1
                """,
                (m_name, *win_args, *scope_args),
            ).fetchone()
            m_latest = (
                self._step_lite_row_to_dict(latest_m_row) if latest_m_row else None
            )

            by_model.append(
                {
                    "model_name": m_name,
                    "is_active": m_name == active_model_name,
                    "first_step_ordinal": 1 if m_steps > 0 else 0,
                    "last_step_ordinal": m_steps,
                    "latest_step": m_latest,
                    "latency_fit": fit,
                    "scatter_steps": scatter_steps,
                    "warm_timed_steps_count": m_warm_timed,
                    "est_delay_steps_count": m_est_delay_count,
                    "cold_delay_steps_count": m_cold_delay_count,
                    "est_warm_delay_s": m_est_warm_delay,
                    "est_cold_delay_s": m_est_cold_delay,
                    "est_total_delay_s": m_est_total_delay,
                    "avg_delay_s": m_avg_delay_s,
                    "steps_count": m_steps,
                    "compaction_steps_count": m_comp_steps,
                    "input_tokens": m_in,
                    "cache_read_tokens": m_cache,
                    "gross_input_tokens": m_gross_in,
                    "cache_hit_pct": m_hit_pct,
                    "output_tokens": m_out,
                    "est_thinking_tokens": m_think,
                    "est_visible_tokens": m_vis,
                    "total_tokens": m_gross_in + m_out,
                    "inference_time_s": m_inf_round,
                    "model_time_s": m_time_round,
                    "wasted_time_s": m_wasted_round,
                    "retry_steps_count": m_retry_steps,
                    "failed_turns_count": m_failed_turns,
                    "timed_steps_count": m_timed_steps,
                    "cold_timed_steps_count": m_cold_timed,
                    "cold_model_time_s": round(m_cold_time_s, 2),
                    "timed_output_tokens": m_timed_out,
                    "avg_effective_tps": m_avg_tps,
                    "tool_time_s": round(m_tool_s, 2),
                    "valuation": self._pricing.value_model_group(
                        m_name, m_steps, m_in, m_cache, m_out
                    ),
                }
            )

        by_model.sort(
            key=lambda bm: (
                1 if _is_unattributed_model(bm["model_name"]) else 0,
                -bm["total_tokens"],
                bm["model_name"],
            )
        )

        gross_in = total_in + total_cache
        inf_time_round = round(total_inf_time, 2)
        wasted_time_round = round(total_wasted_time, 2)
        model_time_round = round(total_inf_time + total_wasted_time, 2)
        total_warm_delay_steps = max(0, total_est_delay_steps - total_cold_delay_steps)
        total_warm_delay_sum = max(0.0, total_est_delay_sum - total_cold_delay_sum)
        est_warm_delay_tot = (
            round(total_warm_delay_sum, 2) if total_warm_delay_steps > 0 else None
        )
        est_cold_delay_tot = (
            round(total_cold_delay_sum, 2) if total_cold_delay_steps > 0 else None
        )
        est_total_delay_tot = (
            round(
                (total_est_delay_sum if total_est_delay_steps > 0 else 0.0)
                + total_wasted_time,
                2,
            )
            if (total_est_delay_steps > 0 or wasted_time_round > 0.0)
            else None
        )
        totals.update(
            {
                "steps_count": total_steps,
                "compaction_steps_count": total_comp_steps,
                "input_tokens": total_in,
                "cache_read_tokens": total_cache,
                "gross_input_tokens": gross_in,
                "cache_hit_pct": (
                    round(total_cache / gross_in * 100.0, 1)
                    if gross_in > 0
                    else 0.0
                ),
                "output_tokens": total_out,
                "est_thinking_tokens": total_think,
                "est_visible_tokens": total_vis,
                "total_tokens": gross_in + total_out,
                "inference_time_s": inf_time_round,
                "model_time_s": model_time_round,
                "wasted_time_s": wasted_time_round,
                "retry_steps_count": total_retry_steps,
                "failed_turns_count": total_failed_turns,
                "timed_steps_count": total_timed_steps,
                "cold_timed_steps_count": total_cold_timed,
                "cold_model_time_s": round(total_cold_time, 2),
                "timed_output_tokens": total_timed_out,
                "avg_effective_tps": (
                    round(total_timed_out / inf_time_round, 1)
                    if inf_time_round > 0.0
                    else None
                ),
                "tool_time_s": round(total_tool_time, 2),
                "compacted_count": compacted_count,
                "warm_timed_steps_count": total_warm_timed,
                "est_delay_steps_count": total_est_delay_steps,
                "cold_delay_steps_count": total_cold_delay_steps,
                "est_warm_delay_s": est_warm_delay_tot,
                "est_cold_delay_s": est_cold_delay_tot,
                "est_total_delay_s": est_total_delay_tot,
                "avg_delay_s": (
                    round(est_total_delay_tot / total_est_delay_steps, 2)
                    if (est_total_delay_tot is not None and total_est_delay_steps > 0)
                    else None
                ),
                "median_delay_s": (
                    round(statistics.median(pooled_sample_delays), 2)
                    if pooled_sample_delays
                    else None
                ),
            }
        )

        where_parts: list[str] = []
        where_args_list: list[Any] = []
        if cutoff_epoch is not None:
            where_parts.append("epoch_s >= ?")
            where_args_list.append(cutoff_epoch)
        if scoped_cids is not None:
            if not scoped_cids:
                where_parts.append("1 = 0")
            else:
                ph = ",".join("?" for _ in scoped_cids)
                where_parts.append(f"conv_id IN ({ph})")
                where_args_list.extend(scoped_cids)
        where_win = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
        global_last_row = self._db.execute(
            f"""
            SELECT conv_id, ordinal, step_index, created_at, epoch_s, model_name,
                   is_checkpoint, input_tokens, cache_read_tokens, output_tokens,
                   est_thinking_tokens, est_visible_tokens, duration_s,
                   duration_source, tool_exec_s, cold_cache_spike, compacted,
                   est_delay_s, retry_wait_s, retry_count
            FROM step_lite
            {where_win}
            ORDER BY epoch_s DESC, conv_id DESC, ordinal DESC
            LIMIT 1
            """,
            tuple(where_args_list),
        ).fetchone()
        latest_step_dict = (
            self._step_lite_row_to_dict(global_last_row) if global_last_row else None
        )
        all_val_rows = self._store.query_window_val_rows(
            cutoff_epoch, conv_ids=scoped_cids
        )
        valuation = _build_all_valuation(all_val_rows)
        val_by_model = valuation.get("by_model") or {}
        for bm in by_model:
            bm_val = val_by_model.get(bm["model_name"])
            if bm_val is not None:
                bm["valuation"] = bm_val

        return {
            "mode": mode,
            "window": norm_win,
            "conversation_id": resp_conv_id,
            "title": resp_title,
            **extra_project_fields,
            "model_name": active_model_name,
            "status": eff_status,
            "generating_since_ms": eff_gen_since,
            "updated_at_ms": max_updated_ms,
            "conversations_count": conversations_count,
            "subagents_count": subagents_count,
            "planner_steps_count": total_steps,
            "latest_step": latest_step_dict,
            "totals": totals,
            "by_model": by_model,
            "valuation": valuation,
            "steps": [],
        }

    def get_pricing_config(self) -> dict[str, Any]:
        with self._lock:
            self.refresh_if_changed()
            try:
                seen_models = [
                    str(r[0])
                    for r in self._db.execute(
                        "SELECT DISTINCT model_name FROM conv_model_totals"
                    ).fetchall()
                ]
            except sqlite3.Error:
                self._recover_db_to_memory()
                self.refresh_if_changed()
                seen_models = [
                    str(r[0])
                    for r in self._db.execute(
                        "SELECT DISTINCT model_name FROM conv_model_totals"
                    ).fetchall()
                ]
            for conv in self._conversations.values():
                if conv.hydrated:
                    for s in conv.steps:
                        seen_models.append(s.model_name)
            return self._pricing.get_config(seen_model_names=seen_models)

    def update_pricing_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._pricing.update_config(payload)
            self._bump_state_rev()
            return self.get_pricing_config()

    def get_metrics(
        self,
        conversation_id: str | None = None,
        window: str = "all",
        refresh: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            if refresh:
                self.refresh_if_changed(target_id=conversation_id)
            try:
                # Verify SQLite handle is still usable before reading memoized payload
                _ = self._db.in_transaction
                norm_win = window if window in VALID_WINDOWS else "all"
                time_bucket = int(time.time())
                cache_key = (
                    conversation_id or "",
                    norm_win,
                    self._state_rev,
                    time_bucket,
                )
                cached = self._metrics_cache.get(cache_key)
                if cached is not None:
                    return dict(cached)
                res = self._get_metrics_unlocked(conversation_id, window=window)
                if len(self._metrics_cache) >= 32:
                    self._metrics_cache.clear()
                self._metrics_cache[cache_key] = res
                return dict(res)
            except sqlite3.Error:
                self._recover_db_to_memory()
                self.refresh_if_changed(target_id=conversation_id)
                res = self._get_metrics_unlocked(conversation_id, window=window)
                return dict(res)

    def get_state_etag(
        self,
        conversation_id: str | None = None,
        window: str = "all",
        refresh: bool = True,
    ) -> str:
        with self._lock:
            if refresh:
                self.refresh_if_changed(target_id=conversation_id)
            norm_win = window if window in VALID_WINDOWS else "all"
            is_agg = conversation_id == ALL_CONVERSATIONS_ID or (
                isinstance(conversation_id, str)
                and conversation_id.startswith(PROJECT_SCOPE_PREFIX)
            )
            eff_target = (
                conversation_id
                if is_agg
                else (conversation_id or self._select_active_conversation_id() or "")
            )
            gen_flag = 1 if self.is_generating() else 0
            win_bucket = (
                int(time.time() // 60)
                if (is_agg and norm_win != "all")
                else 0
            )
            return f"{self._state_rev}:{eff_target}:{norm_win}:{gen_flag}:{win_bucket}"

    def get_snapshot(
        self,
        conversation_id: str | None = None,
        window: str = "all",
        refresh: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            if refresh:
                self.refresh_if_changed(target_id=conversation_id)
            metrics = self.get_metrics(
                conversation_id, window=window, refresh=False
            )
            conversations = self.list_conversations(refresh=False)
            return {
                "etag": self.get_state_etag(
                    conversation_id, window=window, refresh=False
                ),
                "conversations": conversations,
                "metrics": metrics,
            }

    def _get_metrics_unlocked(
        self,
        conversation_id: str | None = None,
        window: str = "all",
    ) -> dict[str, Any]:
        if conversation_id == ALL_CONVERSATIONS_ID:
            return self._get_all_conversations_metrics(window=window)
        if isinstance(conversation_id, str) and conversation_id.startswith(
            PROJECT_SCOPE_PREFIX
        ):
            pid = conversation_id[len(PROJECT_SCOPE_PREFIX) :]
            return self._get_all_conversations_metrics(window=window, project_id=pid)

        latest_cid = (
            str(self._latest_record.get("conversation_id") or "")
            if self._latest_record
            else ""
        )
        target_id = conversation_id or self._select_active_conversation_id()
        conv = self._conversations.get(target_id) if target_id else None

        if conv is None:
            empty_base = self._pricing.empty_scoped_valuation(
                include_session_split=True
            )
            empty_val = dict(empty_base)
            empty_val["conversation_age_s"] = 0.0
            empty_val["first_step_epoch_s"] = None
            empty_val["last_step_epoch_s"] = None
            empty_val["windows"] = {
                "5h": dict(empty_base),
                "7d": dict(empty_base),
                "total": dict(empty_base),
            }
            return {
                "mode": "conversation",
                "conversation_id": target_id,
                "title": "No active conversation",
                "project_id": "",
                "project_name": "",
                "agent_name": "",
                "is_subagent": False,
                "parent_id": None,
                "parent_title": "",
                "root_id": target_id,
                "model_name": "",
                "status": "idle",
                "generating_since_ms": None,
                "updated_at_ms": 0,
                "planner_steps_count": 0,
                "latest_step": None,
                "totals": empty_totals(),
                "by_model": [],
                "context": {
                    "current_tokens": 0,
                    "cache_read_tokens": 0,
                    "uncached_tokens": 0,
                    "cache_hit_pct": 0.0,
                    "cold_cache_spike": False,
                    "compacted": False,
                    "model_switched": False,
                    "breakdown": {
                        "system_tokens": 0,
                        "summary_tokens": 0,
                        "tool_tokens": 0,
                        "model_tokens": 0,
                        "user_tokens": 0,
                    },
                    "top_tools": [],
                },
                "subagents": {
                    "count": 0,
                    "items": [],
                    "totals": {
                        "input_tokens": 0,
                        "cache_read_tokens": 0,
                        "output_tokens": 0,
                        "est_thinking_tokens": 0,
                        "est_visible_tokens": 0,
                        "total_tokens": 0,
                    },
                    "valuation": dict(empty_base["subagents"]),
                },
                "grand_totals": {
                    "input_tokens": 0,
                    "cache_read_tokens": 0,
                    "output_tokens": 0,
                    "est_thinking_tokens": 0,
                    "est_visible_tokens": 0,
                    "total_tokens": 0,
                },
                "valuation": empty_val,
                "steps": [],
            }

        self._ensure_hydrated(conv)
        protected = self._expand_related_ids(latest_cid)
        protected.update(self._expand_related_ids(target_id))
        self._evict_idle_hydrated_convs(protected)

        descendants = _parser.collect_descendant_subagents(
            self._conversations, conv.conversation_id
        )
        sub_ids: list[str] = [
            sub_id
            for sub_id, _, _ in descendants
            if sub_id in self._conversations
        ]
        scope_ids = [conv.conversation_id, *sub_ids]

        grouped_steps: dict[str, list[StepMetrics]] = {}
        for s in conv.steps:
            grouped_steps.setdefault(s.model_name, []).append(s)

        grouped_failed: dict[str, list[Any]] = {}
        for ft in conv.failed_turns:
            grouped_failed.setdefault(ft.model_name, []).append(ft)

        total_ft_wasted = sum(ft.wasted_s for ft in conv.failed_turns)
        total_ft_count = len(conv.failed_turns)
        own_totals = aggregate_steps(
            conv.steps,
            failed_turns_wasted_s=total_ft_wasted,
            failed_turns_count=total_ft_count,
        )

        own_model_order = list(
            dict.fromkeys([*grouped_steps.keys(), *grouped_failed.keys()])
        )
        own_model_set = set(own_model_order)
        sub_models = (
            self._store.query_window_models(None, conv_ids=sub_ids)
            if sub_ids
            else []
        )
        sub_models_set = set(sub_models)
        model_order = list(dict.fromkeys([*own_model_order, *sub_models]))
        global_fits = {
            m_name: self._get_global_model_fit_and_sync_delays(m_name)
            for m_name in model_order
        }
        step_delays: dict[int, float | None] = {
            int(r[0]): (round(float(r[1]), 2) if r[1] is not None else None)
            for r in self._db.execute(
                "SELECT ordinal, est_delay_s FROM step_lite WHERE conv_id = ?",
                (conv.conversation_id,),
            ).fetchall()
        }

        scope_agg_by_model: dict[str, tuple[Any, ...]] = {}
        scope_sql = ""
        scope_args: tuple[Any, ...] = ()
        if sub_models_set:
            scope_agg_rows, _ = self._store.query_all_model_aggregates(
                None, conv_ids=scope_ids
            )
            scope_agg_by_model = {str(r[0]): r for r in scope_agg_rows}
            scope_ph = ",".join("?" for _ in scope_ids)
            scope_sql = f" AND conv_id IN ({scope_ph})"
            scope_args = tuple(scope_ids)

        all_modeled_delays: list[float] = []
        total_warm_timed = 0
        total_est_delay_count = 0
        total_est_delay_sum = 0.0
        total_warm_delay_steps = 0
        total_warm_delay_sum = 0.0
        total_cold_delay_steps = 0
        total_cold_delay_sum = 0.0
        sum_steps = 0
        sum_comp_steps = 0
        sum_in = 0
        sum_cache = 0
        sum_out = 0
        sum_think = 0
        sum_vis = 0
        sum_inf_time = 0.0
        sum_wasted_time = 0.0
        sum_retry_steps = 0
        sum_failed_turns = 0
        sum_timed_steps = 0
        sum_cold_timed = 0
        sum_cold_time = 0.0
        sum_timed_out = 0
        sum_tool_time = 0.0

        own_by_model: list[dict[str, Any]] = []
        sub_only_by_model: list[dict[str, Any]] = []
        for m_name in model_order:
            m_steps = grouped_steps.get(m_name, [])
            m_failed = grouped_failed.get(m_name, [])
            global_fit = global_fits[m_name]
            pre_avail = bool(global_fit.get("prefill_available"))

            if m_name in sub_models_set and m_name in scope_agg_by_model:
                row = scope_agg_by_model[m_name]
                m_steps_n = int(row[1] or 0)
                m_comp_steps = int(row[2] or 0)
                m_in = int(row[3] or 0)
                m_cache = int(row[4] or 0)
                m_out = int(row[5] or 0)
                m_think = int(row[6] or 0)
                m_vis = int(row[7] or 0)
                m_inf_time_s = float(row[8] or 0.0)
                m_timed_steps = int(row[9] or 0)
                m_cold_timed = int(row[10] or 0)
                m_cold_time_s = float(row[11] or 0.0)
                m_timed_out = int(row[12] or 0)
                m_tool_s = float(row[13] or 0.0)
                m_warm_timed_cnt = int(row[14] or 0)
                m_cold_excl_raw = int(row[15] or 0)
                m_est_delay_count = int(row[16] or 0)
                m_delay_sum = float(row[17] or 0.0)
                m_wasted_s = float(row[19] or 0.0)
                m_retry_steps = int(row[20] or 0)
                m_failed_turns = int(row[21] or 0)
                m_cold_delay_count = int(row[22] or 0)
                m_cold_delay_sum = float(row[23] or 0.0)
                m_warm_delay_count = max(0, m_est_delay_count - m_cold_delay_count)
                m_warm_delay_sum = max(0.0, m_delay_sum - m_cold_delay_sum)
                m_cold_excl = 0 if pre_avail else m_cold_excl_raw

                m_gross_in = m_in + m_cache
                m_hit_pct = (
                    round(m_cache / m_gross_in * 100.0, 1)
                    if m_gross_in > 0
                    else 0.0
                )
                m_inf_round = round(m_inf_time_s, 2)
                m_wasted_round = round(m_wasted_s, 2)
                m_time_round = round(m_inf_round + m_wasted_round, 2)
                m_avg_tps = (
                    round(m_timed_out / m_inf_round, 1)
                    if m_inf_round > 0.0
                    else None
                )
                m_agg = {
                    "steps_count": m_steps_n,
                    "compaction_steps_count": m_comp_steps,
                    "input_tokens": m_in,
                    "cache_read_tokens": m_cache,
                    "gross_input_tokens": m_gross_in,
                    "cache_hit_pct": m_hit_pct,
                    "output_tokens": m_out,
                    "est_thinking_tokens": m_think,
                    "est_visible_tokens": m_vis,
                    "total_tokens": m_gross_in + m_out,
                    "inference_time_s": m_inf_round,
                    "model_time_s": m_time_round,
                    "wasted_time_s": m_wasted_round,
                    "retry_steps_count": m_retry_steps,
                    "failed_turns_count": m_failed_turns,
                    "timed_steps_count": m_timed_steps,
                    "cold_timed_steps_count": m_cold_timed,
                    "cold_model_time_s": round(m_cold_time_s, 2),
                    "timed_output_tokens": m_timed_out,
                    "avg_effective_tps": m_avg_tps,
                    "tool_time_s": round(m_tool_s, 2),
                }

                scatter_steps, m_delays = self._query_model_scatter_steps(
                    m_name,
                    global_fit,
                    scope_sql=scope_sql,
                    scope_args=scope_args,
                    est_delay_count=m_est_delay_count,
                )
                if global_fit["available"]:
                    all_modeled_delays.extend(m_delays)
                fit_for_conv: dict[str, Any] = {
                    **global_fit,
                    "cold_excluded_count": m_cold_excl,
                }
                m_avg_delay_s: float | None = (
                    fit_for_conv.get("avg_delay_s")
                    if fit_for_conv.get("available")
                    else None
                )

                m_est_warm_delay: float | None = (
                    round(m_warm_delay_sum, 2) if m_warm_delay_count > 0 else None
                )
                m_est_cold_delay: float | None = (
                    round(m_cold_delay_sum, 2) if m_cold_delay_count > 0 else None
                )
                m_est_total_delay: float | None = (
                    round(
                        (m_delay_sum if m_est_delay_count > 0 else 0.0) + m_wasted_s,
                        2,
                    )
                    if (m_est_delay_count > 0 or m_wasted_round > 0.0)
                    else None
                )

                latest_m_row = self._db.execute(
                    f"""
                    SELECT conv_id, ordinal, step_index, created_at, epoch_s, model_name,
                           is_checkpoint, input_tokens, cache_read_tokens, output_tokens,
                           est_thinking_tokens, est_visible_tokens, duration_s,
                           duration_source, tool_exec_s, cold_cache_spike, compacted,
                           est_delay_s, retry_wait_s, retry_count
                    FROM step_lite
                    WHERE model_name = ? {scope_sql}
                    ORDER BY epoch_s DESC, conv_id DESC, ordinal DESC
                    LIMIT 1
                    """,
                    (m_name, *scope_args),
                ).fetchone()
                if (
                    latest_m_row is not None
                    and str(latest_m_row[0]) == conv.conversation_id
                    and m_steps
                    and int(latest_m_row[1]) == m_steps[-1].ordinal
                ):
                    m_latest: dict[str, Any] | None = m_steps[-1].to_dict()
                    assert m_latest is not None
                    m_latest["est_delay_s"] = step_delays.get(m_steps[-1].ordinal)
                    m_latest.update(
                        self._pricing.value_step(
                            m_steps[-1].model_name,
                            m_steps[-1].input_tokens,
                            m_steps[-1].cache_read_tokens,
                            m_steps[-1].output_tokens,
                        )
                    )
                else:
                    m_latest = (
                        self._step_lite_row_to_dict(latest_m_row)
                        if latest_m_row is not None
                        else None
                    )

                total_warm_timed += m_warm_timed_cnt
                total_est_delay_count += m_est_delay_count
                if m_est_delay_count > 0:
                    total_est_delay_sum += m_delay_sum
                total_warm_delay_steps += m_warm_delay_count
                if m_warm_delay_count > 0:
                    total_warm_delay_sum += m_warm_delay_sum
                total_cold_delay_steps += m_cold_delay_count
                if m_cold_delay_count > 0:
                    total_cold_delay_sum += m_cold_delay_sum

                sum_steps += m_steps_n
                sum_comp_steps += m_comp_steps
                sum_in += m_in
                sum_cache += m_cache
                sum_out += m_out
                sum_think += m_think
                sum_vis += m_vis
                sum_inf_time += m_inf_time_s
                sum_wasted_time += m_wasted_s
                sum_retry_steps += m_retry_steps
                sum_failed_turns += m_failed_turns
                sum_timed_steps += m_timed_steps
                sum_cold_timed += m_cold_timed
                sum_cold_time += m_cold_time_s
                sum_timed_out += m_timed_out
                sum_tool_time += m_tool_s

                bm_entry: dict[str, Any] = {
                    "model_name": m_name,
                    "is_active": m_name == conv.current_model_name,
                    "first_step_ordinal": (
                        m_steps[0].ordinal
                        if m_steps
                        else (1 if m_steps_n > 0 else 0)
                    ),
                    "last_step_ordinal": (
                        m_steps[-1].ordinal if m_steps else m_steps_n
                    ),
                    "latest_step": m_latest,
                    "latency_fit": fit_for_conv,
                    "scatter_steps": scatter_steps,
                    "warm_timed_steps_count": m_warm_timed_cnt,
                    "est_delay_steps_count": m_est_delay_count,
                    "cold_delay_steps_count": m_cold_delay_count,
                    "est_warm_delay_s": m_est_warm_delay,
                    "est_cold_delay_s": m_est_cold_delay,
                    "est_total_delay_s": m_est_total_delay,
                    "avg_delay_s": m_avg_delay_s,
                    "valuation": self._pricing.value_model_group(
                        m_name,
                        m_agg["steps_count"],
                        m_agg["input_tokens"],
                        m_agg["cache_read_tokens"],
                        m_agg["output_tokens"],
                    ),
                    **m_agg,
                }
            else:
                m_ft_wasted = sum(ft.wasted_s for ft in m_failed)
                m_ft_count = len(m_failed)
                m_warm_timed = [
                    s
                    for s in m_steps
                    if s.step_kind != "checkpoint"
                    and not s.cold_cache_spike
                    and s.duration_s is not None
                    and s.duration_s > 0
                    and s.output_tokens > 0
                ]
                total_warm_timed += len(m_warm_timed)
                m_cold_timed_cnt = sum(
                    1
                    for s in m_steps
                    if s.step_kind != "checkpoint"
                    and s.cold_cache_spike
                    and s.duration_s is not None
                    and s.duration_s > 0
                    and s.output_tokens > 0
                )
                m_cold_excl = 0 if pre_avail else m_cold_timed_cnt
                m_modeled_timed = [
                    s
                    for s in m_steps
                    if s.step_kind != "checkpoint"
                    and (pre_avail or not s.cold_cache_spike)
                    and s.duration_s is not None
                    and s.duration_s > 0
                    and s.output_tokens > 0
                ]
                m_warm_delays = [
                    d
                    for s in m_warm_timed
                    if (d := step_delays.get(s.ordinal)) is not None
                ]
                m_cold_delays = [
                    d
                    for s in m_modeled_timed
                    if s.cold_cache_spike
                    and (d := step_delays.get(s.ordinal)) is not None
                ]
                m_delays = [
                    d
                    for s in m_modeled_timed
                    if (d := step_delays.get(s.ordinal)) is not None
                ]
                m_delays_with_retry = [
                    round(d + max(0.0, float(s.retry_wait_s)), 2)
                    for s in m_modeled_timed
                    if (d := step_delays.get(s.ordinal)) is not None
                ]
                m_agg = aggregate_steps(
                    m_steps,
                    failed_turns_wasted_s=m_ft_wasted,
                    failed_turns_count=m_ft_count,
                )
                m_wasted_round = float(m_agg["wasted_time_s"])
                m_warm_delay_sum = sum(m_warm_delays)
                m_cold_delay_sum = sum(m_cold_delays)
                m_delay_sum = sum(m_delays)
                m_est_warm_delay = (
                    round(m_warm_delay_sum, 2) if m_warm_delays else None
                )
                m_est_cold_delay = (
                    round(m_cold_delay_sum, 2) if m_cold_delays else None
                )
                m_cold_delay_count = len(m_cold_delays)
                total_warm_delay_steps += len(m_warm_delays)
                if m_warm_delays:
                    total_warm_delay_sum += m_warm_delay_sum
                total_cold_delay_steps += m_cold_delay_count
                if m_cold_delays:
                    total_cold_delay_sum += m_cold_delay_sum
                if m_delays:
                    m_est_delay_count = len(m_delays)
                    total_est_delay_count += m_est_delay_count
                    total_est_delay_sum += m_delay_sum
                    all_modeled_delays.extend(m_delays_with_retry)
                else:
                    m_est_delay_count = 0
                fit_for_conv = {
                    **global_fit,
                    "cold_excluded_count": m_cold_excl,
                }
                m_avg_delay_s = (
                    fit_for_conv.get("avg_delay_s")
                    if fit_for_conv.get("available")
                    else None
                )
                if m_delays or m_wasted_round > 0.0:
                    m_est_total_delay = round(
                        (m_delay_sum if m_delays else 0.0) + m_wasted_round, 2
                    )
                else:
                    m_est_total_delay = None

                if m_steps:
                    m_latest = m_steps[-1].to_dict()
                    assert m_latest is not None
                    m_latest["est_delay_s"] = step_delays.get(m_steps[-1].ordinal)
                    m_latest.update(
                        self._pricing.value_step(
                            m_steps[-1].model_name,
                            m_steps[-1].input_tokens,
                            m_steps[-1].cache_read_tokens,
                            m_steps[-1].output_tokens,
                        )
                    )
                else:
                    m_latest = None

                sum_steps += int(m_agg["steps_count"])
                sum_comp_steps += int(m_agg["compaction_steps_count"])
                sum_in += int(m_agg["input_tokens"])
                sum_cache += int(m_agg["cache_read_tokens"])
                sum_out += int(m_agg["output_tokens"])
                sum_think += int(m_agg["est_thinking_tokens"])
                sum_vis += int(m_agg["est_visible_tokens"])
                sum_inf_time += sum(
                    float(s.duration_s)
                    for s in m_steps
                    if s.duration_s is not None and s.duration_s > 0
                )
                sum_wasted_time += (
                    sum(float(s.retry_wait_s) for s in m_steps if s.retry_wait_s > 0.0)
                    + float(m_ft_wasted)
                )
                sum_retry_steps += int(m_agg["retry_steps_count"])
                sum_failed_turns += int(m_agg["failed_turns_count"])
                sum_timed_steps += int(m_agg["timed_steps_count"])
                sum_cold_timed += int(m_agg["cold_timed_steps_count"])
                sum_cold_time += sum(
                    float(s.duration_s)
                    for s in m_steps
                    if s.step_kind != "checkpoint"
                    and s.cold_cache_spike
                    and s.duration_s is not None
                    and s.duration_s > 0
                )
                sum_timed_out += int(m_agg["timed_output_tokens"])
                sum_tool_time += sum(float(s.tool_exec_s) for s in m_steps)

                bm_entry = {
                    "model_name": m_name,
                    "is_active": m_name == conv.current_model_name,
                    "first_step_ordinal": m_steps[0].ordinal if m_steps else 0,
                    "last_step_ordinal": m_steps[-1].ordinal if m_steps else 0,
                    "latest_step": m_latest,
                    "latency_fit": fit_for_conv,
                    "warm_timed_steps_count": len(m_warm_timed),
                    "est_delay_steps_count": m_est_delay_count,
                    "cold_delay_steps_count": m_cold_delay_count,
                    "est_warm_delay_s": m_est_warm_delay,
                    "est_cold_delay_s": m_est_cold_delay,
                    "est_total_delay_s": m_est_total_delay,
                    "avg_delay_s": m_avg_delay_s,
                    "valuation": self._pricing.value_model_group(
                        m_name,
                        m_agg["steps_count"],
                        m_agg["input_tokens"],
                        m_agg["cache_read_tokens"],
                        m_agg["output_tokens"],
                    ),
                    **m_agg,
                }

            if m_name in own_model_set:
                own_by_model.append(bm_entry)
            else:
                sub_only_by_model.append(bm_entry)

        sub_only_by_model.sort(
            key=lambda bm: (
                1 if _is_unattributed_model(bm["model_name"]) else 0,
                -bm["total_tokens"],
                bm["model_name"],
            )
        )
        by_model: list[dict[str, Any]] = [*own_by_model, *sub_only_by_model]

        if not sub_models_set:
            totals = dict(own_totals)
        else:
            tot_gross_in = sum_in + sum_cache
            tot_inf_round = round(sum_inf_time, 2)
            tot_wasted_round = round(sum_wasted_time, 2)
            totals = {
                "steps_count": sum_steps,
                "compaction_steps_count": sum_comp_steps,
                "input_tokens": sum_in,
                "cache_read_tokens": sum_cache,
                "gross_input_tokens": tot_gross_in,
                "cache_hit_pct": (
                    round(sum_cache / tot_gross_in * 100.0, 1)
                    if tot_gross_in > 0
                    else 0.0
                ),
                "output_tokens": sum_out,
                "est_thinking_tokens": sum_think,
                "est_visible_tokens": sum_vis,
                "total_tokens": tot_gross_in + sum_out,
                "inference_time_s": tot_inf_round,
                "model_time_s": round(tot_inf_round + tot_wasted_round, 2),
                "wasted_time_s": tot_wasted_round,
                "retry_steps_count": sum_retry_steps,
                "failed_turns_count": sum_failed_turns,
                "timed_steps_count": sum_timed_steps,
                "cold_timed_steps_count": sum_cold_timed,
                "cold_model_time_s": round(sum_cold_time, 2),
                "timed_output_tokens": sum_timed_out,
                "avg_effective_tps": (
                    round(sum_timed_out / tot_inf_round, 1)
                    if tot_inf_round > 0.0
                    else None
                ),
                "tool_time_s": round(sum_tool_time, 2),
            }

        totals["compacted_count"] = conv.compacted_count
        est_warm_delay_tot = (
            round(total_warm_delay_sum, 2) if total_warm_delay_steps > 0 else None
        )
        est_cold_delay_tot = (
            round(total_cold_delay_sum, 2) if total_cold_delay_steps > 0 else None
        )
        wasted_tot = float(totals["wasted_time_s"])
        totals["warm_timed_steps_count"] = total_warm_timed
        totals["est_delay_steps_count"] = total_est_delay_count
        totals["cold_delay_steps_count"] = total_cold_delay_steps
        totals["est_warm_delay_s"] = est_warm_delay_tot
        totals["est_cold_delay_s"] = est_cold_delay_tot
        totals["est_total_delay_s"] = (
            round(
                (total_est_delay_sum if total_est_delay_count > 0 else 0.0)
                + wasted_tot,
                2,
            )
            if (total_est_delay_count > 0 or wasted_tot > 0.0)
            else None
        )
        totals["avg_delay_s"] = (
            round(totals["est_total_delay_s"] / total_est_delay_count, 2)
            if (totals["est_total_delay_s"] is not None and total_est_delay_count > 0)
            else None
        )
        totals["median_delay_s"] = (
            round(statistics.median(all_modeled_delays), 2)
            if all_modeled_delays
            else None
        )

        # Transitive subagent rollup
        sub_items: list[dict[str, Any]] = []
        all_sub_rows: list[dict[str, Any]] = []
        sub_in = 0
        sub_cache = 0
        sub_out = 0
        sub_think = 0
        sub_vis = 0
        eff_status = conv.status
        eff_gen_since = conv.generating_since_ms

        for sub_id, sub_role, model_hint in descendants:
            sub_conv = self._conversations.get(sub_id)
            sub_db_rows = self._db.execute(
                """
                SELECT model_name, counterfactual_model, steps_count, input_tokens, cache_read_tokens,
                       output_tokens, est_thinking_tokens, est_visible_tokens
                FROM conv_model_totals
                WHERE conv_id = ?
                """,
                (sub_id,),
            ).fetchall()

            if sub_conv is None and not sub_db_rows:
                missing_model = normalize_step_model(
                    model_hint or "unknown", is_subagent=True
                )
                res_missing = self._pricing.resolve_model(missing_model)
                is_priced_empty = res_missing.status == "priced"
                sub_items.append(
                    {
                        "conversation_id": sub_id,
                        "role": sub_role or "Subagent",
                        "agent_name": "",
                        "model_name": missing_model,
                        "status": "idle",
                        "planner_steps_count": 0,
                        "input_tokens": 0,
                        "cache_read_tokens": 0,
                        "output_tokens": 0,
                        "est_thinking_tokens": 0,
                        "est_visible_tokens": 0,
                        "cost_usd": 0.0 if is_priced_empty else None,
                        "credits": 0.0 if is_priced_empty else None,
                        "future_cost_usd": 0.0 if is_priced_empty else None,
                        "pricing_status": res_missing.status,
                    }
                )
                continue

            single_sub_rows: list[dict[str, Any]] = []
            if sub_db_rows:
                single_sub_rows = [
                    {
                        "model_name": str(r[0]),
                        "counterfactual_model": str(r[1] or ""),
                        "steps_count": int(r[2]),
                        "input_tokens": int(r[3]),
                        "cache_read_tokens": int(r[4]),
                        "output_tokens": int(r[5]),
                    }
                    for r in sub_db_rows
                ]
                s_steps = sum(int(r[2]) for r in sub_db_rows)
                s_in = sum(int(r[3]) for r in sub_db_rows)
                s_cache = sum(int(r[4]) for r in sub_db_rows)
                s_out = sum(int(r[5]) for r in sub_db_rows)
                s_think = sum(int(r[6]) for r in sub_db_rows)
                s_vis = sum(int(r[7]) for r in sub_db_rows)
            elif sub_conv is not None and sub_conv.hydrated and sub_conv.steps:
                grouped_sub: dict[tuple[str, str], list[StepMetrics]] = {}
                for st in sub_conv.steps:
                    grouped_sub.setdefault(
                        (st.model_name, st.counterfactual_model), []
                    ).append(st)
                single_sub_rows = [
                    {
                        "model_name": sm_name,
                        "counterfactual_model": sm_cf,
                        "steps_count": len(sm_steps),
                        "input_tokens": sum(st.input_tokens for st in sm_steps),
                        "cache_read_tokens": sum(
                            st.cache_read_tokens for st in sm_steps
                        ),
                        "output_tokens": sum(st.output_tokens for st in sm_steps),
                    }
                    for (sm_name, sm_cf), sm_steps in grouped_sub.items()
                ]
                s_steps = len(sub_conv.steps)
                s_in = sum(st.input_tokens for st in sub_conv.steps)
                s_cache = sum(st.cache_read_tokens for st in sub_conv.steps)
                s_out = sum(st.output_tokens for st in sub_conv.steps)
                s_think = sum(st.est_thinking_tokens for st in sub_conv.steps)
                s_vis = sum(st.est_visible_tokens for st in sub_conv.steps)
            else:
                assert sub_conv is not None
                s_steps = sub_conv.summary_steps_count
                s_in = sub_conv.own_input_tokens
                s_cache = sub_conv.own_cache_read_tokens
                s_out = sub_conv.own_output_tokens
                s_think = sub_conv.own_est_thinking_tokens
                s_vis = sub_conv.own_est_visible_tokens
                if s_in or s_cache or s_out:
                    single_sub_rows = [
                        {
                            "model_name": sub_conv.current_model_name,
                            "counterfactual_model": "",
                            "steps_count": s_steps,
                            "input_tokens": s_in,
                            "cache_read_tokens": s_cache,
                            "output_tokens": s_out,
                        }
                    ]

            sub_in += s_in
            sub_cache += s_cache
            sub_out += s_out
            sub_think += s_think
            sub_vis += s_vis
            if (
                sub_conv is not None
                and sub_conv.status == "generating"
                and eff_status != "generating"
            ):
                eff_status = "generating"
                eff_gen_since = sub_conv.generating_since_ms

            sub_model_name = (
                sub_conv.current_model_name
                if sub_conv is not None
                else (
                    single_sub_rows[0]["model_name"]
                    if single_sub_rows
                    else normalize_step_model(
                        model_hint or "unknown", is_subagent=True
                    )
                )
            )
            if single_sub_rows:
                all_sub_rows.extend(single_sub_rows)
                sub_item_val = self._pricing.aggregate_valuation(single_sub_rows)
                cov = sub_item_val["coverage"]
                if cov["priced_steps"] > 0:
                    item_status = (
                        "priced"
                        if (
                            cov["unpriced_steps"] == 0
                            and cov["unattributed_steps"] == 0
                        )
                        else "partial"
                    )
                    item_cost = sub_item_val["cost_usd"]
                    item_credits = sub_item_val["credits"]
                    item_fut = sub_item_val["future_cost_usd"]
                elif cov["unattributed_steps"] > 0:
                    item_status = "unattributed"
                    item_cost = None
                    item_credits = None
                    item_fut = None
                else:
                    item_status = "unpriced"
                    item_cost = None
                    item_credits = None
                    item_fut = None
            else:
                res_sub = self._pricing.resolve_model(sub_model_name)
                item_status = res_sub.status
                is_priced_empty = res_sub.status == "priced"
                item_cost = 0.0 if is_priced_empty else None
                item_credits = 0.0 if is_priced_empty else None
                item_fut = 0.0 if is_priced_empty else None

            sub_title = sub_conv.title if sub_conv is not None else ""
            sub_role_label = (
                sub_role if (sub_role and sub_role != "Subagent") else sub_title
            ) or "Subagent"
            sub_items.append(
                {
                    "conversation_id": sub_id,
                    "role": sub_role_label,
                    "agent_name": sub_conv.agent_name if sub_conv is not None else "",
                    "model_name": sub_model_name,
                    "status": sub_conv.status if sub_conv is not None else "idle",
                    "planner_steps_count": s_steps,
                    "input_tokens": s_in,
                    "cache_read_tokens": s_cache,
                    "output_tokens": s_out,
                    "est_thinking_tokens": s_think,
                    "est_visible_tokens": s_vis,
                    "cost_usd": item_cost,
                    "credits": item_credits,
                    "future_cost_usd": item_fut,
                    "pricing_status": item_status,
                }
            )

        latest_step_dict: dict[str, Any] | None = None
        if conv.steps:
            last_s = conv.steps[-1]
            latest_step_dict = last_s.to_dict()
            latest_step_dict["est_delay_s"] = step_delays.get(last_s.ordinal)
            latest_step_dict.update(
                self._pricing.value_step(
                    last_s.model_name,
                    last_s.input_tokens,
                    last_s.cache_read_tokens,
                    last_s.output_tokens,
                )
            )

        own_val_groups: dict[tuple[str, str], list[StepMetrics]] = {}
        for s in conv.steps:
            own_val_groups.setdefault((s.model_name, s.counterfactual_model), []).append(
                s
            )
        own_rows = [
            {
                "model_name": m_name,
                "counterfactual_model": cf_m,
                "steps_count": len(m_steps),
                "input_tokens": sum(s.input_tokens for s in m_steps),
                "cache_read_tokens": sum(s.cache_read_tokens for s in m_steps),
                "output_tokens": sum(s.output_tokens for s in m_steps),
            }
            for (m_name, cf_m), m_steps in own_val_groups.items()
        ]
        now_s = time.time()
        placeholders = ",".join("?" for _ in scope_ids)
        ep_row = self._db.execute(
            f"SELECT MIN(epoch_s), MAX(epoch_s) FROM step_lite WHERE conv_id IN ({placeholders}) AND epoch_s > 0",
            scope_ids,
        ).fetchone()
        first_ep = float(ep_row[0]) if ep_row and ep_row[0] is not None else None
        last_ep = float(ep_row[1]) if ep_row and ep_row[1] is not None else None
        conv_age_s = (
            round(max(0.0, now_s - first_ep), 3) if first_ep is not None else 0.0
        )

        combined_rows = own_rows + all_sub_rows
        total_val = self._pricing.build_scoped_valuation(
            combined_rows,
            own_rows=own_rows,
            sub_rows=all_sub_rows,
            model_rows=combined_rows,
        )
        val_by_model = total_val.get("by_model") or {}
        for bm in by_model:
            bm_val = val_by_model.get(bm["model_name"])
            if bm_val is not None:
                bm["valuation"] = bm_val
        own_5h = self._store.query_window_val_rows(
            now_s - _WINDOW_SECONDS["5h"], [conv.conversation_id]
        )
        sub_5h = self._store.query_window_val_rows(
            now_s - _WINDOW_SECONDS["5h"], sub_ids
        )
        val_5h = self._pricing.build_scoped_valuation(
            own_5h + sub_5h,
            own_rows=own_5h,
            sub_rows=sub_5h,
            model_rows=own_5h + sub_5h,
        )
        own_7d = self._store.query_window_val_rows(
            now_s - _WINDOW_SECONDS["7d"], [conv.conversation_id]
        )
        sub_7d = self._store.query_window_val_rows(
            now_s - _WINDOW_SECONDS["7d"], sub_ids
        )
        val_7d = self._pricing.build_scoped_valuation(
            own_7d + sub_7d,
            own_rows=own_7d,
            sub_rows=sub_7d,
            model_rows=own_7d + sub_7d,
        )
        sub_val = total_val["subagents"]
        session_val = dict(total_val)
        session_val["conversation_age_s"] = conv_age_s
        session_val["first_step_epoch_s"] = first_ep
        session_val["last_step_epoch_s"] = last_ep
        session_val["windows"] = {
            "5h": val_5h,
            "7d": val_7d,
            "total": dict(total_val),
        }

        parent_conv = (
            self._conversations.get(conv.parent_id) if conv.parent_id else None
        )
        root_id = _parser.root_ancestor_id(
            self._conversations, conv.conversation_id
        )
        return {
            "mode": "conversation",
            "conversation_id": conv.conversation_id,
            "title": conv.title,
            "project_id": conv.project_id or UNASSIGNED_PROJECT_ID,
            "project_name": conv.project_name or UNASSIGNED_PROJECT_NAME,
            "agent_name": conv.agent_name,
            "is_subagent": conv.parent_id is not None,
            "parent_id": conv.parent_id,
            "parent_title": parent_conv.title if parent_conv is not None else "",
            "root_id": root_id,
            "model_name": conv.current_model_name,
            "status": eff_status,
            "generating_since_ms": eff_gen_since,
            "updated_at_ms": conv.updated_at_ms,
            "planner_steps_count": len(conv.steps),
            "latest_step": latest_step_dict,
            "totals": totals,
            "by_model": by_model,
            "context": _parser.compute_context_breakdown(conv),
            "subagents": {
                "count": len(sub_items),
                "items": sub_items,
                "totals": {
                    "input_tokens": sub_in,
                    "cache_read_tokens": sub_cache,
                    "output_tokens": sub_out,
                    "est_thinking_tokens": sub_think,
                    "est_visible_tokens": sub_vis,
                    "total_tokens": sub_in + sub_cache + sub_out,
                },
                "valuation": sub_val,
            },
            "grand_totals": {
                "input_tokens": own_totals["input_tokens"] + sub_in,
                "cache_read_tokens": own_totals["cache_read_tokens"] + sub_cache,
                "output_tokens": own_totals["output_tokens"] + sub_out,
                "est_thinking_tokens": own_totals["est_thinking_tokens"] + sub_think,
                "est_visible_tokens": own_totals["est_visible_tokens"] + sub_vis,
                "total_tokens": (
                    own_totals["total_tokens"] + sub_in + sub_cache + sub_out
                ),
            },
            "valuation": session_val,
            "steps": [
                {
                    **s.to_dict(),
                    "est_delay_s": step_delays.get(s.ordinal),
                    **self._pricing.value_step(
                        s.model_name,
                        s.input_tokens,
                        s.cache_read_tokens,
                        s.output_tokens,
                    ),
                }
                for s in conv.steps
            ],
        }
