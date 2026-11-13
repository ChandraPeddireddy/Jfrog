"""Build a candidate release: resolve, scan, gate, test, and write it to staging."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Callable

from . import gate
from . import registry as reg
from .lockfile import diff_locks, parse_dist_filename, parse_lock, render_install_txt
from .resolve import Blocked, IndexAccess, NoSafeVersion, ResolveError, resolve
from .scan import SCANNED
from .settings import Config
from .tester import run_test
from .util import log, new_release_id, sha256_file, utcnow


def _previous_release(registry: reg.Registry, channel: str) -> dict | None:
    """The newest release that reached the wheelhouse. Its pins are the starting point."""
    for row in registry.list_releases(channel, [reg.PUBLISHED]):
        return row
    return None


def _read_previous_lock(row: dict | None) -> str | None:
    if not row:
        return None
    for base in (row.get("release_path"), row.get("staging_path")):
        if base and (Path(base) / "requirements.lock").exists():
            return (Path(base) / "requirements.lock").read_text()
    return None


def build_release(
    cfg: Config,
    channel_name: str,
    registry: reg.Registry,
    access: IndexAccess,
    scanner,
    upgrade_packages: list[str] | None = None,
    full_upgrade: bool = False,
    classify: Callable[[Blocked], str] | None = None,
    release_id: str | None = None,
    work_root: str | None = None,
) -> dict:
    """Returns a summary dict. ``summary["status"]`` is the release status."""
    settings = cfg.settings
    channel = cfg.channel(channel_name)
    release_id = release_id or new_release_id(channel.name)
    staging = Path(settings.staging_path(channel.name, release_id))
    release_path = settings.release_path(release_id)
    previous = _previous_release(registry, channel.name)
    previous_lock = _read_previous_lock(previous)
    upgrade_packages = [p for p in (upgrade_packages or []) if p]

    registry.create_release({
        "release_id": release_id, "channel": channel.name, "runtime_profile": channel.runtime_profile,
        "status": reg.DOWNLOADING, "staging_path": str(staging), "release_path": release_path,
        "previous_release_id": previous["release_id"] if previous else None,
        "upgrade_packages": ",".join(upgrade_packages),
    })
    registry.add_event("BUILD_STARTED", release_id, channel.name,
                       {"upgrade_packages": upgrade_packages, "full_upgrade": full_upgrade})
    summary: dict = {"release_id": release_id, "channel": channel.name}

    def finish(status: str, reason: str = "", **extra) -> dict:
        registry.update_release(release_id, status=status, status_reason=reason[:2000], **extra)
        registry.add_event(f"BUILD_{status}", release_id, channel.name, {"reason": reason[:2000]})
        summary.update(status=status, reason=reason)
        log.info("release %s: %s %s", release_id, status, reason)
        return summary

    tmp = Path(tempfile.mkdtemp(prefix="libdist-", dir=work_root))
    work, target = tmp / "release", tmp / "test-target"
    try:
        # 1. Resolve and download ------------------------------------------------
        try:
            resolved = resolve(channel, work, access, settings, previous_lock=previous_lock,
                               upgrade_packages=upgrade_packages, full_upgrade=full_upgrade,
                               classify=classify)
        except NoSafeVersion as exc:
            blocked = [b.__dict__ for b in exc.blocked]
            registry.add_event("NO_SAFE_VERSION", release_id, channel.name,
                               {"blocked": blocked, "detail": exc.detail[-1500:]})
            summary["blocked"] = blocked
            return finish(reg.FAILED, f"NO_SAFE_VERSION: {exc}")
        except ResolveError as exc:
            return finish(reg.FAILED, str(exc))
        summary["blocked"] = [b.__dict__ for b in resolved.blocked]

        # 2. Hash every wheel and check it against the lock ---------------------
        hashes_by_pin = {(p.key, p.version): set(p.hashes) for p in resolved.pins}
        artifacts, bad = [], []
        for wheel in sorted(resolved.wheels_dir.glob("*.whl")):
            digest = sha256_file(wheel)
            key = parse_dist_filename(wheel.name)
            if key not in hashes_by_pin or digest not in hashes_by_pin[key]:
                bad.append(wheel.name)
            artifacts.append({"release_id": release_id, "package": key[0] if key else wheel.name,
                              "version": key[1] if key else "", "filename": wheel.name, "sha256": digest})
        if bad:
            return finish(reg.FAILED, f"wheel hash not in the lock file: {bad}")

        # 3. Scan and apply the security gate -----------------------------------
        registry.update_release(release_id, status=reg.SCANNING)
        scans, violations = scanner.scan_release(resolved.wheels_dir)
        scanned_at = utcnow()
        status_by_file = {s.filename: s.status for s in scans}
        for row in artifacts:
            row["scan_status"] = status_by_file.get(row["filename"], "NOT_SCANNED")
            row["scanned_at"] = scanned_at
        registry.add_artifacts(artifacts)
        finding_rows = []
        for scan in scans:
            for f in scan.findings:
                finding_rows.append(_finding_row(release_id, scan.package, scan.version, scan.filename,
                                                 f, cfg.policy.severity_source, "build"))
        for f in violations or []:
            finding_rows.append(_finding_row(release_id, f.component, f.component_version, "", f,
                                             cfg.policy.severity_source, "build"))
        if finding_rows:
            registry.add_findings(finding_rows)
        decision = gate.evaluate(resolved.pins, scans, violations, cfg.policy)
        registry.update_release(release_id, gate_status=decision.status)

        # 4. Test (only if the gate passed) -------------------------------------
        report = None
        if decision.status == gate.PASS:
            registry.update_release(release_id, status=reg.TESTING)
            report = run_test(resolved.lock_path, resolved.wheels_dir, target,
                              skip_imports=channel.skip_imports, extra_imports=channel.extra_imports,
                              smoke_tests=channel.smoke_tests)
            registry.update_release(release_id, test_passed=report.passed)

        # 5. Write the release files and copy the candidate to staging ----------
        diff = diff_locks(parse_lock(previous_lock), resolved.pins) if previous_lock else None
        manifest = {
            "release_id": release_id, "channel": channel.name,
            "runtime_profile": channel.runtime_profile, "created_at": utcnow().isoformat(),
            "previous_release_id": previous["release_id"] if previous else None,
            "upgrade_packages": upgrade_packages,
            "packages": [{"name": p.name, "version": p.version} for p in resolved.pins],
            "artifacts": [{k: a[k] for k in ("filename", "sha256", "scan_status")} for a in artifacts],
            "changes_from_previous": diff.to_dict() if diff else None,
            "blocked_versions": summary["blocked"],
            "gate": decision.to_dict(),
            "resolve_attempts": resolved.attempts,
        }
        (work / "install.txt").write_text(render_install_txt(release_path))
        (work / "manifest.json").write_text(json.dumps(manifest, indent=2))
        (work / "security-findings.json").write_text(json.dumps({
            "scanned_at": scanned_at.isoformat(), "severity_source": cfg.policy.severity_source,
            "artifacts": [s.to_dict() for s in scans],
            "violations": None if violations is None else [f.to_dict() for f in violations],
            "gate": decision.to_dict(),
        }, indent=2))
        (work / "test-results.json").write_text(json.dumps(report.to_dict() if report else
                                                           {"passed": False, "skipped": "gate did not pass"}, indent=2))
        staging.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(work, staging, dirs_exist_ok=True)
        lock_sha = sha256_file(staging / "requirements.lock")
        summary.update(staging_path=str(staging), gate=decision.status, packages=len(resolved.pins),
                       changes=diff.to_dict() if diff else None)

        # 6. Final status --------------------------------------------------------
        if decision.status == gate.FAIL:
            return finish(reg.QUARANTINED, "; ".join(decision.reasons), lock_sha256=lock_sha)
        if decision.status == gate.HOLD:
            return finish(reg.QUARANTINED, "HOLD: " + "; ".join(decision.reasons), lock_sha256=lock_sha)
        if not report.passed:
            failed = {**report.failed_imports, **report.failed_smoke}
            reason = f"tests failed: {json.dumps(failed)[:1500]}" if report.installed else \
                f"offline install failed: {report.install_log[-1500:]}"
            return finish(reg.FAILED, reason, lock_sha256=lock_sha)
        unscanned = [a["filename"] for a in artifacts if a["scan_status"] != SCANNED]
        assert not unscanned, "the gate must hold unscanned wheels"
        return finish(reg.READY_FOR_APPROVAL, "", lock_sha256=lock_sha)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _finding_row(release_id, package, version, filename, finding, severity_source, source) -> dict:
    return {
        "release_id": release_id, "package": package, "version": version, "filename": filename,
        "issue_id": finding.issue_id, "cves": ",".join(finding.cves), "severity": finding.severity,
        "severity_source": severity_source, "fixed_versions": ",".join(finding.fixed_versions),
        "component": f"{finding.component}:{finding.component_version}".strip(":"),
        "is_violation": finding.is_violation, "summary": finding.summary, "source": source,
    }
