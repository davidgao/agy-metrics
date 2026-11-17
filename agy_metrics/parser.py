"""Transcript JSONL, hook JSONL, context breakdown, and subagent tree parser."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
import json
from pathlib import Path
import time
from typing import Any

from .metadata import uri_or_path_to_norm_path
from .models import (
    ConversationState,
    StepMetrics,
    _CHARS_PER_TOKEN,
    _COMPLETED_AT_HEADER_RE,
    _CREATED_AT_HEADER_RE,
    _HookWindow,
    _LIVE_GENERATING_STALE_MS,
    _MODEL_CHANGE_RE,
    _RawErrorEvent,
    _RawFailedTurn,
    _RawGenericStep,
    _RawPlannerStep,
    _SUMMARY_TAG_RE,
    _UUID_RE,
    _extract_base_model_from_orchestrator,
    _extract_user_title_and_text,
    _is_orchestrator_name,
    _is_unattributed_model,
    _safe_int,
    _unwrap_arg_str,
    _parse_iso_epoch,
    normalize_step_model,
)

_CHILD_SPAWN_TOOLS = ("invoke_subagent", "swarm", "run_workflow")


def format_step_model(conv: ConversationState) -> str:
    """Format the attributed model/phase label for the current step."""
    if conv.orchestrator_active:
        base = (
            conv.transcript_model
            if (conv.transcript_model and _is_orchestrator_name(conv.transcript_model))
            else "Orchestrator"
        )
        phase = conv.orchestrator_phase or "Planner"
        if not conv.is_subagent:
            return f"{base} · {phase}"
        if (
            conv.hook_reported_model
            and conv.hook_reported_model != "auto"
            and not _is_orchestrator_name(conv.hook_reported_model)
        ):
            return normalize_step_model(conv.hook_reported_model, is_subagent=True)
        return normalize_step_model(f"{base} · {phase}", is_subagent=True)
    candidate = (
        conv.transcript_model
        or (
            conv.hook_reported_model
            if conv.hook_reported_model and conv.hook_reported_model != "auto"
            else ""
        )
        or conv.fallback_model_hint
        or conv.hook_reported_model
        or "unknown"
    )
    return normalize_step_model(candidate, is_subagent=conv.is_subagent)


def promote_to_orchestrator(conv: ConversationState, phase: str) -> None:
    """Retroactively label prior unattributed steps when Orchestrator phase signals appear."""
    if not conv.orchestrator_active:
        conv.orchestrator_active = True
        base = (
            conv.transcript_model
            if (conv.transcript_model and _is_orchestrator_name(conv.transcript_model))
            else "Orchestrator"
        )
        planner_label = f"{base} · Planner"
        for i in range(len(conv.raw_steps) - 1, -1, -1):
            if conv.raw_steps[i].model_name in ("unknown", "auto", base):
                conv.raw_steps[i].model_name = planner_label
            else:
                break
        for i, raw in enumerate(conv.raw_steps):
            raw.model_switched = (
                i > 0 and conv.raw_steps[i - 1].model_name != raw.model_name
            )
    conv.orchestrator_phase = phase


def append_hook_event(conv: ConversationState, ev: dict[str, Any]) -> None:
    """Append a lifecycle hook event to ConversationState, bounding per-turn tool markers."""
    ts_ms = _safe_int(ev.get("ts_ms"))
    if ts_ms > conv.last_hook_ts_ms:
        conv.last_hook_ts_ms = ts_ms
    ev_name = ev.get("event")
    if ev_name in ("PreInvocation", "PostInvocation", "Stop"):
        conv.hook_events.append(ev)
        return
    if ev_name not in ("PreToolUse", "PostToolUse"):
        return

    last_pre_inv_idx = -1
    for idx in range(len(conv.hook_events) - 1, -1, -1):
        if conv.hook_events[idx].get("event") == "PreInvocation":
            last_pre_inv_idx = idx
            break
    if last_pre_inv_idx == -1:
        return

    if ev_name == "PreToolUse":
        for idx in range(last_pre_inv_idx + 1, len(conv.hook_events)):
            if conv.hook_events[idx].get("event") == "PreToolUse":
                if 0 < ts_ms < _safe_int(conv.hook_events[idx].get("ts_ms")):
                    conv.hook_events[idx] = ev
                return
        conv.hook_events.append(ev)
        return

    post_indices = [
        idx
        for idx in range(last_pre_inv_idx + 1, len(conv.hook_events))
        if conv.hook_events[idx].get("event") == "PostToolUse"
    ]
    if not post_indices:
        conv.hook_events.append(ev)
    elif len(post_indices) == 1:
        first_idx = post_indices[0]
        if 0 < ts_ms < _safe_int(conv.hook_events[first_idx].get("ts_ms")):
            conv.hook_events.insert(first_idx, ev)
        else:
            conv.hook_events.append(ev)
    else:
        first_idx, last_idx = post_indices[0], post_indices[1]
        if 0 < ts_ms < _safe_int(conv.hook_events[first_idx].get("ts_ms")):
            conv.hook_events[first_idx] = ev
        elif ts_ms >= _safe_int(conv.hook_events[last_idx].get("ts_ms")):
            conv.hook_events[last_idx] = ev


def ingest_hooks(conv: ConversationState, hook_path: Path | None) -> bool:
    """Incrementally read complete JSONL lines from a conversation's hook log."""
    if hook_path is None:
        if conv.hook_path is not None:
            conv.hook_path = None
            conv.reset_hook_state()
            return True
        return False

    if conv.hook_path != hook_path:
        conv.hook_path = hook_path
        conv.reset_hook_state()

    try:
        st = hook_path.stat()
    except OSError:
        return False

    if conv.hook_inode is not None and (
        st.st_ino != conv.hook_inode or st.st_size < conv.hook_offset
    ):
        conv.reset_hook_state()

    if st.st_size == conv.hook_offset and conv.hook_inode == st.st_ino:
        return False

    consumed_bytes = 0
    parsed_any = False
    try:
        with open(hook_path, "rb") as f:
            f.seek(conv.hook_offset)
            while True:
                raw_line = f.readline()
                if not raw_line or not raw_line.endswith(b"\n"):
                    break
                consumed_bytes += len(raw_line)
                text = raw_line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    ev = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if not isinstance(ev, dict):
                    continue
                append_hook_event(conv, ev)
                parsed_any = True
                t_path = ev.get("transcript_path")
                if isinstance(t_path, str) and t_path:
                    conv.hook_reported_transcript = t_path
                m_name = ev.get("model_name")
                if isinstance(m_name, str) and m_name:
                    conv.hook_reported_model = m_name
                ws_paths = ev.get("workspace_paths")
                if isinstance(ws_paths, list):
                    for wp in ws_paths:
                        if isinstance(wp, str):
                            norm = uri_or_path_to_norm_path(wp)
                            if norm and norm not in conv.hook_workspace_paths:
                                conv.hook_workspace_paths.append(norm)
    except OSError:
        return False

    if consumed_bytes == 0:
        return False

    conv.hook_inode = st.st_ino
    conv.hook_offset += consumed_bytes
    return parsed_any


def _estimate_thinking_and_visible(
    output_tokens: int,
    thinking_text: str,
    vis_chars: int,
    truncated_fields: set[str],
    is_checkpoint: bool = False,
) -> tuple[int, int]:
    """Estimate thinking vs. visible output tokens while guarding truncated compact logs."""
    if not thinking_text:
        if is_checkpoint:
            vis_est = round(vis_chars / _CHARS_PER_TOKEN)
            if (
                "content" not in truncated_fields
                and output_tokens > 0
                and vis_est > 0
                and output_tokens > round(vis_est * 1.15)
            ):
                est_visible = min(output_tokens, vis_est)
                return max(0, output_tokens - est_visible), est_visible
        return 0, output_tokens

    vis_truncated = ("content" in truncated_fields) or (
        "tool_calls" in truncated_fields
    )
    think_truncated = "thinking" in truncated_fields

    if not vis_truncated:
        est_visible = min(output_tokens, round(vis_chars / _CHARS_PER_TOKEN))
        return max(0, output_tokens - est_visible), est_visible

    if not think_truncated:
        est_thinking = min(
            output_tokens, round(len(thinking_text) / _CHARS_PER_TOKEN)
        )
        return est_thinking, max(0, output_tokens - est_thinking)

    tot_chars = len(thinking_text) + vis_chars
    if tot_chars <= 0:
        return 0, output_tokens
    est_thinking = round(output_tokens * (len(thinking_text) / tot_chars))
    est_thinking = max(0, min(output_tokens, est_thinking))
    return est_thinking, max(0, output_tokens - est_thinking)


def process_transcript_step(
    conv: ConversationState,
    step: dict[str, Any],
    is_live: bool,
    now_ms: int,
) -> None:
    """Process one parsed transcript JSONL record into ConversationState."""
    step_type = step.get("type") or ""
    step_index = _safe_int(step.get("step_index"))
    created_at = str(step.get("created_at") or "")
    created_at_epoch = _parse_iso_epoch(created_at)
    step_status = str(step.get("status") or "DONE").strip().upper()

    raw_trunc = step.get("truncated_fields")
    truncated_fields = (
        {str(x) for x in raw_trunc} if isinstance(raw_trunc, list) else set()
    )

    if step_type in ("USER_INPUT", "SYSTEM_MESSAGE"):
        content = str(step.get("content") or "")
        m_model = _MODEL_CHANGE_RE.search(content)
        if m_model:
            new_model = m_model.group(1).strip()
            conv.transcript_model = new_model
            if _is_orchestrator_name(new_model):
                conv.orchestrator_active = True
                conv.orchestrator_phase = "Planner"
            else:
                conv.orchestrator_active = False
                conv.orchestrator_phase = ""

        if step_type == "USER_INPUT":
            if conv.orchestrator_active:
                conv.orchestrator_phase = "Planner"
            conv.pending_tools.clear()
            conv.last_planner_step = None
            conv.pending_error_steps = 0
            if created_at_epoch is not None:
                conv.last_user_input_epoch = created_at_epoch
            title, clean_text = _extract_user_title_and_text(content)
            if not conv.prompt_title and title:
                conv.prompt_title = title
            if not conv.title and title:
                conv.title = title
            conv.user_chars += len(clean_text)
        else:
            if "Plan mode is off: this turn runs on the execution model" in content:
                promote_to_orchestrator(conv, "Executor")
            elif "[plan review]" in content:
                promote_to_orchestrator(conv, "Planner")

    elif step_type == "ERROR_MESSAGE":
        conv.pending_tools.clear()
        conv.last_planner_step = None
        conv.pending_error_steps += 1
        conv.raw_error_events.append(
            _RawErrorEvent(
                step_index=step_index,
                created_at_epoch=created_at_epoch,
                model_name=format_step_model(conv),
            )
        )

    elif step_type == "CHECKPOINT":
        if step_status in ("CANCELED", "CANCELLED", "ERROR"):
            conv.pending_tools.clear()
            conv.last_planner_step = None
            if step_status == "ERROR":
                conv.pending_error_steps += 1
                conv.raw_error_events.append(
                    _RawErrorEvent(
                        step_index=step_index,
                        created_at_epoch=created_at_epoch,
                        model_name=format_step_model(conv),
                    )
                )
            elif created_at_epoch is not None:
                conv.canceled_epochs.append(created_at_epoch)
            return

        content = str(step.get("content") or "")
        input_tokens = _safe_int(step.get("input_tokens"))
        cache_read_tokens = _safe_int(step.get("cache_read_tokens"))
        output_tokens = _safe_int(step.get("output_tokens"))
        total_context = input_tokens + cache_read_tokens

        conv.compacted_count += 1
        conv.pending_post_compaction = True
        conv.last_checkpoint_epoch = created_at_epoch
        conv.pending_error_steps = 0

        if total_context > 0 or output_tokens > 0:
            ordinal = len(conv.raw_steps) + 1
            cache_hit_pct = (
                (cache_read_tokens / total_context * 100.0)
                if total_context > 0
                else 0.0
            )
            cold_cache_spike = bool(
                ordinal > 1 and input_tokens >= 25000 and cache_hit_pct < 20.0
            )
            thinking_text = str(step.get("thinking") or "")
            m_sum = _SUMMARY_TAG_RE.search(content)
            gen_chars = len(m_sum.group(1)) if m_sum else len(content)
            est_thinking_tokens, est_visible_tokens = _estimate_thinking_and_visible(
                output_tokens,
                thinking_text,
                gen_chars,
                truncated_fields,
                is_checkpoint=True,
            )

            model_label = format_step_model(conv)
            prev_model = conv.raw_steps[-1].model_name if conv.raw_steps else None
            model_switched = bool(prev_model is not None and prev_model != model_label)

            conv.raw_steps.append(
                _RawPlannerStep(
                    ordinal=ordinal,
                    step_index=step_index,
                    created_at=created_at,
                    created_at_epoch=created_at_epoch,
                    model_name=model_label,
                    input_tokens=input_tokens,
                    cache_read_tokens=cache_read_tokens,
                    total_context=total_context,
                    cache_hit_pct=round(cache_hit_pct, 1),
                    output_tokens=output_tokens,
                    est_thinking_tokens=est_thinking_tokens,
                    est_visible_tokens=est_visible_tokens,
                    tool_names=[],
                    compacted=False,
                    cold_cache_spike=cold_cache_spike,
                    summary_chars_before=conv.summary_chars,
                    user_chars_before=conv.user_chars,
                    model_chars_before=conv.model_chars,
                    tool_chars_before=conv.tool_chars,
                    tool_chars_by_name_before=dict(conv.tool_chars_by_name),
                    step_kind="checkpoint",
                    model_switched=model_switched,
                    compaction_event=True,
                )
            )
            conv.pending_compaction_event = False
        else:
            conv.pending_compaction_event = True

        conv.pending_tools.clear()
        conv.last_planner_step = None
        conv.summary_chars = len(content)
        conv.user_chars = 0
        conv.model_chars = 0
        conv.tool_chars = 0
        conv.tool_chars_by_name.clear()
        conv.needs_system_rebaseline = True

    elif step_type == "PLANNER_RESPONSE":
        input_tokens = _safe_int(step.get("input_tokens"))
        cache_read_tokens = _safe_int(step.get("cache_read_tokens"))
        output_tokens = _safe_int(step.get("output_tokens"))
        total_context = input_tokens + cache_read_tokens
        cache_hit_pct = (
            (cache_read_tokens / total_context * 100.0) if total_context > 0 else 0.0
        )

        if step_status in ("CANCELED", "CANCELLED"):
            conv.pending_tools.clear()
            conv.last_planner_step = None
            conv.pending_error_steps = 0
            if created_at_epoch is not None:
                conv.canceled_epochs.append(created_at_epoch)
            if total_context <= 0 and output_tokens <= 0:
                return
            content = str(step.get("content") or "")
            tool_calls = step.get("tool_calls") or []
            if not isinstance(tool_calls, list):
                tool_calls = []
            tool_call_chars = (
                len(json.dumps(tool_calls, separators=(",", ":"))) if tool_calls else 0
            )
            thinking_text = str(step.get("thinking") or "")
            est_thinking_tokens, est_visible_tokens = _estimate_thinking_and_visible(
                output_tokens,
                thinking_text,
                len(content) + tool_call_chars,
                truncated_fields,
                is_checkpoint=False,
            )
            ordinal = len(conv.raw_steps) + 1
            model_label = format_step_model(conv)
            prev_model = conv.raw_steps[-1].model_name if conv.raw_steps else None
            model_switched = bool(prev_model is not None and prev_model != model_label)
            conv.raw_steps.append(
                _RawPlannerStep(
                    ordinal=ordinal,
                    step_index=step_index,
                    created_at=created_at,
                    created_at_epoch=created_at_epoch,
                    model_name=model_label,
                    input_tokens=input_tokens,
                    cache_read_tokens=cache_read_tokens,
                    total_context=total_context,
                    cache_hit_pct=round(cache_hit_pct, 1),
                    output_tokens=output_tokens,
                    est_thinking_tokens=est_thinking_tokens,
                    est_visible_tokens=est_visible_tokens,
                    tool_names=[],
                    compacted=False,
                    cold_cache_spike=False,
                    user_chars_before=conv.user_chars,
                    model_chars_before=conv.model_chars,
                    tool_chars_before=conv.tool_chars,
                    tool_chars_by_name_before=dict(conv.tool_chars_by_name),
                    summary_chars_before=conv.summary_chars,
                    step_kind="planner",
                    model_switched=model_switched,
                    compaction_event=False,
                    step_status="CANCELED",
                )
            )
            return

        if step_status == "ERROR":
            conv.pending_tools.clear()
            conv.last_planner_step = None
            conv.pending_error_steps += 1
            if total_context <= 0 and output_tokens <= 0:
                conv.raw_error_events.append(
                    _RawErrorEvent(
                        step_index=step_index,
                        created_at_epoch=created_at_epoch,
                        model_name=format_step_model(conv),
                    )
                )
                return
            content = str(step.get("content") or "")
            tool_calls = step.get("tool_calls") or []
            if not isinstance(tool_calls, list):
                tool_calls = []
            tool_call_chars = (
                len(json.dumps(tool_calls, separators=(",", ":"))) if tool_calls else 0
            )
            thinking_text = str(step.get("thinking") or "")
            est_thinking_tokens, est_visible_tokens = _estimate_thinking_and_visible(
                output_tokens,
                thinking_text,
                len(content) + tool_call_chars,
                truncated_fields,
                is_checkpoint=False,
            )
            ordinal = len(conv.raw_steps) + 1
            model_label = format_step_model(conv)
            prev_model = conv.raw_steps[-1].model_name if conv.raw_steps else None
            model_switched = bool(prev_model is not None and prev_model != model_label)
            err_before = conv.pending_error_steps
            conv.pending_error_steps = 0
            conv.raw_steps.append(
                _RawPlannerStep(
                    ordinal=ordinal,
                    step_index=step_index,
                    created_at=created_at,
                    created_at_epoch=created_at_epoch,
                    model_name=model_label,
                    input_tokens=input_tokens,
                    cache_read_tokens=cache_read_tokens,
                    total_context=total_context,
                    cache_hit_pct=round(cache_hit_pct, 1),
                    output_tokens=output_tokens,
                    est_thinking_tokens=est_thinking_tokens,
                    est_visible_tokens=est_visible_tokens,
                    tool_names=[],
                    compacted=False,
                    cold_cache_spike=False,
                    user_chars_before=conv.user_chars,
                    model_chars_before=conv.model_chars,
                    tool_chars_before=conv.tool_chars,
                    tool_chars_by_name_before=dict(conv.tool_chars_by_name),
                    summary_chars_before=conv.summary_chars,
                    step_kind="planner",
                    model_switched=model_switched,
                    compaction_event=False,
                    error_steps_before=err_before,
                    step_status="ERROR",
                )
            )
            return

        preceded_by_ckpt = conv.pending_post_compaction
        ckpt_epoch = conv.last_checkpoint_epoch if preceded_by_ckpt else None
        err_before = conv.pending_error_steps
        conv.pending_error_steps = 0

        ordinal = len(conv.raw_steps) + 1
        if conv.pending_post_compaction:
            compacted = True
            compaction_event = conv.pending_compaction_event
            conv.pending_post_compaction = False
            conv.pending_compaction_event = False
        else:
            prev_total = (
                conv.raw_steps[-1].total_context if conv.raw_steps else None
            )
            compacted = bool(
                prev_total is not None
                and prev_total > 0
                and 0 < total_context < prev_total * 0.85
            )
            compaction_event = compacted
            if compacted:
                conv.compacted_count += 1

        cold_cache_spike = bool(
            ordinal > 1 and input_tokens >= 25000 and cache_hit_pct < 20.0
        )

        if conv.system_baseline_tokens is None:
            initial_est = round(
                (conv.summary_chars + conv.user_chars) / _CHARS_PER_TOKEN
            )
            conv.system_baseline_tokens = max(0, total_context - initial_est)
            conv.needs_system_rebaseline = False
        elif conv.needs_system_rebaseline:
            post_cp_est = round(
                (
                    conv.summary_chars
                    + conv.user_chars
                    + conv.model_chars
                    + conv.tool_chars
                )
                / _CHARS_PER_TOKEN
            )
            conv.system_baseline_tokens = max(0, total_context - post_cp_est)
            conv.needs_system_rebaseline = False

        content = str(step.get("content") or "")
        tool_calls = step.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            tool_calls = []

        tool_call_chars = (
            len(json.dumps(tool_calls, separators=(",", ":"))) if tool_calls else 0
        )
        thinking_text = str(step.get("thinking") or "")
        est_thinking_tokens, est_visible_tokens = _estimate_thinking_and_visible(
            output_tokens,
            thinking_text,
            len(content) + tool_call_chars,
            truncated_fields,
            is_checkpoint=False,
        )

        if (
            is_live
            and created_at_epoch is not None
            and 0.0 <= (now_ms / 1000.0 - created_at_epoch) <= 300.0
            and step_index not in conv.first_seen_ms
        ):
            conv.first_seen_ms[step_index] = now_ms

        tool_names: list[str] = []
        pending_for_step: list[tuple[str, str, str]] = []
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            t_name = str(tc.get("name") or "other")
            tool_names.append(t_name)
            role = ""
            req_model = ""
            args = tc.get("args")
            if isinstance(args, dict):
                if t_name == "invoke_subagent":
                    role = _unwrap_arg_str(args.get("Role"))
                    req_model = _unwrap_arg_str(args.get("Model"))
                elif t_name == "swarm":
                    if _unwrap_arg_str(args.get("action")).lower() == "add":
                        role = _unwrap_arg_str(args.get("name")) or "Swarm Worker"
                        req_model = _unwrap_arg_str(args.get("model"))
                elif t_name == "run_workflow":
                    role = _unwrap_arg_str(args.get("Description")) or "Workflow"
            pending_for_step.append((t_name, role, req_model))

        if "exit_plan_mode" in tool_names and not conv.orchestrator_active:
            promote_to_orchestrator(conv, "Planner")
        elif "enter_plan_mode" in tool_names and not conv.orchestrator_active:
            promote_to_orchestrator(conv, "Executor")

        model_label = format_step_model(conv)

        conv.pending_tools.clear()
        for t_name, role, req_model in pending_for_step:
            sub_model = ""
            if t_name in _CHILD_SPAWN_TOOLS:
                if req_model and req_model.lower() != "inherit":
                    sub_model = normalize_step_model(req_model, is_subagent=True)
                elif conv.orchestrator_active:
                    base_m = _extract_base_model_from_orchestrator(
                        conv.transcript_model,
                        conv.orchestrator_phase or "Planner",
                    )
                    sub_model = normalize_step_model(base_m, is_subagent=True)
                elif not _is_unattributed_model(model_label):
                    sub_model = normalize_step_model(model_label, is_subagent=True)
            conv.pending_tools.append((t_name, role, sub_model))

        prev_model = conv.raw_steps[-1].model_name if conv.raw_steps else None
        model_switched = bool(prev_model is not None and prev_model != model_label)

        raw_step = _RawPlannerStep(
            ordinal=ordinal,
            step_index=step_index,
            created_at=created_at,
            created_at_epoch=created_at_epoch,
            model_name=model_label,
            input_tokens=input_tokens,
            cache_read_tokens=cache_read_tokens,
            total_context=total_context,
            cache_hit_pct=round(cache_hit_pct, 1),
            output_tokens=output_tokens,
            est_thinking_tokens=est_thinking_tokens,
            est_visible_tokens=est_visible_tokens,
            tool_names=tool_names,
            compacted=compacted,
            cold_cache_spike=cold_cache_spike,
            user_chars_before=conv.user_chars,
            model_chars_before=conv.model_chars,
            tool_chars_before=conv.tool_chars,
            tool_chars_by_name_before=dict(conv.tool_chars_by_name),
            summary_chars_before=conv.summary_chars,
            step_kind="planner",
            model_switched=model_switched,
            compaction_event=compaction_event,
            preceded_by_checkpoint=preceded_by_ckpt,
            checkpoint_epoch=ckpt_epoch,
            error_steps_before=err_before,
            step_status="DONE",
        )
        conv.raw_steps.append(raw_step)
        conv.last_planner_step = raw_step

        conv.model_chars += len(content) + tool_call_chars

        if "exit_plan_mode" in tool_names:
            conv.orchestrator_phase = "Executor"
        elif "enter_plan_mode" in tool_names:
            conv.orchestrator_phase = "Planner"

    elif step_type == "GENERIC":
        content = str(step.get("content") or "")
        if conv.pending_tools:
            tool_name, sub_role, sub_model = conv.pending_tools.pop(0)
        else:
            tool_name, sub_role, sub_model = "other", "", ""

        if tool_name in _CHILD_SPAWN_TOOLS:
            for found_uuid in _UUID_RE.findall(content):
                norm_uuid = found_uuid.lower()
                if norm_uuid != conv.conversation_id.lower():
                    if norm_uuid not in conv.child_subagents or sub_role:
                        conv.child_subagents[norm_uuid] = (
                            sub_role or conv.child_subagents.get(norm_uuid, "Subagent")
                        )
                    if sub_model:
                        conv.child_subagent_models[norm_uuid] = sub_model

        header = content[:512]
        m_created = _CREATED_AT_HEADER_RE.search(header)
        m_completed = _COMPLETED_AT_HEADER_RE.search(header)
        header_created_epoch = (
            _parse_iso_epoch(m_created.group(1)) if m_created else None
        )
        header_completed_epoch = (
            _parse_iso_epoch(m_completed.group(1)) if m_completed else None
        )

        char_len = len(content)
        conv.tool_chars += char_len
        conv.tool_chars_by_name[tool_name] = (
            conv.tool_chars_by_name.get(tool_name, 0) + char_len
        )

        if conv.last_planner_step is not None:
            conv.last_planner_step.paired_generics.append(
                _RawGenericStep(
                    step_index=step_index,
                    tool_name=tool_name,
                    created_at_epoch=created_at_epoch,
                    header_created_epoch=header_created_epoch,
                    header_completed_epoch=header_completed_epoch,
                    char_len=char_len,
                )
            )


def ingest_transcript(
    conv: ConversationState,
    transcript_path: Path | None,
    *,
    initial_scan_done: bool,
    now_ms: int,
    on_open: Callable[[], None] | None = None,
) -> bool:
    """Incrementally read new complete JSONL lines from a conversation transcript."""
    if transcript_path is None:
        if conv.transcript_path is not None:
            conv.transcript_path = None
            conv.reset_transcript_state()
            return True
        return False

    if conv.transcript_path != transcript_path:
        conv.transcript_path = transcript_path
        conv.reset_transcript_state()

    try:
        st = transcript_path.stat()
    except OSError:
        return False

    if conv.transcript_inode is not None and (
        st.st_ino != conv.transcript_inode or st.st_size < conv.transcript_offset
    ):
        conv.reset_transcript_state()

    if st.st_size == conv.transcript_offset and conv.transcript_inode == st.st_ino:
        if not conv.cold_loaded:
            conv.cold_loaded = True
        conv.last_transcript_check_ms = now_ms
        return False

    prev_check_ms = conv.last_transcript_check_ms
    is_fast_live = bool(
        conv.cold_loaded
        and initial_scan_done
        and prev_check_ms > 0
        and 0 <= (now_ms - prev_check_ms) <= 500
    )
    consumed_bytes = 0
    parsed_steps: list[dict[str, Any]] = []
    try:
        with open(transcript_path, "rb") as f:
            if on_open is not None:
                on_open()
            f.seek(conv.transcript_offset)
            while True:
                raw_line = f.readline()
                if not raw_line or not raw_line.endswith(b"\n"):
                    break
                consumed_bytes += len(raw_line)
                text = raw_line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    step = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if not isinstance(step, dict):
                    continue
                parsed_steps.append(step)
    except OSError:
        return False

    if consumed_bytes == 0:
        return False

    last_idx = len(parsed_steps) - 1
    for idx, step in enumerate(parsed_steps):
        try:
            process_transcript_step(
                conv, step, is_fast_live and idx == last_idx, now_ms
            )
        except Exception:
            continue

    conv.transcript_inode = st.st_ino
    conv.transcript_offset += consumed_bytes
    conv.cold_loaded = True
    conv.last_transcript_check_ms = now_ms
    return bool(parsed_steps)


def build_hook_windows(conv: ConversationState) -> list[_HookWindow]:
    """Build ordered PreInvocation -> PostToolUse/PostInvocation/Stop windows."""
    windows: list[_HookWindow] = []
    current: _HookWindow | None = None
    for ev in sorted(conv.hook_events, key=lambda e: _safe_int(e.get("ts_ms"))):
        event_name = ev.get("event")
        ts_ms = _safe_int(ev.get("ts_ms"))
        if ts_ms <= 0:
            continue
        if event_name == "PreInvocation":
            if (
                current is not None
                and current.t_first_pre_tool_ms is None
                and current.t_first_post_tool_ms is None
                and current.t_post_inv_ms is None
                and current.t_stop_ms is None
            ):
                current.is_superseded = True
            current = _HookWindow(
                t_start_ms=ts_ms,
                model_name=str(ev.get("model_name") or ""),
            )
            windows.append(current)
        elif current is not None:
            if event_name == "PreToolUse":
                if current.t_first_pre_tool_ms is None:
                    current.t_first_pre_tool_ms = ts_ms
                if (
                    current.first_tool_step_idx is None
                    and ev.get("step_idx") is not None
                ):
                    s_idx = _safe_int(ev.get("step_idx"), -1)
                    if s_idx >= 0:
                        current.first_tool_step_idx = s_idx
            elif event_name == "PostToolUse":
                if current.t_first_post_tool_ms is None:
                    current.t_first_post_tool_ms = ts_ms
                current.t_last_post_tool_ms = ts_ms
                if (
                    current.first_tool_step_idx is None
                    and ev.get("step_idx") is not None
                ):
                    s_idx = _safe_int(ev.get("step_idx"), -1)
                    if s_idx >= 0:
                        current.first_tool_step_idx = s_idx
            elif event_name == "PostInvocation" and current.t_post_inv_ms is None:
                current.t_post_inv_ms = ts_ms
            elif event_name == "Stop" and current.t_stop_ms is None:
                current.t_stop_ms = ts_ms
                current.termination_reason = ev.get("termination_reason")
                current.has_stop_error = bool(ev.get("has_error"))
                if str(current.termination_reason or "").upper() in (
                    "USER_CANCELED",
                    "USER_CANCELLED",
                ):
                    current.is_canceled = True
    return windows


def _match_window_for_step(
    windows: list[_HookWindow],
    win_idx: int,
    raw: _RawPlannerStep,
    now_ms: int,
    *,
    allow_delayed_span: bool = True,
) -> tuple[int | None, bool]:
    """Match a transcript step to an unclaimed hook window, detecting delayed starts."""
    if raw.created_at_epoch is None:
        return None, False

    best_idx: int | None = None
    best_diff = 5.0001
    for idx in range(win_idx, len(windows)):
        w = windows[idx]
        if w.is_canceled or w.is_superseded:
            continue
        if (
            w.first_tool_step_idx is not None
            and raw.step_index > 0
            and raw.step_index >= w.first_tool_step_idx
        ):
            continue
        diff = abs(w.t_start_ms / 1000.0 - raw.created_at_epoch)
        if diff <= 5.0 and diff < best_diff:
            best_diff = diff
            best_idx = idx
        elif w.t_start_ms / 1000.0 > raw.created_at_epoch + 5.0:
            break

    if best_idx is not None:
        return best_idx, (raw.error_steps_before > 0)

    if not allow_delayed_span:
        return None, False

    for idx in range(win_idx, len(windows)):
        w = windows[idx]
        if w.is_canceled or w.is_superseded:
            continue
        w_start_s = w.t_start_ms / 1000.0
        if w_start_s > raw.created_at_epoch:
            break
        if (
            w.first_tool_step_idx is not None
            and raw.step_index > 0
            and raw.step_index >= w.first_tool_step_idx
        ):
            continue
        next_start_s = (
            windows[idx + 1].t_start_ms / 1000.0
            if idx + 1 < len(windows)
            else None
        )
        if next_start_s is not None and raw.created_at_epoch >= next_start_s:
            continue
        w_close_ms = (
            w.t_first_pre_tool_ms
            or w.t_first_post_tool_ms
            or w.t_post_inv_ms
            or w.t_stop_ms
        )
        if w_close_ms is not None:
            if raw.created_at_epoch <= (w_close_ms / 1000.0) + 1.0:
                return idx, True
        elif idx == len(windows) - 1:
            if (
                0 <= (now_ms - w.t_start_ms) <= _LIVE_GENERATING_STALE_MS
                and raw.created_at_epoch <= (now_ms / 1000.0) + 2.0
            ):
                return idx, True

    return None, False


def recompute_conversation(
    conv: ConversationState,
    latest_record: dict[str, Any] | None,
    now_ms: int,
) -> None:
    """Recompute per-step timing, model attribution, summaries, and live status."""
    windows = build_hook_windows(conv)
    if windows and conv.canceled_epochs:
        done_epochs = [
            r.created_at_epoch
            for r in conv.raw_steps
            if r.step_kind != "checkpoint"
            and r.step_status == "DONE"
            and r.created_at_epoch is not None
        ]
        for w_i, w in enumerate(windows):
            if w.is_canceled:
                continue
            w_start_s = w.t_start_ms / 1000.0
            next_w_start_s = (
                windows[w_i + 1].t_start_ms / 1000.0
                if w_i + 1 < len(windows)
                else float("inf")
            )
            w_end_ms = w.t_stop_ms or w.t_post_inv_ms
            w_upper_s = (
                min(next_w_start_s, w_end_ms / 1000.0 + 1.0)
                if w_end_ms is not None
                else next_w_start_s
            )
            if any((w_start_s - 5.0) <= ep <= w_upper_s for ep in done_epochs):
                continue
            for c_ep in conv.canceled_epochs:
                if (w_start_s - 1.0) <= c_ep <= w_upper_s:
                    w.is_canceled = True
                    break

    win_idx = 0
    matched_window_indices: set[int] = set()
    ckpt_window_indices: set[int] = set()
    terminal_window_indices: set[int] = set()
    steps_out: list[StepMetrics] = []

    for raw in conv.raw_steps:
        duration_s: float | None = None
        duration_source: str | None = None
        effective_tps: float | None = None
        matched_win: _HookWindow | None = None
        tool_exec_s = 0.0
        retry_wait_s = 0.0
        retry_count = 0

        if raw.step_kind == "checkpoint":
            if raw.created_at_epoch is not None:
                for idx in range(win_idx, len(windows)):
                    w = windows[idx]
                    if w.is_canceled or w.is_superseded:
                        continue
                    if abs(w.t_start_ms / 1000.0 - raw.created_at_epoch) <= 5.0:
                        ckpt_window_indices.add(idx)
                        break
                    if w.t_start_ms / 1000.0 > raw.created_at_epoch + 5.0:
                        break

        elif raw.step_status in ("CANCELED", "CANCELLED"):
            pass

        elif raw.step_status == "ERROR":
            best_idx, _ = _match_window_for_step(
                windows, win_idx, raw, now_ms, allow_delayed_span=True
            )
            if best_idx is not None:
                matched_win = windows[best_idx]
                matched_window_indices.add(best_idx)
                terminal_window_indices.add(best_idx)
                win_idx = best_idx + 1
                t_end_ms = matched_win.t_stop_ms or matched_win.t_post_inv_ms
                if t_end_ms is not None and t_end_ms > matched_win.t_start_ms:
                    retry_wait_s = round(
                        max(0.05, (t_end_ms - matched_win.t_start_ms) / 1000.0), 2
                    )
                elif (
                    raw.created_at_epoch is not None
                    and raw.created_at_epoch > matched_win.t_start_ms / 1000.0
                ):
                    retry_wait_s = round(
                        max(0.0, raw.created_at_epoch - matched_win.t_start_ms / 1000.0),
                        2,
                    )
            retry_count = max(1, raw.error_steps_before)

        else:
            # 1. Tool execution duration (anchor batch start at last streamed block)
            start_epochs = [
                g.header_created_epoch
                for g in raw.paired_generics
                if g.header_created_epoch is not None
            ]
            end_epochs = [
                g.header_completed_epoch
                for g in raw.paired_generics
                if g.header_completed_epoch is not None
            ]
            has_tool_headers = bool(start_epochs and end_epochs)
            all_tools_have_headers = (
                bool(raw.paired_generics)
                and len(raw.paired_generics) >= len(raw.tool_names)
                and all(
                    g.header_created_epoch is not None
                    and g.header_completed_epoch is not None
                    for g in raw.paired_generics
                )
            )
            batch_start_epoch: float | None = None
            if has_tool_headers:
                batch_start_epoch = min(max(start_epochs), min(end_epochs))
                tool_exec_s = round(max(0.0, max(end_epochs) - batch_start_epoch), 2)

            # 2. Match PreInvocation hook window (exact ±5s or span-contained delayed start)
            best_idx, is_delayed_start = _match_window_for_step(
                windows, win_idx, raw, now_ms, allow_delayed_span=True
            )
            if best_idx is not None:
                matched_win = windows[best_idx]
                matched_window_indices.add(best_idx)
                if not raw.tool_names:
                    terminal_window_indices.add(best_idx)
                win_idx = best_idx + 1

            if (
                matched_win is not None
                and matched_win.t_first_pre_tool_ms is not None
                and matched_win.t_last_post_tool_ms is not None
                and matched_win.t_last_post_tool_ms >= matched_win.t_first_pre_tool_ms
            ):
                tool_exec_s = round(
                    (matched_win.t_last_post_tool_ms - matched_win.t_first_pre_tool_ms)
                    / 1000.0,
                    2,
                )

            if matched_win is not None and is_delayed_start:
                is_compaction_gap = bool(
                    raw.preceded_by_checkpoint
                    and raw.checkpoint_epoch is not None
                    and raw.checkpoint_epoch >= (matched_win.t_start_ms / 1000.0) - 2.0
                )
                if not is_compaction_gap and raw.created_at_epoch is not None:
                    retry_wait_s = round(
                        max(0.0, raw.created_at_epoch - matched_win.t_start_ms / 1000.0),
                        2,
                    )
                    if retry_wait_s > 0.0 or raw.error_steps_before > 0:
                        retry_count = max(1, raw.error_steps_before)
                elif raw.error_steps_before > 0:
                    retry_count = raw.error_steps_before

            # 3. Compute model inference duration
            has_tools = len(raw.tool_names) > 0
            seen_ms = conv.first_seen_ms.get(raw.step_index)

            if matched_win is not None:
                if not is_delayed_start:
                    t_start = matched_win.t_start_ms
                    if not has_tools:
                        t_end = matched_win.t_post_inv_ms or matched_win.t_stop_ms
                        if t_end is not None and t_end > t_start:
                            duration_s = round(max(0.05, (t_end - t_start) / 1000.0), 2)
                            duration_source = "hook_ms"
                    else:
                        seen_before_tool_done = (
                            seen_ms is not None
                            and seen_ms > t_start
                            and (
                                matched_win.t_first_post_tool_ms is None
                                or seen_ms < matched_win.t_first_post_tool_ms
                            )
                            and (
                                matched_win.t_post_inv_ms is None
                                or seen_ms < matched_win.t_post_inv_ms
                            )
                            and (
                                not end_epochs
                                or (seen_ms / 1000.0) <= min(end_epochs) + 1.0
                            )
                        )
                        if (
                            matched_win.t_first_pre_tool_ms is not None
                            and matched_win.t_first_pre_tool_ms > t_start
                        ):
                            duration_s = round(
                                max(
                                    0.05,
                                    (matched_win.t_first_pre_tool_ms - t_start) / 1000.0,
                                ),
                                2,
                            )
                            duration_source = "hook_ms"
                        elif seen_before_tool_done:
                            duration_s = round(
                                max(0.05, (seen_ms - t_start) / 1000.0), 2
                            )
                            duration_source = "hook_ms"
                        elif (
                            matched_win.t_first_post_tool_ms is not None
                            and matched_win.t_first_post_tool_ms > t_start
                            and has_tool_headers
                            and batch_start_epoch is not None
                        ):
                            first_end_epoch = min(end_epochs)
                            if (
                                all_tools_have_headers
                                or abs(
                                    matched_win.t_first_post_tool_ms / 1000.0
                                    - first_end_epoch
                                )
                                <= 2.0
                            ):
                                first_exec_start = min(
                                    batch_start_epoch, first_end_epoch
                                )
                                first_tool_exec_s = max(
                                    0.0,
                                    first_end_epoch - first_exec_start,
                                )
                                est = (
                                    matched_win.t_first_post_tool_ms - t_start
                                ) / 1000.0 - first_tool_exec_s
                                duration_s = round(max(0.05, est), 2)
                                duration_source = "hook_ms"
                        elif (
                            matched_win.t_post_inv_ms is not None
                            and matched_win.t_post_inv_ms > t_start
                            and all_tools_have_headers
                        ):
                            est = (
                                matched_win.t_post_inv_ms - t_start
                            ) / 1000.0 - tool_exec_s
                            duration_s = round(max(0.05, est), 2)
                            duration_source = "hook_ms"
                elif raw.created_at_epoch is not None:
                    t_start = max(
                        matched_win.t_start_ms,
                        int(round(raw.created_at_epoch * 1000)),
                    )
                    seen_valid_live = (
                        seen_ms is not None
                        and seen_ms > t_start
                        and (
                            (
                                not has_tools
                                and (
                                    matched_win.t_post_inv_ms is None
                                    or seen_ms <= matched_win.t_post_inv_ms + 1000
                                )
                                and (
                                    matched_win.t_stop_ms is None
                                    or seen_ms <= matched_win.t_stop_ms + 1000
                                )
                            )
                            or (
                                has_tools
                                and (
                                    matched_win.t_first_post_tool_ms is None
                                    or seen_ms < matched_win.t_first_post_tool_ms
                                )
                                and (
                                    matched_win.t_post_inv_ms is None
                                    or seen_ms < matched_win.t_post_inv_ms
                                )
                                and (
                                    not end_epochs
                                    or (seen_ms / 1000.0) <= min(end_epochs) + 1.0
                                )
                            )
                        )
                    )
                    if seen_valid_live and seen_ms is not None:
                        dt = seen_ms / 1000.0 - raw.created_at_epoch
                        if dt > 0.0:
                            duration_s = round(dt, 2)
                            duration_source = "watcher_ms"
                    if duration_s is None:
                        if not has_tools:
                            t_end = matched_win.t_post_inv_ms or matched_win.t_stop_ms
                            if (
                                t_end is not None
                                and t_end > matched_win.t_start_ms
                                and t_end >= t_start - 999
                            ):
                                duration_s = round(
                                    max(0.5, (t_end - t_start) / 1000.0), 2
                                )
                                duration_source = "transcript_s"
                        else:
                            if (
                                matched_win.t_first_pre_tool_ms is not None
                                and matched_win.t_first_pre_tool_ms
                                > matched_win.t_start_ms
                                and matched_win.t_first_pre_tool_ms >= t_start - 999
                            ):
                                duration_s = round(
                                    max(
                                        0.5,
                                        (matched_win.t_first_pre_tool_ms - t_start)
                                        / 1000.0,
                                    ),
                                    2,
                                )
                                duration_source = "transcript_s"
                            elif (
                                matched_win.t_first_post_tool_ms is not None
                                and matched_win.t_first_post_tool_ms
                                > matched_win.t_start_ms
                                and matched_win.t_first_post_tool_ms >= t_start - 999
                                and has_tool_headers
                                and batch_start_epoch is not None
                            ):
                                first_end_epoch = min(end_epochs)
                                if (
                                    all_tools_have_headers
                                    or abs(
                                        matched_win.t_first_post_tool_ms / 1000.0
                                        - first_end_epoch
                                    )
                                    <= 2.0
                                ):
                                    first_exec_start = min(
                                        batch_start_epoch, first_end_epoch
                                    )
                                    first_tool_exec_s = max(
                                        0.0,
                                        first_end_epoch - first_exec_start,
                                    )
                                    est = (
                                        matched_win.t_first_post_tool_ms - t_start
                                    ) / 1000.0 - first_tool_exec_s
                                    duration_s = round(max(0.5, est), 2)
                                    duration_source = "transcript_s"
                            elif (
                                matched_win.t_post_inv_ms is not None
                                and matched_win.t_post_inv_ms > matched_win.t_start_ms
                                and matched_win.t_post_inv_ms >= t_start - 999
                                and all_tools_have_headers
                            ):
                                est = (
                                    matched_win.t_post_inv_ms - t_start
                                ) / 1000.0 - tool_exec_s
                                duration_s = round(max(0.5, est), 2)
                                duration_source = "transcript_s"

            if (
                duration_s is None
                and seen_ms is not None
                and raw.created_at_epoch is not None
                and (
                    not has_tools
                    or not end_epochs
                    or (seen_ms / 1000.0) <= min(end_epochs) + 1.0
                )
            ):
                dt = seen_ms / 1000.0 - raw.created_at_epoch
                if dt > 0.0:
                    duration_s = round(dt, 2)
                    duration_source = "watcher_ms"

            if (
                duration_s is None
                and has_tools
                and raw.paired_generics
                and raw.created_at_epoch is not None
            ):
                gen_starts = [
                    g.header_created_epoch
                    if g.header_created_epoch is not None
                    else g.created_at_epoch
                    for g in raw.paired_generics
                ]
                valid_gen_starts = [ts for ts in gen_starts if ts is not None]
                if valid_gen_starts:
                    tool_phase_start = max(valid_gen_starts)
                    if end_epochs:
                        tool_phase_start = min(tool_phase_start, min(end_epochs))
                    raw_dt = tool_phase_start - raw.created_at_epoch
                    if raw_dt >= 0.0:
                        duration_s = round(max(0.5, raw_dt), 2)
                        duration_source = "transcript_s"

            if duration_s is not None and duration_s > 0.0:
                effective_tps = round(raw.output_tokens / duration_s, 1)

        step_model = raw.model_name
        if _is_unattributed_model(step_model):
            if matched_win is not None and matched_win.model_name not in ("", "auto"):
                step_model = matched_win.model_name
            elif conv.fallback_model_hint:
                step_model = conv.fallback_model_hint

        step_model = normalize_step_model(step_model, is_subagent=conv.is_subagent)
        step_switched = bool(steps_out and steps_out[-1].model_name != step_model)

        steps_out.append(
            StepMetrics(
                ordinal=raw.ordinal,
                step_index=raw.step_index,
                created_at=raw.created_at,
                model_name=step_model,
                input_tokens=raw.input_tokens,
                cache_read_tokens=raw.cache_read_tokens,
                total_context=raw.total_context,
                cache_hit_pct=raw.cache_hit_pct,
                output_tokens=raw.output_tokens,
                est_thinking_tokens=raw.est_thinking_tokens,
                est_visible_tokens=raw.est_visible_tokens,
                duration_s=duration_s,
                duration_source=duration_source,
                effective_tps=effective_tps,
                tool_exec_s=tool_exec_s,
                tool_names=list(raw.tool_names),
                compacted=raw.compacted,
                cold_cache_spike=raw.cold_cache_spike,
                model_switched=step_switched,
                step_kind=raw.step_kind,
                compaction_event=raw.compaction_event,
                retry_wait_s=retry_wait_s,
                retry_count=retry_count,
            )
        )

    conv.steps = steps_out
    conv.summary_steps_count = len(steps_out)
    conv.own_input_tokens = sum(s.input_tokens for s in steps_out)
    conv.own_cache_read_tokens = sum(s.cache_read_tokens for s in steps_out)
    conv.own_output_tokens = sum(s.output_tokens for s in steps_out)
    conv.own_est_thinking_tokens = sum(s.est_thinking_tokens for s in steps_out)
    conv.own_est_visible_tokens = sum(s.est_visible_tokens for s in steps_out)

    last_ctx_step: StepMetrics | None = None
    for st in reversed(steps_out):
        if st.step_kind == "checkpoint" or st.total_context > 0:
            last_ctx_step = st
            break
    if last_ctx_step is None:
        conv.current_tokens = 0
    elif last_ctx_step.step_kind == "checkpoint":
        conv.current_tokens = (conv.system_baseline_tokens or 0) + round(
            (conv.summary_chars + conv.user_chars) / _CHARS_PER_TOKEN
        )
    else:
        conv.current_tokens = last_ctx_step.total_context
    if (conv.orchestrator_active and not conv.is_subagent) or conv.transcript_model:
        conv.current_model_name = format_step_model(conv)
    else:
        raw_current = (
            (
                conv.hook_reported_model
                if conv.hook_reported_model and conv.hook_reported_model != "auto"
                else ""
            )
            or (steps_out[-1].model_name if steps_out else "")
            or conv.fallback_model_hint
            or conv.hook_reported_model
            or "unknown"
        )
        conv.current_model_name = normalize_step_model(
            raw_current, is_subagent=conv.is_subagent
        )
    if not conv.title:
        conv.title = conv.external_title or conv.prompt_title or conv.conversation_id[:8]

    # Track bounded unrecovered failed turns (excluding user cancellations & silent aborts)
    matched_spans = [
        (
            windows[w_i].t_start_ms / 1000.0 - 1.0,
            windows[w_i + 1].t_start_ms / 1000.0
            if w_i + 1 < len(windows)
            else float("inf"),
        )
        for w_i in matched_window_indices
    ]
    unused_errors = [
        err
        for err in conv.raw_error_events
        if err.created_at_epoch is not None
        and not any(lo <= err.created_at_epoch < hi for lo, hi in matched_spans)
    ]
    failed_turns: list[_RawFailedTurn] = []
    for w_i, w in enumerate(windows):
        if (
            w_i in matched_window_indices
            or w_i in ckpt_window_indices
            or w.is_canceled
        ):
            continue
        w_start_s = w.t_start_ms / 1000.0
        next_w_start_s = (
            windows[w_i + 1].t_start_ms / 1000.0
            if w_i + 1 < len(windows)
            else float("inf")
        )
        matching_err: _RawErrorEvent | None = None
        for err in unused_errors:
            if (
                err.created_at_epoch is not None
                and (w_start_s - 1.0) <= err.created_at_epoch < next_w_start_s
            ):
                matching_err = err
                break
        is_stop_err = (
            str(w.termination_reason or "").upper() == "ERROR"
        ) or w.has_stop_error
        if not is_stop_err and matching_err is None:
            continue
        terminal_window_indices.add(w_i)
        wasted_s: float | None = None
        ep_s: float = 0.0
        t_end_ms = w.t_stop_ms or w.t_post_inv_ms
        if t_end_ms is not None and t_end_ms > w.t_start_ms:
            win_tool_s = 0.0
            if (
                w.t_first_pre_tool_ms is not None
                and w.t_last_post_tool_ms is not None
                and w.t_last_post_tool_ms >= w.t_first_pre_tool_ms
            ):
                win_tool_s = (w.t_last_post_tool_ms - w.t_first_pre_tool_ms) / 1000.0
            wasted_s = round(
                max(0.05, (t_end_ms - w.t_start_ms) / 1000.0 - win_tool_s), 2
            )
            ep_s = round(t_end_ms / 1000.0, 3)
        elif matching_err is not None and matching_err.created_at_epoch is not None:
            wasted_s = round(max(0.5, matching_err.created_at_epoch - w_start_s), 2)
            ep_s = float(matching_err.created_at_epoch)
        if wasted_s is None:
            continue

        m_attr = "unknown"
        if matching_err is not None and matching_err.model_name not in (
            "",
            "auto",
            "unknown",
        ):
            m_attr = matching_err.model_name
        elif w.model_name not in ("", "auto") and not _is_orchestrator_name(
            w.model_name
        ):
            m_attr = w.model_name
        elif conv.current_model_name:
            m_attr = conv.current_model_name
        m_attr = normalize_step_model(m_attr, is_subagent=conv.is_subagent)

        failed_turns.append(
            _RawFailedTurn(
                seq=len(failed_turns) + 1,
                epoch_s=ep_s,
                model_name=m_attr,
                wasted_s=wasted_s,
            )
        )
    conv.failed_turns = failed_turns

    last_hook = conv.hook_events[-1] if conv.hook_events else None
    latest_step_ms = 0
    if conv.raw_steps and conv.raw_steps[-1].created_at_epoch is not None:
        latest_step_ms = int(conv.raw_steps[-1].created_at_epoch * 1000)

    last_hook_ms = max(
        conv.last_hook_ts_ms,
        _safe_int(last_hook.get("ts_ms")) if last_hook else 0,
    )
    transcript_mtime_ms = 0
    if conv.transcript_path is not None:
        try:
            transcript_mtime_ms = int(conv.transcript_path.stat().st_mtime * 1000)
        except OSError:
            pass

    conv.updated_at_ms = max(latest_step_ms, last_hook_ms, transcript_mtime_ms)

    is_generating = False
    generating_since: int | None = None
    if windows:
        tail_idx = len(windows) - 1
        tail_win = windows[tail_idx]
        superseded_by_user = bool(
            conv.last_user_input_epoch is not None
            and conv.last_user_input_epoch > (tail_win.t_start_ms / 1000.0) + 2.0
        )
        if (
            tail_idx not in terminal_window_indices
            and not tail_win.is_canceled
            and not tail_win.is_superseded
            and not superseded_by_user
            and tail_win.t_post_inv_ms is None
            and tail_win.t_stop_ms is None
            and tail_win.t_first_post_tool_ms is None
            and 0 <= (now_ms - tail_win.t_start_ms) <= _LIVE_GENERATING_STALE_MS
        ):
            is_generating = True
            generating_since = tail_win.t_start_ms

    if (
        not is_generating
        and not windows
        and latest_record
        and latest_record.get("conversation_id") == conv.conversation_id
        and latest_record.get("status") == "generating"
    ):
        rec_ts = _safe_int(latest_record.get("ts_ms"))
        if 0 <= (now_ms - rec_ts) <= _LIVE_GENERATING_STALE_MS:
            is_generating = True
            generating_since = rec_ts

    conv.status = "generating" if is_generating else "idle"
    conv.generating_since_ms = generating_since
    conv.last_accessed_mono = time.monotonic()


def compute_context_breakdown(conv: ConversationState) -> dict[str, Any]:
    """Compute token composition (system/summary/tool/model/user) for active context."""
    pair_idx: int | None = None
    for idx in range(len(conv.steps) - 1, -1, -1):
        st = conv.steps[idx]
        if st.step_kind == "checkpoint" or st.total_context > 0:
            pair_idx = idx
            break
    if pair_idx is None or pair_idx >= len(conv.raw_steps):
        return {
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
        }

    latest_raw = conv.raw_steps[pair_idx]
    latest_step = conv.steps[pair_idx]
    if latest_step.step_kind == "checkpoint":
        sys_tok = conv.system_baseline_tokens or 0
        sum_tok = round(conv.summary_chars / _CHARS_PER_TOKEN)
        usr_tok = round(conv.user_chars / _CHARS_PER_TOKEN)
        cur_tok = sys_tok + sum_tok + usr_tok
        return {
            "current_tokens": cur_tok,
            "cache_read_tokens": 0,
            "uncached_tokens": cur_tok,
            "cache_hit_pct": 0.0,
            "cold_cache_spike": False,
            "compacted": True,
            "model_switched": False,
            "breakdown": {
                "system_tokens": sys_tok,
                "summary_tokens": sum_tok,
                "tool_tokens": 0,
                "model_tokens": 0,
                "user_tokens": usr_tok,
            },
            "top_tools": [],
        }

    latest_context = latest_raw.total_context

    baseline = conv.system_baseline_tokens or 0
    system_tokens = min(baseline, latest_context)
    dynamic_budget = max(0, latest_context - system_tokens)

    raw_summary = latest_raw.summary_chars_before / _CHARS_PER_TOKEN
    raw_user = latest_raw.user_chars_before / _CHARS_PER_TOKEN
    raw_model = latest_raw.model_chars_before / _CHARS_PER_TOKEN
    raw_tools = latest_raw.tool_chars_before / _CHARS_PER_TOKEN
    raw_sum = raw_summary + raw_user + raw_model + raw_tools

    if dynamic_budget == 0:
        summary_tokens = 0
        user_tokens = 0
        model_tokens = 0
        tool_tokens = 0
    elif raw_sum <= 0.0:
        summary_tokens = 0
        user_tokens = dynamic_budget
        model_tokens = 0
        tool_tokens = 0
    else:
        summary_tokens = round(dynamic_budget * (raw_summary / raw_sum))
        tool_tokens = round(dynamic_budget * (raw_tools / raw_sum))
        model_tokens = round(dynamic_budget * (raw_model / raw_sum))
        user_tokens = dynamic_budget - summary_tokens - tool_tokens - model_tokens
        if user_tokens < 0:
            deficit = -user_tokens
            user_tokens = 0
            take = min(tool_tokens, deficit)
            tool_tokens -= take
            deficit -= take
            if deficit > 0:
                take = min(model_tokens, deficit)
                model_tokens -= take
                deficit -= take
            if deficit > 0:
                summary_tokens = max(0, summary_tokens - deficit)

    top_tools: list[dict[str, Any]] = []
    if tool_tokens > 0 and latest_raw.tool_chars_by_name_before:
        total_tool_chars = sum(latest_raw.tool_chars_by_name_before.values())
        if total_tool_chars > 0:
            sorted_tools = sorted(
                latest_raw.tool_chars_by_name_before.items(),
                key=lambda kv: kv[1],
                reverse=True,
            )
            head = sorted_tools[:6]
            tail = sorted_tools[6:]
            for t_name, t_chars in head:
                t_tok = round(tool_tokens * (t_chars / total_tool_chars))
                t_pct = (
                    round((t_tok / latest_context * 100.0), 1)
                    if latest_context
                    else 0.0
                )
                top_tools.append({"name": t_name, "tokens": t_tok, "pct": t_pct})
            if tail:
                other_chars = sum(c for _, c in tail)
                other_tok = round(tool_tokens * (other_chars / total_tool_chars))
                other_pct = (
                    round((other_tok / latest_context * 100.0), 1)
                    if latest_context
                    else 0.0
                )
                top_tools.append({"name": "other", "tokens": other_tok, "pct": other_pct})

    return {
        "current_tokens": latest_step.total_context,
        "cache_read_tokens": latest_step.cache_read_tokens,
        "uncached_tokens": latest_step.input_tokens,
        "cache_hit_pct": latest_step.cache_hit_pct,
        "cold_cache_spike": latest_step.cold_cache_spike,
        "compacted": latest_step.compacted,
        "model_switched": latest_step.model_switched,
        "breakdown": {
            "system_tokens": system_tokens,
            "summary_tokens": summary_tokens,
            "tool_tokens": tool_tokens,
            "model_tokens": model_tokens,
            "user_tokens": user_tokens,
        },
        "top_tools": top_tools,
    }


def _would_create_parent_cycle(
    conversations: dict[str, ConversationState],
    parent_id: str,
    child_id: str,
) -> bool:
    cur: str | None = parent_id
    visited: set[str] = set()
    while cur and cur not in visited:
        if cur == child_id:
            return True
        visited.add(cur)
        node = conversations.get(cur)
        cur = node.parent_id if node is not None else None
    return False


def link_subagents(
    conversations: dict[str, ConversationState],
    on_hint_changed: Callable[[ConversationState], None],
) -> None:
    """Link direct child subagents to their parent and propagate fallback model hints."""
    prev_state = {
        cid: (c.is_subagent, c.fallback_model_hint)
        for cid, c in conversations.items()
    }
    for conv in conversations.values():
        conv.parent_id = None
        conv.nesting_depth = 0

    pending_hints: dict[str, str] = {}
    for parent in conversations.values():
        for child_id in parent.child_subagents:
            child = conversations.get(child_id)
            if (
                child is not None
                and child_id != parent.conversation_id
                and not _would_create_parent_cycle(
                    conversations, parent.conversation_id, child_id
                )
            ):
                child.parent_id = parent.conversation_id
                hint = parent.child_subagent_models.get(child_id, "")
                pending_hints[child_id] = (
                    normalize_step_model(hint, is_subagent=True) if hint else ""
                )
    for child in conversations.values():
        if child.parent_id is not None or not child.external_parent_id:
            continue
        parent = conversations.get(child.external_parent_id)
        if (
            parent is not None
            and child.external_parent_id != child.conversation_id
            and not _would_create_parent_cycle(
                conversations, parent.conversation_id, child.conversation_id
            )
        ):
            child.parent_id = parent.conversation_id
            fallback_role = (
                child.external_title
                or (f"Subagent · {child.agent_name}" if child.agent_name else "Subagent")
            )
            parent.child_subagents.setdefault(child.conversation_id, fallback_role)
            if (
                not child.fallback_model_hint
                and not _is_unattributed_model(parent.current_model_name)
            ):
                derived = normalize_step_model(
                    parent.current_model_name, is_subagent=True
                )
                if not _is_unattributed_model(derived):
                    pending_hints[child.conversation_id] = derived
    for conv in conversations.values():
        depth = 0
        cur: ConversationState | None = conv
        visited: set[str] = set()
        while (
            cur is not None
            and cur.parent_id
            and cur.parent_id in conversations
            and cur.conversation_id not in visited
        ):
            visited.add(cur.conversation_id)
            depth += 1
            cur = conversations.get(cur.parent_id)
        conv.nesting_depth = depth

    for cid, conv in conversations.items():
        now_sub = conv.parent_id is not None
        new_hint = pending_hints.get(cid, conv.fallback_model_hint) if now_sub else ""
        if (now_sub, new_hint) != prev_state[cid]:
            conv.is_subagent = now_sub
            conv.fallback_model_hint = new_hint
            on_hint_changed(conv)


def root_ancestor_id(conversations: dict[str, ConversationState], cid: str) -> str:
    """Walk parent links up to the top-level ancestor with cycle protection."""
    cur = cid
    visited: set[str] = set()
    while cur not in visited:
        visited.add(cur)
        conv = conversations.get(cur)
        if conv is None or not conv.parent_id or conv.parent_id not in conversations:
            return cur
        cur = conv.parent_id
    return cid


def collect_descendant_subagents(
    conversations: dict[str, ConversationState],
    root_id: str,
) -> list[tuple[str, str, str]]:
    """Collect all descendant subagents in BFS order: (sub_id, role, model_hint)."""
    root = conversations.get(root_id)
    if root is None:
        return []
    out: list[tuple[str, str, str]] = []
    visited: set[str] = {root_id}
    queue: deque[ConversationState] = deque([root])
    while queue:
        parent = queue.popleft()
        for sub_id, sub_role in parent.child_subagents.items():
            if sub_id in visited:
                continue
            visited.add(sub_id)
            model_hint = parent.child_subagent_models.get(sub_id, "")
            out.append((sub_id, sub_role, model_hint))
            child = conversations.get(sub_id)
            if child is not None:
                queue.append(child)
    return out


def expand_related_ids(
    conversations: dict[str, ConversationState],
    seed_id: str | None,
) -> set[str]:
    """Return the root conversation plus all descendant subagents related to seed_id."""
    if not seed_id:
        return set()
    out: set[str] = {seed_id}
    if seed_id not in conversations:
        return out
    root_id = root_ancestor_id(conversations, seed_id)
    out.add(root_id)
    for sub_id, _, _ in collect_descendant_subagents(conversations, root_id):
        out.add(sub_id)
    return out
