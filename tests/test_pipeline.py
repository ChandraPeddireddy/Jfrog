"""End-to-end tests in local mode.

These run the real resolver (uv), the real downloader and installer (pip) and
the real file copies, against a folder of hand-made wheels. JFrog, Xray and the
Databricks compute policy API are replaced by fakes from conftest.py.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from conftest import FakePolicies, FakeScanner, make_wheel
from libdist import registry as reg
from libdist.build import build_release
from libdist.cli import main
from libdist.lockfile import read_lock
from libdist.promote import PromoteError, eligibility_problems, revoke, rollback, switch
from libdist.publish import PublishError, publish, verify_release
from libdist.reassess import reassess
from libdist.resolve import IndexAccess, NoSafeVersion, resolve
from libdist.scan import Finding
from libdist.tester import run_test
from libdist.util import CommandResult, run, utcnow


def _build(cfg, registry, scanner=None, release_id=None, **kwargs):
    access = IndexAccess.for_settings(cfg.settings)
    return build_release(cfg, "demo", registry, access, scanner or FakeScanner(),
                         release_id=release_id, **kwargs)


def _approve(registry, release_id):
    registry.update_release(release_id, status=reg.APPROVED, approved_by="sec@example.com",
                            approved_at=utcnow())


def _published(cfg, registry, release_id, **kwargs):
    summary = _build(cfg, registry, release_id=release_id, **kwargs)
    assert summary["status"] == reg.READY_FOR_APPROVAL, summary
    _approve(registry, release_id)
    publish(cfg, registry, release_id)
    return summary


@pytest.fixture
def registry(tmp_path):
    return reg.LocalRegistry(tmp_path / "registry.json", actor="test")


@pytest.fixture
def policies():
    return FakePolicies({
        "P1": {"policy_id": "P1", "name": "canary", "definition": "{}", "libraries": [{"pypi": {"package": "keep"}}]},
        "P2": {"policy_id": "P2", "name": "main-a", "definition": "{}"},
        "P3": {"policy_id": "P3", "name": "main-b", "definition": "{}"},
    })


# ---- resolve ----------------------------------------------------------------
def test_resolve_pins_hidden_dependencies_and_downloads_wheels(cfg, tmp_path):
    result = resolve(cfg.channel("demo"), tmp_path / "work", IndexAccess.for_settings(cfg.settings), cfg.settings)
    assert {(p.name, p.version) for p in result.pins} == {("alpha", "1.0"), ("beta", "1.1")}
    assert all(p.hashes for p in result.pins)
    assert sorted(w.name for w in result.wheels_dir.iterdir()) == [
        "alpha-1.0-py3-none-any.whl", "beta-1.1-py3-none-any.whl"]


def test_resolve_excludes_a_blocked_version_and_retries(cfg, tmp_path):
    """Simulate JFrog answering 403 for beta 1.1: the pipeline must fall back to beta 1.0."""
    seen = []

    def runner(cmd, **kwargs):
        seen.append(cmd[:3])
        if "download" in cmd and "beta==1.1" in (tmp_path / "work" / "requirements.lock").read_text():
            return CommandResult(1, "", "ERROR: HTTP error 403 while getting "
                                        "https://jf.example/api/pypi/r/packages/beta-1.1-py3-none-any.whl")
        return run(cmd, **kwargs)

    result = resolve(cfg.channel("demo"), tmp_path / "work", IndexAccess.for_settings(cfg.settings),
                     cfg.settings, runner=runner, classify=lambda b: "curation")
    assert {(p.name, p.version) for p in result.pins} == {("alpha", "1.0"), ("beta", "1.0")}
    assert [(b.name, b.version, b.reason) for b in result.blocked] == [("beta", "1.1", "curation")]
    assert result.attempts == 2
    assert "beta!=1.1" in (tmp_path / "work" / "constraints.txt").read_text()
    assert [w.name for w in result.wheels_dir.glob("beta*")] == ["beta-1.0-py3-none-any.whl"]


def test_resolve_reports_no_safe_version_when_every_version_is_blocked(cfg, tmp_path):
    def runner(cmd, **kwargs):
        if "download" in cmd:
            lock = (tmp_path / "work" / "requirements.lock").read_text()
            version = "1.1" if "beta==1.1" in lock else "1.0"
            return CommandResult(1, "", f"ERROR: HTTP error 403 while getting https://jf/beta-{version}-py3-none-any.whl")
        return run(cmd, **kwargs)

    with pytest.raises(NoSafeVersion) as info:
        resolve(cfg.channel("demo"), tmp_path / "work", IndexAccess.for_settings(cfg.settings),
                cfg.settings, runner=runner)
    assert {(b.name, b.version) for b in info.value.blocked} == {("beta", "1.1"), ("beta", "1.0")}


def test_security_rebuild_changes_only_the_named_package(cfg, tmp_path, index):
    access = IndexAccess.for_settings(cfg.settings)
    first = resolve(cfg.channel("demo"), tmp_path / "w1", access, cfg.settings)
    make_wheel(index, "alpha", "1.1", ["beta>=1"])
    make_wheel(index, "beta", "1.2")
    kept = resolve(cfg.channel("demo"), tmp_path / "w2", access, cfg.settings,
                   previous_lock=first.lock_path.read_text())
    assert {(p.name, p.version) for p in kept.pins} == {("alpha", "1.0"), ("beta", "1.1")}
    upgraded = resolve(cfg.channel("demo"), tmp_path / "w3", access, cfg.settings,
                       previous_lock=first.lock_path.read_text(), upgrade_packages=["beta"])
    assert {(p.name, p.version) for p in upgraded.pins} == {("alpha", "1.0"), ("beta", "1.2")}
    full = resolve(cfg.channel("demo"), tmp_path / "w4", access, cfg.settings,
                   previous_lock=first.lock_path.read_text(), full_upgrade=True)
    assert {(p.name, p.version) for p in full.pins} == {("alpha", "1.1"), ("beta", "1.2")}


# ---- offline install test ---------------------------------------------------
def test_offline_install_and_import(cfg, tmp_path):
    result = resolve(cfg.channel("demo"), tmp_path / "work", IndexAccess.for_settings(cfg.settings), cfg.settings)
    report = run_test(result.lock_path, result.wheels_dir, tmp_path / "target")
    assert report.passed and report.imports == {"alpha": "ok", "beta": "ok"}


def test_offline_install_fails_when_a_wheel_is_modified(cfg, tmp_path):
    result = resolve(cfg.channel("demo"), tmp_path / "work", IndexAccess.for_settings(cfg.settings), cfg.settings)
    wheel = next(result.wheels_dir.glob("beta*"))
    wheel.write_bytes(wheel.read_bytes() + b"tampered")
    report = run_test(result.lock_path, result.wheels_dir, tmp_path / "target")
    assert not report.installed and not report.passed


def test_import_failure_is_reported(cfg, tmp_path, index):
    make_wheel(index, "beta", "1.3", body="raise RuntimeError('native library missing')\n")
    result = resolve(cfg.channel("demo"), tmp_path / "work", IndexAccess.for_settings(cfg.settings), cfg.settings)
    report = run_test(result.lock_path, result.wheels_dir, tmp_path / "target")
    assert report.installed and not report.passed
    assert "native library missing" in report.failed_imports["beta"]


# ---- build ------------------------------------------------------------------
def test_build_produces_a_complete_candidate(cfg, registry):
    summary = _build(cfg, registry, release_id="demo-20261101-0900")
    assert summary["status"] == reg.READY_FOR_APPROVAL
    staging = Path(summary["staging_path"])
    for name in ["requirements.lock", "install.txt", "manifest.json", "security-findings.json",
                 "test-results.json", "requirements.in", "constraints.txt"]:
        assert (staging / name).exists(), name
    assert len(list((staging / "wheels").glob("*.whl"))) == 2
    row = registry.get_release("demo-20261101-0900")
    assert row["gate_status"] == "PASS" and row["test_passed"] is True and row["lock_sha256"]
    assert len(registry._select("dependency_artifacts")) == 2
    assert [e["action"] for e in registry.list_events("demo-20261101-0900")] == [
        "BUILD_STARTED", "BUILD_READY_FOR_APPROVAL"]


def test_build_quarantines_a_blocking_finding(cfg, registry):
    scanner = FakeScanner({"beta-1.1-py3-none-any.whl": [Finding("XRAY-7", "Critical", cves=["CVE-1"])]})
    summary = _build(cfg, registry, scanner, release_id="demo-20261101-0900")
    assert summary["status"] == reg.QUARANTINED and "XRAY-7" in summary["reason"]
    finding = registry.list_findings("demo-20261101-0900")[0]
    assert (finding["package"], finding["severity"], finding["source"]) == ("beta", "Critical", "build")


def test_build_holds_when_a_scan_fails_and_never_passes_it(cfg, registry):
    summary = _build(cfg, registry, FakeScanner(fail={"alpha-1.0-py3-none-any.whl"}), release_id="demo-20261101-0900")
    assert summary["status"] == reg.QUARANTINED and summary["reason"].startswith("HOLD")
    with pytest.raises(PublishError):
        publish(cfg, registry, "demo-20261101-0900")


def test_build_records_no_safe_version(cfg, registry, monkeypatch):
    from libdist import build as build_module
    from libdist.resolve import Blocked

    def blocked(*args, **kwargs):
        raise NoSafeVersion([Blocked("beta", "1.1", "https://jf/beta", "curation")], "detail")

    monkeypatch.setattr(build_module, "resolve", blocked)
    summary = _build(cfg, registry, release_id="demo-20261101-0900")
    assert summary["status"] == reg.FAILED and summary["reason"].startswith("NO_SAFE_VERSION")
    assert "NO_SAFE_VERSION" in [e["action"] for e in registry.list_events("demo-20261101-0900")]


# ---- publish ----------------------------------------------------------------
def test_publish_requires_approval_and_verifies_the_copy(cfg, registry):
    _build(cfg, registry, release_id="demo-20261101-0900")
    with pytest.raises(PublishError, match="only APPROVED"):
        publish(cfg, registry, "demo-20261101-0900")
    _approve(registry, "demo-20261101-0900")
    result = publish(cfg, registry, "demo-20261101-0900")
    release = Path(result["release_path"])
    assert (release / "COMPLETE").exists() and verify_release(release) == []
    assert (release / "install.txt").read_text().splitlines()[1] == f"--find-links {release}/wheels"
    assert registry.get_release("demo-20261101-0900")["status"] == reg.PUBLISHED
    with pytest.raises(PublishError):  # releases are never overwritten
        registry.update_release("demo-20261101-0900", status=reg.APPROVED)
        publish(cfg, registry, "demo-20261101-0900")


def test_publish_refuses_a_candidate_changed_after_the_build(cfg, registry):
    summary = _build(cfg, registry, release_id="demo-20261101-0900")
    _approve(registry, "demo-20261101-0900")
    lock = Path(summary["staging_path"]) / "requirements.lock"
    lock.write_text(lock.read_text() + "\n# edited\n")
    with pytest.raises(PublishError, match="changed after the build"):
        publish(cfg, registry, "demo-20261101-0900")


def test_verify_release_detects_tampering(cfg, registry):
    _published(cfg, registry, "demo-20261101-0900")
    release = Path(cfg.settings.release_path("demo-20261101-0900"))
    wheel = next((release / "wheels").glob("beta*"))
    wheel.write_bytes(b"changed")
    assert any("sha256 does not match" in p for p in verify_release(release))


def test_published_release_installs_from_its_own_install_txt(cfg, registry, tmp_path):
    """What compute does: pip install -r <release>/install.txt, with no index configured."""
    _published(cfg, registry, "demo-20261101-0900")
    install_txt = Path(cfg.settings.release_path("demo-20261101-0900")) / "install.txt"
    import sys

    result = run([sys.executable, "-m", "pip", "install", "-r", str(install_txt),
                  "--target", str(tmp_path / "site"), "--disable-pip-version-check"])
    assert result.returncode == 0, result.output
    assert (tmp_path / "site" / "alpha" / "__init__.py").exists()


# ---- switch, rollback, revoke ----------------------------------------------
def test_switch_rollback_and_eligibility(cfg, registry, policies, index):
    _published(cfg, registry, "demo-20261101-0900")
    root = cfg.settings.releases_root
    with pytest.raises(PromoteError, match="not eligible"):
        switch(cfg, registry, policies, "demo-unknown", "canary")

    switch(cfg, registry, policies, "demo-20261101-0900", "canary")
    assert policies.policies["P1"]["libraries"] == [
        {"pypi": {"package": "keep"}}, {"requirements": f"{root}/demo-20261101-0900/install.txt"}]
    assert "libraries" not in policies.policies["P2"]  # other waves untouched

    make_wheel(index, "beta", "1.2")
    second = _published(cfg, registry, "demo-20261108-0900", upgrade_packages=["beta"])
    assert second["changes"]["changed"] == [{"package": "beta", "from": "1.1", "to": "1.2"}]
    switch(cfg, registry, policies, "demo-20261108-0900", "canary")
    ref = registry.list_references("demo")[0]
    assert (ref["release_id"], ref["previous_release_id"]) == ("demo-20261108-0900", "demo-20261101-0900")

    rollback(cfg, registry, policies, "demo", "canary")
    assert policies.policies["P1"]["libraries"][1] == {"requirements": f"{root}/demo-20261101-0900/install.txt"}
    assert "ROLLBACK" in [e["action"] for e in registry.list_events()]


def test_rollback_is_refused_when_the_previous_release_is_not_eligible(cfg, registry, policies, index):
    _published(cfg, registry, "demo-20261101-0900")
    switch(cfg, registry, policies, "demo-20261101-0900", "canary")
    make_wheel(index, "beta", "1.2")
    _published(cfg, registry, "demo-20261108-0900", upgrade_packages=["beta"])
    switch(cfg, registry, policies, "demo-20261108-0900", "canary")

    result = revoke(registry, "demo-20261101-0900", "critical finding, grace period over")
    assert result["references_still_using"] == []
    with pytest.raises(PromoteError, match="rollback to demo-20261101-0900 refused"):
        rollback(cfg, registry, policies, "demo", "canary")
    assert policies.policies["P1"]["libraries"][1]["requirements"].endswith("demo-20261108-0900/install.txt")
    assert "ROLLBACK_REFUSED" in [e["action"] for e in registry.list_events()]


def test_switch_refuses_a_release_that_was_changed_in_the_wheelhouse(cfg, registry, policies):
    _published(cfg, registry, "demo-20261101-0900")
    release = Path(cfg.settings.release_path("demo-20261101-0900"))
    next((release / "wheels").glob("alpha*")).write_bytes(b"changed")
    with pytest.raises(PromoteError, match="failed verification"):
        switch(cfg, registry, policies, "demo-20261101-0900", "canary")
    assert policies.edits == []


# ---- reassess ---------------------------------------------------------------
def test_reassess_opens_a_grace_period_and_expires_it(cfg, registry, policies):
    _published(cfg, registry, "demo-20261101-0900")
    switch(cfg, registry, policies, "demo-20261101-0900", "canary")
    now = utcnow()

    assert reassess(cfg, registry, FakeScanner(), now=now)["releases"]["demo-20261101-0900"]["new_findings"] == 0

    finding = Finding("XRAY-9", "Critical", cves=["CVE-2026-9"], fixed_versions=["1.2"])
    scanner = FakeScanner({"beta-1.1-py3-none-any.whl": [finding]})
    rebuilds = []
    summary = reassess(cfg, registry, scanner, now=now, trigger_rebuild=lambda c, p: rebuilds.append((c, p)))
    assert summary["rebuilds"] == {"demo": ["beta"]} and rebuilds == [("demo", ["beta"])]
    exception = registry.list_exceptions(status="OPEN")[0]
    assert (exception["kind"], exception["severity"], exception["issue_id"]) == ("GRACE", "Critical", "XRAY-9")
    assert exception["deadline_at"] == (now + timedelta(hours=24)).isoformat()
    assert eligibility_problems(registry, "demo-20261101-0900", now) == []  # still inside the grace period

    again = reassess(cfg, registry, scanner, now=now + timedelta(hours=4))
    assert again["releases"]["demo-20261101-0900"]["new_findings"] == 0  # not recorded twice
    assert len(registry.list_exceptions()) == 1

    late = reassess(cfg, registry, scanner, now=now + timedelta(hours=25))
    assert late["expired"] == [exception["exception_id"]]
    assert registry.get_release("demo-20261101-0900")["status"] == reg.PUBLISHED  # never revoked automatically
    assert eligibility_problems(registry, "demo-20261101-0900", now + timedelta(hours=25)) != []
    assert "GRACE_EXPIRED" in [e["action"] for e in registry.list_events()]


def test_an_exception_extended_by_security_keeps_the_release_eligible_until_its_new_deadline(cfg, registry, policies):
    _published(cfg, registry, "demo-20261101-0900")
    switch(cfg, registry, policies, "demo-20261101-0900", "canary")
    now = utcnow()
    scanner = FakeScanner({"beta-1.1-py3-none-any.whl": [Finding("XRAY-9", "Critical")]})
    reassess(cfg, registry, scanner, now=now)
    exception = registry.list_exceptions()[0]
    registry.update_exception(exception["exception_id"], status="APPROVED", approver="sec@example.com",
                              deadline_at=now + timedelta(days=5))
    assert reassess(cfg, registry, scanner, now=now + timedelta(days=2))["expired"] == []
    assert eligibility_problems(registry, "demo-20261101-0900", now + timedelta(days=2)) == []
    assert reassess(cfg, registry, scanner, now=now + timedelta(days=6))["expired"] == [exception["exception_id"]]
    assert eligibility_problems(registry, "demo-20261101-0900", now + timedelta(days=6)) != []


def test_reassess_records_a_failed_rescan(cfg, registry, policies):
    _published(cfg, registry, "demo-20261101-0900")
    switch(cfg, registry, policies, "demo-20261101-0900", "canary")
    summary = reassess(cfg, registry, FakeScanner(fail={"beta-1.1-py3-none-any.whl"}))
    assert summary["releases"]["demo-20261101-0900"] == "RESCAN_FAILED"
    assert registry.list_exceptions() == []


# ---- command line, local mode -----------------------------------------------
def test_cli_local_dry_run(cfg, tmp_path, capsys):
    base = ["--config-dir", str(cfg.dir), "--local-registry", str(tmp_path / "reg.json"), "--local-test-scanner"]
    assert main(base + ["init-registry"]) == 0
    assert main(base + ["build", "--channel", "demo"]) == 0
    registry = reg.LocalRegistry(tmp_path / "reg.json")
    release_id = registry.list_releases("demo")[0]["release_id"]
    assert main(base + ["publish", "--release-id", release_id]) == 1  # not approved yet
    assert main(base + ["approve-local", "--release-id", release_id, "--approver", "sec@example.com"]) == 0
    assert main(base + ["publish", "--release-id", release_id]) == 0
    assert main(base + ["verify", "--release-id", release_id]) == 0
    assert main(base + ["status", "--channel", "demo"]) == 0
    assert '"status": "PUBLISHED"' in capsys.readouterr().out
    assert read_lock(Path(cfg.settings.release_path(release_id)) / "requirements.lock")
