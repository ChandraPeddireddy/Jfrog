"""Command line entry point. On Databricks each job task calls one command.

    python run.py --config-dir <dir> <command> [options]

Exit codes: 0 success, 1 error, 2 the release was not approved for use
(no safe version, quarantined, or tests failed).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from . import registry as reg
from .settings import Config
from .util import log, setup_logging


# ---------------------------------------------------------------------------
# Wiring: registry, credentials, scanner, workspace client
# ---------------------------------------------------------------------------
def _workspace_client():
    from databricks.sdk import WorkspaceClient

    return WorkspaceClient()


def _registry(args, cfg: Config) -> reg.Registry:
    if args.local_registry:
        return reg.LocalRegistry(args.local_registry)
    from pyspark.sql import SparkSession

    spark = SparkSession.builder.getOrCreate()
    return reg.SparkRegistry(spark, cfg.settings.catalog, cfg.settings.schema)


def _jfrog_credentials(cfg: Config) -> tuple[str, str]:
    """From the environment (local use) or the Databricks secret scope (jobs)."""
    user, token = os.environ.get("LIBDIST_JFROG_USER"), os.environ.get("LIBDIST_JFROG_TOKEN")
    if user and token:
        return user, token
    jf = cfg.settings.jfrog
    dbutils = _workspace_client().dbutils
    return (dbutils.secrets.get(jf.secret_scope, jf.secret_user_key),
            dbutils.secrets.get(jf.secret_scope, jf.secret_token_key))


class LocalTestScanner:
    """Marks every wheel as scanned with no findings. For local test mode only."""

    def scan_release(self, wheels_dir):
        from .lockfile import parse_dist_filename
        from .scan import SCANNED, ArtifactScan
        from .util import sha256_file

        scans = []
        for wheel in sorted(Path(wheels_dir).glob("*.whl")):
            name, version = parse_dist_filename(wheel.name)
            scans.append(ArtifactScan(wheel.name, name, version, sha256_file(wheel), SCANNED))
        return scans, []


def _scanner(args, cfg: Config):
    if args.local_test_scanner:
        if not cfg.settings.find_links:
            raise SystemExit("--local-test-scanner is only allowed when settings.find_links is set")
        log.warning("LOCAL TEST SCANNER: wheels are NOT scanned by Xray")
        return LocalTestScanner()
    from .scan import JfScanner

    _, token = _jfrog_credentials(cfg)
    jf = cfg.settings.jfrog
    home = tempfile.mkdtemp(prefix="jfrog-cli-")
    cli_path = jf.cli_path
    if os.path.isfile(cli_path) and not os.access(cli_path, os.X_OK):
        # A file on a Unity Catalog volume cannot be made executable in place: use a local copy.
        cli_path = shutil.copy(cli_path, os.path.join(home, "jf"))
        os.chmod(cli_path, 0o755)
    scanner = JfScanner(cli_path, home, jf.watches, jf.scan_threads, releases_repo=jf.releases_repo)
    scanner.configure(jf.platform_url, token)
    return scanner


def _ensure_uv(cfg: Config, env: dict[str, str]) -> None:
    """Use uv if it is on the cluster, else install it from the configured index."""
    settings = cfg.settings
    if shutil.which(settings.uv_path):
        return
    tools = Path(tempfile.gettempdir()) / "libdist-tools"
    spec = f"uv=={settings.uv_version}" if settings.uv_version else "uv"
    log.info("uv not found; installing %s into %s", spec, tools)
    subprocess.run([sys.executable, "-m", "pip", "install", "--target", str(tools), "--upgrade",
                    "--disable-pip-version-check", spec], check=True, env={**os.environ, **env})
    settings.uv_path = str(tools / "bin" / "uv")


def _print(data) -> None:
    print(json.dumps(data, indent=2, default=str))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_init_registry(args, cfg):
    _registry(args, cfg).init_tables()
    _print({"registry": f"{cfg.settings.catalog}.{cfg.settings.schema}", "tables": list(reg.TABLES)})
    return 0


def cmd_build(args, cfg):
    from .build import build_release
    from .resolve import IndexAccess, make_block_classifier

    registry = _registry(args, cfg)
    classify = None
    if cfg.settings.find_links:
        access = IndexAccess.for_settings(cfg.settings)
    else:
        user, token = _jfrog_credentials(cfg)
        access = IndexAccess.for_settings(cfg.settings, user, token)
        classify = make_block_classifier(user, token)
    _ensure_uv(cfg, access.env)
    upgrades = [p.strip() for p in (args.upgrade_package or "").split(",") if p.strip()]
    summary = build_release(cfg, args.channel, registry, access, _scanner(args, cfg),
                            upgrade_packages=upgrades, full_upgrade=args.full_upgrade, classify=classify)
    _print(summary)
    return 0 if summary["status"] == reg.READY_FOR_APPROVAL else 2


def cmd_publish(args, cfg):
    from .publish import publish

    _print(publish(cfg, _registry(args, cfg), args.release_id))
    return 0


def cmd_verify(args, cfg):
    from .publish import verify_release

    problems = verify_release(cfg.settings.release_path(args.release_id))
    _print({"release_id": args.release_id, "intact": not problems, "problems": problems})
    return 0 if not problems else 2


def _policies(args):
    from .references import PolicyClient

    return PolicyClient(_workspace_client())


def cmd_switch(args, cfg):
    from .promote import switch

    _print(switch(cfg, _registry(args, cfg), _policies(args), args.release_id, args.wave))
    return 0


def cmd_rollback(args, cfg):
    from .promote import rollback

    _print(rollback(cfg, _registry(args, cfg), _policies(args), args.channel, args.wave))
    return 0


def cmd_revoke(args, cfg):
    from .promote import revoke

    _print(revoke(_registry(args, cfg), args.release_id, args.reason))
    return 0


def cmd_reassess(args, cfg):
    from .reassess import reassess

    trigger = None
    if args.build_job_id:
        client = _workspace_client()

        def trigger(channel: str, packages: list[str]) -> None:
            log.info("starting replacement build for %s: %s", channel, packages)
            client.jobs.run_now(job_id=int(args.build_job_id),
                                job_parameters={"channel": channel, "upgrade_package": ",".join(packages)})

    summary = reassess(cfg, _registry(args, cfg), _scanner(args, cfg), trigger_rebuild=trigger)
    _print(summary)
    return 0


def cmd_health(args, cfg):
    from pyspark.sql import SparkSession

    from .health import check

    wave = cfg.channel(args.channel).wave(args.wave)
    result = check(SparkSession.builder.getOrCreate(), wave.job_ids,
                   datetime.fromisoformat(args.since), args.baseline_days, args.min_runs, args.max_increase)
    _print(result.__dict__)
    return 0 if result.healthy else 2


def cmd_waiver(args, cfg):
    from .waiver import request_waiver

    registry = _registry(args, cfg)
    _, token = _jfrog_credentials(cfg)
    jf = cfg.settings.jfrog
    response = request_waiver(jf.platform_url, token, args.package, args.version,
                              jf.curated_repo, args.reason)
    registry.add_event("WAIVER_REQUESTED", "", args.channel or "",
                       {"package": args.package, "version": args.version, "reason": args.reason,
                        "response": response})
    _print(response)
    return 0


def cmd_discover(args, cfg):
    from .discover import discover

    rows = discover(_workspace_client(), args.notebook_root, args.max_notebooks)
    _registry(args, cfg).replace_consumers(rows)
    _print({"consumer_rows": len(rows)})
    return 0


def cmd_status(args, cfg):
    registry = _registry(args, cfg)
    releases = registry.list_releases(args.channel)[: args.limit]
    keep = ("release_id", "channel", "status", "status_reason", "gate_status", "test_passed",
            "approved_by", "created_at")
    _print({
        "releases": [{k: r.get(k) for k in keep} for r in releases],
        "references": registry.list_references(args.channel),
        "open_exceptions": registry.list_exceptions(status="OPEN"),
    })
    return 0


def cmd_approve_local(args, cfg):
    """Approval for LOCAL TEST MODE only. On Databricks, Security runs sql/approve_release.sql."""
    if not args.local_registry:
        raise SystemExit("approve-local only works with --local-registry; see docs/RUNBOOK.md")
    registry = _registry(args, cfg)
    row = registry.get_release(args.release_id)
    if not row or row["status"] != reg.READY_FOR_APPROVAL:
        raise SystemExit(f"{args.release_id} is not READY_FOR_APPROVAL")
    from .util import utcnow

    registry.update_release(args.release_id, status=reg.APPROVED, approved_by=args.approver,
                            approved_at=utcnow())
    registry.add_event("APPROVED", args.release_id, row["channel"], {"approver": args.approver})
    _print({"release_id": args.release_id, "status": reg.APPROVED})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="libdist", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config-dir", default=os.environ.get("LIBDIST_CONFIG_DIR", "config"))
    parser.add_argument("--local-registry", help="JSON file used instead of Delta tables (tests, dry runs)")
    parser.add_argument("--local-test-scanner", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-registry", help="create the registry tables").set_defaults(func=cmd_init_registry)

    p = sub.add_parser("build", help="resolve, scan, test and stage a candidate release")
    p.add_argument("--channel", required=True)
    p.add_argument("--upgrade-package", default="", help="comma-separated packages to upgrade")
    p.add_argument("--full-upgrade", action="store_true", help="routine refresh of every version")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("publish", help="copy an APPROVED release to the wheelhouse")
    p.add_argument("--release-id", required=True)
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("verify", help="check a published release against its hashes")
    p.add_argument("--release-id", required=True)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("switch", help="point one wave of compute policies at a release")
    p.add_argument("--release-id", required=True)
    p.add_argument("--wave", required=True)
    p.set_defaults(func=cmd_switch)

    p = sub.add_parser("rollback", help="point a wave back at its previous release")
    p.add_argument("--channel", required=True)
    p.add_argument("--wave", required=True)
    p.set_defaults(func=cmd_rollback)

    p = sub.add_parser("revoke", help="mark a release REVOKED")
    p.add_argument("--release-id", required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_revoke)

    p = sub.add_parser("reassess", help="rescan releases in use and track grace deadlines")
    p.add_argument("--build-job-id", default="", help="start replacement builds with this job")
    p.set_defaults(func=cmd_reassess)

    p = sub.add_parser("health", help="canary failure rate against the baseline")
    p.add_argument("--channel", required=True)
    p.add_argument("--wave", required=True)
    p.add_argument("--since", required=True, help="time of the switch, ISO 8601")
    p.add_argument("--baseline-days", type=int, default=14)
    p.add_argument("--min-runs", type=int, default=20)
    p.add_argument("--max-increase", type=float, default=0.05)
    p.set_defaults(func=cmd_health)

    p = sub.add_parser("waiver", help="request a JFrog Curation waiver for a fix version")
    p.add_argument("--package", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--channel", default="")
    p.set_defaults(func=cmd_waiver)

    p = sub.add_parser("discover", help="snapshot library use in this workspace")
    p.add_argument("--notebook-root", default="", help="also scan notebooks under this path for %%pip lines")
    p.add_argument("--max-notebooks", type=int, default=2000)
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("status", help="recent releases, references and open exceptions")
    p.add_argument("--channel", default=None)
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("approve-local", help=argparse.SUPPRESS)
    p.add_argument("--release-id", required=True)
    p.add_argument("--approver", required=True)
    p.set_defaults(func=cmd_approve_local)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)
    cfg = Config(args.config_dir)
    try:
        return args.func(args, cfg)
    except SystemExit:
        raise
    except Exception as exc:
        log.exception("command %s failed", args.command)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
