#!/usr/bin/env python3
"""Zero-dependency project health check runner (py_compile + manifests + static assets + unittest)."""

from __future__ import annotations

from pathlib import Path
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install as installer  # noqa: E402
from sidecars.dashboard.server import assemble_dashboard_html  # noqa: E402

_STATIC_DIR = REPO_ROOT / "sidecars" / "dashboard" / "static"
_PY_DIRS = ("agy_metrics", "hooks", "sidecars", "scripts", "tests")
_EXPORT_IMPORT_RE = re.compile(
    r"""import\s+(?:\{([^}]+)\}\s+from\s+|[^'"]*?from\s+)['"](\.[^'"]+)['"]"""
)
_ASSET_ATTR_RE = re.compile(r"""(?:href|src)=["']/static/([^"']+)["']""")


def _check_py_compile() -> list[str]:
    errors: list[str] = []
    py_files = [REPO_ROOT / "install.py"]
    for rel_dir in _PY_DIRS:
        base = REPO_ROOT / rel_dir
        if base.is_dir():
            py_files.extend(
                sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)
            )
    for path in py_files:
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as exc:
            errors.append(f"py_compile failed for {path}: {exc}")
    print(f"[check] py_compile: OK ({len(py_files)} files)")
    return errors


def _check_static_assets() -> list[str]:
    errors: list[str] = []
    index_html = _STATIC_DIR / "index.html"
    if not index_html.is_file():
        return [f"Missing {index_html}"]

    html = index_html.read_text(encoding="utf-8")
    referenced = _ASSET_ATTR_RE.findall(html)
    if not referenced:
        errors.append("index.html does not reference any /static/* assets")
    for rel in referenced:
        if not (_STATIC_DIR / rel).is_file():
            errors.append(f"index.html references missing static asset: /static/{rel}")

    js_files = sorted(_STATIC_DIR.glob("*.js"))
    for js_file in js_files:
        text = js_file.read_text(encoding="utf-8")
        for match in _EXPORT_IMPORT_RE.finditer(text):
            imports_raw, target_rel = match.group(1) or "", match.group(2)
            target_path = (js_file.parent / target_rel).resolve()
            if not target_path.is_file():
                errors.append(
                    f"{js_file.name} imports missing module {target_rel!r}"
                )
                continue
            if imports_raw.strip():
                target_text = target_path.read_text(encoding="utf-8")
                for ident in [
                    part.split(" as ")[0].strip()
                    for part in imports_raw.split(",")
                    if part.strip()
                ]:
                    if not re.search(
                        rf"\bexport\s+(?:async\s+)?(?:function|const|let|var|class)\s+{re.escape(ident)}\b",
                        target_text,
                    ):
                        errors.append(
                            f"{js_file.name} imports missing export {ident!r} from {target_rel}"
                        )

    if not errors:
        try:
            assembled = assemble_dashboard_html(_STATIC_DIR, "check-csrf-token").decode(
                "utf-8"
            )
            if '<meta name="agy-csrf-token"' not in assembled:
                errors.append("Assembled dashboard HTML missing agy-csrf-token meta tag")
            if '<script type="module"' in assembled:
                errors.append("Assembled dashboard HTML still contains module script tag")
            m = re.search(r"<script>\n(.*)\n  </script>", assembled, re.DOTALL)
            if not m:
                errors.append("Assembled dashboard HTML missing inline <script> bundle")
            else:
                script_body = m.group(1)
                if re.search(
                    r"^\s*(?:import\s+[\s\S]*?from\s+['\"]|export\s+(?:async\s+)?(?:function|const|let|var|class)\b)",
                    script_body,
                    re.MULTILINE,
                ):
                    errors.append(
                        "Assembled dashboard JS bundle still contains top-level import/export syntax"
                    )
                gjs_bin = shutil.which("gjs")
                if gjs_bin and not errors:
                    with tempfile.TemporaryDirectory() as tmp:
                        js_tmp = Path(tmp) / "bundle.js"
                        js_tmp.write_text(script_body, encoding="utf-8")
                        proc = subprocess.run(
                            [
                                gjs_bin,
                                "-c",
                                "const [, b] = imports.gi.GLib.file_get_contents(ARGV[0]); "
                                "new Function(imports.byteArray.toString(b));",
                                str(js_tmp),
                            ],
                            capture_output=True,
                            text=True,
                            timeout=10,
                        )
                        if proc.returncode != 0:
                            errors.append(
                                f"Assembled JS syntax check failed: {proc.stderr.strip()}"
                            )
        except Exception as exc:
            errors.append(f"assemble_dashboard_html failed: {exc}")

    print(
        f"[check] static assets: OK ({len(js_files)} JS modules + styles.css + index.html + assembled bundle)"
    )
    return errors


def main() -> int:
    errors = _check_py_compile()
    manifest_errors = installer._validate_manifests(REPO_ROOT)
    if manifest_errors:
        errors.extend(manifest_errors)
    else:
        print("[check] manifests: OK")
    errors.extend(_check_static_assets())

    if errors:
        for err in errors:
            print(f"[ERROR] {err}", file=sys.stderr)
        return 1

    suite = unittest.defaultTestLoader.discover(str(REPO_ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        return 1
    if result.skipped:
        print(
            f"[ERROR] Unexpected skipped test(s): {len(result.skipped)}",
            file=sys.stderr,
        )
        return 1
    print(f"[check] unittest: OK ({result.testsRun} tests passed, 0 skipped)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
