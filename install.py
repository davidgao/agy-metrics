#!/usr/bin/env python3
"""Installer and validator for the agy-metrics Antigravity Desktop plugin."""

from __future__ import annotations

import argparse
import filecmp
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile

PLUGIN_NAME = "agy-metrics"
REPO_ROOT = Path(__file__).resolve().parent
RUNTIME_ENTRIES = (
    "plugin.json",
    "hooks.json",
    "README.md",
    "LICENSE",
    "assets",
    "hooks",
    "sidecars",
    "agy_metrics",
)


def _plugins_dir(plugins_dir: Path | None = None) -> Path:
    if plugins_dir is not None:
        return Path(plugins_dir)
    return Path.home() / ".gemini" / "config" / "plugins"


def _target_path(plugins_dir: Path | None = None) -> Path:
    return _plugins_dir(plugins_dir) / PLUGIN_NAME


def _is_ignored_name(name: str) -> bool:
    return (
        name.startswith(".")
        or name == "__pycache__"
        or name.endswith((".pyc", ".pyo"))
    )


def _iter_runtime_files(repo_root: Path) -> list[Path]:
    """Return sorted relative file paths comprising the runtime plugin payload."""
    rel_files: list[Path] = []
    for entry_name in RUNTIME_ENTRIES:
        src = repo_root / entry_name
        if src.is_file() and not _is_ignored_name(src.name):
            rel_files.append(Path(entry_name))
        elif src.is_dir() and not _is_ignored_name(src.name):
            for dirpath, dirnames, filenames in os.walk(src):
                dirnames[:] = sorted(
                    d for d in dirnames if not _is_ignored_name(d)
                )
                for fname in sorted(filenames):
                    if _is_ignored_name(fname):
                        continue
                    rel_files.append((Path(dirpath) / fname).relative_to(repo_root))
    return rel_files


def _remove_target(target: Path) -> None:
    """Remove an existing file, symlink, or directory tree at target."""
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    elif os.path.lexists(target):
        target.unlink()


def _is_installed_in_sync(repo_root: Path, target: Path) -> bool:
    """Return True when target contains an exact copy of runtime files."""
    if not target.is_dir() or target.is_symlink():
        return False
    installed_files: set[Path] = set()
    for dirpath, dirnames, filenames in os.walk(target):
        cur_dir = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not _is_ignored_name(d)]
        for fname in filenames:
            if _is_ignored_name(fname):
                continue
            fpath = cur_dir / fname
            if not fpath.is_file():
                return False
            installed_files.add(fpath.relative_to(target))
    expected = set(_iter_runtime_files(repo_root))
    if installed_files != expected:
        return False
    return all(
        filecmp.cmp(repo_root / rel, target / rel, shallow=False)
        for rel in expected
    )


def _iter_hook_commands(node: object) -> list[str]:
    commands: list[str] = []
    if isinstance(node, dict):
        if node.get("type") == "command" and isinstance(node.get("command"), str):
            commands.append(node["command"])
        for val in node.values():
            commands.extend(_iter_hook_commands(val))
    elif isinstance(node, list):
        for item in node:
            commands.extend(_iter_hook_commands(item))
    return commands


def _validate_manifests(repo_root: Path = REPO_ROOT) -> list[str]:
    errors: list[str] = []
    manifest_paths = {
        "plugin": repo_root / "plugin.json",
        "hooks": repo_root / "hooks.json",
        "sidecar": repo_root / "sidecars" / "dashboard" / "sidecar.json",
        "pricing": repo_root / "agy_metrics" / "pricing.json",
    }
    loaded: dict[str, object] = {}
    for key, path in manifest_paths.items():
        if not path.is_file():
            errors.append(f"Missing manifest: {path}")
            continue
        try:
            loaded[key] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"Invalid JSON in {path}: {exc}")

    repo_resolved = repo_root.resolve()

    plugin_data = loaded.get("plugin")
    if isinstance(plugin_data, dict):
        logo = plugin_data.get("logo")
        if logo is not None:
            if (
                not isinstance(logo, str)
                or not logo.strip()
                or "://" in logo
                or Path(logo).is_absolute()
            ):
                errors.append(f"Invalid plugin logo path: {logo!r}")
            else:
                logo_path = (repo_root / logo).resolve()
                if not logo_path.is_relative_to(repo_resolved) or not logo_path.is_file():
                    errors.append(f"Missing plugin logo asset: {logo}")
    elif "plugin" in loaded:
        errors.append("plugin.json root must be a JSON object")

    hooks_data = loaded.get("hooks")
    if isinstance(hooks_data, dict):
        for cmd in _iter_hook_commands(hooks_data):
            try:
                tokens = shlex.split(cmd)
            except ValueError as exc:
                errors.append(f"Unparseable hook command {cmd!r}: {exc}")
                continue
            script_toks = [
                tok
                for tok in tokens
                if tok.startswith("./") or tok.endswith(".py")
            ]
            if not script_toks:
                errors.append(f"Hook command does not reference a local script: {cmd!r}")
                continue
            for tok in script_toks:
                script_path = (repo_root / tok).resolve()
                if not script_path.is_relative_to(repo_resolved) or not script_path.is_file():
                    errors.append(f"Missing hook script referenced by hooks.json: {tok}")
    elif "hooks" in loaded:
        errors.append("hooks.json root must be a JSON object")

    sidecar_data = loaded.get("sidecar")
    sidecar_dir = repo_root / "sidecars" / "dashboard"
    if isinstance(sidecar_data, dict):
        args_list = sidecar_data.get("args") or []
        if not isinstance(args_list, list):
            errors.append("sidecar.json 'args' must be a list")
        else:
            py_args = [str(a) for a in args_list if str(a).endswith(".py")]
            if not py_args:
                errors.append("sidecar.json must specify a .py server script in 'args'")
            for arg in py_args:
                srv_path = (sidecar_dir / arg).resolve()
                if not srv_path.is_relative_to(repo_resolved) or not srv_path.is_file():
                    errors.append(f"Missing sidecar entrypoint: {arg}")
        index_html = sidecar_dir / "static" / "index.html"
        if not index_html.is_file():
            errors.append(f"Missing dashboard entrypoint: {index_html}")
    elif "sidecar" in loaded:
        errors.append("sidecar.json root must be a JSON object")

    if "pricing" in loaded:
        pricing_path = manifest_paths["pricing"]
        try:
            if str(REPO_ROOT) not in sys.path:
                sys.path.insert(0, str(REPO_ROOT))
            from agy_metrics.pricing import PricingCatalog

            cat = PricingCatalog(bundled_path=pricing_path)
            cfg = cat.get_config()
            if not cfg.get("catalog"):
                errors.append(f"No valid pricing models loaded from {pricing_path}")
        except Exception as exc:
            errors.append(f"Invalid pricing catalog {pricing_path}: {exc}")

    return errors


def cmd_status(
    repo_root: Path = REPO_ROOT,
    plugins_dir: Path | None = None,
) -> int:
    target = _target_path(plugins_dir)
    errors = _validate_manifests(repo_root)
    if errors:
        for err in errors:
            print(f"[ERROR] {err}", file=sys.stderr)
        return 1

    print(f"Source repo : {repo_root}")
    print(
        "Manifests   : OK (plugin.json, hooks.json, sidecars/dashboard/sidecar.json, agy_metrics/pricing.json)"
    )
    if not os.path.lexists(target):
        print(f"Install path: {target} (not installed)")
        return 0
    if target.is_symlink() or not target.is_dir():
        print(f"[ERROR] Install path {target} is not a directory", file=sys.stderr)
        return 1
    if not (target / "plugin.json").is_file():
        print(f"[ERROR] Install path {target} is missing plugin.json", file=sys.stderr)
        return 1
    if _is_installed_in_sync(repo_root, target):
        print(f"Install path: {target} (installed, up to date)")
    else:
        print(f"Install path: {target} (installed, differs from source; run --upgrade to update)")
    return 0


def cmd_install(
    upgrade: bool = False,
    repo_root: Path = REPO_ROOT,
    plugins_dir: Path | None = None,
) -> int:
    errors = _validate_manifests(repo_root)
    if errors:
        for err in errors:
            print(f"[ERROR] {err}", file=sys.stderr)
        return 1

    dest_plugins_dir = _plugins_dir(plugins_dir)
    target = _target_path(plugins_dir)
    repo_resolved = repo_root.resolve()
    if target.is_dir() and not target.is_symlink() and target.resolve() == repo_resolved:
        print(
            f"[ERROR] Target path {target} resolves to source repository",
            file=sys.stderr,
        )
        return 1

    existed = os.path.lexists(target)
    if existed and not upgrade:
        print(
            f"[ERROR] Plugin already exists at {target}. Re-run with --upgrade to replace it.",
            file=sys.stderr,
        )
        return 1

    dest_plugins_dir.mkdir(parents=True, exist_ok=True)
    rel_files = _iter_runtime_files(repo_root)
    manifest_rel = Path("plugin.json")
    ordered_files = [p for p in rel_files if p != manifest_rel]
    if manifest_rel in rel_files:
        ordered_files.append(manifest_rel)

    stage_dir = Path(tempfile.mkdtemp(prefix=f".{PLUGIN_NAME}-stage-", dir=dest_plugins_dir))
    try:
        umask = os.umask(0)
        os.umask(umask)
        stage_dir.chmod(0o777 & ~umask)

        for rel in ordered_files:
            src = repo_root / rel
            dst = stage_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

        if os.path.lexists(target):
            if not upgrade:
                print(
                    f"[ERROR] Plugin already exists at {target}. Re-run with --upgrade to replace it.",
                    file=sys.stderr,
                )
                return 1
            _remove_target(target)

        stage_dir.rename(target)
    finally:
        if os.path.lexists(stage_dir):
            _remove_target(stage_dir)

    verb = "Upgraded" if existed else "Installed"
    print(f"{verb} plugin runtime files ({len(rel_files)}) at {target}")
    return 0


def cmd_uninstall(plugins_dir: Path | None = None) -> int:
    target = _target_path(plugins_dir)
    if not os.path.lexists(target):
        print(f"Not installed ({target} does not exist)")
        return 0
    _remove_target(target)
    print(f"Removed {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install/manage agy-metrics plugin")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--upgrade",
        action="store_true",
        help="Replace an existing installation at ~/.gemini/config/plugins/agy-metrics",
    )
    group.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove ~/.gemini/config/plugins/agy-metrics",
    )
    group.add_argument(
        "--status",
        action="store_true",
        help="Show installation status and validate plugin JSON manifests",
    )
    args = parser.parse_args(argv)

    if args.status:
        return cmd_status()
    if args.uninstall:
        return cmd_uninstall()
    return cmd_install(upgrade=args.upgrade)


if __name__ == "__main__":
    raise SystemExit(main())
