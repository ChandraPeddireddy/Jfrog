"""Rescan the releases that compute is using, and track grace deadlines.

For each new blocking finding this job:
  1. records the finding,
  2. opens a GRACE exception with a deadline taken from policy.yml.

A finding that was already recorded but has become blocking (Xray raised its
severity, or policy.yml was tightened) gets a grace period in the same way.

Then, for every release in use that has an active grace period and no
replacement on the way, it starts a replacement build that upgrades only the
affected packages. It tries again on every run, so a build that failed because
a fix was still inside the Curation window succeeds once the fix is allowed or
a waiver is approved.

When a deadline passes, the exception is marked EXPIRED. The release is revoked
only if policy.yml says ``on_grace_expiry: revoke`` (decision D7).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import registry as reg
from .build import _accepted_issues, _finding_row
from .promote import _as_datetime, revoke
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
        # Issues that already have an exception on this release, or that Security accepted for the channel.
        open_issues = {e["issue_id"] for e in registry.list_exceptions(release_id=release_id)}
        open_issues |= _accepted_issues(registry, row["channel"])
        new_rows, affected = [], set()
        candidates = [(s.package, s.version, s.filename, f) for s in scans for f in s.findings]
        candidates += [(f.component, f.component_version, "", f) for f in violations]
        for package, version, filename, finding in candidates:
            if (filename, finding.issue_id) not in known:
                known.add((filename, finding.issue_id))
                new_rows.append(_finding_row(release_id, package, version, filename, finding,
                                             policy.severity_source, "reassess"))
            # Not only new findings: one that became blocking (Xray raised its severity, or
            # policy.yml was tightened) also needs a grace period.
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

    for exc in registry.list_exceptions():
        if exc.get("status") not in ("OPEN", "APPROVED") or exc.get("kind") != "GRACE":
            continue
        deadline = _as_datetime(exc.get("deadline_at"))
        if deadline and deadline.timestamp() < now.timestamp():
            registry.update_exception(exc["exception_id"], status="EXPIRED")
            registry.add_event("GRACE_EXPIRED", exc.get("release_id") or "", exc.get("channel") or "",
                               {"issue_id": exc.get("issue_id"), "severity": exc.get("severity"),
                                "deadline": str(exc.get("deadline_at"))})
            summary["expired"].append(exc["exception_id"])
            release = registry.get_release(exc.get("release_id") or "")
            if policy.on_grace_expiry == "revoke" and release and release["status"] == reg.PUBLISHED:
                revoke(registry, release["release_id"],
                       f"grace period for {exc.get('issue_id')} ({exc.get('severity')}) ended")
                summary.setdefault("revoked", []).append(release["release_id"])

    # Replacement builds: every release in use with an active grace period and nothing on the way.
    in_use = set(registry.releases_in_use())
    for exc in registry.list_exceptions():
        if exc.get("kind") != "GRACE" or exc.get("status") not in ("OPEN", "APPROVED", "EXPIRED"):
            continue
        if exc.get("release_id") not in in_use or not exc.get("package"):
            continue
        if _replacement_on_the_way(registry, exc["channel"], exc["release_id"], now):
            continue
        summary["rebuilds"].setdefault(exc["channel"], set()).add(exc["package"])

    summary["rebuilds"] = {c: sorted(p) for c, p in summary["rebuilds"].items()}
    registry.add_event("RESCAN_COMPLETED", "", "", {
        "releases": len(summary["releases"]), "expired": len(summary["expired"]),
        "rebuilds": summary["rebuilds"]})
    if trigger_rebuild:
        for channel, packages in summary["rebuilds"].items():
            trigger_rebuild(channel, packages)
    return summary


_BUILDING = (reg.DOWNLOADING, reg.SCANNING, reg.TESTING)
_WAITING = (reg.READY_FOR_APPROVAL, reg.APPROVED, reg.PUBLISHED)


def _replacement_on_the_way(registry: reg.Registry, channel: str, release_id: str, now: datetime,
                            stale_hours: float = 6) -> bool:
    """True when a newer release of the channel is being built, awaits approval, or is published."""
    current = registry.get_release(release_id) or {}
    for other in registry.list_releases(channel):
        if other["release_id"] == release_id or str(other.get("created_at")) <= str(current.get("created_at")):
            continue
        if other["status"] in _WAITING:
            rolled_back = any(r["release_id"] == other["release_id"] and r["status"] == "ROLLED_BACK"
                              for r in registry.list_rollouts(channel))
            if not rolled_back:
                return True
            continue
        created = _as_datetime(other.get("created_at"))
        if other["status"] in _BUILDING and created and \
                (now.timestamp() - created.timestamp()) < stale_hours * 3600:
            return True
    return False
