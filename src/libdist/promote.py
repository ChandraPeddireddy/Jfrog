"""Switch compute to a release, roll back, and revoke."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from . import registry as reg
from .publish import verify_release
from .references import (
    environment_version_in_yaml,
    release_in_environment_yaml,
    render_environment_yaml,
    repoint_libraries,
    with_pip_redirect,
)
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
           verify_files: bool = True, environments=None) -> dict:
    """Point every compute reference of one wave at ``release_id``.

    ``policies`` edits classic compute policies. ``environments`` refreshes
    serverless base environments; it is only needed when the wave lists any.
    """
    row = registry.get_release(release_id)
    problems = eligibility_problems(registry, release_id)
    if problems:
        raise PromoteError(f"{release_id} is not eligible: {problems}")
    channel = cfg.channel(row["channel"])
    wave = channel.wave(wave_name)
    settings = cfg.settings
    if verify_files:
        broken = verify_release(row["release_path"])
        if broken:
            raise PromoteError(f"{release_id} failed verification in the wheelhouse: {broken}")
    if not wave.policy_ids and not wave.base_environments:
        raise PromoteError(f"wave {wave_name!r} of channel {channel.name} lists no compute references")
    if wave.base_environments and environments is None:
        raise PromoteError("this wave has serverless base environments but no client was given")

    changed = []

    def record(reference_type: str, reference_id: str, previous: str | None) -> None:
        registry.set_reference(channel.name, wave.name, reference_type, reference_id, release_id, previous)
        registry.add_event("SWITCH", release_id, channel.name, {
            "wave": wave.name, "reference_type": reference_type, "reference_id": reference_id,
            "previous_release_id": previous})
        changed.append({"reference_type": reference_type, "reference_id": reference_id,
                        "previous_release_id": previous})
        log.info("%s %s: %s -> %s", reference_type, reference_id, previous, release_id)

    for policy_id in wave.policy_ids:
        policy = policies.get(policy_id)
        libraries, previous = repoint_libraries(policy.get("libraries"), settings.releases_root,
                                                channel.name, release_id)
        if previous == release_id:
            log.info("policy %s already points at %s", policy_id, release_id)
            continue
        if channel.pip_redirect:
            policy = with_pip_redirect(policy, f"{row['release_path']}/wheels")
        policies.set_libraries(policy, libraries)
        record("compute_policy", policy_id, previous)

    for name in wave.base_environments:
        env = environments.get(name)
        path = Path(str(env.get("filepath", "")))
        if not str(path).startswith(settings.environments_root + "/"):
            raise PromoteError(f"{name}: its YAML file {path} is not under {settings.environments_root}")
        old_text = path.read_text() if path.exists() else ""
        previous = release_in_environment_yaml(old_text, settings.releases_root, channel.name)
        if previous == release_id:
            log.info("base environment %s already points at %s", name, release_id)
            continue
        version = environment_version_in_yaml(old_text) or channel.serverless_environment_version
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_environment_yaml(row["release_path"], version))
        try:
            environments.refresh(name)
        except Exception as exc:
            if old_text:
                path.write_text(old_text)  # put the file back; the environment was not rebuilt
            registry.add_event("SWITCH_FAILED", release_id, channel.name,
                               {"wave": wave.name, "reference_id": name, "error": str(exc)[:1000]})
            raise PromoteError(f"refresh of {name} failed, its YAML was restored: {exc}") from exc
        record("base_environment", name, previous)

    return {"release_id": release_id, "wave": wave.name, "changed": changed}


def close_superseded_exceptions(registry: reg.Registry, channel_name: str) -> list[str]:
    """Close grace periods of releases that no compute reference uses any more."""
    in_use = {r.get("release_id") for r in registry.list_references(channel_name, all_sites=True)}
    closed = []
    for exc in registry.list_exceptions():
        if exc.get("channel") != channel_name or exc.get("kind") != "GRACE":
            continue
        if exc.get("status") in ("OPEN", "APPROVED", "EXPIRED") and exc.get("release_id") not in in_use:
            registry.update_exception(exc["exception_id"], status="CLOSED", closed_at=utcnow())
            closed.append(exc["exception_id"])
    if closed:
        registry.add_event("EXCEPTIONS_CLOSED", "", channel_name, {"exception_ids": closed})
    return closed


def rollback(cfg: Config, registry: reg.Registry, policies, channel_name: str, wave_name: str,
             environments=None) -> dict:
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
    result = switch(cfg, registry, policies, target, wave_name, environments=environments)
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
    still = [r for r in registry.list_references(row["channel"], all_sites=True)
             if r.get("release_id") == release_id]
    registry.add_event("REVOKED", release_id, row["channel"],
                       {"reason": reason, "references_still_using": [r["reference_id"] for r in still]})
    return {"release_id": release_id, "references_still_using": still}
