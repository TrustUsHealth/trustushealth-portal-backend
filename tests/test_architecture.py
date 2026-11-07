"""Structural rules from PRD 4.2 and 4.4."""

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

APP = Path("app")


def imports_of(path: Path) -> set[str]:
    out = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def test_modules_never_import_each_other():
    """A module may import from core and domain only; modules talk through events."""
    offenders = []
    for path in (APP / "modules").rglob("*.py"):
        own = path.relative_to(APP / "modules").parts[0]
        for imp in imports_of(path):
            m = re.match(r"app\.modules\.(\w+)", imp)
            if m and m.group(1) != own:
                offenders.append(f"{path}: imports {imp}")
    assert not offenders, offenders


def test_core_and_domain_do_not_import_modules():
    offenders = [
        f"{p}: {i}"
        for sub in ("core", "domain")
        if (APP / sub).exists()
        for p in (APP / sub).rglob("*.py")
        for i in imports_of(p)
        if i.startswith("app.modules")
    ]
    assert not offenders, offenders


def test_every_module_folder_is_listed_in_modules_yaml():
    from app.core.config import load_config

    listed = {m.key for m in load_config("config").modules}
    on_disk = {p.name for p in (APP / "modules").iterdir() if p.is_dir() and not p.name.startswith("__")}
    assert on_disk <= listed, f"module folders missing from modules.yaml: {on_disk - listed}"


def test_operation_ids_are_unique_for_the_generated_client(portal):
    ids = [op["operationId"] for p in portal.app.openapi()["paths"].values() for op in p.values()]
    assert len(ids) == len(set(ids))


def test_committed_openapi_json_is_up_to_date():
    r = subprocess.run([sys.executable, "-m", "scripts.export_openapi", "--check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_committed_openapi_is_valid_json():
    assert "paths" in json.loads(Path("openapi.json").read_text())
