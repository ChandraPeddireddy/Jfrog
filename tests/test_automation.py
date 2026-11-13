"""Tests for the phase 2 and 3 features: zero-touch promotion, waivers, serverless,
pip redirect, clean-up, import from another metastore and the migration report."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from conftest import FakePolicies, FakeScanner, make_wheel
from libdist import registry as reg
from libdist.build import build_release
from libdist.cleanup import cleanup, plan
from libdist.health import HealthResult
from libdist.migrate import classify, migration_report, requirements_in
from libdist.orchestrate import advance, start_rollout
from libdist.promote import PromoteError, switch
from libdist.publish import PublishError, publish
from libdist.reassess import reassess
from libdist.references import (
    release_in_environment_yaml,
    render_environment_yaml,
    with_pip_redirect,
)
from libdist.resolve import Blocked, IndexAccess, NoSafeVersion
from libdist.scan import Finding
from libdist.settings import Config, Policy
from libdist.transfer import import_release
from libdist.util import utcnow


class FakeEnvironments:
    """Stands in for BaseEnvironmentClient."""

    def __init__(self, filepaths: dict[str, str], fail: bool = False):
        self.filepaths, self.fail, self.refreshed = filepaths, fail, []

    def get(self, name):
        return {"name": name, "filepath": self.filepaths[name]}

    def refresh(self, name):
        if self.fail:
            raise RuntimeError("environment build failed")
        self.refreshed.append(name)


def set_channel(cfg: Config, **updates) -> None:
    path = cfg.dir / "channels" / "demo" / "channel.yml"
    data = yaml.safe_load(path.read_text())
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key].update(value)
        else:
            data[key] = value
    path.write_text(yaml.safe_dump(data))


def healthy(*_):
    return HealthResult(True, 40, 1, 400, 8, "ok")


def unhealthy(*_):
    return HealthResult(False, 40, 20, 400, 8, "failure rate 50%")


def too_few(*_):
    return HealthResult(None, 3, 0, 400, 8, "only 3 canary runs")


@pytest.fixture
def registry(tmp_path):
    return reg.LocalRegistry(tmp_path / "registry.json", actor="test")


@pytest.fixture
def policies():
    return FakePolicies({p: {"policy_id": p, "name": p, "definition": "{}"} for p in ("P1", "P2", "P3")})


def _build(cfg, registry, release_id, scanner=None, **kwargs):
    return build_release(cfg, "demo", registry, IndexAccess.for_settings(cfg.settings),
                         scanner or FakeScanner(), release_id=release_id, **kwargs)


def _publish(cfg, registry, release_id, **kwargs):
    assert _build(cfg, registry, release_id, **kwargs)["status"] == reg.READY_FOR_APPROVAL
    registry.update_release(release_id, status=reg.APPROVED, approved_by="sec@example.com", approved_at=utcnow())
    publish(cfg, registry, release_id)


def _lib(policies, policy_id):
    libs = policies.policies[policy_id].get("libraries") or []
    return libs[-1]["requirements"].split("/")[-2] if libs else None


# ---- zero-touch promotion ---------------------------------------------------
def test_manual_mode_never_approves_and_auto_off_never_switches(cfg, registry, policies):
    _build(cfg, registry, "demo-20261101-0900")
    assert advance(cfg, registry, policies)["demo"]["approved"] == []
    assert registry.get_release("demo-20261101-0900")["status"] == reg.READY_FOR_APPROVAL

    registry.update_release("demo-20261101-0900", status=reg.APPROVED, approved_by="sec", approved_at=utcnow())
    assert advance(cfg, registry, policies)["demo"]["published"] == ["demo-20261101-0900"]
    assert registry.list_rollouts() == [] and policies.edits == []  # rollout.auto is false


def test_automatic_flow_from_candidate_to_all_waves(cfg, registry, policies):
    cfg.policy.approval_mode = "automatic"
    set_channel(cfg, rollout={"auto": True, "soak_hours": 24})
    _build(cfg, registry, "demo-20261101-0900")
    t0 = utcnow()

    first = advance(cfg, registry, policies, health=healthy, now=t0)["demo"]
    assert first["approved"] == first["published"] == ["demo-20261101-0900"] and first["rollout_started"]
    assert registry.get_release("demo-20261101-0900")["approved_by"] == "policy:automatic"
    assert _lib(policies, "P1") == "demo-20261101-0900" and _lib(policies, "P2") is None

    assert advance(cfg, registry, policies, health=healthy, now=t0 + timedelta(hours=2))["demo"]["rollout"] == "SOAKING"
    assert _lib(policies, "P2") is None
    assert advance(cfg, registry, policies, health=healthy, now=t0 + timedelta(hours=25))["demo"]["rollout"] == "SWITCHED:main"
    assert _lib(policies, "P2") == _lib(policies, "P3") == "demo-20261101-0900"
    # the main wave has no job_ids: it completes after its own soak time
    assert advance(cfg, registry, policies, health=healthy, now=t0 + timedelta(hours=26))["demo"]["rollout"] == "SOAKING"
    assert advance(cfg, registry, policies, health=healthy, now=t0 + timedelta(hours=50))["demo"]["rollout"] == "COMPLETED"
    assert registry.list_rollouts("demo")[0]["status"] == "COMPLETED"
    assert "rollout" not in advance(cfg, registry, policies, health=healthy, now=t0 + timedelta(hours=51))["demo"]


def test_unhealthy_canary_is_rolled_back_at_once_and_not_retried(cfg, registry, policies, index):
    set_channel(cfg, rollout={"auto": True})
    _publish(cfg, registry, "demo-20261101-0900")
    t0 = utcnow()
    advance(cfg, registry, policies, health=healthy, now=t0)
    for hours in (25, 50):
        advance(cfg, registry, policies, health=healthy, now=t0 + timedelta(hours=hours))
    make_wheel(index, "beta", "1.2")
    _publish(cfg, registry, "demo-20261108-0900", upgrade_packages=["beta"])
    t1 = t0 + timedelta(days=7)
    advance(cfg, registry, policies, health=healthy, now=t1)
    assert _lib(policies, "P1") == "demo-20261108-0900"

    result = advance(cfg, registry, policies, health=unhealthy, now=t1 + timedelta(minutes=30))["demo"]
    assert result["rollout"] == "ROLLED_BACK" and _lib(policies, "P1") == "demo-20261101-0900"
    assert _lib(policies, "P2") == "demo-20261101-0900"
    later = advance(cfg, registry, policies, health=healthy, now=t1 + timedelta(hours=5))["demo"]
    assert "rollout_started" not in later and _lib(policies, "P1") == "demo-20261101-0900"


def test_security_rebuild_uses_the_short_soak_time(cfg, registry, policies, index):
    set_channel(cfg, rollout={"auto": True, "soak_hours": 24, "urgent_soak_hours": 2})
    make_wheel(index, "beta", "1.2")
    _publish(cfg, registry, "demo-20261101-0900", upgrade_packages=["beta"])
    t0 = utcnow()
    advance(cfg, registry, policies, health=healthy, now=t0)
    assert advance(cfg, registry, policies, health=healthy, now=t0 + timedelta(hours=3))["demo"]["rollout"] == "SWITCHED:main"


def test_too_few_canary_runs_holds_or_proceeds_as_configured(cfg, registry, policies):
    set_channel(cfg, rollout={"auto": True, "soak_hours": 1, "max_wait_hours": 10})
    _publish(cfg, registry, "demo-20261101-0900")
    t0 = utcnow()
    advance(cfg, registry, policies, health=too_few, now=t0)
    assert advance(cfg, registry, policies, health=too_few, now=t0 + timedelta(hours=2))["demo"]["rollout"] == "WAITING_FOR_RUNS"
    assert advance(cfg, registry, policies, health=too_few, now=t0 + timedelta(hours=11))["demo"]["rollout"] == "HELD"
    assert advance(cfg, registry, policies, health=too_few, now=t0 + timedelta(hours=12))["demo"]["rollout"] == "HELD"
    assert [e["action"] for e in registry.list_events()].count("ROLLOUT_WAITING") == 1
    set_channel(cfg, rollout={"on_insufficient_data": "proceed"})
    assert advance(cfg, registry, policies, health=too_few, now=t0 + timedelta(hours=13))["demo"]["rollout"] == "SWITCHED:main"


def test_security_rebuilds_mode_approves_only_rebuilds(cfg, registry, policies, index):
    cfg.policy.approval_mode = "security_rebuilds"
    _build(cfg, registry, "demo-20261101-0900")
    assert advance(cfg, registry, policies)["demo"]["approved"] == []
    registry.update_release("demo-20261101-0900", status=reg.APPROVED, approved_by="sec", approved_at=utcnow())
    advance(cfg, registry, policies)
    make_wheel(index, "beta", "1.2")
    _build(cfg, registry, "demo-20261108-0900", upgrade_packages=["beta"])
    assert advance(cfg, registry, policies)["demo"]["approved"] == ["demo-20261108-0900"]


def test_rollout_is_rolled_back_when_its_release_is_revoked(cfg, registry, policies, index):
    set_channel(cfg, rollout={"auto": True})
    _publish(cfg, registry, "demo-20261101-0900")
    t0 = utcnow()
    for hours in (0, 25, 50):
        advance(cfg, registry, policies, health=healthy, now=t0 + timedelta(hours=hours))
    make_wheel(index, "beta", "1.2")
    _publish(cfg, registry, "demo-20261108-0900", upgrade_packages=["beta"])
    advance(cfg, registry, policies, health=healthy, now=t0 + timedelta(days=7))
    registry.update_release("demo-20261108-0900", status=reg.REVOKED)
    result = advance(cfg, registry, policies, health=healthy, now=t0 + timedelta(days=7, hours=1))["demo"]
    assert result["rollout"] == "ROLLED_BACK" and _lib(policies, "P1") == "demo-20261101-0900"


def test_only_one_rollout_at_a_time(cfg, registry, policies):
    _publish(cfg, registry, "demo-20261101-0900")
    start_rollout(cfg, registry, policies, None, "demo-20261101-0900")
    with pytest.raises(PromoteError, match="already has a rollout"):
        start_rollout(cfg, registry, policies, None, "demo-20261101-0900")


# ---- serverless and pip redirect -------------------------------------------
def test_environment_yaml_round_trip():
    root = "/Volumes/c/s/wheelhouse/releases"
    text = render_environment_yaml(f"{root}/ds-20261112-1405", "4")
    parsed = yaml.safe_load(text)
    assert parsed["environment_version"] == "4"
    assert parsed["dependencies"] == ["--no-index", f"--find-links {root}/ds-20261112-1405/wheels",
                                      "--only-binary=:all:", "--require-hashes",
                                      f"-r {root}/ds-20261112-1405/requirements.lock"]
    assert release_in_environment_yaml(text, root, "ds") == "ds-20261112-1405"
    assert release_in_environment_yaml(text, root, "other") is None


def test_serverless_switch_rewrites_the_yaml_and_refreshes(cfg, registry, policies):
    name = "workspace-base-environments/demo-canary"
    yaml_path = Path(cfg.settings.environments_root) / "demo-canary.yml"
    set_channel(cfg, waves=[{"name": "canary", "policy_ids": [], "base_environments": [name], "job_ids": []}])
    _publish(cfg, registry, "demo-20261101-0900")
    envs = FakeEnvironments({name: str(yaml_path)})
    with pytest.raises(PromoteError, match="no client"):
        switch(cfg, registry, policies, "demo-20261101-0900", "canary")
    switch(cfg, registry, policies, "demo-20261101-0900", "canary", environments=envs)
    assert envs.refreshed == [name]
    assert release_in_environment_yaml(yaml_path.read_text(), cfg.settings.releases_root, "demo") == "demo-20261101-0900"
    ref = registry.list_references("demo")[0]
    assert (ref["reference_type"], ref["reference_id"]) == ("base_environment", name)


def test_serverless_switch_restores_the_yaml_when_the_refresh_fails(cfg, registry, policies, index):
    name = "workspace-base-environments/demo-canary"
    yaml_path = Path(cfg.settings.environments_root) / "demo-canary.yml"
    set_channel(cfg, waves=[{"name": "canary", "policy_ids": [], "base_environments": [name], "job_ids": []}])
    _publish(cfg, registry, "demo-20261101-0900")
    switch(cfg, registry, policies, "demo-20261101-0900", "canary", environments=FakeEnvironments({name: str(yaml_path)}))
    before = yaml_path.read_text()
    make_wheel(index, "beta", "1.2")
    _publish(cfg, registry, "demo-20261108-0900", upgrade_packages=["beta"])
    with pytest.raises(PromoteError, match="YAML was restored"):
        switch(cfg, registry, policies, "demo-20261108-0900", "canary",
               environments=FakeEnvironments({name: str(yaml_path)}, fail=True))
    assert yaml_path.read_text() == before
    assert registry.list_references("demo")[0]["release_id"] == "demo-20261101-0900"


def test_serverless_switch_refuses_a_yaml_outside_the_wheelhouse(cfg, registry, policies, tmp_path):
    name = "workspace-base-environments/elsewhere"
    set_channel(cfg, waves=[{"name": "canary", "policy_ids": [], "base_environments": [name], "job_ids": []}])
    _publish(cfg, registry, "demo-20261101-0900")
    with pytest.raises(PromoteError, match="is not under"):
        switch(cfg, registry, policies, "demo-20261101-0900", "canary",
               environments=FakeEnvironments({name: str(tmp_path / "elsewhere.yml")}))


def test_pip_redirect_sets_the_environment_variables(cfg, registry, policies):
    set_channel(cfg, pip_redirect=True)
    _publish(cfg, registry, "demo-20261101-0900")
    switch(cfg, registry, policies, "demo-20261101-0900", "canary")
    applied = json.loads(policies.policies["P1"]["definition"])
    assert applied["spark_env_vars.PIP_FIND_LINKS"]["value"] == cfg.settings.release_path("demo-20261101-0900") + "/wheels"
    wheels = "/Volumes/c/s/wheelhouse/releases/demo-20261101-0900/wheels"
    plain = with_pip_redirect({"policy_id": "P", "definition": '{"spark_version": {"type": "fixed", "value": "x"}}'}, wheels)
    rules = json.loads(plain["definition"])
    assert rules["spark_env_vars.PIP_NO_INDEX"] == {"type": "fixed", "value": "1"}
    assert rules["spark_env_vars.PIP_FIND_LINKS"] == {"type": "fixed", "value": wheels}
    assert rules["spark_version"]["value"] == "x"
    family = with_pip_redirect({"policy_id": "P", "policy_family_id": "job-cluster"}, wheels)
    assert "definition" not in family
    assert json.loads(family["policy_family_definition_overrides"])["spark_env_vars.PIP_NO_INDEX"]["value"] == "1"


# ---- waiver automation ------------------------------------------------------
def test_waiver_is_requested_once_for_the_newest_curation_blocked_fix(cfg, registry, monkeypatch):
    from libdist import build as build_module

    cfg.policy.waiver_mode = "request"
    blocked = [Blocked("beta", "1.2", "u", "curation"), Blocked("beta", "1.10", "u", "curation"),
               Blocked("beta", "1.1", "u", "xray"), Blocked("alpha", "9.9", "u", "curation")]

    def no_safe_version(*args, **kwargs):
        raise NoSafeVersion(blocked, "detail")

    monkeypatch.setattr(build_module, "resolve", no_safe_version)
    calls = []

    def waiver(package, version, reason):
        calls.append((package, version))
        return {"id": 42}

    first = _build(cfg, registry, "demo-20261101-0900", upgrade_packages=["beta"], request_waiver=waiver)
    assert first["status"] == reg.FAILED and first["waivers"] == [{"package": "beta", "version": "1.10"}]
    second = _build(cfg, registry, "demo-20261101-1300", upgrade_packages=["beta"], request_waiver=waiver)
    assert second["waivers"] == [] and calls == [("beta", "1.10")]
    pending = [e for e in registry.list_exceptions() if e["kind"] == "WAIVER"]
    assert len(pending) == 1 and pending[0]["status"] == "OPEN"

    cfg.policy.waiver_mode = "off"
    third = _build(cfg, registry, "demo-20261101-1700", upgrade_packages=["beta"], request_waiver=waiver)
    assert "waivers" not in third
    # not a security rebuild: nothing is requested
    cfg.policy.waiver_mode = "request"
    registry.data["dependency_exceptions"].clear()
    assert _build(cfg, registry, "demo-20261101-2100", request_waiver=waiver)["waivers"] == []


def test_policy_reads_unquoted_yaml_off_and_rejects_bad_values():
    assert Policy(waiver_mode=False).waiver_mode == "off"
    with pytest.raises(ValueError):
        Policy(approval_mode="sometimes")


# ---- reassess: retries and revoke ------------------------------------------
def test_reassess_retries_the_replacement_build_until_one_is_on_the_way(cfg, registry, policies, index):
    _publish(cfg, registry, "demo-20261101-0900")
    switch(cfg, registry, policies, "demo-20261101-0900", "canary")
    scanner = FakeScanner({"beta-1.1-py3-none-any.whl": [Finding("XRAY-9", "High")]})
    now, triggered = utcnow(), []

    def trigger(channel, packages):
        triggered.append((channel, tuple(packages)))

    reassess(cfg, registry, scanner, now=now, trigger_rebuild=trigger)
    reassess(cfg, registry, scanner, now=now + timedelta(hours=4), trigger_rebuild=trigger)
    assert triggered == [("demo", ("beta",)), ("demo", ("beta",))]  # tried again: no replacement yet

    make_wheel(index, "beta", "1.2")
    assert _build(cfg, registry, "demo-20261102-0900", upgrade_packages=["beta"])["status"] == reg.READY_FOR_APPROVAL
    reassess(cfg, registry, scanner, now=now + timedelta(hours=8), trigger_rebuild=trigger)
    assert len(triggered) == 2  # a candidate is waiting for approval: no new build


def test_grace_expiry_revokes_only_when_policy_says_so(cfg, registry, policies):
    _publish(cfg, registry, "demo-20261101-0900")
    switch(cfg, registry, policies, "demo-20261101-0900", "canary")
    scanner = FakeScanner({"beta-1.1-py3-none-any.whl": [Finding("XRAY-9", "Critical")]})
    now = utcnow()
    reassess(cfg, registry, scanner, now=now)
    cfg.policy.on_grace_expiry = "revoke"
    summary = reassess(cfg, registry, scanner, now=now + timedelta(hours=25))
    assert summary["revoked"] == ["demo-20261101-0900"]
    assert registry.get_release("demo-20261101-0900")["status"] == reg.REVOKED


def test_finished_rollout_closes_the_old_releases_grace_period(cfg, registry, policies, index):
    set_channel(cfg, rollout={"auto": True, "urgent_soak_hours": 1},
                waves=[{"name": "canary", "policy_ids": ["P1"], "job_ids": []}])
    _publish(cfg, registry, "demo-20261101-0900")
    t0 = utcnow()
    advance(cfg, registry, policies, now=t0)
    advance(cfg, registry, policies, now=t0 + timedelta(hours=25))
    reassess(cfg, registry, FakeScanner({"beta-1.1-py3-none-any.whl": [Finding("XRAY-9", "High")]}), now=t0 + timedelta(hours=26))
    assert registry.list_exceptions(status="OPEN")
    make_wheel(index, "beta", "1.2")
    _publish(cfg, registry, "demo-20261103-0900", upgrade_packages=["beta"])
    advance(cfg, registry, policies, now=t0 + timedelta(hours=30))
    advance(cfg, registry, policies, now=t0 + timedelta(hours=32))
    assert registry.list_rollouts("demo")[0]["status"] == "COMPLETED"
    assert registry.list_exceptions(status="OPEN") == []
    assert registry.list_exceptions()[0]["status"] == "CLOSED"


def test_a_finding_accepted_by_security_does_not_block_the_next_build(cfg, registry):
    scanner = FakeScanner({"beta-1.1-py3-none-any.whl": [Finding("XRAY-9", "High")]})
    assert _build(cfg, registry, "demo-20261101-0900", scanner)["status"] == reg.QUARANTINED
    registry.add_exception({"channel": "demo", "package": "beta", "issue_id": "XRAY-9", "severity": "High",
                            "kind": "RISK_ACCEPTED", "status": "APPROVED", "approver": "sec@example.com",
                            "deadline_at": utcnow() + timedelta(days=30), "rationale": "no fix exists"})
    assert _build(cfg, registry, "demo-20261101-1300", scanner)["status"] == reg.READY_FOR_APPROVAL


# ---- clean-up ---------------------------------------------------------------
def test_cleanup_deletes_only_unused_releases_past_retention(cfg, registry, policies, index):
    for day, version in (("01", None), ("08", "1.2"), ("15", "1.3")):
        if version:
            make_wheel(index, "beta", version)
        _publish(cfg, registry, f"demo-202611{day}-0900", upgrade_packages=["beta"] if version else None)
        switch(cfg, registry, policies, f"demo-202611{day}-0900", "canary")
    _build(cfg, registry, "demo-20261116-0900", FakeScanner(fail={"alpha-1.0-py3-none-any.whl"}))
    now = utcnow()
    # 01 was left two switches ago; 08 is the rollback target; 15 is in use
    assert plan(cfg, registry, now + timedelta(days=1))["wheelhouse"] == []  # inside retention
    later = now + timedelta(days=40)
    report = cleanup(cfg, registry, apply=False, now=later)
    assert [w["release_id"] for w in report["wheelhouse"]] == ["demo-20261101-0900"]
    assert [s["release_id"] for s in report["staging"]] == [
        "demo-20261116-0900", "demo-20261115-0900", "demo-20261108-0900", "demo-20261101-0900"]
    assert Path(cfg.settings.release_path("demo-20261101-0900")).exists()  # report only

    cleanup(cfg, registry, apply=True, now=later)
    assert not Path(cfg.settings.release_path("demo-20261101-0900")).exists()
    assert Path(cfg.settings.release_path("demo-20261108-0900")).exists()
    assert Path(cfg.settings.release_path("demo-20261115-0900")).exists()
    assert registry.get_release("demo-20261101-0900")["status"] == reg.RETIRED


# ---- import from another metastore -----------------------------------------
def test_import_release_verifies_and_registers(cfg, registry, tmp_path):
    _publish(cfg, registry, "demo-20261101-0900")
    source = Path(cfg.settings.release_path("demo-20261101-0900"))
    other = Config(cfg.dir)
    other.settings.catalog = "othercat"
    other_registry = reg.LocalRegistry(tmp_path / "other.json")
    result = import_release(other, other_registry, str(source))
    target = Path(result["release_path"])
    assert "othercat" in str(target) and (target / "COMPLETE").exists()
    assert f"--find-links {target}/wheels" in (target / "install.txt").read_text()
    row = other_registry.get_release("demo-20261101-0900")
    assert row["status"] == reg.PUBLISHED and row["approved_by"].startswith("import:")
    with pytest.raises(PublishError, match="already registered"):
        import_release(other, other_registry, str(source))


def test_import_release_rejects_a_tampered_source(cfg, registry, tmp_path):
    _publish(cfg, registry, "demo-20261101-0900")
    source = Path(cfg.settings.release_path("demo-20261101-0900"))
    next((source / "wheels").glob("beta*")).write_bytes(b"changed")
    other = Config(cfg.dir)
    other.settings.catalog = "othercat"
    other_registry = reg.LocalRegistry(tmp_path / "other.json")
    with pytest.raises(PublishError, match="failed verification"):
        import_release(other, other_registry, str(source))
    assert other_registry.get_release("demo-20261101-0900") is None


# ---- migration report -------------------------------------------------------
def test_migration_classification():
    pins = {"requests": "2.32.3", "typing-extensions": "4.12.2"}
    assert classify("requests", pins) == ("SATISFIED", "2.32.3")
    assert classify("requests>=2,<3", pins) == ("SATISFIED", "2.32.3")
    assert classify("Typing_Extensions", pins) == ("SATISFIED", "4.12.2")
    assert classify("requests==2.31.0", pins) == ("VERSION_CONFLICT", "2.32.3")
    assert classify("pandas", pins) == ("NOT_IN_RELEASE", "")
    assert classify("/Volumes/a/b.whl", pins) == ("UNPARSED", "")
    assert requirements_in("pip_install", "-U requests>=2 'pandas==2.1.4' -r /x/req.txt --index-url https://x") == [
        "requests>=2", "pandas==2.1.4"]
    assert requirements_in("pypi", "simplejson==3.19 (repo: https://r)") == ["simplejson==3.19"]
    assert requirements_in("whl", "/Volumes/a.whl") == []


def test_migration_report_is_kept_per_workspace(cfg, registry):
    _publish(cfg, registry, "demo-20261101-0900")

    def row(host, kind, spec):
        return {"snapshot_at": utcnow(), "workspace_host": host, "object_type": "notebook", "object_id": spec,
                "object_name": "nb", "owner": "", "policy_id": "", "library_kind": kind, "library_spec": spec}

    registry.replace_consumers("https://a", [row("https://a", "pip_install", "alpha beta==0.9 gamma")])
    registry.replace_consumers("https://b", [row("https://b", "pypi", "alpha>=1")])
    registry.replace_consumers("https://a", [row("https://a", "pip_install", "alpha beta==0.9 gamma")])
    assert len(registry.list_consumers()) == 2  # workspace b was not wiped by a's snapshot
    report = migration_report(registry, "demo", "demo-20261101-0900", cfg.settings.release_path("demo-20261101-0900"))
    assert report["counts"] == {"SATISFIED": 2, "VERSION_CONFLICT": 1, "NOT_IN_RELEASE": 1, "UNPARSED": 0}
    assert report["not_in_release"] == ["gamma"]
    assert len(registry._select("dependency_migration")) == 4


# ---- several workspaces on one metastore -----------------------------------
def test_sites_keep_their_own_references_and_rollouts(cfg, tmp_path, policies):
    hub = reg.LocalRegistry(tmp_path / "shared.json")
    _publish(cfg, hub, "demo-20261101-0900")
    switch(cfg, hub, policies, "demo-20261101-0900", "canary")

    spoke = reg.LocalRegistry(tmp_path / "shared.json")  # same tables, another workspace
    spoke.site = "ws2"
    assert spoke.list_references("demo") == [] and spoke.list_rollouts() == []
    spoke_policies = FakePolicies({p: {"policy_id": p, "name": p, "definition": "{}"} for p in ("P1", "P2", "P3")})
    cfg.settings.role = "spoke"
    set_channel(cfg, rollout={"auto": True})
    result = advance(cfg, spoke, spoke_policies)["demo"]
    assert "approved" not in result and result["rollout_started"]  # a spoke never approves or publishes
    assert _lib(spoke_policies, "P1") == "demo-20261101-0900"

    merged = reg.LocalRegistry(tmp_path / "shared.json")
    assert {r["site"] for r in merged.list_references(all_sites=True)} == {"default", "ws2"}
    assert merged.releases_in_use() == ["demo-20261101-0900"]
    assert len(merged.list_rollouts()) == 0 and len(spoke.list_rollouts()) == 1


def test_copy_tree_copies_contents_only(tmp_path):
    from libdist.util import copy_tree

    (tmp_path / "a" / "wheels").mkdir(parents=True)
    (tmp_path / "a" / "wheels" / "x.whl").write_bytes(b"1")
    (tmp_path / "a" / "lock").write_text("l")
    copy_tree(tmp_path / "a", tmp_path / "b")
    assert (tmp_path / "b" / "wheels" / "x.whl").read_bytes() == b"1" and (tmp_path / "b" / "lock").read_text() == "l"
