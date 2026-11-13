"""Test helpers: hand-made wheels, a local config folder, and fakes for JFrog and Databricks."""

from __future__ import annotations

import base64
import hashlib
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from libdist.lockfile import parse_dist_filename  # noqa: E402
from libdist.scan import SCANNED, ArtifactScan, Finding  # noqa: E402
from libdist.settings import Config  # noqa: E402
from libdist.util import sha256_file  # noqa: E402


def make_wheel(folder: Path, name: str, version: str, requires: list[str] | None = None,
               body: str = "") -> Path:
    """Write a minimal, valid pure-Python wheel without any build tool."""
    folder.mkdir(parents=True, exist_ok=True)
    module = name.replace("-", "_")
    dist_info = f"{module}-{version}.dist-info"
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    for req in requires or []:
        metadata += f"Requires-Dist: {req}\n"
    files = {
        f"{module}/__init__.py": f'__version__ = "{version}"\n{body}',
        f"{dist_info}/METADATA": metadata,
        f"{dist_info}/WHEEL": "Wheel-Version: 1.0\nGenerator: libdist-tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{dist_info}/top_level.txt": f"{module}\n",
    }
    record = []
    for path, content in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content.encode()).digest()).rstrip(b"=").decode()
        record.append(f"{path},sha256={digest},{len(content.encode())}")
    record.append(f"{dist_info}/RECORD,,")
    files[f"{dist_info}/RECORD"] = "\n".join(record) + "\n"
    wheel = folder / f"{module}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return wheel


class FakeScanner:
    """Stands in for JfScanner. ``findings`` maps a wheel filename to its findings."""

    def __init__(self, findings: dict[str, list[Finding]] | None = None, fail: set[str] | None = None,
                 violations: list[Finding] | None = None):
        self.findings = findings or {}
        self.fail = fail or set()
        self.violations = violations if violations is not None else []

    def scan_release(self, wheels_dir):
        scans = []
        for wheel in sorted(Path(wheels_dir).glob("*.whl")):
            name, version = parse_dist_filename(wheel.name)
            if wheel.name in self.fail:
                scans.append(ArtifactScan(wheel.name, name, version, sha256_file(wheel), "FAILED",
                                          error="simulated scanner failure"))
            else:
                scans.append(ArtifactScan(wheel.name, name, version, sha256_file(wheel), SCANNED,
                                          list(self.findings.get(wheel.name, []))))
        return scans, self.violations


class FakePolicies:
    """Stands in for PolicyClient: compute policies kept in a dict."""

    def __init__(self, policies: dict[str, dict]):
        self.policies = policies
        self.edits: list[tuple[str, list[dict]]] = []

    def get(self, policy_id):
        return dict(self.policies[policy_id])

    def set_libraries(self, policy, libraries):
        self.policies[policy["policy_id"]] = {**policy, "libraries": libraries}
        self.edits.append((policy["policy_id"], libraries))


@pytest.fixture
def index(tmp_path) -> Path:
    """A folder of wheels that plays the role of the package index."""
    folder = tmp_path / "index"
    make_wheel(folder, "alpha", "1.0", ["beta>=1"])
    make_wheel(folder, "beta", "1.0")
    make_wheel(folder, "beta", "1.1")
    return folder


@pytest.fixture
def cfg(tmp_path, index) -> Config:
    """A config folder in local test mode: volumes under tmp_path, index = local folder."""
    config = tmp_path / "config"
    channel = config / "channels" / "demo"
    channel.mkdir(parents=True)
    (config / "settings.yml").write_text(yaml.safe_dump({
        "catalog": "cat", "schema": "libs", "volumes_root": str(tmp_path / "Volumes"),
        "find_links": str(index), "max_resolve_retries": 5,
    }))
    (config / "policy.yml").write_text(yaml.safe_dump({
        "block_severities": ["Critical", "High"], "grace_values_approved": True,
        "grace_hours": {"Critical": 24, "High": 72},
    }))
    (channel / "channel.yml").write_text(yaml.safe_dump({
        "runtime_profile": "local-test", "owner": "owner@example.com",
        "waves": [{"name": "canary", "policy_ids": ["P1"], "job_ids": ["11"]},
                  {"name": "main", "policy_ids": ["P2", "P3"], "job_ids": []}],
    }))
    (channel / "channel.in").write_text("alpha\n")
    (channel / "constraints.txt").write_text("")
    return Config(config)
