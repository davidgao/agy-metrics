"""Shared data models, constants, and parsing helpers for agy-metrics."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
from typing import Any

ALL_CONVERSATIONS_ID = "__all__"
PROJECT_SCOPE_PREFIX = "project:"
UNASSIGNED_PROJECT_ID = "__unassigned__"
UNASSIGNED_PROJECT_NAME = "No Project"
VALID_WINDOWS = ("5h", "24h", "7d", "30d", "all")
_WINDOW_SECONDS = {
    "5h": 5.0 * 3600.0,
    "24h": 86400.0,
    "7d": 7.0 * 86400.0,
    "30d": 30.0 * 86400.0,
}
_MAX_HYDRATED_CONVS = 16
_ROLLUP_SCHEMA_VERSION = 7

_CHARS_PER_TOKEN = 3.8
_FIT_MIN_WARM_STEPS = 4
_FIT_MIN_TOKEN_SPREAD = 200
_FIT_MIN_PAIR_DX = 50
_FIT_MAX_RECENT_STEPS = 100
_FIT_RECENCY_HALF_LIFE = 35.0
_DELAY_FREEZE_MIN_WARM = 15
_FIT_TARGET_TAU = 0.05
_FIT_MIN_BELOW_WEIGHT = 1.5
_FIT_SCALE_N0 = 600.0
_FIT_MIN_TTFT_S = 0.05
_FIT_MIN_SLOPE_S_PER_TOK = 2e-4
_LIVE_GENERATING_STALE_MS = 360_000

_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
_TAG_BLOCK_RE = re.compile(r"<([A-Za-z0-9_:-]+)>.*?</\1>", re.DOTALL)
_TAG_SINGLE_RE = re.compile(r"<[^>]+>")
_MODEL_CHANGE_RE = re.compile(
    r"changed setting `Model Selection` from .+? to (.+?)\.(?:\s|$)"
)
_SUMMARY_TAG_RE = re.compile(r"<summary>\s*(.*?)\s*</summary>", re.DOTALL)
_CREATED_AT_HEADER_RE = re.compile(r"^Created At:\s*(\S+)", re.MULTILINE)
_COMPLETED_AT_HEADER_RE = re.compile(r"^Completed At:\s*(\S+)", re.MULTILINE)
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
_SAFE_CONV_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_ORCH_PAREN_RE = re.compile(r"\(([^)]+)\)")
_SUBAGENT_PREFIX_RE = re.compile(r"^subagent\s*·\s*(.+)$", re.IGNORECASE)
_DATE_STAMP_TOKEN_RE = re.compile(r"^20\d{6}$")
_PARAM_SIZE_TOKEN_RE = re.compile(r"^\d+(?:\.\d+)?[bBmMkK]$")

_SHORT_MODEL_ALIASES = {
    "flash_lite": "Gemini Flash-Lite",
    "flash-lite": "Gemini Flash-Lite",
    "flash lite": "Gemini Flash-Lite",
    "flash": "Gemini Flash",
    "pro": "Gemini Pro",
    "carbon": "Gemini 4 Carbon",
    "argon": "Gemini 4 Argon",
    "hydrogen": "Gemini 4 Hydrogen",
    "opus": "Claude Opus",
    "sonnet": "Claude Sonnet",
    "haiku": "Claude Haiku",
    "gpt-oss": "GPT-OSS",
    "gpt_oss": "GPT-OSS",
    "gpt oss": "GPT-OSS",
}

_EFFORT_QUALIFIERS = {
    "high": "High",
    "medium": "Medium",
    "low": "Low",
    "minimal": "Minimal",
    "thinking": "Thinking",
}


def _parse_iso_epoch(raw: str | None) -> float | None:
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _safe_int(value: Any, default: int = 0) -> int:
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return default
        return int(value)
    try:
        if isinstance(value, str) and "." in value:
            fval = float(value)
            return int(fval) if math.isfinite(fval) else default
        return int(value)
    except (ValueError, TypeError, OverflowError):
        return default


def _unwrap_arg_str(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        return str(value)
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == '"' and stripped[-1] == '"':
        try:
            decoded = json.loads(stripped)
            if isinstance(decoded, str):
                return decoded
        except json.JSONDecodeError:
            return stripped[1:-1]
    return stripped


def _extract_user_title_and_text(raw_content: str) -> tuple[str, str]:
    match = _USER_REQUEST_RE.search(raw_content)
    if match:
        clean = match.group(1).strip()
    else:
        clean = _TAG_BLOCK_RE.sub("", raw_content)
        clean = _TAG_SINGLE_RE.sub("", clean).strip()
        if not clean:
            clean = raw_content.strip()

    first_line = ""
    for line in clean.splitlines():
        s = " ".join(line.split())
        if s:
            first_line = s
            break
    if len(first_line) > 80:
        title = first_line[:77].rstrip() + "..."
    else:
        title = first_line
    return title, clean


def _is_orchestrator_name(name: str) -> bool:
    return "orchestrator" in name.strip().lower()


def _is_unattributed_model(name: str) -> bool:
    return not name or name.strip().lower() in ("unknown", "auto")


def _extract_base_model_from_orchestrator(
    transcript_model: str, phase: str
) -> str:
    """Resolve the underlying base model name when an Orchestrator turn inherits."""
    if phase.strip().lower() == "executor":
        return "Hydrogen"
    m_hint = _ORCH_PAREN_RE.search(transcript_model or "")
    hint = m_hint.group(1).strip() if m_hint else ""
    return hint or "Carbon"


def humanize_model_name(raw: str) -> str:
    """Normalize a raw backend slug or shorthand alias into a clean display label."""
    s = (raw or "").strip()
    if not s or _is_unattributed_model(s):
        return s

    m_sub = _SUBAGENT_PREFIX_RE.match(s)
    if m_sub:
        inner_clean = humanize_model_name(m_sub.group(1).strip())
        if not inner_clean or _is_unattributed_model(inner_clean):
            return inner_clean or "unknown"
        if inner_clean.startswith("Subagent · "):
            return inner_clean
        return f"Subagent · {inner_clean}"

    if _is_orchestrator_name(s):
        return s

    alias = _SHORT_MODEL_ALIASES.get(s.lower())
    if alias is not None:
        return alias

    if " " in s and any(ch.isupper() for ch in s):
        return s
    if (
        "-" not in s
        and "_" not in s
        and " " not in s
        and any(ch.isupper() for ch in s)
        and any(ch.islower() for ch in s)
    ):
        return s

    raw_tokens = [t for t in re.split(r"[-_\s]+", s) if t]
    if not raw_tokens:
        return s

    tokens = [
        t
        for t in raw_tokens
        if t.lower() != "tiered" and not _DATE_STAMP_TOKEN_RE.fullmatch(t)
    ]
    if not tokens:
        tokens = list(raw_tokens)
    while len(tokens) > 1 and tokens[-1].lower() == "latest":
        tokens.pop()

    qualifiers: list[str] = []
    while len(tokens) > 1 and tokens[-1].lower() in _EFFORT_QUALIFIERS:
        qualifiers.append(_EFFORT_QUALIFIERS[tokens.pop().lower()])
    qualifiers.reverse()

    merged: list[str] = []
    idx = 0
    while idx < len(tokens):
        if idx + 1 < len(tokens):
            a, b = tokens[idx], tokens[idx + 1]
            al, bl = a.lower(), b.lower()
            if (al, bl) == ("gpt", "oss"):
                merged.append("GPT-OSS")
                idx += 2
                continue
            if (al, bl) == ("flash", "lite"):
                merged.append("Flash-Lite")
                idx += 2
                continue
            if (
                len(a) == 1
                and len(b) == 1
                and a.isdigit()
                and a != "0"
                and b.isdigit()
            ):
                merged.append(f"{a}.{b}")
                idx += 2
                continue
        merged.append(tokens[idx])
        idx += 1

    formatted: list[str] = []
    for tok in merged:
        if tok in ("GPT-OSS", "Flash-Lite") or re.fullmatch(r"\d+(?:\.\d+)?", tok):
            formatted.append(tok)
        elif tok.lower() == "gpt":
            formatted.append("GPT")
        elif tok.lower() == "oss":
            formatted.append("OSS")
        elif _PARAM_SIZE_TOKEN_RE.fullmatch(tok):
            formatted.append(tok.upper())
        else:
            formatted.append(tok.capitalize())

    base_str = " ".join(formatted)
    base_alias = _SHORT_MODEL_ALIASES.get(base_str.lower())
    if base_alias is not None:
        base_str = base_alias
    if qualifiers:
        return f"{base_str} ({', '.join(qualifiers)})"
    return base_str


def normalize_step_model(raw: str, *, is_subagent: bool = False) -> str:
    """Format a step or hook model name and prefix subagent steps consistently."""
    s = (raw or "").strip()
    if not s or _is_unattributed_model(s):
        return s or "unknown"

    if is_subagent:
        m_sub = _SUBAGENT_PREFIX_RE.match(s)
        inner = m_sub.group(1).strip() if m_sub else s
        if not inner or _is_unattributed_model(inner):
            return inner or "unknown"
        if _is_orchestrator_name(inner):
            phase = "Executor" if "executor" in inner.lower() else "Planner"
            base = _extract_base_model_from_orchestrator(inner, phase)
            clean = humanize_model_name(base)
        else:
            clean = humanize_model_name(inner)
        if not clean or _is_unattributed_model(clean):
            return clean or "unknown"
        return clean if clean.startswith("Subagent · ") else f"Subagent · {clean}"

    clean = humanize_model_name(s)
    return clean or "unknown"


@dataclass
class _RawGenericStep:
    step_index: int
    tool_name: str
    created_at_epoch: float | None
    header_created_epoch: float | None
    header_completed_epoch: float | None
    char_len: int


@dataclass
class _RawErrorEvent:
    step_index: int
    created_at_epoch: float | None
    model_name: str


@dataclass
class _RawFailedTurn:
    seq: int
    epoch_s: float
    model_name: str
    wasted_s: float


@dataclass
class _RawPlannerStep:
    ordinal: int
    step_index: int
    created_at: str
    created_at_epoch: float | None
    model_name: str
    input_tokens: int
    cache_read_tokens: int
    total_context: int
    cache_hit_pct: float
    output_tokens: int
    est_thinking_tokens: int
    est_visible_tokens: int
    tool_names: list[str]
    compacted: bool
    cold_cache_spike: bool
    user_chars_before: int
    model_chars_before: int
    tool_chars_before: int
    tool_chars_by_name_before: dict[str, int]
    summary_chars_before: int = 0
    step_kind: str = "planner"
    model_switched: bool = False
    compaction_event: bool = False
    preceded_by_checkpoint: bool = False
    checkpoint_epoch: float | None = None
    error_steps_before: int = 0
    step_status: str = "DONE"
    paired_generics: list[_RawGenericStep] = field(default_factory=list)


@dataclass
class _HookWindow:
    t_start_ms: int
    model_name: str
    t_first_pre_tool_ms: int | None = None
    t_first_post_tool_ms: int | None = None
    t_last_post_tool_ms: int | None = None
    t_post_inv_ms: int | None = None
    t_stop_ms: int | None = None
    termination_reason: str | None = None
    first_tool_step_idx: int | None = None
    has_stop_error: bool = False
    is_canceled: bool = False
    is_superseded: bool = False


@dataclass
class StepMetrics:
    ordinal: int
    step_index: int
    created_at: str
    model_name: str
    input_tokens: int
    cache_read_tokens: int
    total_context: int
    cache_hit_pct: float
    output_tokens: int
    est_thinking_tokens: int
    est_visible_tokens: int
    duration_s: float | None
    duration_source: str | None
    effective_tps: float | None
    tool_exec_s: float
    tool_names: list[str]
    compacted: bool
    cold_cache_spike: bool
    model_switched: bool = False
    step_kind: str = "planner"
    compaction_event: bool = False
    retry_wait_s: float = 0.0
    retry_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("compaction_event", None)
        return payload


@dataclass
class ConversationState:
    conversation_id: str
    title: str = ""
    current_model_name: str = ""
    transcript_path: Path | None = None
    transcript_inode: int | None = None
    transcript_offset: int = 0
    hook_path: Path | None = None
    hook_inode: int | None = None
    hook_offset: int = 0
    cold_loaded: bool = False
    last_transcript_check_ms: int = 0
    first_seen_ms: dict[int, int] = field(default_factory=dict)
    raw_steps: list[_RawPlannerStep] = field(default_factory=list)
    hook_events: list[dict[str, Any]] = field(default_factory=list)
    pending_tools: list[tuple[str, str, str]] = field(default_factory=list)
    last_planner_step: _RawPlannerStep | None = None
    summary_chars: int = 0
    user_chars: int = 0
    model_chars: int = 0
    tool_chars: int = 0
    tool_chars_by_name: dict[str, int] = field(default_factory=dict)
    system_baseline_tokens: int | None = None
    needs_system_rebaseline: bool = False
    pending_post_compaction: bool = False
    pending_compaction_event: bool = False
    compacted_count: int = 0
    child_subagents: dict[str, str] = field(default_factory=dict)
    child_subagent_models: dict[str, str] = field(default_factory=dict)
    parent_id: str | None = None
    is_subagent: bool = False
    steps: list[StepMetrics] = field(default_factory=list)
    hook_reported_transcript: str = ""
    hook_reported_model: str = ""
    transcript_model: str = ""
    orchestrator_active: bool = False
    orchestrator_phase: str = ""
    fallback_model_hint: str = ""
    status: str = "idle"
    generating_since_ms: int | None = None
    updated_at_ms: int = 0
    current_tokens: int = 0
    hydrated: bool = True
    persisted_sig: tuple[str, int, int, int, str, int, int, int] | None = None
    persisted_steps_fp: tuple[Any, ...] | None = None
    last_hook_ts_ms: int = 0
    summary_steps_count: int = 0
    own_input_tokens: int = 0
    own_cache_read_tokens: int = 0
    own_output_tokens: int = 0
    own_est_thinking_tokens: int = 0
    own_est_visible_tokens: int = 0
    last_accessed_mono: float = 0.0
    last_checkpoint_epoch: float | None = None
    pending_error_steps: int = 0
    raw_error_events: list[_RawErrorEvent] = field(default_factory=list)
    canceled_epochs: list[float] = field(default_factory=list)
    failed_turns: list[_RawFailedTurn] = field(default_factory=list)
    last_user_input_epoch: float | None = None
    prompt_title: str = ""
    external_title: str = ""
    agent_name: str = ""
    external_parent_id: str = ""
    external_project_id: str = ""
    external_workspace_paths: list[str] = field(default_factory=list)
    hook_workspace_paths: list[str] = field(default_factory=list)
    project_id: str = ""
    project_name: str = ""
    nesting_depth: int = 0

    def dehydrate(self) -> None:
        """Drop heavyweight step lists while keeping summary and cache metadata."""
        self.transcript_path = None
        self.transcript_inode = None
        self.transcript_offset = 0
        self.hook_path = None
        self.hook_inode = None
        self.hook_offset = 0
        self.last_hook_ts_ms = 0
        self.cold_loaded = False
        self.last_transcript_check_ms = 0
        self.first_seen_ms.clear()
        self.raw_steps.clear()
        self.hook_events.clear()
        self.pending_tools.clear()
        self.last_planner_step = None
        self.summary_chars = 0
        self.user_chars = 0
        self.model_chars = 0
        self.tool_chars = 0
        self.tool_chars_by_name.clear()
        self.system_baseline_tokens = None
        self.needs_system_rebaseline = False
        self.pending_post_compaction = False
        self.pending_compaction_event = False
        self.transcript_model = ""
        self.orchestrator_active = False
        self.orchestrator_phase = ""
        self.steps.clear()
        self.last_checkpoint_epoch = None
        self.pending_error_steps = 0
        self.raw_error_events.clear()
        self.canceled_epochs.clear()
        self.failed_turns.clear()
        self.last_user_input_epoch = None
        self.hydrated = False

    def reset_transcript_state(self) -> None:
        self.transcript_inode = None
        self.transcript_offset = 0
        self.last_transcript_check_ms = 0
        self.raw_steps.clear()
        self.pending_tools.clear()
        self.last_planner_step = None
        self.summary_chars = 0
        self.user_chars = 0
        self.model_chars = 0
        self.tool_chars = 0
        self.tool_chars_by_name.clear()
        self.system_baseline_tokens = None
        self.needs_system_rebaseline = False
        self.pending_post_compaction = False
        self.pending_compaction_event = False
        self.compacted_count = 0
        self.child_subagents.clear()
        self.child_subagent_models.clear()
        self.transcript_model = ""
        self.orchestrator_active = False
        self.orchestrator_phase = ""
        self.prompt_title = ""
        self.title = self.external_title
        self.last_checkpoint_epoch = None
        self.pending_error_steps = 0
        self.raw_error_events.clear()
        self.canceled_epochs.clear()
        self.failed_turns.clear()
        self.last_user_input_epoch = None

    def reset_hook_state(self) -> None:
        self.hook_inode = None
        self.hook_offset = 0
        self.last_hook_ts_ms = 0
        self.hook_events.clear()
        self.hook_reported_transcript = ""
        self.hook_reported_model = ""
        self.hook_workspace_paths.clear()
