"""SQLite WAL rollup cache, schema migrations, and corruption recovery."""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import sqlite3
from typing import Any

from .models import (
    ConversationState,
    _DELAY_FREEZE_MIN_WARM,
    _FIT_CACHE_HIGH_TOK,
    _FIT_CACHE_MAX_IN_TOK,
    _FIT_CACHE_MIN_HIGH_STEPS,
    _FIT_CACHE_MIN_SPREAD,
    _FIT_CACHE_MIN_STEPS,
    _FIT_MAX_HIGH_CACHE_STEPS,
    _FIT_MAX_HIGH_PREFILL_STEPS,
    _FIT_MAX_ORIGIN_STEPS,
    _FIT_MAX_RECENT_STEPS,
    _FIT_ORIGIN_MAX_CACHE_TOK,
    _FIT_ORIGIN_MAX_IN_TOK,
    _FIT_PREFILL_HIGH_TOK,
    _FIT_PREFILL_MIN_HIGH_STEPS,
    _FIT_PREFILL_MIN_SPREAD,
    _FIT_PREFILL_MIN_STEPS,
    _ROLLUP_SCHEMA_VERSION,
    _is_unattributed_model,
    _parse_iso_epoch,
    _safe_int,
)
from .roofline import compute_roofline_fit

_ROLLUP_AGG_COLS_SQL = """
    SUM(is_step),
    COALESCE(SUM(CASE WHEN is_step = 1 THEN is_checkpoint ELSE 0 END), 0),
    COALESCE(SUM(input_tokens), 0),
    COALESCE(SUM(cache_read_tokens), 0),
    COALESCE(SUM(output_tokens), 0),
    COALESCE(SUM(est_thinking_tokens), 0),
    COALESCE(SUM(est_visible_tokens), 0),
    COALESCE(SUM(CASE WHEN is_step = 1 AND duration_s IS NOT NULL AND duration_s > 0 THEN duration_s ELSE 0.0 END), 0.0),
    COALESCE(SUM(CASE WHEN is_step = 1 AND duration_s IS NOT NULL AND duration_s > 0 THEN 1 ELSE 0 END), 0),
    COALESCE(SUM(CASE WHEN is_step = 1 AND is_checkpoint = 0 AND cold_cache_spike = 1 AND duration_s IS NOT NULL AND duration_s > 0 THEN 1 ELSE 0 END), 0),
    COALESCE(SUM(CASE WHEN is_step = 1 AND is_checkpoint = 0 AND cold_cache_spike = 1 AND duration_s IS NOT NULL AND duration_s > 0 THEN duration_s ELSE 0.0 END), 0.0),
    COALESCE(SUM(CASE WHEN is_step = 1 AND duration_s IS NOT NULL AND duration_s > 0 THEN output_tokens ELSE 0 END), 0),
    COALESCE(SUM(tool_exec_s), 0.0),
    COALESCE(SUM(CASE WHEN is_step = 1 AND is_checkpoint = 0 AND cold_cache_spike = 0 AND duration_s IS NOT NULL AND duration_s > 0 AND output_tokens > 0 THEN 1 ELSE 0 END), 0),
    COALESCE(SUM(CASE WHEN is_step = 1 AND is_checkpoint = 0 AND cold_cache_spike = 1 AND duration_s IS NOT NULL AND duration_s > 0 AND output_tokens > 0 THEN 1 ELSE 0 END), 0),
    COALESCE(SUM(CASE WHEN is_step = 1 AND est_delay_s IS NOT NULL THEN 1 ELSE 0 END), 0),
    COALESCE(SUM(est_delay_s), 0.0),
    COALESCE(SUM(compaction_event), 0),
    COALESCE(SUM(wasted_s), 0.0),
    COALESCE(SUM(is_retry), 0),
    COALESCE(SUM(is_failed_turn), 0),
    COALESCE(SUM(CASE WHEN is_step = 1 AND cold_cache_spike = 1 AND est_delay_s IS NOT NULL THEN 1 ELSE 0 END), 0),
    COALESCE(SUM(CASE WHEN is_step = 1 AND cold_cache_spike = 1 AND est_delay_s IS NOT NULL THEN est_delay_s ELSE 0.0 END), 0.0)
"""


def _events_union_sql(where_clause: str) -> str:
    return f"""
        SELECT (CASE WHEN epoch_s > 0 THEN CAST(epoch_s / 3600 AS INT) * 3600 ELSE 0 END) AS hour_epoch,
               conv_id, model_name,
               1 AS is_step, is_checkpoint, input_tokens, cache_read_tokens,
               output_tokens, est_thinking_tokens, est_visible_tokens,
               duration_s, tool_exec_s, cold_cache_spike, est_delay_s, compaction_event,
               retry_wait_s AS wasted_s,
               (CASE WHEN retry_wait_s > 0 OR retry_count > 0 THEN 1 ELSE 0 END) AS is_retry,
               0 AS is_failed_turn
        FROM step_lite
        WHERE {where_clause}
        UNION ALL
        SELECT (CASE WHEN epoch_s > 0 THEN CAST(epoch_s / 3600 AS INT) * 3600 ELSE 0 END) AS hour_epoch,
               conv_id, model_name,
               0 AS is_step, 0 AS is_checkpoint, 0 AS input_tokens, 0 AS cache_read_tokens,
               0 AS output_tokens, 0 AS est_thinking_tokens, 0 AS est_visible_tokens,
               NULL AS duration_s, 0.0 AS tool_exec_s, 0 AS cold_cache_spike, NULL AS est_delay_s, 0 AS compaction_event,
               wasted_s,
               0 AS is_retry,
               1 AS is_failed_turn
        FROM failed_turn_lite
        WHERE {where_clause}
    """


class MetricsStore:
    """SQLite persistence and rollup query manager with automatic corruption recovery."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.closed = False
        self.wal_write_count = 0
        self.synced_delay_sig: dict[str, tuple[Any, ...]] = {}
        self.global_fit_cache: dict[str, tuple[Any, dict[str, Any]]] = {}
        self.db: sqlite3.Connection = self.init_rollup_db()

    def close(self) -> None:
        if self.closed:
            return
        db = getattr(self, "db", None)
        if db is not None:
            try:
                db.close()
            except sqlite3.Error:
                pass
        self.closed = True

    @staticmethod
    def create_rollup_schema(db: sqlite3.Connection) -> None:
        ver = db.execute("PRAGMA user_version").fetchone()[0]
        if ver != _ROLLUP_SCHEMA_VERSION:
            db.executescript(
                """
                DROP TABLE IF EXISTS failed_turn_lite;
                DROP TABLE IF EXISTS step_lite;
                DROP TABLE IF EXISTS hour_model_totals;
                DROP TABLE IF EXISTS conv_model_totals;
                DROP TABLE IF EXISTS conv_meta;
                """
            )
            db.execute(f"PRAGMA user_version = {_ROLLUP_SCHEMA_VERSION}")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS conv_meta (
                conv_id TEXT PRIMARY KEY,
                t_path TEXT NOT NULL,
                t_ino INT NOT NULL,
                t_size INT NOT NULL,
                t_mtime_ns INT NOT NULL,
                h_path TEXT NOT NULL,
                h_ino INT NOT NULL,
                h_size INT NOT NULL,
                h_mtime_ns INT NOT NULL,
                hook_reported_transcript TEXT NOT NULL,
                fallback_model_hint TEXT NOT NULL,
                title TEXT NOT NULL,
                current_model_name TEXT NOT NULL,
                planner_steps_count INT NOT NULL,
                current_tokens INT NOT NULL,
                updated_at_ms INT NOT NULL,
                compacted_count INT NOT NULL,
                child_subagents_json TEXT NOT NULL,
                child_subagent_models_json TEXT NOT NULL,
                own_in INT NOT NULL,
                own_cache INT NOT NULL,
                own_out INT NOT NULL,
                own_think INT NOT NULL,
                own_vis INT NOT NULL,
                prompt_title TEXT NOT NULL,
                hook_workspace_paths_json TEXT NOT NULL,
                is_subagent INT NOT NULL DEFAULT 0
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS conv_model_totals (
                conv_id TEXT NOT NULL,
                model_name TEXT NOT NULL,
                steps_count INT NOT NULL,
                compaction_steps_count INT NOT NULL,
                input_tokens INT NOT NULL,
                cache_read_tokens INT NOT NULL,
                output_tokens INT NOT NULL,
                est_thinking_tokens INT NOT NULL,
                est_visible_tokens INT NOT NULL,
                model_time_s REAL NOT NULL,
                timed_steps_count INT NOT NULL,
                cold_timed_steps_count INT NOT NULL,
                cold_model_time_s REAL NOT NULL,
                timed_output_tokens INT NOT NULL,
                tool_time_s REAL NOT NULL,
                warm_timed_steps_count INT NOT NULL,
                cold_excluded_count INT NOT NULL,
                est_delay_steps_count INT NOT NULL,
                est_total_delay_s REAL NOT NULL,
                compacted_events_count INT NOT NULL,
                wasted_time_s REAL NOT NULL,
                retry_steps_count INT NOT NULL,
                failed_turns_count INT NOT NULL,
                cold_delay_steps_count INT NOT NULL,
                est_cold_delay_s REAL NOT NULL,
                PRIMARY KEY (conv_id, model_name)
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS hour_model_totals (
                hour_epoch INT NOT NULL,
                conv_id TEXT NOT NULL,
                model_name TEXT NOT NULL,
                steps_count INT NOT NULL,
                compaction_steps_count INT NOT NULL,
                input_tokens INT NOT NULL,
                cache_read_tokens INT NOT NULL,
                output_tokens INT NOT NULL,
                est_thinking_tokens INT NOT NULL,
                est_visible_tokens INT NOT NULL,
                model_time_s REAL NOT NULL,
                timed_steps_count INT NOT NULL,
                cold_timed_steps_count INT NOT NULL,
                cold_model_time_s REAL NOT NULL,
                timed_output_tokens INT NOT NULL,
                tool_time_s REAL NOT NULL,
                warm_timed_steps_count INT NOT NULL,
                cold_excluded_count INT NOT NULL,
                est_delay_steps_count INT NOT NULL,
                est_total_delay_s REAL NOT NULL,
                compacted_events_count INT NOT NULL,
                wasted_time_s REAL NOT NULL,
                retry_steps_count INT NOT NULL,
                failed_turns_count INT NOT NULL,
                cold_delay_steps_count INT NOT NULL,
                est_cold_delay_s REAL NOT NULL,
                PRIMARY KEY (hour_epoch, conv_id, model_name)
            ) WITHOUT ROWID;

            CREATE INDEX IF NOT EXISTS idx_hour_conv ON hour_model_totals(conv_id);

            CREATE TABLE IF NOT EXISTS step_lite (
                conv_id TEXT NOT NULL,
                ordinal INT NOT NULL,
                step_index INT NOT NULL,
                created_at TEXT NOT NULL,
                epoch_s REAL NOT NULL,
                model_name TEXT NOT NULL,
                is_checkpoint INT NOT NULL,
                input_tokens INT NOT NULL,
                cache_read_tokens INT NOT NULL,
                output_tokens INT NOT NULL,
                est_thinking_tokens INT NOT NULL,
                est_visible_tokens INT NOT NULL,
                duration_s REAL,
                duration_source TEXT,
                tool_exec_s REAL NOT NULL,
                cold_cache_spike INT NOT NULL,
                compacted INT NOT NULL,
                compaction_event INT NOT NULL,
                retry_wait_s REAL NOT NULL DEFAULT 0.0,
                retry_count INT NOT NULL DEFAULT 0,
                est_delay_s REAL,
                delay_finalized INT NOT NULL DEFAULT 0,
                PRIMARY KEY (conv_id, ordinal)
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS failed_turn_lite (
                conv_id TEXT NOT NULL,
                seq INT NOT NULL,
                epoch_s REAL NOT NULL,
                model_name TEXT NOT NULL,
                wasted_s REAL NOT NULL,
                PRIMARY KEY (conv_id, seq)
            ) WITHOUT ROWID;

            CREATE INDEX IF NOT EXISTS idx_failed_turn_epoch
                ON failed_turn_lite(epoch_s DESC, conv_id DESC);

            CREATE INDEX IF NOT EXISTS idx_step_epoch
                ON step_lite(epoch_s DESC, conv_id DESC, ordinal DESC);
            CREATE INDEX IF NOT EXISTS idx_step_model_epoch
                ON step_lite(model_name, epoch_s DESC, conv_id DESC, ordinal DESC);
            CREATE INDEX IF NOT EXISTS idx_step_model_timed
                ON step_lite(model_name, epoch_s DESC, conv_id DESC, ordinal DESC)
                WHERE is_checkpoint = 0 AND duration_s IS NOT NULL AND duration_s > 0;
            CREATE INDEX IF NOT EXISTS idx_step_model_unfinalized
                ON step_lite(model_name, conv_id, ordinal)
                WHERE delay_finalized = 0
                  AND is_checkpoint = 0
                  AND duration_s IS NOT NULL
                  AND duration_s > 0
                  AND output_tokens > 0;
            """
        )

    def unlink_rollup_files(self) -> None:
        db_path = self.state_dir / "rollup_cache.sqlite3"
        for p in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    def init_rollup_db(self, allow_unlink: bool = True) -> sqlite3.Connection:
        db_path = self.state_dir / "rollup_cache.sqlite3"
        db: sqlite3.Connection | None = None
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(str(db_path), timeout=15.0, check_same_thread=False)
            db.execute("PRAGMA busy_timeout=3000")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("PRAGMA wal_autocheckpoint=250")
            self.create_rollup_schema(db)
            db.execute("SELECT COUNT(*) FROM conv_meta LIMIT 1").fetchone()
            db.execute("SELECT COUNT(*) FROM step_lite LIMIT 1").fetchone()
            return db
        except sqlite3.DatabaseError:
            if db is not None:
                try:
                    db.close()
                except sqlite3.Error:
                    pass
                db = None
            if allow_unlink:
                self.unlink_rollup_files()
                return self.init_rollup_db(allow_unlink=False)
        except (OSError, sqlite3.Error):
            if db is not None:
                try:
                    db.close()
                except sqlite3.Error:
                    pass
        mem_db = sqlite3.connect(":memory:", check_same_thread=False)
        self.create_rollup_schema(mem_db)
        return mem_db

    def reset_to_memory_db(self) -> None:
        db = getattr(self, "db", None)
        if db is not None:
            try:
                db.close()
            except sqlite3.Error:
                pass
        mem_db = sqlite3.connect(":memory:", check_same_thread=False)
        self.create_rollup_schema(mem_db)
        self.db = mem_db
        self.closed = False
        self.wal_write_count = 0
        self.synced_delay_sig.clear()
        self.global_fit_cache.clear()

    def reset_fresh_disk_db(self) -> None:
        db = getattr(self, "db", None)
        if db is not None:
            try:
                db.close()
            except sqlite3.Error:
                pass
        self.unlink_rollup_files()
        self.db = self.init_rollup_db(allow_unlink=False)
        self.closed = False
        self.wal_write_count = 0
        self.synced_delay_sig.clear()
        self.global_fit_cache.clear()

    def load_persisted_summaries(self) -> dict[str, ConversationState]:
        conversations: dict[str, ConversationState] = {}
        rows = self.db.execute(
            """
            SELECT conv_id, t_path, t_ino, t_size, t_mtime_ns,
                   h_path, h_ino, h_size, h_mtime_ns,
                   hook_reported_transcript, fallback_model_hint, title,
                   current_model_name, planner_steps_count, current_tokens,
                   updated_at_ms, compacted_count,
                   child_subagents_json, child_subagent_models_json,
                   own_in, own_cache, own_out, own_think, own_vis,
                   prompt_title, hook_workspace_paths_json, is_subagent
            FROM conv_meta
            """
        ).fetchall()
        for row in rows:
            cid = str(row[0])
            conv = ConversationState(conversation_id=cid, hydrated=False)
            conv.persisted_sig = (
                str(row[1]),
                _safe_int(row[2]),
                _safe_int(row[3]),
                _safe_int(row[4]),
                str(row[5]),
                _safe_int(row[6]),
                _safe_int(row[7]),
                _safe_int(row[8]),
            )
            conv.hook_reported_transcript = str(row[9] or "")
            conv.fallback_model_hint = str(row[10] or "")
            conv.title = str(row[11] or cid[:8])
            conv.current_model_name = str(row[12] or "unknown")
            conv.summary_steps_count = _safe_int(row[13])
            conv.current_tokens = _safe_int(row[14])
            conv.updated_at_ms = _safe_int(row[15])
            conv.compacted_count = _safe_int(row[16])
            try:
                sub_map = json.loads(row[17] or "{}")
                if isinstance(sub_map, dict):
                    conv.child_subagents = {
                        str(k): str(v) for k, v in sub_map.items()
                    }
            except json.JSONDecodeError:
                pass
            try:
                sub_models = json.loads(row[18] or "{}")
                if isinstance(sub_models, dict):
                    conv.child_subagent_models = {
                        str(k): str(v) for k, v in sub_models.items()
                    }
            except json.JSONDecodeError:
                pass
            conv.own_input_tokens = _safe_int(row[19])
            conv.own_cache_read_tokens = _safe_int(row[20])
            conv.own_output_tokens = _safe_int(row[21])
            conv.own_est_thinking_tokens = _safe_int(row[22])
            conv.own_est_visible_tokens = _safe_int(row[23])
            conv.prompt_title = str(row[24] or "")
            try:
                ws_list = json.loads(row[25] or "[]")
                if isinstance(ws_list, list):
                    conv.hook_workspace_paths = [str(w) for w in ws_list if isinstance(w, str) and w]
            except json.JSONDecodeError:
                pass
            conv.is_subagent = bool(_safe_int(row[26]))
            conversations[cid] = conv
        return conversations

    def update_persisted_titles(self, updates: list[tuple[str, str]]) -> None:
        if not updates:
            return
        try:
            with self.db:
                self.db.executemany(
                    "UPDATE conv_meta SET title = ? WHERE conv_id = ?",
                    updates,
                )
        except sqlite3.Error:
            pass

    def delete_conversation_rollup(
        self, cid: str, on_error: Callable[[], None] | None = None
    ) -> None:
        self.synced_delay_sig.clear()
        try:
            with self.db:
                self.db.execute("DELETE FROM failed_turn_lite WHERE conv_id = ?", (cid,))
                self.db.execute("DELETE FROM step_lite WHERE conv_id = ?", (cid,))
                self.db.execute(
                    "DELETE FROM hour_model_totals WHERE conv_id = ?", (cid,)
                )
                self.db.execute(
                    "DELETE FROM conv_model_totals WHERE conv_id = ?", (cid,)
                )
                self.db.execute("DELETE FROM conv_meta WHERE conv_id = ?", (cid,))
        except sqlite3.Error:
            if on_error is not None:
                on_error()

    def refresh_conv_rollup_tables(self, conv_ids: list[str] | set[str]) -> None:
        agg = _ROLLUP_AGG_COLS_SQL
        union_sql = _events_union_sql("conv_id = ?")
        for cid in conv_ids:
            self.db.execute(
                "DELETE FROM hour_model_totals WHERE conv_id = ?", (cid,)
            )
            self.db.execute(
                "DELETE FROM conv_model_totals WHERE conv_id = ?", (cid,)
            )
            self.db.execute(
                f"""
                INSERT INTO conv_model_totals
                SELECT conv_id, model_name, {agg}
                FROM ({union_sql})
                GROUP BY conv_id, model_name
                """,
                (cid, cid),
            )
            self.db.execute(
                f"""
                INSERT INTO hour_model_totals
                SELECT hour_epoch, conv_id, model_name, {agg}
                FROM ({union_sql})
                GROUP BY hour_epoch, conv_id, model_name
                """,
                (cid, cid),
            )

    def persist_conversation_rollup(
        self,
        conv: ConversationState,
        file_sig: tuple[str, int, int, int, str, int, int, int],
        on_recover: Callable[[], None] | None = None,
        _allow_recover: bool = True,
    ) -> None:
        cid = conv.conversation_id

        core_rows: list[tuple[Any, ...]] = []
        touched_models: set[str] = set()
        raw_steps = conv.raw_steps
        for idx, s in enumerate(conv.steps):
            epoch_s = 0.0
            if idx < len(raw_steps) and raw_steps[idx].created_at_epoch is not None:
                epoch_s = float(raw_steps[idx].created_at_epoch)
            elif s.created_at:
                parsed = _parse_iso_epoch(s.created_at)
                if parsed is not None:
                    epoch_s = float(parsed)

            is_ckpt = 1 if s.step_kind == "checkpoint" else 0
            cold = 1 if s.cold_cache_spike else 0
            comp = 1 if s.compacted else 0
            comp_ev = 1 if s.compaction_event else 0
            touched_models.add(s.model_name)
            core_rows.append(
                (
                    cid,
                    s.ordinal,
                    s.step_index,
                    s.created_at,
                    epoch_s,
                    s.model_name,
                    is_ckpt,
                    s.input_tokens,
                    s.cache_read_tokens,
                    s.output_tokens,
                    s.est_thinking_tokens,
                    s.est_visible_tokens,
                    float(s.duration_s) if s.duration_s is not None else None,
                    s.duration_source,
                    float(s.tool_exec_s),
                    cold,
                    comp,
                    comp_ev,
                    float(s.retry_wait_s),
                    int(s.retry_count),
                )
            )

        failed_rows: list[tuple[Any, ...]] = [
            (cid, ft.seq, float(ft.epoch_s), ft.model_name, float(ft.wasted_s))
            for ft in conv.failed_turns
        ]
        steps_fp: tuple[Any, ...] = (tuple(core_rows), tuple(failed_rows))
        steps_changed = conv.persisted_steps_fp != steps_fp

        conv_meta_params = (
            cid,
            file_sig[0],
            file_sig[1],
            file_sig[2],
            file_sig[3],
            file_sig[4],
            file_sig[5],
            file_sig[6],
            file_sig[7],
            conv.hook_reported_transcript,
            conv.fallback_model_hint,
            conv.title,
            conv.current_model_name,
            conv.summary_steps_count,
            conv.current_tokens,
            conv.updated_at_ms,
            conv.compacted_count,
            json.dumps(conv.child_subagents, separators=(",", ":")),
            json.dumps(conv.child_subagent_models, separators=(",", ":")),
            conv.own_input_tokens,
            conv.own_cache_read_tokens,
            conv.own_output_tokens,
            conv.own_est_thinking_tokens,
            conv.own_est_visible_tokens,
            conv.prompt_title,
            json.dumps(conv.hook_workspace_paths, separators=(",", ":")),
            1 if conv.is_subagent else 0,
        )

        try:
            if not steps_changed:
                with self.db:
                    self.db.execute(
                        """
                        INSERT OR REPLACE INTO conv_meta VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        conv_meta_params,
                    )
            else:
                prev_delays: dict[tuple[int, str, int, int, int, float], tuple[float, int]] = {}
                for row in self.db.execute(
                    """
                    SELECT ordinal, model_name, input_tokens, cache_read_tokens,
                           output_tokens, duration_s, est_delay_s, delay_finalized
                    FROM step_lite
                    WHERE conv_id = ? AND est_delay_s IS NOT NULL AND duration_s IS NOT NULL
                    """,
                    (cid,),
                ).fetchall():
                    prev_delays[
                        (
                            _safe_int(row[0]),
                            str(row[1]),
                            _safe_int(row[2]),
                            _safe_int(row[3]),
                            _safe_int(row[4]),
                            round(float(row[5]), 6),
                        )
                    ] = (float(row[6]), _safe_int(row[7]))

                step_rows: list[tuple[Any, ...]] = []
                for row in core_rows:
                    is_ckpt = row[6]
                    in_tok = row[7]
                    cache_tok = row[8]
                    out_tok = row[9]
                    dur_s = row[12]
                    saved_delay: float | None = None
                    saved_fin = 0
                    if (
                        not is_ckpt
                        and dur_s is not None
                        and dur_s > 0.0
                        and out_tok > 0
                    ):
                        prev = prev_delays.get(
                            (
                                row[1],
                                row[5],
                                in_tok,
                                cache_tok,
                                out_tok,
                                round(float(dur_s), 6),
                            )
                        )
                        if prev is not None:
                            saved_delay, saved_fin = prev
                    step_rows.append((*row, saved_delay, saved_fin))

                for old_m_row in self.db.execute(
                    "SELECT model_name FROM conv_model_totals WHERE conv_id = ?",
                    (cid,),
                ).fetchall():
                    touched_models.add(str(old_m_row[0]))
                for m_name in touched_models:
                    self.synced_delay_sig.pop(m_name, None)

                with self.db:
                    self.db.execute("DELETE FROM failed_turn_lite WHERE conv_id = ?", (cid,))
                    self.db.execute("DELETE FROM step_lite WHERE conv_id = ?", (cid,))
                    self.db.execute(
                        """
                        INSERT OR REPLACE INTO conv_meta VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        conv_meta_params,
                    )
                    if step_rows:
                        self.db.executemany(
                            "INSERT INTO step_lite VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            step_rows,
                        )
                    if failed_rows:
                        self.db.executemany(
                            "INSERT INTO failed_turn_lite VALUES (?, ?, ?, ?, ?)",
                            failed_rows,
                        )
                    self.refresh_conv_rollup_tables([cid])
                conv.persisted_steps_fp = steps_fp

            conv.persisted_sig = file_sig
            self.wal_write_count += 1
            if self.wal_write_count % 50 == 0:
                try:
                    self.db.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except sqlite3.Error:
                    pass
        except sqlite3.Error:
            if _allow_recover and on_recover is not None:
                on_recover()

    def get_global_model_fit_and_sync_delays(
        self,
        model_name: str,
        on_fit_computed: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Compute global staged 3-speed roofline per model & sync step delays."""
        cnt_row = self.db.execute(
            """
            SELECT COALESCE(SUM(warm_timed_steps_count), 0),
                   COALESCE(SUM(cold_excluded_count), 0)
            FROM conv_model_totals
            WHERE model_name = ?
            """,
            (model_name,),
        ).fetchone()
        g_warm = int(cnt_row[0] or 0) if cnt_row else 0
        g_cold = int(cnt_row[1] or 0) if cnt_row else 0

        if _is_unattributed_model(model_name):
            return {
                "available": False,
                "reason": "unattributed",
                "warm_count": g_warm,
                "cold_count": g_cold,
                "used_count": 0,
                "token_spread": 0,
                "quantile_pct": None,
                "inferred_ttft_s": None,
                "inferred_base_s": None,
                "inferred_decode_tps": None,
                "slope_s_per_tok": None,
                "avg_delay_s": None,
                "median_delay_s": None,
                "cold_excluded_count": g_cold,
                "prefill_available": False,
                "prefill_reason": "unattributed",
                "inferred_prefill_tps": None,
                "prefill_slope_s_per_tok": None,
                "prefill_quantile_pct": None,
                "prefill_spread": 0,
                "high_prefill_count": 0,
                "cache_available": False,
                "cache_reason": "unattributed",
                "inferred_cache_tps": None,
                "cache_slope_s_per_tok": None,
                "cache_quantile_pct": None,
                "cache_spread": 0,
                "high_cache_count": 0,
            }

        elig_row = self.db.execute(
            """
            SELECT COUNT(*),
                   COALESCE(MIN(input_tokens), 0),
                   COALESCE(MAX(input_tokens), 0),
                   COALESCE(SUM(CASE WHEN input_tokens >= ? THEN 1 ELSE 0 END), 0),
                   COALESCE(SUM(CASE WHEN input_tokens < ? THEN 1 ELSE 0 END), 0),
                   COALESCE(MIN(CASE WHEN input_tokens < ? THEN cache_read_tokens END), 0),
                   COALESCE(MAX(CASE WHEN input_tokens < ? THEN cache_read_tokens END), 0),
                   COALESCE(SUM(CASE WHEN input_tokens < ? AND cache_read_tokens >= ? THEN 1 ELSE 0 END), 0)
            FROM step_lite
            WHERE model_name = ?
              AND is_checkpoint = 0
              AND duration_s IS NOT NULL
              AND duration_s > 0
              AND output_tokens > 0
            """,
            (
                _FIT_PREFILL_HIGH_TOK,
                _FIT_CACHE_MAX_IN_TOK,
                _FIT_CACHE_MAX_IN_TOK,
                _FIT_CACHE_MAX_IN_TOK,
                _FIT_CACHE_MAX_IN_TOK,
                _FIT_CACHE_HIGH_TOK,
                model_name,
            ),
        ).fetchone()
        g_all = int(elig_row[0] or 0) if elig_row else 0
        g_p_spread = (
            max(0, int(elig_row[2] or 0) - int(elig_row[1] or 0))
            if (elig_row and g_all > 0)
            else 0
        )
        g_hi_p = int(elig_row[3] or 0) if elig_row else 0
        g_low_p_cnt = int(elig_row[4] or 0) if elig_row else 0
        g_c_spread = (
            max(0, int(elig_row[6] or 0) - int(elig_row[5] or 0))
            if (elig_row and g_low_p_cnt > 0)
            else 0
        )
        g_hi_c = int(elig_row[7] or 0) if elig_row else 0

        cac_cand_ok = (
            g_low_p_cnt >= _FIT_CACHE_MIN_STEPS
            and g_c_spread >= _FIT_CACHE_MIN_SPREAD
            and g_hi_c >= _FIT_CACHE_MIN_HIGH_STEPS
        )
        pre_cand_ok = (
            g_all >= _FIT_PREFILL_MIN_STEPS
            and g_p_spread >= _FIT_PREFILL_MIN_SPREAD
            and g_hi_p >= _FIT_PREFILL_MIN_HIGH_STEPS
        )

        q_timed = """
            SELECT conv_id, ordinal, epoch_s, input_tokens, cache_read_tokens,
                   output_tokens, duration_s, duration_source, cold_cache_spike
            FROM step_lite
            WHERE model_name = ?
              AND is_checkpoint = 0
              AND duration_s IS NOT NULL
              AND duration_s > 0
              AND output_tokens > 0
        """
        warm_rows_desc = self.db.execute(
            q_timed
            + """
              AND cold_cache_spike = 0
            ORDER BY epoch_s DESC, conv_id DESC, ordinal DESC
            LIMIT ?
            """,
            (model_name, _FIT_MAX_RECENT_STEPS),
        ).fetchall()
        warm_key = tuple(
            (
                int(r[3]),
                int(r[4]),
                int(r[5]),
                float(r[6]),
                str(r[7] or ""),
                int(r[8]),
            )
            for r in reversed(warm_rows_desc)
        )

        all_points_arg: tuple[tuple[int, int, int, float, str, int], ...] | None = None
        if not (cac_cand_ok or pre_cand_ok):
            cache_key: Any = warm_key
        else:
            r_orig = self.db.execute(
                q_timed
                + """
                  AND input_tokens < ?
                  AND cache_read_tokens < ?
                ORDER BY epoch_s DESC, conv_id DESC, ordinal DESC
                LIMIT ?
                """,
                (
                    model_name,
                    _FIT_ORIGIN_MAX_IN_TOK,
                    _FIT_ORIGIN_MAX_CACHE_TOK,
                    _FIT_MAX_ORIGIN_STEPS,
                ),
            ).fetchall()
            r_hic = self.db.execute(
                q_timed
                + """
                  AND input_tokens < ?
                  AND cache_read_tokens >= ?
                ORDER BY epoch_s DESC, conv_id DESC, ordinal DESC
                LIMIT ?
                """,
                (
                    model_name,
                    _FIT_CACHE_MAX_IN_TOK,
                    _FIT_CACHE_HIGH_TOK,
                    _FIT_MAX_HIGH_CACHE_STEPS,
                ),
            ).fetchall()
            r_hip = self.db.execute(
                q_timed
                + """
                  AND input_tokens >= ?
                ORDER BY epoch_s DESC, conv_id DESC, ordinal DESC
                LIMIT ?
                """,
                (model_name, _FIT_PREFILL_HIGH_TOK, _FIT_MAX_HIGH_PREFILL_STEPS),
            ).fetchall()
            dedup: dict[tuple[str, int], tuple[Any, ...]] = {}
            for r in (*warm_rows_desc, *r_orig, *r_hic, *r_hip):
                dedup[(str(r[0]), int(r[1]))] = r
            ordered = sorted(
                dedup.values(),
                key=lambda r: (float(r[2]), str(r[0]), int(r[1])),
            )
            all_points_arg = tuple(
                (
                    int(r[3]),
                    int(r[4]),
                    int(r[5]),
                    float(r[6]),
                    str(r[7] or ""),
                    int(r[8]),
                )
                for r in ordered
            )
            cache_key = (warm_key, all_points_arg)

        cached = self.global_fit_cache.get(model_name)
        if cached is not None and cached[0] == cache_key:
            base_fit = cached[1]
        else:
            if on_fit_computed is not None:
                on_fit_computed()
            base_fit = compute_roofline_fit(warm_key, all_points=all_points_arg)
            self.global_fit_cache[model_name] = (cache_key, base_fit)

        pre_avail = bool(base_fit.get("prefill_available"))
        if not pre_cand_ok:
            if g_all < _FIT_PREFILL_MIN_STEPS:
                g_pre_reason = "min_steps"
            elif g_p_spread < _FIT_PREFILL_MIN_SPREAD:
                g_pre_reason = "min_spread"
            else:
                g_pre_reason = "min_high_steps"
        else:
            g_pre_reason = str(base_fit.get("prefill_reason") or "ok")

        if not cac_cand_ok:
            if g_low_p_cnt < _FIT_CACHE_MIN_STEPS:
                g_cac_reason = "min_steps"
            elif g_c_spread < _FIT_CACHE_MIN_SPREAD:
                g_cac_reason = "min_spread"
            else:
                g_cac_reason = "min_high_steps"
        else:
            g_cac_reason = str(base_fit.get("cache_reason") or "ok")

        fit: dict[str, Any] = {
            **base_fit,
            "warm_count": g_warm,
            "cold_count": g_cold,
            "cold_excluded_count": 0 if pre_avail else g_cold,
            "prefill_reason": g_pre_reason,
            "prefill_spread": g_p_spread,
            "high_prefill_count": g_hi_p,
            "cache_reason": g_cac_reason,
            "cache_spread": g_c_spread,
            "high_cache_count": g_hi_c,
        }
        if fit["available"]:
            b_val = float(fit["inferred_ttft_s"])
            s_dec_val = float(fit["slope_s_per_tok"])
            s_pre_val = float(fit.get("prefill_slope_s_per_tok") or 0.0)
            s_cac_val = float(fit.get("cache_slope_s_per_tok") or 0.0)
            sig = (
                g_warm,
                g_cold if pre_avail else 0,
                b_val,
                s_dec_val,
                s_pre_val,
                s_cac_val,
                pre_avail,
            )
            if self.synced_delay_sig.get(model_name) != sig:
                if pre_avail:
                    affected_rows = self.db.execute(
                        """
                        SELECT DISTINCT conv_id
                        FROM step_lite
                        WHERE model_name = ?
                          AND delay_finalized = 0
                          AND is_checkpoint = 0
                          AND duration_s IS NOT NULL
                          AND duration_s > 0
                          AND output_tokens > 0
                        """,
                        (model_name,),
                    ).fetchall()
                    if affected_rows:
                        affected_cids = [str(r[0]) for r in affected_rows]
                        warm_fin = 1 if g_warm >= _DELAY_FREEZE_MIN_WARM else 0
                        cold_fin = (
                            1
                            if (g_warm >= _DELAY_FREEZE_MIN_WARM and g_hi_p >= 10)
                            else 0
                        )
                        with self.db:
                            self.db.execute(
                                """
                                UPDATE step_lite
                                SET est_delay_s = ROUND(MAX(0.0, duration_s - (? + ? * input_tokens + ? * cache_read_tokens + ? * output_tokens)), 2),
                                    delay_finalized = (CASE WHEN cold_cache_spike = 1 THEN ? ELSE ? END)
                                WHERE model_name = ?
                                  AND delay_finalized = 0
                                  AND is_checkpoint = 0
                                  AND duration_s IS NOT NULL
                                  AND duration_s > 0
                                  AND output_tokens > 0
                                """,
                                (
                                    b_val,
                                    s_pre_val,
                                    s_cac_val,
                                    s_dec_val,
                                    cold_fin,
                                    warm_fin,
                                    model_name,
                                ),
                            )
                            self.refresh_conv_rollup_tables(affected_cids)
                else:
                    affected_rows = self.db.execute(
                        """
                        SELECT DISTINCT conv_id
                        FROM step_lite
                        WHERE model_name = ?
                          AND delay_finalized = 0
                          AND is_checkpoint = 0
                          AND cold_cache_spike = 0
                          AND duration_s IS NOT NULL
                          AND duration_s > 0
                          AND output_tokens > 0
                        """,
                        (model_name,),
                    ).fetchall()
                    if affected_rows:
                        affected_cids = [str(r[0]) for r in affected_rows]
                        new_fin = 1 if g_warm >= _DELAY_FREEZE_MIN_WARM else 0
                        with self.db:
                            self.db.execute(
                                """
                                UPDATE step_lite
                                SET est_delay_s = ROUND(MAX(0.0, duration_s - (? + ? * input_tokens + ? * cache_read_tokens + ? * output_tokens)), 2),
                                    delay_finalized = ?
                                WHERE model_name = ?
                                  AND delay_finalized = 0
                                  AND is_checkpoint = 0
                                  AND cold_cache_spike = 0
                                  AND duration_s IS NOT NULL
                                  AND duration_s > 0
                                  AND output_tokens > 0
                                """,
                                (
                                    b_val,
                                    s_pre_val,
                                    s_cac_val,
                                    s_dec_val,
                                    new_fin,
                                    model_name,
                                ),
                            )
                            self.refresh_conv_rollup_tables(affected_cids)
                self.synced_delay_sig[model_name] = sig

        return fit

    def query_window_val_rows(
        self,
        cutoff_epoch: float,
        conv_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if conv_ids is not None and len(conv_ids) == 0:
            return []
        boundary_hour = int(cutoff_epoch // 3600) * 3600
        ceil_hour = boundary_hour + 3600
        if conv_ids is None:
            sql = """
                SELECT model_name,
                       SUM(steps_count),
                       SUM(input_tokens),
                       SUM(cache_read_tokens),
                       SUM(output_tokens)
                FROM (
                    SELECT model_name, steps_count, input_tokens, cache_read_tokens, output_tokens
                    FROM hour_model_totals
                    WHERE hour_epoch >= ?
                    UNION ALL
                    SELECT model_name,
                           COUNT(*),
                           COALESCE(SUM(input_tokens), 0),
                           COALESCE(SUM(cache_read_tokens), 0),
                           COALESCE(SUM(output_tokens), 0)
                    FROM step_lite
                    WHERE epoch_s >= ? AND epoch_s < ?
                    GROUP BY model_name
                )
                GROUP BY model_name
            """
            params: tuple[Any, ...] = (ceil_hour, cutoff_epoch, ceil_hour)
        else:
            placeholders = ",".join("?" for _ in conv_ids)
            sql = f"""
                SELECT model_name,
                       SUM(steps_count),
                       SUM(input_tokens),
                       SUM(cache_read_tokens),
                       SUM(output_tokens)
                FROM (
                    SELECT model_name, steps_count, input_tokens, cache_read_tokens, output_tokens
                    FROM hour_model_totals
                    WHERE conv_id IN ({placeholders}) AND hour_epoch >= ?
                    UNION ALL
                    SELECT model_name,
                           COUNT(*),
                           COALESCE(SUM(input_tokens), 0),
                           COALESCE(SUM(cache_read_tokens), 0),
                           COALESCE(SUM(output_tokens), 0)
                    FROM step_lite
                    WHERE conv_id IN ({placeholders}) AND epoch_s >= ? AND epoch_s < ?
                    GROUP BY model_name
                )
                GROUP BY model_name
            """
            params = (*conv_ids, ceil_hour, *conv_ids, cutoff_epoch, ceil_hour)

        rows = self.db.execute(sql, params).fetchall()
        return [
            {
                "model_name": str(r[0]),
                "steps_count": int(r[1] or 0),
                "input_tokens": int(r[2] or 0),
                "cache_read_tokens": int(r[3] or 0),
                "output_tokens": int(r[4] or 0),
            }
            for r in rows
        ]

    def query_window_models(
        self,
        cutoff_epoch: float | None,
        conv_ids: list[str] | None = None,
    ) -> list[str]:
        """Return distinct model names active inside the given window."""
        if conv_ids is not None and len(conv_ids) == 0:
            return []
        if cutoff_epoch is None:
            if conv_ids is None:
                return [
                    str(r[0])
                    for r in self.db.execute(
                        "SELECT DISTINCT model_name FROM conv_model_totals"
                    ).fetchall()
                ]
            placeholders = ",".join("?" for _ in conv_ids)
            return [
                str(r[0])
                for r in self.db.execute(
                    f"SELECT DISTINCT model_name FROM conv_model_totals WHERE conv_id IN ({placeholders})",
                    tuple(conv_ids),
                ).fetchall()
            ]
        boundary_hour = int(cutoff_epoch // 3600) * 3600
        ceil_hour = boundary_hour + 3600
        if conv_ids is None:
            return [
                str(r[0])
                for r in self.db.execute(
                    """
                    SELECT DISTINCT model_name FROM hour_model_totals WHERE hour_epoch >= ?
                    UNION
                    SELECT DISTINCT model_name FROM step_lite WHERE epoch_s >= ? AND epoch_s < ?
                    UNION
                    SELECT DISTINCT model_name FROM failed_turn_lite WHERE epoch_s >= ? AND epoch_s < ?
                    """,
                    (ceil_hour, cutoff_epoch, ceil_hour, cutoff_epoch, ceil_hour),
                ).fetchall()
            ]
        placeholders = ",".join("?" for _ in conv_ids)
        return [
            str(r[0])
            for r in self.db.execute(
                f"""
                SELECT DISTINCT model_name FROM hour_model_totals WHERE conv_id IN ({placeholders}) AND hour_epoch >= ?
                UNION
                SELECT DISTINCT model_name FROM step_lite WHERE conv_id IN ({placeholders}) AND epoch_s >= ? AND epoch_s < ?
                UNION
                SELECT DISTINCT model_name FROM failed_turn_lite WHERE conv_id IN ({placeholders}) AND epoch_s >= ? AND epoch_s < ?
                """,
                (*conv_ids, ceil_hour, *conv_ids, cutoff_epoch, ceil_hour, *conv_ids, cutoff_epoch, ceil_hour),
            ).fetchall()
        ]

    def query_all_model_aggregates(
        self,
        cutoff_epoch: float | None,
        conv_ids: list[str] | None = None,
    ) -> tuple[list[tuple[Any, ...]], list[str] | None]:
        """Return (model_agg_rows, distinct_conv_ids_or_none) after delay sync."""
        if conv_ids is not None and len(conv_ids) == 0:
            return [], []
        if cutoff_epoch is None:
            if conv_ids is None:
                agg_rows = self.db.execute(
                    """
                    SELECT model_name,
                           SUM(steps_count), SUM(compaction_steps_count),
                           SUM(input_tokens), SUM(cache_read_tokens),
                           SUM(output_tokens), SUM(est_thinking_tokens), SUM(est_visible_tokens),
                           SUM(model_time_s), SUM(timed_steps_count),
                           SUM(cold_timed_steps_count), SUM(cold_model_time_s),
                           SUM(timed_output_tokens), SUM(tool_time_s),
                           SUM(warm_timed_steps_count), SUM(cold_excluded_count),
                           SUM(est_delay_steps_count), SUM(est_total_delay_s),
                           SUM(compacted_events_count),
                           SUM(wasted_time_s), SUM(retry_steps_count), SUM(failed_turns_count),
                           SUM(cold_delay_steps_count), SUM(est_cold_delay_s)
                    FROM conv_model_totals
                    GROUP BY model_name
                    """
                ).fetchall()
                return agg_rows, None
            placeholders = ",".join("?" for _ in conv_ids)
            agg_rows = self.db.execute(
                f"""
                SELECT model_name,
                       SUM(steps_count), SUM(compaction_steps_count),
                       SUM(input_tokens), SUM(cache_read_tokens),
                       SUM(output_tokens), SUM(est_thinking_tokens), SUM(est_visible_tokens),
                       SUM(model_time_s), SUM(timed_steps_count),
                       SUM(cold_timed_steps_count), SUM(cold_model_time_s),
                       SUM(timed_output_tokens), SUM(tool_time_s),
                       SUM(warm_timed_steps_count), SUM(cold_excluded_count),
                       SUM(est_delay_steps_count), SUM(est_total_delay_s),
                       SUM(compacted_events_count),
                       SUM(wasted_time_s), SUM(retry_steps_count), SUM(failed_turns_count),
                       SUM(cold_delay_steps_count), SUM(est_cold_delay_s)
                FROM conv_model_totals
                WHERE conv_id IN ({placeholders})
                GROUP BY model_name
                """,
                tuple(conv_ids),
            ).fetchall()
            win_conv_rows = self.db.execute(
                f"SELECT DISTINCT conv_id FROM conv_model_totals WHERE conv_id IN ({placeholders})",
                tuple(conv_ids),
            ).fetchall()
            return agg_rows, [str(r[0]) for r in win_conv_rows]

        boundary_hour = int(cutoff_epoch // 3600) * 3600
        ceil_hour = boundary_hour + 3600
        if conv_ids is None:
            union_sql = _events_union_sql("epoch_s >= ? AND epoch_s < ?")
            agg_rows = self.db.execute(
                f"""
                SELECT model_name,
                       SUM(steps_count), SUM(compaction_steps_count),
                       SUM(input_tokens), SUM(cache_read_tokens),
                       SUM(output_tokens), SUM(est_thinking_tokens), SUM(est_visible_tokens),
                       SUM(model_time_s), SUM(timed_steps_count),
                       SUM(cold_timed_steps_count), SUM(cold_model_time_s),
                       SUM(timed_output_tokens), SUM(tool_time_s),
                       SUM(warm_timed_steps_count), SUM(cold_excluded_count),
                       SUM(est_delay_steps_count), SUM(est_total_delay_s),
                       SUM(compacted_events_count),
                       SUM(wasted_time_s), SUM(retry_steps_count), SUM(failed_turns_count),
                       SUM(cold_delay_steps_count), SUM(est_cold_delay_s)
                FROM (
                    SELECT model_name, steps_count, compaction_steps_count,
                           input_tokens, cache_read_tokens,
                           output_tokens, est_thinking_tokens, est_visible_tokens,
                           model_time_s, timed_steps_count,
                           cold_timed_steps_count, cold_model_time_s,
                           timed_output_tokens, tool_time_s,
                           warm_timed_steps_count, cold_excluded_count,
                           est_delay_steps_count, est_total_delay_s,
                           compacted_events_count,
                           wasted_time_s, retry_steps_count, failed_turns_count,
                           cold_delay_steps_count, est_cold_delay_s
                    FROM hour_model_totals
                    WHERE hour_epoch >= ?
                    UNION ALL
                    SELECT model_name, {_ROLLUP_AGG_COLS_SQL}
                    FROM ({union_sql})
                    GROUP BY model_name
                )
                GROUP BY model_name
                """,
                (ceil_hour, cutoff_epoch, ceil_hour, cutoff_epoch, ceil_hour),
            ).fetchall()
            win_conv_rows = self.db.execute(
                """
                SELECT DISTINCT conv_id FROM hour_model_totals WHERE hour_epoch >= ?
                UNION
                SELECT DISTINCT conv_id FROM step_lite WHERE epoch_s >= ? AND epoch_s < ?
                UNION
                SELECT DISTINCT conv_id FROM failed_turn_lite WHERE epoch_s >= ? AND epoch_s < ?
                """,
                (ceil_hour, cutoff_epoch, ceil_hour, cutoff_epoch, ceil_hour),
            ).fetchall()
            return agg_rows, [str(r[0]) for r in win_conv_rows]

        placeholders = ",".join("?" for _ in conv_ids)
        union_sql = _events_union_sql(
            f"conv_id IN ({placeholders}) AND epoch_s >= ? AND epoch_s < ?"
        )
        params = (
            *conv_ids,
            ceil_hour,
            *conv_ids,
            cutoff_epoch,
            ceil_hour,
            *conv_ids,
            cutoff_epoch,
            ceil_hour,
        )
        agg_rows = self.db.execute(
            f"""
            SELECT model_name,
                   SUM(steps_count), SUM(compaction_steps_count),
                   SUM(input_tokens), SUM(cache_read_tokens),
                   SUM(output_tokens), SUM(est_thinking_tokens), SUM(est_visible_tokens),
                   SUM(model_time_s), SUM(timed_steps_count),
                   SUM(cold_timed_steps_count), SUM(cold_model_time_s),
                   SUM(timed_output_tokens), SUM(tool_time_s),
                   SUM(warm_timed_steps_count), SUM(cold_excluded_count),
                   SUM(est_delay_steps_count), SUM(est_total_delay_s),
                   SUM(compacted_events_count),
                   SUM(wasted_time_s), SUM(retry_steps_count), SUM(failed_turns_count),
                   SUM(cold_delay_steps_count), SUM(est_cold_delay_s)
            FROM (
                SELECT model_name, steps_count, compaction_steps_count,
                       input_tokens, cache_read_tokens,
                       output_tokens, est_thinking_tokens, est_visible_tokens,
                       model_time_s, timed_steps_count,
                       cold_timed_steps_count, cold_model_time_s,
                       timed_output_tokens, tool_time_s,
                       warm_timed_steps_count, cold_excluded_count,
                       est_delay_steps_count, est_total_delay_s,
                       compacted_events_count,
                       wasted_time_s, retry_steps_count, failed_turns_count,
                       cold_delay_steps_count, est_cold_delay_s
                FROM hour_model_totals
                WHERE conv_id IN ({placeholders}) AND hour_epoch >= ?
                UNION ALL
                SELECT model_name, {_ROLLUP_AGG_COLS_SQL}
                FROM ({union_sql})
                GROUP BY model_name
            )
            GROUP BY model_name
            """,
            params,
        ).fetchall()
        win_conv_rows = self.db.execute(
            f"""
            SELECT DISTINCT conv_id FROM hour_model_totals WHERE conv_id IN ({placeholders}) AND hour_epoch >= ?
            UNION
            SELECT DISTINCT conv_id FROM step_lite WHERE conv_id IN ({placeholders}) AND epoch_s >= ? AND epoch_s < ?
            UNION
            SELECT DISTINCT conv_id FROM failed_turn_lite WHERE conv_id IN ({placeholders}) AND epoch_s >= ? AND epoch_s < ?
            """,
            params,
        ).fetchall()
        return agg_rows, [str(r[0]) for r in win_conv_rows]
