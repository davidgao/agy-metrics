#!/usr/bin/env python3
"""Fast, self-contained lifecycle hook collector for agy-metrics."""

import json
import os
import re
import sys
import time

_SAFE_CONV_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _app_data_from_executable_data_dir(raw_dir: str) -> str | None:
    if not raw_dir or not raw_dir.strip():
        return None
    cur = os.path.normpath(os.path.expanduser(raw_dir.strip()))
    while cur:
        parent = os.path.dirname(cur)
        if os.path.basename(cur) == "sidecar_data" and parent and parent != cur:
            return parent
        if parent == cur:
            break
        cur = parent
    return None


def _app_data_from_transcript_path(transcript_path: str) -> str | None:
    if not transcript_path or not transcript_path.strip():
        return None
    expanded = os.path.expanduser(transcript_path.strip())
    if not os.path.isabs(expanded):
        return None
    cur = os.path.normpath(expanded)
    while cur:
        parent = os.path.dirname(cur)
        if (
            os.path.basename(cur) == "brain"
            and parent
            and parent != cur
            and parent != os.sep
        ):
            return parent
        if parent == cur:
            break
        cur = parent
    return None


def _get_state_dir(transcript_path: str = "") -> str:
    explicit = os.environ.get("AGY_METRICS_STATE_DIR")
    if explicit:
        return os.path.expanduser(explicit)
    plugin_data = os.environ.get("PLUGIN_DATA")
    if plugin_data:
        return os.path.expanduser(plugin_data)
    app_data = (
        os.environ.get("AGY_METRICS_APP_DATA_DIR")
        or os.environ.get("ANTIGRAVITY_APP_DATA_DIR")
        or _app_data_from_executable_data_dir(
            os.environ.get("ANTIGRAVITY_EXECUTABLE_DATA_DIR") or ""
        )
        or _app_data_from_transcript_path(transcript_path)
        or "~/.gemini/antigravity"
    )
    app_data = os.path.expanduser(app_data)
    plugin_root = os.path.join(app_data, "plugin_data")
    plugin_data_dir = os.path.join(plugin_root, "agy-metrics")
    legacy_dir = os.path.join(app_data, "agy_metrics")
    if os.path.isdir(plugin_data_dir) or os.path.isdir(plugin_root):
        return plugin_data_dir
    if os.path.isdir(legacy_dir):
        return legacy_dir
    return plugin_data_dir


def _record_event(ts_ms: int, argv_event: str) -> str:
    raw = sys.stdin.read()
    payload = json.loads(raw) if raw.strip() else {}
    if not isinstance(payload, dict):
        payload = {}

    event_name = argv_event or payload.get("hookEventName") or "Unknown"
    conv_id = str(payload.get("conversationId") or "")
    if not conv_id or not _SAFE_CONV_RE.fullmatch(conv_id):
        return event_name

    transcript_path = str(payload.get("transcriptPath") or "")
    state_dir = _get_state_dir(transcript_path)
    conv_dir = os.path.join(state_dir, "conversations")
    os.makedirs(conv_dir, exist_ok=True)

    tool_call = payload.get("toolCall")
    tool_name = tool_call.get("name") if isinstance(tool_call, dict) else None

    record = {
        "ts_ms": ts_ms,
        "event": event_name,
        "conversation_id": conv_id,
        "model_name": payload.get("modelName") or "",
        "transcript_path": transcript_path,
        "workspace_paths": payload.get("workspacePaths") or [],
        "invocation_num": payload.get("invocationNum"),
        "step_idx": payload.get("stepIdx"),
        "tool_name": tool_name,
        "termination_reason": payload.get("terminationReason"),
        "has_error": bool(payload.get("error")),
    }

    line = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
    hook_log = os.path.join(conv_dir, f"{conv_id}.hooks.jsonl")
    fd = os.open(hook_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)

    latest_record = dict(record)
    latest_record["status"] = "generating" if event_name == "PreInvocation" else "idle"
    latest_path = os.path.join(state_dir, "latest.json")
    tmp_path = os.path.join(state_dir, f"latest.json.tmp.{os.getpid()}")
    try:
        tmp_fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(
                tmp_fd, json.dumps(latest_record, separators=(",", ":")).encode("utf-8")
            )
        finally:
            os.close(tmp_fd)
        os.replace(tmp_path, latest_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return event_name


def main() -> None:
    ts_ms = int(time.time() * 1000)
    argv_event = sys.argv[1] if len(sys.argv) > 1 else ""
    event_name = argv_event
    try:
        event_name = _record_event(ts_ms, argv_event)
    except Exception:
        pass

    if event_name == "Stop":
        sys.stdout.write('{"decision":"allow"}\n')
    else:
        sys.stdout.write("{}\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
