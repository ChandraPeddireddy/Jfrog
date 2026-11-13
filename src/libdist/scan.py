"""Xray scanning through the JFrog CLI, behind a small adapter.

The rest of the pipeline only sees ``ArtifactScan`` and ``Finding``. If the
JFrog CLI output format differs in the customer's version, change
``parse_simple_json`` only.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .lockfile import parse_dist_filename
from .settings import canonical_severity
from .util import CommandResult, log, run, sha256_file

Runner = Callable[..., CommandResult]

SCANNED = "SCANNED"
SCAN_FAILED = "FAILED"


@dataclass
class Finding:
    issue_id: str
    severity: str
    component: str = ""  # the impacted component as Xray names it
    component_version: str = ""
    cves: list[str] = field(default_factory=list)
    fixed_versions: list[str] = field(default_factory=list)
    summary: str = ""
    is_violation: bool = False  # True when it comes from a Watch / policy

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ArtifactScan:
    filename: str
    package: str
    version: str
    sha256: str
    status: str  # SCANNED or FAILED
    findings: list[Finding] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["findings"] = [f.to_dict() for f in self.findings]
        return data


def _extract_json(text: str) -> dict | None:
    """The CLI can print log lines around the JSON document."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _clean_component(name: str) -> str:
    name = name or ""
    if "://" in name:
        name = name.split("://", 1)[1]
    return name.split(":", 1)[0]


def _row_to_finding(row: dict, is_violation: bool) -> Finding:
    cves = []
    for cve in row.get("cves") or []:
        cve_id = cve.get("id") if isinstance(cve, dict) else cve
        if cve_id:
            cves.append(str(cve_id))
    issue_id = str(row.get("issueId") or (cves[0] if cves else "") or row.get("summary", "")[:60])
    return Finding(
        issue_id=issue_id,
        severity=canonical_severity(row.get("severity")),
        component=_clean_component(row.get("impactedPackageName", "")),
        component_version=str(row.get("impactedPackageVersion", "")),
        cves=cves,
        fixed_versions=[str(v) for v in (row.get("fixedVersions") or [])],
        summary=str(row.get("summary", ""))[:500],
        is_violation=is_violation,
    )


def parse_simple_json(text: str) -> list[Finding] | None:
    """Parse ``jf scan --format=simple-json``. Returns None if it is not valid output.

    Expected top-level keys: ``vulnerabilities`` and ``securityViolations``
    (either may be missing or null when there is nothing to report).
    """
    data = _extract_json(text)
    if data is None:
        return None
    findings = [_row_to_finding(r, False) for r in data.get("vulnerabilities") or []]
    findings += [_row_to_finding(r, True) for r in data.get("securityViolations") or []]
    return findings


class JfScanner:
    """Runs ``jf scan`` on wheel files.

    Vulnerabilities come from a scan without Watches, one wheel at a time, so
    every artifact gets its own scan status. If Watches are configured a second
    scan reports policy violations (the CLI shows only violations when Watches
    are given).
    """

    SERVER_ID = "libdist"

    def __init__(self, cli_path: str, home_dir: str | Path, watches: list[str] | None = None,
                 threads: int = 4, runner: Runner = run, releases_repo: str = ""):
        self.cli = cli_path
        self.home = str(home_dir)
        self.watches = watches or []
        self.threads = max(1, threads)
        self.runner = runner
        self.env = dict(os.environ)
        self.env.update({"JFROG_CLI_HOME_DIR": self.home, "CI": "true", "JFROG_CLI_REPORT_USAGE": "false"})
        if releases_repo:
            self.env["JFROG_CLI_RELEASES_REPO"] = f"{self.SERVER_ID}/{releases_repo}"

    def configure(self, platform_url: str, token: str) -> None:
        Path(self.home).mkdir(parents=True, exist_ok=True)
        result = self.runner(
            [self.cli, "config", "add", self.SERVER_ID, f"--url={platform_url}",
             "--access-token-stdin", "--interactive=false", "--overwrite=true"],
            env=self.env, input_text=token, secrets=[token],
        )
        if result.returncode != 0:
            raise RuntimeError(f"jf config add failed:\n{result.output[-2000:]}")

    def scan_file(self, path: str | Path) -> ArtifactScan:
        path = Path(path)
        name, version = parse_dist_filename(path.name) or (path.name, "")
        scan = ArtifactScan(path.name, name, version, sha256_file(path), SCAN_FAILED)
        result = self.runner(
            [self.cli, "scan", str(path), f"--server-id={self.SERVER_ID}",
             "--format=simple-json", "--fail=false"],
            env=self.env,
        )
        if result.returncode != 0:
            scan.error = f"jf scan exit code {result.returncode}: {result.stderr[-1000:]}"
            return scan
        findings = parse_simple_json(result.stdout)
        if findings is None:
            scan.error = "jf scan did not return a JSON document"
            return scan
        scan.status, scan.findings = SCANNED, findings
        return scan

    def scan_violations(self, wheels_dir: str | Path) -> list[Finding] | None:
        """Policy violations for the whole folder. None means the scan failed."""
        if not self.watches:
            return []
        result = self.runner(
            [self.cli, "scan", f"{wheels_dir}/*.whl", f"--server-id={self.SERVER_ID}",
             f"--watches={','.join(self.watches)}", "--format=simple-json", "--fail=false"],
            env=self.env,
        )
        if result.returncode != 0:
            log.error("violation scan failed: %s", result.stderr[-1000:])
            return None
        findings = parse_simple_json(result.stdout)
        return None if findings is None else [f for f in findings if f.is_violation]

    def scan_release(self, wheels_dir: str | Path) -> tuple[list[ArtifactScan], list[Finding] | None]:
        wheels = sorted(Path(wheels_dir).glob("*.whl"))
        with ThreadPoolExecutor(max_workers=self.threads) as pool:
            scans = list(pool.map(self.scan_file, wheels))
        return scans, self.scan_violations(wheels_dir)
