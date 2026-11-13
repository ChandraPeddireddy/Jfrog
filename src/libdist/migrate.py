"""Migration report: will today's library requests be satisfied by a channel's release?

Reads the discovery snapshot (``dependency_consumers``) and compares every PyPI
requirement and ``%pip install`` line with the release's lock file.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from . import registry as reg
from .lockfile import normalize_name, read_lock
from .util import utcnow

SATISFIED, CONFLICT, MISSING, UNPARSED = "SATISFIED", "VERSION_CONFLICT", "NOT_IN_RELEASE", "UNPARSED"


def requirements_in(kind: str, spec: str) -> list[str]:
    """The individual requirement strings in one discovered library entry."""
    if kind == "pypi":
        return [spec.split(" (repo:", 1)[0].strip()]
    if kind in ("pip_install", "environment"):
        try:
            tokens = shlex.split(spec, comments=True)
        except ValueError:
            return [spec]
        out, skip = [], False
        for token in tokens:
            if skip:
                skip = False
            elif token in ("-r", "--requirement", "-c", "--constraint", "-i", "--index-url",
                           "--extra-index-url", "-f", "--find-links", "--target", "-t"):
                skip = True
            elif not token.startswith("-"):
                out.append(token)
        return out
    return []


def classify(requirement: str, pins: dict[str, str]) -> tuple[str, str]:
    """Return (result, pinned version in the release or '')."""
    from packaging.requirements import InvalidRequirement, Requirement

    if "/" in requirement or requirement.endswith((".whl", ".txt")):
        return UNPARSED, ""  # a path or URL, not a package requirement
    try:
        parsed = Requirement(requirement)
    except InvalidRequirement:
        return UNPARSED, ""
    pinned = pins.get(normalize_name(parsed.name))
    if pinned is None:
        return MISSING, ""
    if parsed.specifier and not parsed.specifier.contains(pinned, prereleases=True):
        return CONFLICT, pinned
    return SATISFIED, pinned


def migration_report(registry: reg.Registry, channel: str, release_id: str, release_path: str,
                     workspace_host: str | None = None) -> dict:
    pins = {p.key: p.version for p in read_lock(Path(release_path) / "requirements.lock")}
    now, rows, counts = utcnow(), [], {SATISFIED: 0, CONFLICT: 0, MISSING: 0, UNPARSED: 0}
    hosts = set()
    for consumer in registry.list_consumers(workspace_host):
        for requirement in requirements_in(consumer.get("library_kind") or "", consumer.get("library_spec") or ""):
            result, pinned = classify(requirement, pins)
            counts[result] += 1
            hosts.add(consumer.get("workspace_host") or "")
            rows.append({
                "snapshot_at": now, "workspace_host": consumer.get("workspace_host") or "",
                "object_type": consumer.get("object_type"), "object_id": consumer.get("object_id"),
                "object_name": consumer.get("object_name"), "owner": consumer.get("owner"),
                "requirement": requirement, "channel": channel, "release_id": release_id,
                "result": result, "pinned_version": pinned})
    for host in hosts:
        registry.replace_migration(host, channel, [r for r in rows if r["workspace_host"] == host])
    missing = sorted({r["requirement"] for r in rows if r["result"] == MISSING})
    return {"release_id": release_id, "requirements": len(rows), "counts": counts,
            "not_in_release": missing[:200]}
