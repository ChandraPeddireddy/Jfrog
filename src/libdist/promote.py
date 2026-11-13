"""Switch compute to a release, roll back, and revoke."""

from __future__ import annotations

from datetime import datetime

from . import registry as reg
from .publish import verify_release
from .references import repoint_libraries
from .settings import Config
from .util import log, utcnow


class PromoteError(RuntimeError):
    pass


def _as_datetime(value) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def eligibility_problems(registry: reg.Registry, release_id: str, now: datetime | None = None) -> list[str]:
    """Why a release may not be given to compute. Empty list means it is eligible.

    A release is eligible when it is PUBLISHED and has no exception whose
    deadline has passed. A release inside an open grace period, or with an
    exception that Security extended to a later deadline, is still eligible.
    """
    now = now or utcnow()
    row = registry.get_release(release_id)
    if not row:
        return [f"unknown release {release_id}"]
    problems = []
    if row["status"] != reg.PUBLISHED:
        problems.append(f"status is {row['status']}, not PUBLISHED")
    for exc in registry.list_exceptions(release_id=release_id):
        deadline = _as_datetime(exc.get("deadline_at"))
        if exc.get("status") == "EXPIRED" or (
            exc.get("status") in ("OPEN", "APPROVED") and deadline and deadline.timestamp() < now.timestamp()
        ):
            problems.append(f"exception for {exc.get('issue_id')} is past its deadline")
    return problems


def switch(cfg: Config, registry: reg.Registry, policies, release_id: str, wave_name: str,
           verify_files: bool = True) -> dict:
    """Point every compute policy of one wave at ``release_id``."""
    row = registry.get_release(release_id)
    problems = eligibility_problems(registry, release_id)
    if problems:
        raise PromoteError(f"{release_id} is not eligible: {problems}")
    channel = cfg.channel(row["channel"])
    wave = channel.wave(wave_name)
    if verify_files:
        broken = verify_release(row["release_path"])
        if broken:
            raise PromoteError(f"{release_id} failed verification in the wheelhouse: {broken}")
    if not wave.policy_ids:
        raise PromoteError(f"wave {wave_name!r} of channel {channel.name} lists no compute policies")

    changed = []
    for policy_id in wave.policy_ids:
        policy = policies.get(policy_id)
        libraries, previous = repoint_libraries(policy.get("libraries"), cfg.settings.releases_root,
                                                channel.name, release_id)
        if previous == release_id:
            log.info("policy %s already points at %s", policy_id, release_id)
            continue
        policies.set_libraries(policy, libraries)
        registry.set_reference(channel.name, wave.name, "compute_policy", policy_id, release_id, previous)
        registry.add_event("SWITCH", release_id, channel.name,
                           {"wave": wave.name, "policy_id": policy_id, "previous_release_id": previous})
        changed.append({"policy_id": policy_id, "previous_release_id": previous})
        log.info("policy %s: %s -> %s", policy_id, previous, release_id)
    return {"release_id": release_id, "wave": wave.name, "changed": changed}


def rollback(cfg: Config, registry: reg.Registry, policies, channel_name: str, wave_name: str) -> dict:
    """Point a wave back at the release it used before. Refused if that release is not eligible."""
    refs = [r for r in registry.list_references(channel_name) if r.get("wave") == wave_name]
    targets = {r.get("previous_release_id") for r in refs}
    if not refs or targets == {None}:
        raise PromoteError(f"no previous release is recorded for wave {wave_name!r}")
    if len(targets) != 1:
        raise PromoteError(f"policies in wave {wave_name!r} have different previous releases: {targets}")
    target = targets.pop()
    problems = eligibility_problems(registry, target)
    if problems:
        registry.add_event("ROLLBACK_REFUSED", target, channel_name,
                           {"wave": wave_name, "problems": problems})
        raise PromoteError(
            f"rollback to {target} refused, it is no longer eligible: {problems}. "
            "Stop the remaining waves and escalate to the channel owner and Security."
        )
    result = switch(cfg, registry, policies, target, wave_name)
    registry.add_event("ROLLBACK", target, channel_name, {"wave": wave_name})
    return result


def revoke(registry: reg.Registry, release_id: str, reason: str) -> dict:
    """Mark a release REVOKED and report which compute still points at it.

    Revocation does not change compute by itself: switch the listed references
    to an eligible release. It also does not affect runs that already started.
    """
    row = registry.get_release(release_id)
    if not row:
        raise PromoteError(f"unknown release {release_id}")
    registry.update_release(release_id, status=reg.REVOKED, revoked_at=utcnow(),
                            status_reason=reason[:2000])
    still = [r for r in registry.list_references(row["channel"]) if r.get("release_id") == release_id]
    registry.add_event("REVOKED", release_id, row["channel"],
                       {"reason": reason, "references_still_using": [r["reference_id"] for r in still]})
    return {"release_id": release_id, "references_still_using": still}
