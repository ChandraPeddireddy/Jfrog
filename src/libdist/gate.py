"""The security gate: turns scan results into PASS, FAIL or HOLD.

Rules (from the customer design document):
  * a wheel that was not scanned successfully holds the release;
  * a finding with unknown severity holds the release, it is never treated as low;
  * a finding with a blocked severity, or any policy violation, fails the release;
  * "no findings" passes only when every wheel in the lock was scanned.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .lockfile import Pin
from .scan import SCANNED, ArtifactScan, Finding
from .settings import SEVERITIES, Policy

PASS, FAIL, HOLD = "PASS", "FAIL", "HOLD"


@dataclass
class GateDecision:
    status: str
    reasons: list[str] = field(default_factory=list)
    blocking: list[dict] = field(default_factory=list)
    package_severity: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "reasons": self.reasons,
            "blocking": self.blocking,
            "package_severity": self.package_severity,
        }


def highest(severities: list[str]) -> str:
    for sev in SEVERITIES:  # ordered from Critical down; Unknown is last
        if sev in severities:
            return sev
    return ""


def evaluate(
    pins: list[Pin],
    scans: list[ArtifactScan],
    violations: list[Finding] | None,
    policy: Policy,
    accepted_issue_ids: set[str] | None = None,
) -> GateDecision:
    """``accepted_issue_ids`` are findings covered by an approved, unexpired exception."""
    accepted = accepted_issue_ids or set()
    hold: list[str] = []
    fail: list[str] = []
    blocking: list[dict] = []
    package_severity: dict[str, str] = {}

    by_package: dict[tuple[str, str], list[ArtifactScan]] = {}
    for scan in scans:
        by_package.setdefault((scan.package, scan.version), []).append(scan)

    # Coverage: every pinned package needs at least one wheel, all scanned successfully.
    for pin in pins:
        package_scans = by_package.get((pin.key, pin.version))
        if not package_scans:
            hold.append(f"{pin.name}=={pin.version}: no wheel was scanned")
            continue
        for scan in package_scans:
            if scan.status != SCANNED:
                hold.append(f"{scan.filename}: scan failed ({scan.error or 'no detail'})")

    pinned = {(p.key, p.version) for p in pins}
    for key in by_package:
        if key not in pinned:
            hold.append(f"{key[0]}=={key[1]}: wheel present but not in the lock file")

    for scan in scans:
        severities = []
        for finding in scan.findings:
            if finding.issue_id in accepted:
                continue
            severities.append(finding.severity)
            label = f"{scan.package}=={scan.version}: {finding.issue_id} ({finding.severity})"
            if finding.severity == "Unknown":
                hold.append(f"{label}: unknown severity")
            elif policy.blocks(finding.severity):
                fail.append(label)
                blocking.append({"package": scan.package, "version": scan.version, **finding.to_dict()})
        top = highest(severities)
        if top:
            package_severity[f"{scan.package}=={scan.version}"] = top

    if violations is None:
        if policy.hold_on_scan_failure:
            hold.append("the Watch (policy violation) scan failed")
    else:
        for finding in violations:
            if finding.issue_id in accepted:
                continue
            fail.append(f"policy violation: {finding.component} {finding.issue_id} ({finding.severity})")
            blocking.append({"package": finding.component, "version": finding.component_version,
                             **finding.to_dict()})

    if fail:
        return GateDecision(FAIL, fail + hold, blocking, package_severity)
    if hold:
        return GateDecision(HOLD, hold, blocking, package_severity)
    return GateDecision(PASS, [], [], package_severity)
