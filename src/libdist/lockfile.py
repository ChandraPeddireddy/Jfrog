"""Read and compare pinned, hashed requirement files, and write install.txt."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


def normalize_name(name: str) -> str:
    """PEP 503 normalisation: lower case, runs of ``-_.`` become ``-``."""
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True)
class Pin:
    name: str
    version: str
    hashes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def key(self) -> str:
        return normalize_name(self.name)


_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==([^\s;\\]+)")
_HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})")


def parse_lock(text: str) -> list[Pin]:
    """Parse the output of ``uv pip compile --generate-hashes``.

    Each requirement is ``name==version`` followed by one or more
    ``--hash=sha256:...`` options, usually on continuation lines.
    """
    pins: list[Pin] = []
    current: tuple[str, str] | None = None
    hashes: list[str] = []

    def flush() -> None:
        nonlocal current, hashes
        if current:
            pins.append(Pin(current[0], current[1], tuple(hashes)))
        current, hashes = None, []

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _PIN.match(line)
        if match:
            flush()
            current = (match.group(1), match.group(2))
        hashes.extend(_HASH.findall(line))
    flush()
    return pins


def read_lock(path: str | Path) -> list[Pin]:
    return parse_lock(Path(path).read_text())


@dataclass
class LockDiff:
    added: list[Pin]
    removed: list[Pin]
    changed: list[tuple[Pin, Pin]]  # (old, new)

    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.changed)

    def to_dict(self) -> dict:
        return {
            "added": [f"{p.name}=={p.version}" for p in self.added],
            "removed": [f"{p.name}=={p.version}" for p in self.removed],
            "changed": [
                {"package": new.name, "from": old.version, "to": new.version}
                for old, new in self.changed
            ],
        }


def diff_locks(old: list[Pin], new: list[Pin]) -> LockDiff:
    old_by = {p.key: p for p in old}
    new_by = {p.key: p for p in new}
    added = [new_by[k] for k in sorted(new_by.keys() - old_by.keys())]
    removed = [old_by[k] for k in sorted(old_by.keys() - new_by.keys())]
    changed = [
        (old_by[k], new_by[k])
        for k in sorted(old_by.keys() & new_by.keys())
        if old_by[k].version != new_by[k].version
    ]
    return LockDiff(added, removed, changed)


_WHEEL = re.compile(r"^(?P<name>.+?)-(?P<version>[^-]+)(?:-\d[^-]*)?-[^-]+-[^-]+-[^-]+\.whl$")
_SDIST = re.compile(r"^(?P<name>.+)-(?P<version>[^-]+)\.(?:tar\.gz|zip)$")


def parse_dist_filename(filename: str) -> tuple[str, str] | None:
    """Return ``(name, version)`` from a wheel or sdist file name, else None."""
    base = filename.rsplit("/", 1)[-1].split("#", 1)[0].split("?", 1)[0]
    if base.endswith(".metadata"):
        base = base[: -len(".metadata")]
    match = _WHEEL.match(base) or _SDIST.match(base)
    if not match:
        return None
    return normalize_name(match.group("name")), match.group("version")


def render_install_txt(release_path: str) -> str:
    """The file a compute policy or base environment points at.

    It makes pip install only from this release, offline, with hash checking.
    """
    release_path = release_path.rstrip("/")
    return (
        "--no-index\n"
        f"--find-links {release_path}/wheels\n"
        "--only-binary=:all:\n"
        "--require-hashes\n"
        f"-r {release_path}/requirements.lock\n"
    )
