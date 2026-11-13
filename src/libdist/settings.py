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

    def blocks(self, severity: str) -> bool:
        return canonical_severity(severity) in self.block_severities

    def grace_for(self, severity: str) -> int:
        return int(self.grace_hours.get(canonical_severity(severity), 0))


@dataclass
class Wave:
    name: str
    policy_ids: list[str] = field(default_factory=list)
    job_ids: list[str] = field(default_factory=list)  # used only for the health check


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
        smoke = [str(cdir / s) for s in raw.pop("smoke_tests", None) or []]
        return Channel(
            name=name,
            requirements_in=str(cdir / "channel.in"),
            constraints=str(cdir / "constraints.txt"),
            waves=waves,
            smoke_tests=smoke,
            **raw,
        )
