"""Read-only Antigravity desktop metadata reader (projects, conversation titles, subagent links)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any
import urllib.parse

_PBTXT_TITLE_DBL_RE = re.compile(r'(?:^|\s)title\s*:\s*"((?:\\.|[^"\\])*)"')
_PBTXT_TITLE_SGL_RE = re.compile(r"(?:^|\s)title\s*:\s*'((?:\\.|[^'\\])*)'")
_SIMPLE_ESCAPES = {
    "n": b"\n",
    "r": b"\r",
    "t": b"\t",
    "v": b"\v",
    "f": b"\f",
    "a": b"\a",
    "b": b"\b",
    "\\": b"\\",
    '"': b'"',
    "'": b"'",
    "?": b"?",
}


@dataclass(frozen=True)
class ProjectMeta:
    project_id: str
    name: str
    folder_paths: tuple[str, ...]
    is_sidecar: bool = False


@dataclass(frozen=True)
class ConversationExternalMeta:
    conversation_id: str
    title: str
    project_id: str
    workspace_paths: tuple[str, ...]
    agent_name: str
    parent_id: str
    nesting_depth: int


def uri_or_path_to_norm_path(raw: str | None) -> str:
    """Normalize a file:// URI or local filesystem path, returning '' if invalid/relative."""
    if not raw or not isinstance(raw, str):
        return ""
    text = raw.strip()
    if not text:
        return ""
    if text.startswith("file://"):
        try:
            parsed = urllib.parse.urlsplit(text)
        except ValueError:
            return ""
        path = urllib.parse.unquote(parsed.path or "")
        if len(path) >= 3 and path[0] == "/" and path[2] == ":" and path[1].isalpha():
            path = path[1:]
    else:
        path = text

    is_win_abs = len(path) >= 3 and path[0].isalpha() and path[1] == ":" and path[2] in ("/", "\\")
    if not os.path.isabs(path) and not is_win_abs:
        return ""
    norm = os.path.normpath(path)
    return "" if norm in ("", ".") else norm


def _unescape_pbtxt_string(escaped: str) -> str:
    buf = bytearray()
    n = len(escaped)
    i = 0
    while i < n:
        ch = escaped[i]
        if ch != "\\" or i + 1 >= n:
            buf.extend(ch.encode("utf-8"))
            i += 1
            continue
        nxt = escaped[i + 1]
        if "0" <= nxt <= "7":
            j = i + 1
            while j < n and j < i + 4 and "0" <= escaped[j] <= "7":
                j += 1
            buf.append(int(escaped[i + 1 : j], 8) & 0xFF)
            i = j
        elif nxt in ("x", "X"):
            j = i + 2
            while j < n and j < i + 4 and escaped[j] in "0123456789abcdefABCDEF":
                j += 1
            if j > i + 2:
                buf.append(int(escaped[i + 2 : j], 16) & 0xFF)
                i = j
            else:
                buf.extend(nxt.encode("utf-8"))
                i += 2
        elif nxt == "u" and i + 6 <= n:
            hex_part = escaped[i + 2 : i + 6]
            try:
                buf.extend(chr(int(hex_part, 16)).encode("utf-8"))
                i += 6
                continue
            except ValueError:
                pass
            buf.extend(nxt.encode("utf-8"))
            i += 2
        elif nxt == "U" and i + 10 <= n:
            hex_part = escaped[i + 2 : i + 10]
            try:
                cp = int(hex_part, 16)
                if 0 <= cp <= 0x10FFFF:
                    buf.extend(chr(cp).encode("utf-8"))
                    i += 10
                    continue
            except ValueError:
                pass
            buf.extend(nxt.encode("utf-8"))
            i += 2
        elif nxt in _SIMPLE_ESCAPES:
            buf.extend(_SIMPLE_ESCAPES[nxt])
            i += 2
        else:
            buf.extend(nxt.encode("utf-8"))
            i += 2
    return bytes(buf).decode("utf-8", errors="replace").strip()


def parse_pbtxt_title(raw_text: str) -> str:
    """Extract and unescape the top-level `title:` field from an annotation .pbtxt file."""
    if not raw_text:
        return ""
    m = _PBTXT_TITLE_DBL_RE.search(raw_text) or _PBTXT_TITLE_SGL_RE.search(raw_text)
    if not m:
        return ""
    return _unescape_pbtxt_string(m.group(1))


def _stat_tuple(path: Path) -> tuple[int, int, int]:
    try:
        st = path.stat()
    except OSError:
        return (0, 0, 0)
    return (int(st.st_ino), int(st.st_size), int(st.st_mtime_ns))


def _parse_workspace_uris(raw: Any) -> tuple[str, ...]:
    items: list[Any]
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return ()
        if text.startswith("["):
            try:
                decoded = json.loads(text)
                items = decoded if isinstance(decoded, list) else [text]
            except json.JSONDecodeError:
                items = [text]
        else:
            items = [text]
    else:
        return ()

    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            continue
        norm = uri_or_path_to_norm_path(item)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return tuple(out)


class MetadataIndex:
    """Stat-cached reader for Antigravity projects, summaries DB, and annotation titles."""

    def __init__(self, app_data_dir: Path, projects_dir: Path) -> None:
        self.app_data_dir = Path(app_data_dir)
        self.projects_dir = Path(projects_dir)
        self.projects: dict[str, ProjectMeta] = {}
        self.conversations: dict[str, ConversationExternalMeta] = {}

        self._proj_file_cache: dict[str, tuple[tuple[int, int], ProjectMeta | None]] = {}
        self._db_sig: tuple[tuple[int, int, int], tuple[int, int, int]] | None = None
        self._db_mtime_ns: int = 0
        self._db_rows: dict[str, tuple[str, str, tuple[str, ...], str, str, int]] = {}
        self._ann_file_cache: dict[str, tuple[tuple[int, int], str, int]] = {}
        self._ann_titles: dict[str, tuple[str, int]] = {}
        self._prev_title_sources: dict[str, tuple[str, str]] = {}

    def _refresh_projects(self) -> dict[str, ProjectMeta]:
        if not self.projects_dir.is_dir():
            self._proj_file_cache.clear()
            return {}

        seen_names: set[str] = set()
        out: dict[str, ProjectMeta] = {}
        try:
            with os.scandir(self.projects_dir) as it:
                for entry in it:
                    if not entry.name.endswith(".json") or not entry.is_file():
                        continue
                    seen_names.add(entry.name)
                    try:
                        st = entry.stat()
                    except OSError:
                        continue
                    sig = (int(st.st_mtime_ns), int(st.st_size))
                    cached = self._proj_file_cache.get(entry.name)
                    if cached is not None and cached[0] == sig:
                        meta = cached[1]
                    else:
                        meta = self._parse_project_file(Path(entry.path))
                        self._proj_file_cache[entry.name] = (sig, meta)
                    if meta is not None:
                        out[meta.project_id] = meta
        except OSError:
            return dict(self.projects)

        for stale in set(self._proj_file_cache.keys()) - seen_names:
            self._proj_file_cache.pop(stale, None)
        return out

    @staticmethod
    def _parse_project_file(path: Path) -> ProjectMeta | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        pid = str(data.get("id") or path.stem).strip()
        name = str(data.get("name") or pid).strip()
        if not pid:
            return None
        sidecar_info = data.get("sidecar")
        is_sidecar = bool(
            isinstance(sidecar_info, dict) and sidecar_info.get("sidecarId")
        )
        folder_paths: list[str] = []
        seen: set[str] = set()
        proj_res = data.get("projectResources")
        if isinstance(proj_res, dict):
            resources = proj_res.get("resources")
            if isinstance(resources, list):
                for res in resources:
                    if not isinstance(res, dict):
                        continue
                    candidates = [res.get("folderUri")]
                    git_folder = res.get("gitFolder")
                    if isinstance(git_folder, dict):
                        candidates.append(git_folder.get("folderUri"))
                    for cand in candidates:
                        norm = uri_or_path_to_norm_path(cand)
                        if norm and norm not in seen:
                            seen.add(norm)
                            folder_paths.append(norm)
        return ProjectMeta(
            project_id=pid,
            name=name or pid,
            folder_paths=tuple(folder_paths),
            is_sidecar=is_sidecar,
        )

    def _refresh_summaries_db(self) -> None:
        db_path = self.app_data_dir / "conversation_summaries.db"
        if not db_path.is_file():
            self._db_sig = None
            self._db_mtime_ns = 0
            self._db_rows.clear()
            return

        wal_path = Path(f"{db_path}-wal")
        db_stat = _stat_tuple(db_path)
        wal_stat = _stat_tuple(wal_path)
        cur_sig = (db_stat, wal_stat)
        if self._db_sig == cur_sig:
            return

        conn: sqlite3.Connection | None = None
        try:
            uri = f"{db_path.resolve().as_uri()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=1.0)
            tables = {
                str(r[0])
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "conversation_summaries" not in tables:
                self._db_sig = cur_sig
                self._db_mtime_ns = max(db_stat[2], wal_stat[2])
                self._db_rows.clear()
                return

            avail_cols = {
                str(r[1])
                for r in conn.execute("PRAGMA table_info(conversation_summaries)").fetchall()
            }
            if "conversation_id" not in avail_cols:
                self._db_sig = cur_sig
                self._db_mtime_ns = max(db_stat[2], wal_stat[2])
                self._db_rows.clear()
                return

            wanted = (
                "conversation_id",
                "title",
                "workspace_uris",
                "project_id",
                "agent_name",
                "parent_conversation_id",
                "nesting_depth",
            )
            select_exprs = [
                col if col in avail_cols else ("0" if col == "nesting_depth" else "''")
                for col in wanted
            ]
            rows = conn.execute(
                f"SELECT {', '.join(select_exprs)} FROM conversation_summaries"
            ).fetchall()

            parent_map: dict[str, str] = {}
            if "conversation_parents" in tables:
                p_cols = {
                    str(r[1])
                    for r in conn.execute("PRAGMA table_info(conversation_parents)").fetchall()
                }
                if {"conversation_id", "parent_id"}.issubset(p_cols):
                    for prow in conn.execute(
                        "SELECT conversation_id, parent_id FROM conversation_parents"
                    ).fetchall():
                        pcid = str(prow[0] or "").strip()
                        ppid = str(prow[1] or "").strip()
                        if pcid and ppid:
                            parent_map[pcid] = ppid

            parsed_rows: dict[str, tuple[str, str, tuple[str, ...], str, str, int]] = {}
            for row in rows:
                cid = str(row[0] or "").strip()
                if not cid:
                    continue
                title = str(row[1] or "").strip()
                ws_paths = _parse_workspace_uris(row[2])
                proj_id = str(row[3] or "").strip()
                agent_name = str(row[4] or "").strip()
                parent_id = str(row[5] or "").strip() or parent_map.get(cid, "")
                try:
                    depth = max(0, int(row[6] or 0))
                except (TypeError, ValueError):
                    depth = 0
                parsed_rows[cid] = (
                    title,
                    proj_id,
                    ws_paths,
                    agent_name,
                    parent_id,
                    depth,
                )

            self._db_rows = parsed_rows
            self._db_mtime_ns = max(db_stat[2], wal_stat[2])
            self._db_sig = cur_sig
        except (sqlite3.Error, OSError):
            return
        finally:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

    def _refresh_annotations(self) -> None:
        ann_dir = self.app_data_dir / "annotations"
        if not ann_dir.is_dir():
            self._ann_file_cache.clear()
            self._ann_titles.clear()
            return

        seen_names: set[str] = set()
        titles: dict[str, tuple[str, int]] = {}
        try:
            with os.scandir(ann_dir) as it:
                for entry in it:
                    if not entry.name.endswith(".pbtxt") or not entry.is_file():
                        continue
                    seen_names.add(entry.name)
                    try:
                        st = entry.stat()
                    except OSError:
                        continue
                    sig = (int(st.st_mtime_ns), int(st.st_size))
                    cached = self._ann_file_cache.get(entry.name)
                    if cached is not None and cached[0] == sig:
                        title, mtime_ns = cached[1], cached[2]
                    else:
                        try:
                            raw_text = Path(entry.path).read_text(
                                encoding="utf-8", errors="replace"
                            )
                            title = parse_pbtxt_title(raw_text)
                        except OSError:
                            title = ""
                        mtime_ns = int(st.st_mtime_ns)
                        self._ann_file_cache[entry.name] = (sig, title, mtime_ns)
                    if title:
                        cid = entry.name[: -len(".pbtxt")]
                        titles[cid] = (title, mtime_ns)
        except OSError:
            return

        for stale in set(self._ann_file_cache.keys()) - seen_names:
            self._ann_file_cache.pop(stale, None)
        self._ann_titles = titles

    def refresh_if_changed(self) -> bool:
        """Refresh external project, summary DB, and annotation metadata if files changed."""
        new_projects = self._refresh_projects()
        self._refresh_summaries_db()
        self._refresh_annotations()

        new_convs: dict[str, ConversationExternalMeta] = {}
        new_title_sources: dict[str, tuple[str, str]] = {}
        all_cids = set(self._db_rows.keys()) | set(self._ann_titles.keys())
        for cid in all_cids:
            db_entry = self._db_rows.get(cid)
            ann_entry = self._ann_titles.get(cid)
            db_title = db_entry[0] if db_entry is not None else ""
            ann_title = ann_entry[0] if ann_entry is not None else ""
            ann_mtime_ns = ann_entry[1] if ann_entry is not None else 0
            new_title_sources[cid] = (db_title, ann_title)

            if ann_title and db_title:
                if ann_title == db_title:
                    title = ann_title
                elif cid in self._prev_title_sources and cid in self.conversations:
                    prev_db, prev_ann = self._prev_title_sources[cid]
                    db_changed = db_title != prev_db
                    ann_changed = ann_title != prev_ann
                    if ann_changed and not db_changed:
                        title = ann_title
                    elif db_changed and not ann_changed:
                        title = db_title
                    elif ann_changed and db_changed:
                        title = (
                            ann_title if ann_mtime_ns >= self._db_mtime_ns else db_title
                        )
                    else:
                        title = self.conversations[cid].title
                else:
                    title = ann_title if ann_mtime_ns >= self._db_mtime_ns else db_title
            else:
                title = ann_title or db_title

            if db_entry is not None:
                _, proj_id, ws_paths, agent_name, parent_id, depth = db_entry
            else:
                proj_id, ws_paths, agent_name, parent_id, depth = (
                    "",
                    (),
                    "",
                    "",
                    0,
                )

            new_convs[cid] = ConversationExternalMeta(
                conversation_id=cid,
                title=title,
                project_id=proj_id,
                workspace_paths=ws_paths,
                agent_name=agent_name,
                parent_id=parent_id,
                nesting_depth=depth,
            )

        self._prev_title_sources = new_title_sources
        changed = new_projects != self.projects or new_convs != self.conversations
        if changed:
            self.projects = new_projects
            self.conversations = new_convs
        return changed

    def resolve_project_for_paths(self, paths: Iterable[str]) -> tuple[str, str]:
        """Resolve `(project_id, project_name)` from workspace paths or folder basename."""
        norm_paths: list[str] = []
        seen: set[str] = set()
        for raw in paths:
            norm = uri_or_path_to_norm_path(raw)
            if norm and norm not in seen:
                seen.add(norm)
                norm_paths.append(norm)

        if not norm_paths:
            return ("", "")

        best_match: tuple[int, str, str] | None = None
        for proj in self.projects.values():
            if proj.is_sidecar or not proj.folder_paths:
                continue
            for fp in proj.folder_paths:
                for p in norm_paths:
                    if (
                        p == fp
                        or (fp == os.sep and p.startswith(os.sep))
                        or p.startswith(fp + os.sep)
                    ):
                        cand = (len(fp), proj.project_id, proj.name)
                        if best_match is None or (
                            cand[0] > best_match[0]
                            or (cand[0] == best_match[0] and cand[1] < best_match[1])
                        ):
                            best_match = cand

        if best_match is not None:
            return (best_match[1], best_match[2])

        first_path = norm_paths[0]
        return (f"path:{first_path}", os.path.basename(first_path) or first_path)
