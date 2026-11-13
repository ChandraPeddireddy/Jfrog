"""Resolve a channel into a pinned, hashed lock file and download its wheels.

When the index refuses a version with HTTP 403 (JFrog Curation or Xray), the
version is excluded in constraints.txt and the resolution is tried again.
"""

from __future__ import annotations

import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .lockfile import Pin, normalize_name, parse_dist_filename, read_lock
from .settings import Channel, Settings
from .util import CommandResult, log, run

Runner = Callable[..., CommandResult]


@dataclass
class IndexAccess:
    """How uv and pip reach the package index. Credentials stay in ``env``."""

    env: dict[str, str] = field(default_factory=dict)
    uv_args: list[str] = field(default_factory=list)
    pip_args: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)

    @classmethod
    def for_settings(cls, settings: Settings, user: str = "", token: str = "") -> "IndexAccess":
        if settings.find_links:  # local test mode, no JFrog
            args = ["--no-index", "--find-links", settings.find_links]
            return cls(uv_args=list(args), pip_args=list(args))
        url = settings.jfrog.index_url(user, token)
        env = {
            "UV_DEFAULT_INDEX": url,
            "UV_INDEX_URL": url,  # older uv versions
            "PIP_INDEX_URL": url,
            "PIP_NO_INPUT": "1",
        }
        return cls(env=env, secrets=[token, url])


@dataclass(frozen=True)
class Blocked:
    name: str
    version: str | None  # None means the whole package was refused
    url: str
    reason: str = "unknown"  # curation | xray | unknown

    @property
    def exclusion(self) -> str | None:
        return f"{self.name}!={self.version}" if self.version else None


class ResolveError(RuntimeError):
    """The resolver or the download failed for a reason other than a block."""


class NoSafeVersion(RuntimeError):
    """Every usable version of at least one package is blocked."""

    def __init__(self, blocked: list[Blocked], detail: str):
        self.blocked = blocked
        self.detail = detail
        names = sorted({b.name for b in blocked})
        super().__init__(f"no installable version for: {', '.join(names) or 'unknown'}")


@dataclass
class ResolveResult:
    lock_path: Path
    wheels_dir: Path
    pins: list[Pin]
    blocked: list[Blocked]
    attempts: int


_URL = re.compile(r"https?://[^\s'\"<>)\]]+")


def find_blocked(output: str) -> list[Blocked]:
    """Find packages that the index refused with HTTP 403 in pip or uv output."""
    found: dict[tuple[str, str | None], Blocked] = {}
    for line in output.splitlines():
        if "403" not in line:
            continue
        urls = [u.rstrip(".,;:") for u in _URL.findall(line)]
        files = [(u, parse_dist_filename(u)) for u in urls]
        if any(parsed for _, parsed in files):
            # pip adds "(from <index page>)" after the refused file: ignore that page URL.
            for url, parsed in files:
                if parsed:
                    found.setdefault(parsed, Blocked(parsed[0], parsed[1], url))
            continue
        for url in urls:
            # A refused index page, e.g. .../simple/certifi/ : the whole package is blocked.
            match = re.search(r"/simple/([^/]+)/?$", url)
            if match:
                name = normalize_name(match.group(1))
                found.setdefault((name, None), Blocked(name, None, url))
    return list(found.values())


def _tail(text: str, lines: int = 40) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def prune_wheels(wheels_dir: Path, pins: list[Pin]) -> list[str]:
    """Delete downloaded files that are not in the final lock (from earlier attempts)."""
    wanted = {(p.key, p.version) for p in pins}
    removed = []
    for path in sorted(wheels_dir.iterdir()):
        parsed = parse_dist_filename(path.name)
        if not parsed or parsed not in wanted:
            path.unlink()
            removed.append(path.name)
    return removed


def resolve(
    channel: Channel,
    workdir: str | Path,
    access: IndexAccess,
    settings: Settings,
    previous_lock: str | None = None,
    upgrade_packages: list[str] | None = None,
    full_upgrade: bool = False,
    runner: Runner = run,
    classify: Callable[[Blocked], str] | None = None,
    base_env: dict[str, str] | None = None,
) -> ResolveResult:
    """Produce ``requirements.lock`` and ``wheels/`` in ``workdir``.

    ``previous_lock`` is the text of the release currently in use. uv keeps its
    pins, so only new libraries and ``upgrade_packages`` change. Pass
    ``full_upgrade=True`` for a routine refresh of every version.
    """
    import os

    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    wheels = workdir / "wheels"
    wheels.mkdir(exist_ok=True)
    lock = workdir / "requirements.lock"
    constraints = workdir / "constraints.txt"
    shutil.copyfile(channel.requirements_in, workdir / "requirements.in")
    base_constraints = Path(channel.constraints).read_text() if Path(channel.constraints).exists() else ""
    constraints.write_text(base_constraints)

    if previous_lock and not full_upgrade:
        lock.write_text(previous_lock)
    elif lock.exists():
        lock.unlink()

    env = dict(base_env if base_env is not None else os.environ)
    env.update(access.env)

    compile_cmd = [
        settings.uv_path, "pip", "compile", str(workdir / "requirements.in"),
        "-c", str(constraints),
        "--generate-hashes", "--only-binary", ":all:", "--no-header",
        "--python", sys.executable,
        "-o", str(lock),
        *access.uv_args,
    ]
    for pkg in upgrade_packages or []:
        compile_cmd += ["--upgrade-package", pkg]

    download_cmd = [
        sys.executable, "-m", "pip", "download",
        "-r", str(lock), "-d", str(wheels),
        "--require-hashes", "--only-binary=:all:", "--no-deps",
        "--disable-pip-version-check",
        *access.pip_args,
    ]

    blocked: list[Blocked] = []
    seen: set[tuple[str, str | None]] = set()

    def record(new: list[Blocked], detail: str) -> None:
        """Add exclusions for newly blocked versions, or stop if nothing can be excluded."""
        progress = False
        for item in new:
            if (item.name, item.version) in seen:
                continue
            seen.add((item.name, item.version))
            if classify:
                item = Blocked(item.name, item.version, item.url, classify(item))
            blocked.append(item)
            if item.exclusion:
                with open(constraints, "a") as handle:
                    handle.write(f"\n{item.exclusion}  # blocked by index ({item.reason})")
                log.warning("blocked by index (%s): %s", item.reason, item.exclusion)
                progress = True
        if not progress:
            raise NoSafeVersion(blocked, detail)

    for attempt in range(1, settings.max_resolve_retries + 1):
        result = runner(compile_cmd, env=env, secrets=access.secrets)
        if result.returncode != 0:
            new = find_blocked(result.output)
            if new:
                record(new, _tail(result.output))
                continue
            if blocked:
                # Nothing is refused any more, but the exclusions left no solution.
                raise NoSafeVersion(blocked, _tail(result.output))
            raise ResolveError(f"uv pip compile failed:\n{_tail(result.output)}")

        result = runner(download_cmd, env=env, secrets=access.secrets)
        if result.returncode != 0:
            new = find_blocked(result.output)
            if new:
                record(new, _tail(result.output))
                continue
            raise ResolveError(f"pip download failed:\n{_tail(result.output)}")

        pins = read_lock(lock)
        if not pins:
            raise ResolveError("the lock file is empty; check channel.in")
        missing_hash = [p.name for p in pins if not p.hashes]
        if missing_hash:
            raise ResolveError(f"pins without a sha256 hash: {missing_hash}")
        prune_wheels(wheels, pins)
        return ResolveResult(lock, wheels, pins, blocked, attempt)

    raise NoSafeVersion(blocked, f"retry limit of {settings.max_resolve_retries} reached")


def make_block_classifier(user: str, token: str, timeout: int = 30) -> Callable[[Blocked], str]:
    """Ask the index why a URL was refused, and label it curation, xray or unknown.

    JFrog puts the reason in the 403 response body. The exact wording differs
    between versions, so this is a best-effort label (POC test P4).
    """
    import requests

    def classify(item: Blocked) -> str:
        try:
            response = requests.get(item.url, auth=(user, token), timeout=timeout, stream=True)
            body = response.text[:4000].lower() if response.status_code == 403 else ""
        except Exception as exc:  # network errors must not stop the build
            log.warning("could not classify block for %s: %s", item.name, exc)
            return "unknown"
        if "curation" in body:
            return "curation"
        if "xray" in body:
            return "xray"
        return "unknown"

    return classify
