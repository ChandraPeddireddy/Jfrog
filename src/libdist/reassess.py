"""Rescan the releases that compute is using, and track grace deadlines.

For each new blocking finding this job:
  1. records the finding,
  2. opens a GRACE exception with a deadline taken from policy.yml,
  3. optionally starts a replacement build that upgrades only the affected package.

When a deadline passes, the exception is marked EXPIRED and an event is
written. The job never revokes a release by itself; that is a Security
decision (D7) and a runbook step.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import registry as reg
from .build import _finding_row
from .promote import _as_datetime
from .scan import SCANNED
from .settings import Config
from .util import log, utcnow


def reassess(cfg: Config, registry: reg.Registry, scanner, now: datetime | None = None,
             trigger_rebuild: Callable[[str, list[str]], None] | None = None) -> dict:
    now = now or utcnow()
    policy = cfg.policy
    summary: dict = {"releases": {}, "expired": [], "rebuilds": {}}
    if not policy.grace_values_approved:
        log.warning("policy.yml: grace_values_approved is false; grace hours are examples, "
                    "not values approved by Security (decision D1)")

    for release_id in registry.releases_in_use():
        row = registry.get_release(release_id)
        if not row or row["status"] != reg.PUBLISHED:
            continue
        wheels = Path(row["release_path"]) / "wheels"
        scans, violations = scanner.scan_release(wheels)
        failed = [s.filename for s in scans if s.status != SCANNED]
        if failed or violations is None:
            registry.add_event("RESCAN_FAILED", release_id, row["channel"],
                               {"failed": failed, "watch_scan_failed": violations is None})
            summary["releases"][release_id] = "RESCAN_FAILED"
            continue

        known = {(f.get("filename") or "", f["issue_id"]) for f in registry.list_findings(release_id)}
        open_issues = {e["issue_id"] for e in registry.list_exceptions(release_id=release_id)}
        new_rows, affected = [], set()
        candidates = [(s.package, s.version, s.filename, f) for s in scans for f in s.findings]
        candidates += [(f.component, f.component_version, "", f) for f in violations]
        for package, version, filename, finding in candidates:
            if (filename, finding.issue_id) in known:
                continue
            known.add((filename, finding.issue_id))
            new_rows.append(_finding_row(release_id, package, version, filename, finding,
                                         policy.severity_source, "reassess"))
            blocking = finding.is_violation or finding.severity == "Unknown" or policy.blocks(finding.severity)
            if not blocking or finding.issue_id in open_issues:
                continue
            open_issues.add(finding.issue_id)
            hours = policy.grace_for(finding.severity)
            deadline = now + timedelta(hours=hours)
            registry.add_exception({
                "release_id": release_id, "channel": row["channel"], "package": package,
                "version": version, "issue_id": finding.issue_id, "severity": finding.severity,
                "kind": "GRACE", "deadline_at": deadline,
                "rationale": f"automatic grace period of {hours}h for a new {finding.severity} finding",
            })
            registry.add_event("NEW_FINDING", release_id, row["channel"], {
                "package": package, "version": version, "issue_id": finding.issue_id,
                "severity": finding.severity, "fixed_versions": finding.fixed_versions,
                "grace_deadline": deadline.isoformat(),
            })
            affected.add(package)
        if new_rows:
            registry.add_findings(new_rows)
        summary["releases"][release_id] = {"new_findings": len(new_rows), "affected": sorted(affected)}
        if affected:
            summary["rebuilds"].setdefault(row["channel"], set()).update(affected)

    for exc in registry.list_exceptions():
        if exc.get("status") not in ("OPEN", "APPROVED"):
            continue
        deadline = _as_datetime(exc.get("deadline_at"))
        if deadline and deadline.timestamp() < now.timestamp():
            registry.update_exception(exc["exception_id"], status="EXPIRED")
            registry.add_event("GRACE_EXPIRED", exc.get("release_id") or "", exc.get("channel") or "",
                               {"issue_id": exc.get("issue_id"), "severity": exc.get("severity"),
                                "deadline": str(exc.get("deadline_at"))})
            summary["expired"].append(exc["exception_id"])

    summary["rebuilds"] = {c: sorted(p) for c, p in summary["rebuilds"].items()}
    registry.add_event("RESCAN_COMPLETED", "", "", {
        "releases": len(summary["releases"]), "expired": len(summary["expired"]),
        "rebuilds": summary["rebuilds"]})
    if trigger_rebuild:
        for channel, packages in summary["rebuilds"].items():
            trigger_rebuild(channel, packages)
    return summary
