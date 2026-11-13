"""Unit tests for the pure logic: lock files, block detection, scan parsing, gate, references."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import make_wheel
from libdist import gate
from libdist import registry as reg
from libdist.discover import library_specs, pip_install_lines
from libdist.health import failure_rate_sql, judge
from libdist.lockfile import Pin, diff_locks, parse_dist_filename, parse_lock, render_install_txt
from libdist.references import repoint_libraries
from libdist.resolve import find_blocked
from libdist.scan import SCANNED, ArtifactScan, Finding, JfScanner, parse_simple_json
from libdist.settings import Config, Policy
from libdist.tester import top_level_modules
from libdist.util import CommandResult, new_release_id, redact
from libdist.waiver import waiver_body

REPO = Path(__file__).resolve().parents[1]
H1, H2 = "a" * 64, "b" * 64

LOCK = f"""\
# comment
certifi==2026.1.4 \\
    --hash=sha256:{H1} \\
    --hash=sha256:{H2}
    # via requests
requests[socks]==2.32.3 --hash=sha256:{H1}
typing_extensions==4.12.2 ; python_version < "3.13" \\
    --hash=sha256:{H2}
"""


# ---- lock files -------------------------------------------------------------
def test_parse_lock_reads_pins_and_hashes():
    pins = parse_lock(LOCK)
    assert [(p.name, p.version) for p in pins] == [
        ("certifi", "2026.1.4"), ("requests", "2.32.3"), ("typing_extensions", "4.12.2")]
    assert pins[0].hashes == (H1, H2)
    assert pins[2].key == "typing-extensions" and pins[2].hashes == (H2,)


def test_diff_locks_reports_only_real_changes():
    old = [Pin("certifi", "1"), Pin("idna", "3"), Pin("gone", "1")]
    new = [Pin("certifi", "2"), Pin("idna", "3"), Pin("fresh", "1")]
    diff = diff_locks(old, new).to_dict()
    assert diff == {"added": ["fresh==1"], "removed": ["gone==1"],
                    "changed": [{"package": "certifi", "from": "1", "to": "2"}]}


@pytest.mark.parametrize("filename,expected", [
    ("certifi-2026.10.19-py3-none-any.whl", ("certifi", "2026.10.19")),
    ("typing_extensions-4.12.2-py3-none-any.whl", ("typing-extensions", "4.12.2")),
    ("numpy-2.1.0-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl", ("numpy", "2.1.0")),
    ("https://x/api/pypi/r/packages/ab/cd/certifi-2026.10.19-py3-none-any.whl#sha256=ff", ("certifi", "2026.10.19")),
    ("certifi-2026.10.19-py3-none-any.whl.metadata", ("certifi", "2026.10.19")),
    ("some_pkg-1.0.tar.gz", ("some-pkg", "1.0")),
    ("index.html", None),
])
def test_parse_dist_filename(filename, expected):
    assert parse_dist_filename(filename) == expected


def test_install_txt_points_only_at_its_release():
    text = render_install_txt("/Volumes/c/s/wheelhouse/releases/ds-20261112-1405/")
    assert text.splitlines() == [
        "--no-index",
        "--find-links /Volumes/c/s/wheelhouse/releases/ds-20261112-1405/wheels",
        "--only-binary=:all:",
        "--require-hashes",
        "-r /Volumes/c/s/wheelhouse/releases/ds-20261112-1405/requirements.lock",
    ]


# ---- 403 detection ----------------------------------------------------------
def test_find_blocked_in_pip_output():
    out = ("ERROR: HTTP error 403 while getting https://jf.example/artifactory/api/pypi/pypi/packages/"
           "packages/aa/bb/certifi-2026.10.19-py3-none-any.whl (from https://jf.example/artifactory/api/pypi/pypi/simple/certifi/)\n"
           "ERROR: 403 Client Error: Forbidden for url: https://jf.example/x/certifi-2026.10.19-py3-none-any.whl")
    blocked = find_blocked(out)
    assert [(b.name, b.version) for b in blocked] == [("certifi", "2026.10.19")]
    assert blocked[0].exclusion == "certifi!=2026.10.19"


def test_find_blocked_in_uv_output_and_whole_package():
    out = ("error: Failed to fetch: `https://jf.example/api/pypi/pypi/packages/idna-3.11-py3-none-any.whl.metadata`\n"
           "  Caused by: HTTP status client error (403 Forbidden) for url (https://jf.example/api/pypi/pypi/packages/idna-3.11-py3-none-any.whl.metadata)\n"
           "  Caused by: HTTP status client error (403 Forbidden) for url (https://jf.example/api/pypi/pypi/simple/leftpad/)")
    blocked = {(b.name, b.version) for b in find_blocked(out)}
    assert blocked == {("idna", "3.11"), ("leftpad", None)}


def test_find_blocked_ignores_other_errors():
    assert find_blocked("ERROR: HTTP error 404 while getting https://x/certifi-1-py3-none-any.whl") == []


# ---- scan output ------------------------------------------------------------
SIMPLE_JSON = json.dumps({
    "vulnerabilities": [{
        "severity": "critical", "impactedPackageName": "pypi://certifi", "impactedPackageVersion": "2025.1.1",
        "fixedVersions": ["[2026.10.19]"], "cves": [{"id": "CVE-2026-0001"}], "issueId": "XRAY-1",
        "summary": "bad cert bundle"}],
    "securityViolations": [{"severity": "High", "impactedPackageName": "idna", "impactedPackageVersion": "3.0",
                            "cves": [], "issueId": "XRAY-2"}],
})


def test_parse_simple_json_normalises_findings():
    findings = parse_simple_json("12:00 [Info] scanning...\n" + SIMPLE_JSON + "\n")
    assert [(f.issue_id, f.severity, f.component, f.is_violation) for f in findings] == [
        ("XRAY-1", "Critical", "certifi", False), ("XRAY-2", "High", "idna", True)]
    assert findings[0].cves == ["CVE-2026-0001"]


def test_parse_simple_json_empty_and_invalid():
    assert parse_simple_json('{"vulnerabilities": null}') == []
    assert parse_simple_json("no json here") is None
    assert parse_simple_json("[1, 2]") is None


def test_jf_scanner_marks_failures(tmp_path):
    wheel = make_wheel(tmp_path, "alpha", "1.0")
    calls = []

    def runner(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd[1] == "config":
            return CommandResult(0, "", "")
        return CommandResult(1, "", "boom") if "--watches=w1" not in cmd else CommandResult(0, SIMPLE_JSON, "")

    scanner = JfScanner("jf", tmp_path / "home", watches=["w1"], runner=runner)
    scanner.configure("https://jf.example", "s3cret")
    assert calls[0][1]["input_text"] == "s3cret" and "s3cret" not in " ".join(calls[0][0])
    scans, violations = scanner.scan_release(tmp_path)
    assert scans[0].status == "FAILED" and "exit code 1" in scans[0].error
    assert [v.issue_id for v in violations] == ["XRAY-2"]
    assert str(wheel) in calls[1][0]


# ---- gate -------------------------------------------------------------------
def _scan(name, version, findings=(), status=SCANNED):
    return ArtifactScan(f"{name}-{version}-py3-none-any.whl", name, version, "0" * 64, status, list(findings))


PINS = [Pin("alpha", "1.0"), Pin("beta", "1.1")]
POLICY = Policy(block_severities=["Critical", "High"])


def test_gate_passes_clean_release_with_full_coverage():
    decision = gate.evaluate(PINS, [_scan("alpha", "1.0"), _scan("beta", "1.1")], [], POLICY)
    assert decision.status == gate.PASS


def test_gate_low_finding_passes_and_is_recorded():
    low = Finding("X-9", "Low")
    decision = gate.evaluate(PINS, [_scan("alpha", "1.0", [low]), _scan("beta", "1.1")], [], POLICY)
    assert decision.status == gate.PASS and decision.package_severity == {"alpha==1.0": "Low"}


def test_gate_fails_on_blocked_severity_and_on_violation():
    high = Finding("X-1", "High")
    assert gate.evaluate(PINS, [_scan("alpha", "1.0", [high]), _scan("beta", "1.1")], [], POLICY).status == gate.FAIL
    violation = [Finding("X-2", "Medium", is_violation=True)]
    assert gate.evaluate(PINS, [_scan("alpha", "1.0"), _scan("beta", "1.1")], violation, POLICY).status == gate.FAIL


def test_gate_holds_when_a_wheel_was_not_scanned():
    decision = gate.evaluate(PINS, [_scan("alpha", "1.0")], [], POLICY)
    assert decision.status == gate.HOLD and "beta==1.1" in decision.reasons[0]
    failed = gate.evaluate(PINS, [_scan("alpha", "1.0"), _scan("beta", "1.1", status="FAILED")], [], POLICY)
    assert failed.status == gate.HOLD


def test_gate_holds_unknown_severity_and_failed_watch_scan():
    unknown = Finding("X-3", "Unknown")
    assert gate.evaluate(PINS, [_scan("alpha", "1.0", [unknown]), _scan("beta", "1.1")], [], POLICY).status == gate.HOLD
    assert gate.evaluate(PINS, [_scan("alpha", "1.0"), _scan("beta", "1.1")], None, POLICY).status == gate.HOLD


def test_gate_accepts_findings_covered_by_an_exception():
    high = Finding("X-1", "High")
    scans = [_scan("alpha", "1.0", [high]), _scan("beta", "1.1")]
    assert gate.evaluate(PINS, scans, [], POLICY, accepted_issue_ids={"X-1"}).status == gate.PASS


# ---- compute references -----------------------------------------------------
ROOT = "/Volumes/c/s/wheelhouse/releases"


def test_repoint_replaces_only_this_channels_entry():
    libs = [{"pypi": {"package": "x"}},
            {"requirements": f"{ROOT}/ds-20261101-0900/install.txt"},
            {"requirements": f"{ROOT}/ds-ml-20261101-0900/install.txt"},
            {"requirements": "/Volumes/other/requirements.txt"}]
    new, previous = repoint_libraries(libs, ROOT, "ds", "ds-20261112-1405")
    assert previous == "ds-20261101-0900"
    assert new[1] == {"requirements": f"{ROOT}/ds-20261112-1405/install.txt"}
    assert new[0] == libs[0] and new[2] == libs[2] and new[3] == libs[3] and len(new) == 4


def test_repoint_appends_when_the_policy_has_no_entry_yet():
    new, previous = repoint_libraries(None, ROOT, "ds", "ds-20261112-1405")
    assert previous is None and new == [{"requirements": f"{ROOT}/ds-20261112-1405/install.txt"}]


# ---- small helpers ----------------------------------------------------------
def test_redact_removes_credentials():
    text = "GET https://bob:tok3n@jf.example/simple/ with tok3n"
    assert redact(text, ["tok3n"]) == "GET https://***:***@jf.example/simple/ with ***"


def test_release_id_matches_the_reference_pattern():
    from datetime import datetime, timezone

    from libdist.references import release_pattern

    rid = new_release_id("ds-basics", datetime(2026, 11, 12, 14, 5, tzinfo=timezone.utc))
    assert rid == "ds-basics-20261112-1405"
    assert release_pattern(ROOT, "ds-basics").match(f"{ROOT}/{rid}/install.txt")
    assert not release_pattern(ROOT, "ds").match(f"{ROOT}/{rid}/install.txt")


def test_top_level_modules_with_and_without_top_level_txt(tmp_path):
    import zipfile

    wheel = make_wheel(tmp_path, "my-pkg", "1.0")
    assert top_level_modules(wheel) == ["my_pkg"]
    bare = tmp_path / "bare-1.0-py3-none-any.whl"
    with zipfile.ZipFile(bare, "w") as archive:
        archive.writestr("pkg_a/__init__.py", "")
        archive.writestr("pkg_a/sub/__init__.py", "")
        archive.writestr("single.py", "")
        archive.writestr("bare-1.0.dist-info/METADATA", "")
    assert top_level_modules(bare) == ["pkg_a", "single"]


def test_discover_helpers():
    source = "# Databricks notebook source\n# MAGIC %pip install requests>=2 certifi\n%pip install  pandas==2.1.4 \nprint('pip install x')\n"
    assert pip_install_lines(source) == ["requests>=2 certifi", "pandas==2.1.4"]
    libs = [{"pypi": {"package": "simplejson==3.19", "repo": "https://r"}}, {"whl": "/Volumes/a/b.whl"},
            {"requirements": "/Volumes/a/install.txt"}, {"maven": {"coordinates": "g:a:1"}}]
    assert library_specs(libs) == [("pypi", "simplejson==3.19 (repo: https://r)"), ("whl", "/Volumes/a/b.whl"),
                                   ("requirements", "/Volumes/a/install.txt"), ("maven", "g:a:1")]


def test_health_judgement():
    assert judge(5, 0, 100, 2).healthy is None
    assert judge(40, 1, 400, 8).healthy is True
    assert judge(40, 10, 400, 8).healthy is False
    sql = failure_rate_sql(["11", "12"])
    assert "job_id IN ('11', '12')" in sql and "system.lakeflow.job_run_timeline" in sql
    with pytest.raises(ValueError):
        failure_rate_sql(["1; DROP TABLE x"])


def test_waiver_body_matches_the_jfrog_api():
    assert waiver_body("certifi", "2026.10.19", "pypi-remote", "fixes CVE") == {
        "pkg_type": "pypi", "pkg_name": "certifi", "pkg_version": "2026.10.19",
        "repo_key": "pypi-remote", "reason": "fixes CVE"}


# ---- the files shipped in the repo -----------------------------------------
def test_repo_config_loads_and_sql_matches_the_code():
    cfg = Config(REPO / "config")
    assert cfg.channel_names() == ["ds-basics"]
    channel = cfg.channel("ds-basics")
    assert [w.name for w in channel.waves] == ["canary", "main"]
    assert cfg.settings.releases_root == "/Volumes/main/libs/wheelhouse/releases"
    assert cfg.policy.blocks("critical") and not cfg.policy.blocks("Low")
    shipped = (REPO / "sql" / "02_registry_tables.sql").read_text()
    assert reg.ddl("${catalog}", "${schema}") in shipped


def test_local_registry_rejects_unknown_columns(tmp_path):
    registry = reg.LocalRegistry(tmp_path / "r.json")
    with pytest.raises(KeyError):
        registry.create_release({"release_id": "x", "not_a_column": 1})
    registry.create_release({"release_id": "x", "channel": "c", "status": reg.DOWNLOADING})
    registry.update_release("x", status=reg.FAILED)
    assert reg.LocalRegistry(tmp_path / "r.json").get_release("x")["status"] == reg.FAILED
