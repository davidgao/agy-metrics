"""Path resolution helpers for agy-metrics."""

from __future__ import annotations

import os
from pathlib import Path


def _app_data_from_executable_data_dir(raw_dir: str | None) -> Path | None:
    if not raw_dir or not raw_dir.strip():
        return None
    p = Path(raw_dir.strip()).expanduser()
    for candidate in (p, *p.parents):
        if candidate.name == "sidecar_data" and candidate.parent != candidate:
            return candidate.parent
    return None


def get_app_data_dir() -> Path:
    explicit = os.environ.get("AGY_METRICS_APP_DATA_DIR")
    if explicit:
        return Path(explicit).expanduser()
    env_dir = os.environ.get("ANTIGRAVITY_APP_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    exec_app_data = _app_data_from_executable_data_dir(
        os.environ.get("ANTIGRAVITY_EXECUTABLE_DATA_DIR")
    )
    if exec_app_data is not None:
        return exec_app_data

    gemini_root = Path.home() / ".gemini"
    default = gemini_root / "antigravity"
    if not (default / "brain").is_dir() and gemini_root.is_dir():
        try:
            for cand in sorted(gemini_root.iterdir(), key=lambda c: c.name):
                if cand.name != "config" and cand.is_dir() and (cand / "brain").is_dir():
                    return cand
        except OSError:
            pass
    return default


def get_brain_dir() -> Path:
    explicit = os.environ.get("AGY_METRICS_BRAIN_DIR")
    if explicit:
        return Path(explicit).expanduser()
    return get_app_data_dir() / "brain"


def get_state_dir() -> Path:
    explicit = os.environ.get("AGY_METRICS_STATE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    plugin_data = os.environ.get("PLUGIN_DATA")
    if plugin_data:
        return Path(plugin_data).expanduser()
    app_data = get_app_data_dir()
    plugin_data_dir = app_data / "plugin_data" / "agy-metrics"
    legacy_dir = app_data / "agy_metrics"
    if plugin_data_dir.is_dir() or (app_data / "plugin_data").is_dir():
        return plugin_data_dir
    if legacy_dir.is_dir():
        return legacy_dir
    return plugin_data_dir
