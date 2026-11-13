"""The promote job: moves releases forward without a person starting each step.

One run of ``advance`` does, for every channel:

  1. approve candidates by the rules, when policy.yml allows it (decision D2);
  2. publish every APPROVED release to the wheelhouse;
  3. start a rollout for the newest published release (channel.yml: rollout.auto);
  4. move a running rollout to its next wave when the current wave is healthy,
     or roll it back when the failure rate rose.

It is safe to run again at any time: every step checks the registry first.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from . import registry as reg
from .health import HealthResult
from .promote import PromoteError, _as_datetime, close_superseded_exceptions, rollback, switch
from .publish import PublishError, publish
from .settings import Channel, Config
from .util import log, utcnow

# health(job_ids, switch_time, baseline_days, min_runs, max_increase) -> HealthResult
HealthFn = Callable[[list[str], datetime, int, int, float], HealthResult]

IN_PROGRESS, COMPLETED, ROLLED_BACK, HALTED = "IN_PROGRESS", "COMPLETED", "ROLLED_BACK", "HALTED"


def _is_security_rebuild(release: dict) -> bool:
    return bool((release.get("upgrade_packages") or "").strip())


def auto_approve(cfg: Config, registry: reg.Registry, channel: Channel) -> list[str]:
    approved = []
    for release in registry.list_releases(channel.name, [reg.READY_FOR_APPROVAL]):
        if not cfg.policy.auto_approves(_is_security_rebuild(release)):
            continue
        if release.get("gate_status") != "PASS" or not release.get("test_passed"):
            continue
        approver = f"policy:{cfg.policy.approval_mode}"
        registry.update_release(release["release_id"], status=reg.APPROVED, approved_by=approver,
                                approved_at=utcnow())
        registry.add_event("APPROVED", release["release_id"], channel.name,
                           {"approver": approver, "automatic": True})
        approved.append(release["release_id"])
    return approved


def publish_approved(cfg: Config, registry: reg.Registry, channel: Channel) -> tuple[list[str], list[str]]:
    published, failed = [], []
    for release in reversed(registry.list_releases(channel.name, [reg.APPROVED])):  # oldest first
        try:
            publish(cfg, registry, release["release_id"])
            published.append(release["release_id"])
        except PublishError as exc:
            log.error("publish of %s failed: %s", release["release_id"], exc)
            registry.add_event("PUBLISH_FAILED", release["release_id"], channel.name, {"error": str(exc)[:1500]})
            failed.append(release["release_id"])
    return published, failed


def start_rollout(cfg: Config, registry: reg.Registry, policies, environments, release_id: str) -> str:
    """Switch the first wave and record the rollout. Used by the job and by the command."""
    release = registry.get_release(release_id)
    if not release:
        raise PromoteError(f"unknown release {release_id}")
    channel = cfg.channel(release["channel"])
    if not channel.waves:
        raise PromoteError(f"channel {channel.name} has no waves")
    if any(r["status"] == IN_PROGRESS for r in registry.list_rollouts(channel.name)):
        raise PromoteError(f"channel {channel.name} already has a rollout in progress")
    first = channel.waves[0]
    switch(cfg, registry, policies, release_id, first.name, environments=environments)
    rollout_id = registry.create_rollout(channel.name, release_id, first.name)
    registry.add_event("ROLLOUT_STARTED", release_id, channel.name, {"rollout_id": rollout_id, "wave": first.name})
    return rollout_id


def _maybe_start(cfg, registry, policies, environments, channel: Channel) -> str | None:
    if not channel.rollout.auto or not channel.waves:
        return None
    rollouts = registry.list_rollouts(channel.name)
    if any(r["status"] in (IN_PROGRESS, HALTED) for r in rollouts):
        return None  # finish or resolve the current one first
    published = registry.list_releases(channel.name, [reg.PUBLISHED])
    if not published:
        return None
    newest = published[0]
    if any(r["release_id"] == newest["release_id"] for r in rollouts):
        return None  # already rolled out, or rolled back: never retried automatically
    return start_rollout(cfg, registry, policies, environments, newest["release_id"])


def _roll_back(cfg, registry, policies, environments, channel: Channel, rollout: dict, reason: str) -> str:
    """Undo every wave this rollout switched, last wave first."""
    try:
        for index in range(int(rollout["wave_index"]), -1, -1):
            rollback(cfg, registry, policies, channel.name, channel.waves[index].name, environments=environments)
    except PromoteError as exc:
        registry.update_rollout(rollout["rollout_id"], status=HALTED, detail=f"{reason}; rollback refused: {exc}"[:1500])
        registry.add_event("ROLLOUT_HALTED", rollout["release_id"], channel.name,
                           {"reason": reason, "error": str(exc)[:1000]})
        return HALTED
    registry.update_rollout(rollout["rollout_id"], status=ROLLED_BACK, detail=reason[:1500])
    registry.add_event("ROLLOUT_ROLLED_BACK", rollout["release_id"], channel.name, {"reason": reason})
    return ROLLED_BACK


def _advance_rollout(cfg, registry, policies, environments, channel: Channel, rollout: dict,
                     health: HealthFn | None, now: datetime) -> str:
    settings = channel.rollout
    release = registry.get_release(rollout["release_id"]) or {}
    index = int(rollout["wave_index"])
    wave = channel.waves[index]
    switched_at = _as_datetime(rollout["wave_switched_at"])
    hours = (now.timestamp() - switched_at.timestamp()) / 3600
    soak = settings.urgent_soak_hours if _is_security_rebuild(release) else settings.soak_hours

    if release.get("status") != reg.PUBLISHED:
        return _roll_back(cfg, registry, policies, environments, channel, rollout,
                          f"release is {release.get('status')}, no longer PUBLISHED")

    result: HealthResult | None = None
    if wave.job_ids and health:
        result = health(wave.job_ids, switched_at, settings.baseline_days, settings.min_runs,
                        settings.max_increase)
        if result.healthy is False:
            return _roll_back(cfg, registry, policies, environments, channel, rollout,
                              f"wave {wave.name} unhealthy: {result.message}")

    if hours < soak:
        return "SOAKING"
    if result is not None and result.healthy is None:
        if hours < settings.max_wait_hours:
            return "WAITING_FOR_RUNS"
        if settings.on_insufficient_data == "hold":
            if not str(rollout.get("detail") or "").startswith("held"):
                registry.update_rollout(rollout["rollout_id"], detail=f"held: {result.message}")
                registry.add_event("ROLLOUT_WAITING", rollout["release_id"], channel.name,
                                   {"wave": wave.name, "message": result.message})
            return "HELD"

    if index + 1 < len(channel.waves):
        nxt = channel.waves[index + 1]
        try:
            switch(cfg, registry, policies, rollout["release_id"], nxt.name, environments=environments)
        except PromoteError as exc:
            registry.update_rollout(rollout["rollout_id"], status=HALTED, detail=str(exc)[:1500])
            registry.add_event("ROLLOUT_HALTED", rollout["release_id"], channel.name, {"error": str(exc)[:1000]})
            return HALTED
        registry.update_rollout(rollout["rollout_id"], wave_index=index + 1, wave_name=nxt.name,
                                wave_switched_at=now, detail="")
        registry.add_event("ROLLOUT_WAVE", rollout["release_id"], channel.name, {"wave": nxt.name})
        return f"SWITCHED:{nxt.name}"

    registry.update_rollout(rollout["rollout_id"], status=COMPLETED, detail="")
    registry.add_event("ROLLOUT_COMPLETED", rollout["release_id"], channel.name, {})
    close_superseded_exceptions(registry, channel.name)
    return COMPLETED


def advance(cfg: Config, registry: reg.Registry, policies, environments=None,
            health: HealthFn | None = None, now: datetime | None = None) -> dict:
    now = now or utcnow()
    summary: dict = {}
    for name in cfg.channel_names():
        channel = cfg.channel(name)
        result: dict = {}
        try:
            if cfg.settings.role == "hub":  # a spoke only rolls published releases out to its own compute
                result["approved"] = auto_approve(cfg, registry, channel)
                result["published"], result["publish_failed"] = publish_approved(cfg, registry, channel)
            for rollout in registry.list_rollouts(name):
                if rollout["status"] == IN_PROGRESS:
                    result["rollout"] = _advance_rollout(cfg, registry, policies, environments, channel,
                                                         rollout, health, now)
            started = _maybe_start(cfg, registry, policies, environments, channel)
            if started:
                result["rollout_started"] = started
        except Exception as exc:  # one channel must not stop the others
            log.exception("channel %s failed", name)
            registry.add_event("PROMOTE_ERROR", "", name, {"error": str(exc)[:1500]})
            result["error"] = str(exc)
        summary[name] = result
    registry.add_event("PROMOTE_COMPLETED", "", "", {"channels": len(summary)})
    return summary
