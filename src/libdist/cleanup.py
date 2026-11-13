"""Delete old candidates and unused releases, following the retention rules in policy.yml.

A wheelhouse release is deleted only when all of these hold:
  * no compute reference names it as its current or previous release;
  * it is not the newest published release of its channel;
  * compute left it more than ``retention_days`` ago (runs that started on it have finished).

By default this only reports. Pass ``apply=True`` to delete.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta
from pathlib import Path

from . import registry as reg
from .promote import _as_datetime
from .settings import Config
from .util import log, utcnow

_KEEP_IN_STAGING = (reg.READY_FOR_APPROVAL, reg.APPROVED, reg.DOWNLOADING, reg.SCANNING, reg.TESTING)


def _last_left(registry: reg.Registry, release_id: str) -> datetime | None:
    """When compute last switched away from a release (the newest SWITCH event that replaced it)."""
    import json

    latest = None
    for event in registry.list_events():
        if event.get("action") != "SWITCH":
            continue
        details = json.loads(event.get("details") or "{}")
        if details.get("previous_release_id") == release_id:
            when = _as_datetime(event.get("event_time"))
            if when and (latest is None or when.timestamp() > latest.timestamp()):
                latest = when
    return latest


def plan(cfg: Config, registry: reg.Registry, now: datetime | None = None) -> dict:
    now = now or utcnow()
    policy = cfg.policy
    referenced = set()
    for ref in registry.list_references(all_sites=True):
        referenced.update({ref.get("release_id"), ref.get("previous_release_id")})
    newest = {}
    for release in registry.list_releases(statuses=[reg.PUBLISHED]):
        newest.setdefault(release["channel"], release["release_id"])  # list is newest first

    wheelhouse, staging = [], []
    for release in registry.list_releases():
        rid, created = release["release_id"], _as_datetime(release.get("created_at"))
        unused = rid not in referenced and newest.get(release["channel"]) != rid
        if release["status"] in (reg.PUBLISHED, reg.REVOKED) and release.get("published_at") and unused:
            left = _last_left(registry, rid) or _as_datetime(release.get("published_at"))
            if left and now.timestamp() - left.timestamp() > timedelta(days=policy.retention_days).total_seconds():
                wheelhouse.append({"release_id": rid, "path": release["release_path"], "unused_since": str(left)})
        if release["status"] not in _KEEP_IN_STAGING and created and release.get("staging_path"):
            age = now.timestamp() - created.timestamp()
            if age > timedelta(days=policy.staging_retention_days).total_seconds():
                staging.append({"release_id": rid, "path": release["staging_path"]})
    return {"wheelhouse": wheelhouse, "staging": staging}


def cleanup(cfg: Config, registry: reg.Registry, apply: bool = False, now: datetime | None = None) -> dict:
    result = plan(cfg, registry, now)
    result["applied"] = apply
    if not apply:
        return result
    roots = {"wheelhouse": cfg.settings.releases_root, "staging": cfg.settings.staging_root}
    for kind, items in (("wheelhouse", result["wheelhouse"]), ("staging", result["staging"])):
        for item in items:
            path = Path(item["path"])
            if not str(path).startswith(roots[kind] + "/"):  # never delete outside the pipeline's folders
                log.error("refusing to delete %s: outside %s", path, roots[kind])
                item["deleted"] = False
                continue
            existed = path.exists()
            shutil.rmtree(path, ignore_errors=True)
            item["deleted"] = existed and not path.exists()
            if kind == "wheelhouse" and item["deleted"]:
                registry.update_release(item["release_id"], status=reg.RETIRED)
            if item["deleted"]:
                registry.add_event("CLEANUP_DELETED", item["release_id"], "", {"kind": kind, "path": str(path)})
    registry.add_event("CLEANUP_COMPLETED", "", "", {
        "wheelhouse": len(result["wheelhouse"]), "staging": len(result["staging"])})
    return result
