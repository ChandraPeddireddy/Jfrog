"""Compatibility test: install the release offline, then import its modules.

The install goes into an empty folder with ``pip install --target`` and uses
only the release's own wheels. The imports run in a separate Python process
with that folder first on the path, on top of the runtime's own packages.

This proves the release installs without an index and that its modules load.
It does not prove that a library behaves the same as the previous version;
team smoke tests cover that.
"""

from __future__ import annotations

import json
import os
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .util import CommandResult, run

Runner = Callable[..., CommandResult]


def top_level_modules(wheel_path: str | Path) -> list[str]:
    """Importable top-level module names of a wheel."""
    modules: set[str] = set()
    with zipfile.ZipFile(wheel_path) as archive:
        names = archive.namelist()
        declared = [n for n in names if n.endswith(".dist-info/top_level.txt")]
        if declared:
            for line in archive.read(declared[0]).decode().splitlines():
                if line.strip():
                    modules.add(line.strip().replace("/", "."))
            return sorted(modules)
        for name in names:
            first = name.split("/", 1)[0]
            if first.endswith((".dist-info", ".data")):
                continue
            if "/" in name:
                if name == f"{first}/__init__.py":
                    modules.add(first)
            elif first.endswith(".py"):
                modules.add(first[:-3])
            elif ".cpython-" in first or first.endswith((".so", ".pyd")):
                modules.add(first.split(".", 1)[0])
    return sorted(modules)


@dataclass
class TestReport:
    __test__ = False  # not a pytest class

    installed: bool
    install_log: str = ""
    imports: dict[str, str] = field(default_factory=dict)  # module -> "ok" or the error
    smoke: dict[str, str] = field(default_factory=dict)

    @property
    def failed_imports(self) -> dict[str, str]:
        return {m: e for m, e in self.imports.items() if e != "ok"}

    @property
    def failed_smoke(self) -> dict[str, str]:
        return {m: e for m, e in self.smoke.items() if e != "ok"}

    @property
    def passed(self) -> bool:
        return self.installed and not self.failed_imports and not self.failed_smoke

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "installed": self.installed,
            "imports_ok": sorted(m for m, e in self.imports.items() if e == "ok"),
            "imports_failed": self.failed_imports,
            "smoke": self.smoke,
            "install_log_tail": self.install_log[-2000:],
        }


_IMPORT_SCRIPT = r"""
import importlib, json, sys
out = {}
for name in json.loads(sys.argv[1]):
    try:
        importlib.import_module(name)
        out[name] = "ok"
    except BaseException as exc:
        out[name] = f"{type(exc).__name__}: {exc}"[:300]
print("LIBDIST_IMPORTS=" + json.dumps(out))
"""


def run_test(
    lock_path: str | Path,
    wheels_dir: str | Path,
    target_dir: str | Path,
    skip_imports: list[str] | None = None,
    extra_imports: list[str] | None = None,
    smoke_tests: list[str] | None = None,
    runner: Runner = run,
) -> TestReport:
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)

    install = runner([
        sys.executable, "-m", "pip", "install",
        "--no-index", "--find-links", str(wheels_dir),
        "--only-binary=:all:", "--require-hashes",
        "-r", str(lock_path),
        "--target", str(target), "--upgrade",
        "--disable-pip-version-check", "--no-warn-script-location",
    ], env={**os.environ, "PIP_INDEX_URL": "", "PIP_EXTRA_INDEX_URL": "", "PIP_CONFIG_FILE": os.devnull})
    report = TestReport(installed=install.returncode == 0, install_log=install.output)
    if not report.installed:
        return report

    skip = set(skip_imports or [])
    modules: set[str] = set(extra_imports or [])
    for wheel in sorted(Path(wheels_dir).glob("*.whl")):
        modules.update(top_level_modules(wheel))
    modules = {m for m in modules if m not in skip and not m.startswith("_")}

    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(target), os.environ.get("PYTHONPATH", "")])}
    result = runner([sys.executable, "-c", _IMPORT_SCRIPT, json.dumps(sorted(modules))], env=env)
    marker = [line for line in result.stdout.splitlines() if line.startswith("LIBDIST_IMPORTS=")]
    if marker:
        report.imports = json.loads(marker[-1].split("=", 1)[1])
    else:  # the interpreter crashed, for example a native library failed to load
        report.imports = {"<import run>": f"crashed: {result.stderr[-500:]}"}

    for test_file in smoke_tests or []:
        smoke = runner([sys.executable, str(test_file)], env=env)
        report.smoke[Path(test_file).name] = "ok" if smoke.returncode == 0 else smoke.output[-500:]
    return report
