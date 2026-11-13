"""Bring a release that was published in another metastore or region into this wheelhouse.

Volumes belong to one metastore. To serve workspaces on another metastore, copy
the release folder to storage both sides can reach (for example an external
volume on shared cloud storage), then run ``import-release`` on the receiving side.

The import verifies every wheel against the lock file, writes a new install.txt
for the local path, and registers the release as PUBLISHED with its origin recorded.
It does not rescan: the local reassess job rescans it on its next run.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import registry as reg
from .lockfile import render_install_txt
from .publish import RELEASE_FILES, PublishError, verify_release
from .settings import Config
from .util import sha256_file, utcnow


def import_release(cfg: Config, registry: reg.Registry, source_path: str) -> dict:
    src = Path(source_path)
    manifest_file = src / "manifest.json"
    if not manifest_file.exists() or not (src / "COMPLETE").exists():
        raise PublishError(f"{src} is not a complete published release (manifest.json and COMPLETE are required)")
    manifest = json.loads(manifest_file.read_text())
    release_id, channel = manifest["release_id"], manifest["channel"]
    if channel not in cfg.channel_names():
        raise PublishError(f"channel {channel!r} is not configured here; add config/channels/{channel}/ first")
    local_channel = cfg.channel(channel)
    if local_channel.runtime_profile != manifest.get("runtime_profile"):
        raise PublishError(f"runtime profile differs: release {manifest.get('runtime_profile')!r}, "
                           f"local channel {local_channel.runtime_profile!r}")
    if registry.get_release(release_id):
        raise PublishError(f"release {release_id} is already registered here")

    dst = Path(cfg.settings.release_path(release_id))
    if (dst / "COMPLETE").exists():
        raise PublishError(f"{dst} already exists")
    (dst / "wheels").mkdir(parents=True, exist_ok=True)
    for wheel in sorted((src / "wheels").glob("*.whl")):
        shutil.copyfile(wheel, dst / "wheels" / wheel.name)
    for name in RELEASE_FILES:
        if name != "install.txt" and (src / name).exists():
            shutil.copyfile(src / name, dst / name)
    (dst / "install.txt").write_text(render_install_txt(str(dst)))

    problems = verify_release(dst, require_complete=False)
    if problems:
        raise PublishError(f"imported files failed verification, nothing registered: {problems}")
    (dst / "COMPLETE").write_text(f"{release_id} imported {utcnow().isoformat()} from {src}\n")

    registry.create_release({
        "release_id": release_id, "channel": channel, "runtime_profile": manifest.get("runtime_profile"),
        "status": reg.PUBLISHED, "status_reason": f"imported from {src}", "staging_path": str(src),
        "release_path": str(dst), "lock_sha256": sha256_file(dst / "requirements.lock"),
        "previous_release_id": manifest.get("previous_release_id"),
        "upgrade_packages": ",".join(manifest.get("upgrade_packages") or []),
        "gate_status": (manifest.get("gate") or {}).get("status"), "test_passed": True,
        "approved_by": f"import:{registry.actor}", "approved_at": utcnow(), "published_at": utcnow(),
    })
    registry.add_artifacts([{"release_id": release_id, "package": "", "version": "", "filename": a["filename"],
                             "sha256": a["sha256"], "scan_status": a.get("scan_status")}
                            for a in manifest.get("artifacts", [])])
    registry.add_event("IMPORTED", release_id, channel, {"source": str(src)})
    return {"release_id": release_id, "release_path": str(dst), "install_txt": f"{dst}/install.txt"}
