"""Publish an approved candidate: copy it to the wheelhouse and verify every hash."""

from __future__ import annotations

import shutil
from pathlib import Path

from . import registry as reg
from .lockfile import parse_dist_filename, read_lock, render_install_txt
from .settings import Config
from .util import log, sha256_file, utcnow

RELEASE_FILES = ["requirements.in", "requirements.lock", "constraints.txt", "install.txt",
                 "manifest.json", "security-findings.json", "test-results.json"]


class PublishError(RuntimeError):
    pass


def verify_release(path: str | Path, require_complete: bool = True) -> list[str]:
    """Return a list of problems with a release folder. An empty list means it is intact."""
    path = Path(path)
    problems: list[str] = []
    lock = path / "requirements.lock"
    if not lock.exists():
        return [f"{lock} is missing"]
    if require_complete and not (path / "COMPLETE").exists():
        problems.append("COMPLETE marker is missing")
    pins = read_lock(lock)
    hashes = {(p.key, p.version): set(p.hashes) for p in pins}
    seen: set[tuple[str, str]] = set()
    wheels = path / "wheels"
    for wheel in sorted(wheels.glob("*")) if wheels.exists() else []:
        key = parse_dist_filename(wheel.name)
        if key not in hashes:
            problems.append(f"{wheel.name}: not in requirements.lock")
        elif sha256_file(wheel) not in hashes[key]:
            problems.append(f"{wheel.name}: sha256 does not match requirements.lock")
        else:
            seen.add(key)
    for key in sorted(set(hashes) - seen):
        problems.append(f"{key[0]}=={key[1]}: no verified wheel in wheels/")
    install = path / "install.txt"
    if not install.exists():
        problems.append("install.txt is missing")
    elif install.read_text() != render_install_txt(str(path)):
        problems.append("install.txt does not point at this release folder")
    return problems


def publish(cfg: Config, registry: reg.Registry, release_id: str) -> dict:
    """Copy staging -> wheelhouse. Order: copy, verify hashes, COMPLETE, registry record last."""
    row = registry.get_release(release_id)
    if not row:
        raise PublishError(f"unknown release {release_id}")
    if row["status"] != reg.APPROVED:
        raise PublishError(f"release {release_id} has status {row['status']}; only APPROVED can be published")
    if not row.get("approved_by"):
        raise PublishError(f"release {release_id} has no approver recorded")
    if row.get("gate_status") != "PASS" or not row.get("test_passed"):
        raise PublishError(f"release {release_id} did not pass the gate and the tests")

    src, dst = Path(row["staging_path"]), Path(cfg.settings.release_path(release_id))
    if str(dst) != row["release_path"]:
        raise PublishError("release_path in the registry does not match the configured wheelhouse")
    if sha256_file(src / "requirements.lock") != row.get("lock_sha256"):
        raise PublishError("requirements.lock in staging changed after the build; rebuild the release")
    if (dst / "COMPLETE").exists():
        raise PublishError(f"{dst} is already published; releases are never overwritten")

    (dst / "wheels").mkdir(parents=True, exist_ok=True)
    for wheel in sorted((src / "wheels").glob("*.whl")):
        shutil.copyfile(wheel, dst / "wheels" / wheel.name)
    for name in RELEASE_FILES:
        if (src / name).exists():
            shutil.copyfile(src / name, dst / name)

    problems = verify_release(dst, require_complete=False)
    if problems:
        registry.add_event("PUBLISH_FAILED", release_id, row["channel"], {"problems": problems})
        raise PublishError(f"copy verification failed, release not registered: {problems}")

    (dst / "COMPLETE").write_text(f"{release_id} {utcnow().isoformat()}\n")
    registry.update_release(release_id, status=reg.PUBLISHED, published_at=utcnow())
    registry.add_event("PUBLISHED", release_id, row["channel"], {"release_path": str(dst)})
    log.info("published %s to %s", release_id, dst)
    return {"release_id": release_id, "release_path": str(dst), "install_txt": f"{dst}/install.txt"}
