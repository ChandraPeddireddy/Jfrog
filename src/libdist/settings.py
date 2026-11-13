"""Configuration: settings.yml, policy.yml and one channel.yml per channel."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SEVERITIES = ["Critical", "High", "Medium", "Low", "Unknown"]


def canonical_severity(value: str | None) -> str:
    if not value:
        return "Unknown"
    for sev in SEVERITIES:
        if sev.lower() == str(value).strip().lower():
            return sev
    return "Unknown"


@dataclass
class JFrogSettings:
    platform_url: str = ""
    pypi_repo: str = ""  # repository the pipeline resolves from (virtual or remote)
    curated_repo: str = ""  # curated remote repository key, used for waiver requests
    secret_scope: str = "libdist"
    secret_user_key: str = "jfrog-user"
    secret_token_key: str = "jfrog-token"
    cli_path: str = "jf"
    watches: list[str] = field(default_factory=list)
    scan_threads: int = 4
    # Artifactory repository that proxies releases.jfrog.io, so the CLI can fetch
    # its Xray indexer without internet access. Empty = download directly.
    releases_repo: str = ""

    def index_url(self, user: str, token: str) -> str:
        host = self.platform_url.rstrip("/").split("://", 1)[-1]
        return f"https://{user}:{token}@{host}/artifactory/api/pypi/{self.pypi_repo}/simple"


@dataclass
class Settings:
    catalog: str
    schema: str
    staging_volume: str = "staging"
    wheelhouse_volume: str = "wheelhouse"
    jfrog: JFrogSettings = field(default_factory=JFrogSettings)
    uv_path: str = "uv"
    uv_version: str = ""
    max_resolve_retries: int = 15
    # Local test mode: resolve from a folder of wheels instead of JFrog.
    find_links: str | None = None
    # One name per workspace that switches compute. Compute references and rollouts are kept per site.
    site: str = "default"
    # hub: this workspace builds, approves and publishes. spoke: it only rolls releases out to its own compute.
    role: str = "hub"
    # Root of Unity Catalog volume paths. Only changed in local tests.
    volumes_root: str = "/Volumes"

    @property
    def staging_root(self) -> str:
        return f"{self.volumes_root}/{self.catalog}/{self.schema}/{self.staging_volume}"

    @property
    def wheelhouse_root(self) -> str:
        return f"{self.volumes_root}/{self.catalog}/{self.schema}/{self.wheelhouse_volume}"

    @property
    def releases_root(self) -> str:
        return f"{self.wheelhouse_root}/releases"

    def staging_path(self, channel: str, release_id: str) -> str:
        return f"{self.staging_root}/{channel}/{release_id}"

    def release_path(self, release_id: str) -> str:
        return f"{self.releases_root}/{release_id}"

    @property
    def environments_root(self) -> str:
        """Serverless environment YAML files, written by the promoter."""
        return f"{self.wheelhouse_root}/environments"

    def table(self, name: str) -> str:
        return f"{self.catalog}.{self.schema}.{name}"


@dataclass
class Policy:
    """Security rules kept as code. Security owns this file."""

    block_severities: list[str] = field(default_factory=lambda: ["Critical", "High"])
    severity_source: str = "xray"
    hold_on_scan_failure: bool = True
    # Hours an approved release may stay in use after a new blocking finding.
    grace_hours: dict[str, int] = field(default_factory=dict)
    grace_values_approved: bool = False
    # manual: Security approves every release.
    # security_rebuilds: releases built with upgrade_package are approved by the rules; others manually.
    # automatic: every release that passes the gate and the tests is approved by the rules.
    approval_mode: str = "manual"
    # off: never ask JFrog for a waiver. request: ask for a Curation waiver when the fix is too new.
    waiver_mode: str = "off"
    # alert: only record that a grace deadline passed. revoke: also mark the release REVOKED.
    on_grace_expiry: str = "alert"
    retention_days: int = 7  # keep an unused release this long after compute left it
    staging_retention_days: int = 30

    def __post_init__(self) -> None:
        if self.waiver_mode is False:  # YAML reads an unquoted  off  as false
            self.waiver_mode = "off"
        allowed = {"approval_mode": ("manual", "security_rebuilds", "automatic"),
                   "waiver_mode": ("off", "request"), "on_grace_expiry": ("alert", "revoke")}
        for name, values in allowed.items():
            if getattr(self, name) not in values:
                raise ValueError(f"policy.yml: {name} must be one of {values}, not {getattr(self, name)!r}")

    def auto_approves(self, is_security_rebuild: bool) -> bool:
        return self.approval_mode == "automatic" or (
            self.approval_mode == "security_rebuilds" and is_security_rebuild)

    def blocks(self, severity: str) -> bool:
        return canonical_severity(severity) in self.block_severities

    def grace_for(self, severity: str) -> int:
        return int(self.grace_hours.get(canonical_severity(severity), 0))


@dataclass
class Wave:
    name: str
    policy_ids: list[str] = field(default_factory=list)  # classic compute policies
    base_environments: list[str] = field(default_factory=list)  # serverless: workspace-base-environments/<id>
    job_ids: list[str] = field(default_factory=list)  # used only for the health check


@dataclass
class Rollout:
    """How a release moves through the waves when the promote job drives it."""

    auto: bool = False  # false: Ops starts every wave by hand
    soak_hours: float = 24  # minimum time on a wave before the next one
    urgent_soak_hours: float = 2  # used for security rebuilds (built with upgrade_package)
    min_runs: int = 20  # canary runs needed before the failure rate is judged
    max_increase: float = 0.05  # allowed rise in failure rate over the baseline
    baseline_days: int = 14
    max_wait_hours: float = 72  # how long to wait for min_runs
    on_insufficient_data: str = "hold"  # hold | proceed, after max_wait_hours without min_runs

    def __post_init__(self) -> None:
        if self.on_insufficient_data not in ("hold", "proceed"):
            raise ValueError("channel.yml: rollout.on_insufficient_data must be hold or proceed")


@dataclass
class Channel:
    name: str
    runtime_profile: str
    requirements_in: str  # path to channel.in
    constraints: str  # path to constraints.txt
    owner: str = ""
    skip_imports: list[str] = field(default_factory=list)
    extra_imports: list[str] = field(default_factory=list)
    smoke_tests: list[str] = field(default_factory=list)  # python files run after imports
    waves: list[Wave] = field(default_factory=list)
    rollout: Rollout = field(default_factory=Rollout)
    # Also set PIP_NO_INDEX / PIP_FIND_LINKS on the compute policy, so existing
    # "%pip install" lines resolve from the release (POC test P11).
    pip_redirect: bool = False
    serverless_environment_version: str = "4"

    def wave(self, name: str) -> Wave:
        for wave in self.waves:
            if wave.name == name:
                return wave
        raise KeyError(f"channel {self.name} has no wave named {name!r}")


def _load(path: Path) -> dict[str, Any]:
    with open(path) as handle:
        return yaml.safe_load(handle) or {}


class Config:
    """Loads everything under the ``config/`` directory."""

    def __init__(self, config_dir: str | Path):
        self.dir = Path(config_dir)
        raw = _load(self.dir / "settings.yml")
        jfrog = JFrogSettings(**(raw.pop("jfrog", None) or {}))
        self.settings = Settings(jfrog=jfrog, **raw)
        self.policy = Policy(**_load(self.dir / "policy.yml"))
        self.policy.block_severities = [canonical_severity(s) for s in self.policy.block_severities]
        self.policy.grace_hours = {
            canonical_severity(k): int(v) for k, v in (self.policy.grace_hours or {}).items()
        }

    def channel_names(self) -> list[str]:
        root = self.dir / "channels"
        return sorted(p.name for p in root.iterdir() if (p / "channel.yml").exists())

    def channel(self, name: str) -> Channel:
        cdir = self.dir / "channels" / name
        if not (cdir / "channel.yml").exists():
            raise FileNotFoundError(f"no channel.yml for channel {name!r} in {cdir}")
        raw = _load(cdir / "channel.yml")
        waves = [Wave(**w) for w in raw.pop("waves", None) or []]
        for wave in waves:
            wave.policy_ids = [str(x) for x in wave.policy_ids]
            wave.job_ids = [str(x) for x in wave.job_ids]
            wave.base_environments = [str(x) for x in wave.base_environments]
        rollout = Rollout(**(raw.pop("rollout", None) or {}))
        raw["serverless_environment_version"] = str(raw.get("serverless_environment_version", "4"))
        smoke = [str(cdir / s) for s in raw.pop("smoke_tests", None) or []]
        return Channel(
            name=name,
            requirements_in=str(cdir / "channel.in"),
            constraints=str(cdir / "constraints.txt"),
            waves=waves,
            rollout=rollout,
            smoke_tests=smoke,
            **raw,
        )
