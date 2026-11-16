"""Path resolution helpers for agy-metrics."""

from __future__ import annotations

import os
from pathlib import Path


def get_app_data_dir() -> Path:
    explicit = os.environ.get("AGY_METRICS_APP_DATA_DIR")
    if explicit:
        return Path(explicit).expanduser()
    env_dir = os.environ.get("ANTIGRAVITY_APP_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    return Path.home() / ".gemini" / "antigravity"


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
