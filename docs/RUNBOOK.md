# Runbook

Day-to-day operation of the library distribution pipeline. For first-time setup see
[DEPLOYMENT.md](DEPLOYMENT.md).

Commands below use `-t prod`. Replace it with your target. `<catalog>.<schema>` is the registry schema.

## Quick reference

| I want to | Do this |
|---|---|
| See the current state | Dashboard queries D1 and D2 in `sql/monitoring_queries.sql` |
| Build a release | `databricks bundle run -t prod libdist_build --params channel=<channel>` |
| Replace one vulnerable library | `databricks bundle run -t prod libdist_build --params channel=<channel>,upgrade_package=<package>` |
| Approve a release | Security runs `sql/approve_release.sql` |
| Publish an approved release | `databricks bundle run -t prod libdist_publish --params release_id=<id>` |
| Give a release to compute | `databricks bundle run -t prod libdist_switch --params release_id=<id>,wave=<wave>` |
| Undo a switch | `databricks bundle run -t prod libdist_rollback --params channel=<channel>,wave=<wave>` |
| Revoke a release | `databricks bundle run -t prod libdist_revoke --params release_id=<id>,reason="<why>"` |
| Rescan now | `databricks bundle run -t prod libdist_reassess` |
| Run the promotion steps now | `databricks bundle run -t prod libdist_promote` |
| See what clean-up would delete | `databricks bundle run -t prod libdist_cleanup` (add `--params apply=true` to delete) |
| See rollouts | Dashboard query D7; `python src/run.py --config-dir config status --channel <channel>` |

## Manual or automatic

The same steps can be run by a person or by the `libdist-promote` job. What the job does depends
on three switches. All are off after the first deployment.

| Switch | Off | On |
|---|---|---|
| `libdist-promote` schedule (job) | Nothing happens by itself | Every 30 minutes: publishes every `APPROVED` release |
| `rollout.auto` (`channel.yml`) | Ops runs `libdist-switch` per wave | The job starts the first wave after a publish and moves on when the wave is healthy |
| `approval_mode` (`config/policy.yml`) | `manual`: Security approves each release | `security_rebuilds` or `automatic`: the job approves candidates that passed the gate and the tests |

With all three on, a replacement for a vulnerable library goes from finding to every wave without
a person: `libdist-reassess` finds it and starts the build, `libdist-promote` approves, publishes
and rolls it out. People are needed only for the alerts below.

The manual jobs keep working when the automation is on. Use them to act faster than the schedule.

## Release states

```
DOWNLOADING → SCANNING → TESTING → READY_FOR_APPROVAL → APPROVED → PUBLISHED
      │           │          │                                         │
      └───────────┴──────────┴──► FAILED or QUARANTINED                └──► REVOKED
```

| State | Meaning |
|---|---|
| `READY_FOR_APPROVAL` | Candidate in staging. Gate passed, tests passed. Waiting for Security. |
| `APPROVED` | Security approved it. Not in the wheelhouse yet. |
| `PUBLISHED` | In the wheelhouse, every hash verified. Compute may use it. |
| `QUARANTINED` | A blocking finding, or a scan that failed or was incomplete. Stays in staging as evidence. |
| `FAILED` | Could not be resolved, or the tests failed. |
| `REVOKED` | Must not be given to compute any more. |

## Normal release

1. **Build.** Run `libdist-build` for the channel.
2. **Review.** Security reviews the candidate with `sql/approve_release.sql`. The changes from the
   previous release are in `<staging_path>/manifest.json` under `changes_from_previous`.
3. **Approve.** Security runs the `UPDATE` in that file.
4. **Publish.** Run `libdist-publish`.
5. **Canary.** Run `libdist-switch` with `wave=canary`. Note the time.
6. **Wait and check.** Let the canary jobs run for the agreed period (decision D8). Run query H1
   in `sql/monitoring_queries.sql` with the canary job IDs and the switch time. Compare the
   failure rate with the baseline.
7. **Roll out.** If healthy, run `libdist-switch` for each remaining wave, with the same check between waves.
8. **If unhealthy.** Run `libdist-rollback` for the canary wave and tell the channel owner.

With `rollout.auto: true`, steps 5 to 8 are done by `libdist-promote`: it waits `soak_hours`
(`urgent_soak_hours` for a security rebuild), checks the failure rate of the wave's `job_ids`,
then switches the next wave, or rolls every switched wave back.

A switch affects compute that starts after it. Job clusters pick it up at the next run. All-purpose
clusters pick it up at the next restart. Runs that already started keep the release they started with.

### Two kinds of build

| Kind | Command | What changes |
|---|---|---|
| Security rebuild | `upgrade_package=<package>` | Only that package, and whatever it forces. Every other version stays. |
| Add a library | no `upgrade_package`, after editing `channel.in` | Only the new library and its new dependencies. |
| Routine refresh | Run `src/run.py build --channel <c> --full-upgrade` from a one-off job | Every library moves to its newest allowed version. Use a longer canary. |

Never mix a routine refresh into a security rebuild.

## Alerts and what to do

Alert queries are in `sql/monitoring_queries.sql`.

### A1. A build failed or was quarantined

Open the run output or `dependency_releases.status_reason`.

**A build is quarantined**

| Reason starts with | Meaning | Action |
|---|---|---|
| A package and an issue ID, for example `certifi==2025.1.1: XRAY-123 (Critical)` | Xray reports a blocking finding in that version | Rebuild with `upgrade_package=<package>`. If the lock keeps the same version, see "No safe version". |
| `HOLD: <wheel>: scan failed` | Xray did not return a result for that wheel | Check JFrog and Xray health and the token. Rebuild. Never approve a held release by hand. |
| `HOLD: ... unknown severity` | A finding has no severity | Ask Security to classify it. Do not treat it as low. |
| `policy violation:` | An Xray Watch reports a violation | Ask the policy owner. |

**No safe version** (`NO_SAFE_VERSION` in the reason)

The `NO_SAFE_VERSION` event lists each blocked version and, where the pipeline could tell, whether
Curation or Xray blocked it (`reason` is `curation`, `xray` or `unknown`).

1. **Blocked by Curation (the fix is too new).** This is outcome B in the proposed solution.
   Request a Curation waiver for that exact fix version, if Security has agreed a waiver path (decision D10):

   ```bash
   python src/run.py --config-dir config waiver --package <package> --version <version> \
       --reason "Fixes <CVE> in release <release_id>, which is in use" --channel <channel>
   ```

   Run it from a notebook or a one-off job as the builder. Approval happens in JFrog under
   Curation > Waiver Requests. After it is approved, rebuild with `upgrade_package=<package>`.
2. **Blocked by Xray in every version (no fix exists).** This is outcome C. Tell the channel owner
   and Security. The release in use stays until its grace deadline. Security decides: accept the
   risk for longer, or revoke.
3. **Reason unknown.** Open the blocked URL in the JFrog UI audit log to see which control blocked it.

### A2. New finding in a release in use

`libdist-reassess` found a new blocking finding, opened a grace period, and started a replacement
build (`upgrade_package` set to the affected package).

1. Find the replacement build in the `libdist-build` runs.
2. If it reached `READY_FOR_APPROVAL`: ask Security to approve, then publish and switch, canary first.
   For a Critical finding use the shorter canary window agreed in decision D8.
3. If it failed: follow "No safe version" above.

### A3. Grace deadline close or passed

The release contains a library Xray now blocks, and no replacement is in use yet.

1. Check the replacement build and the waiver request. Speed them up if you can.
2. If the deadline will be missed, escalate to Security **before** it passes. Security chooses
   (decision D7):
   - **Extend.** Security records the decision:

     ```sql
     UPDATE <catalog>.<schema>.dependency_exceptions
     SET status = 'APPROVED', approver = current_user(), deadline_at = '<new deadline>',
         rationale = '<why, and the compensating controls>'
     WHERE exception_id = '<id>';
     ```

   - **Revoke.** See "Revoke a release".

The pipeline revokes a release by itself only if Security set `on_grace_expiry: revoke` in
`config/policy.yml`. With the default, `alert`, it never does.

**Accepting a finding that has no fix (outcome C).** Security can accept a finding for a channel
for a limited time. Builds then pass the gate for that finding until the deadline:

```sql
INSERT INTO <catalog>.<schema>.dependency_exceptions
  (exception_id, channel, package, issue_id, severity, kind, status, opened_at, deadline_at, approver, rationale)
VALUES (uuid(), '<channel>', '<package>', '<issue id>', '<severity>', 'RISK_ACCEPTED', 'APPROVED',
        current_timestamp(), '<deadline>', current_user(), '<why, and the compensating controls>');
```

### A4. Compute still points at a revoked release

Switch those policies to an eligible release now: `libdist-switch` with the replacement release and the wave.
If no eligible release exists, this is an incident: jobs on that policy will keep installing the revoked release.

### A5. A candidate is waiting for approval

Remind the security approver. A candidate that is not needed any more can be rejected with the
commented `UPDATE` in `sql/approve_release.sql`.

### A6. Rescan failed, rollback refused, publish failed

| Event | Meaning | Action |
|---|---|---|
| `RESCAN_FAILED` | Xray did not return a full result for a release in use | Check JFrog and Xray. Run `libdist-reassess` again. Until it succeeds, new findings are not being detected. |
| `ROLLBACK_REFUSED` | The previous release is revoked or past a grace deadline | Do not force it. Stop the remaining waves. Escalate to the channel owner and Security. |
| `PUBLISH_FAILED` | A copied wheel did not match its hash | Nothing was registered. Run `libdist-publish` again. If it repeats, check the volume storage. |

### A8. A rollout was rolled back, halted or held

| Rollout status | Meaning | Action |
|---|---|---|
| `ROLLED_BACK` | The failure rate rose on a wave, or the release was revoked during the rollout. Every switched wave is back on the previous release. | Read `detail`. Tell the channel owner. The job does not retry that release. Fix the cause and build a new release, or start it again by hand with `rollout-start` if the failures were unrelated. |
| `HALTED` | A switch or a rollback was refused, for example because the previous release is no longer eligible. Compute may be split between two releases. | Read `detail`. Decide with the channel owner and Security which release every wave should use, set it with `libdist-switch`, then mark the rollout done: `UPDATE dependency_rollouts SET status = 'COMPLETED' WHERE rollout_id = '<id>'`. No new rollout starts for the channel while one is `HALTED`. |
| `IN_PROGRESS`, detail starts with `held` | Too few canary runs to judge after `max_wait_hours`. | Check the canary jobs ran. Then either switch the next wave by hand with `libdist-switch`, or set `on_insufficient_data: proceed`. |

### A9. The promote job has not completed

Check the `libdist-promote` schedule and its last runs. While it is down, approved releases are
not published and rollouts do not move. Use the manual jobs meanwhile.

### A10. A waiver request is pending or failed

- **Pending:** ask the JFrog policy owner to decide in Curation > Waiver Requests. After approval
  the next `libdist-reassess` run rebuilds. To go faster, run `libdist-build` with `upgrade_package`.
- **Rejected in JFrog:** the pipeline does not read the decision. Close the row so the alert clears,
  and treat the finding as "no fix available":
  `UPDATE dependency_exceptions SET status = 'CLOSED', closed_at = current_timestamp() WHERE kind = 'WAIVER' AND package = '<p>' AND version = '<v>'`.
- **Failed:** read the `WAIVER_FAILED` event. Usually the Curation policy forbids waiver requests
  or `jfrog.curated_repo` is wrong.

### A7. No rescan in 8 hours

Check that the `libdist-reassess` schedule is not paused and that its last runs succeeded.

## Rollback

```bash
databricks bundle run -t prod libdist_rollback --params channel=<channel>,wave=<wave>
```

The wave's policies go back to the release they used before the last switch. The command refuses
when that release is no longer eligible. Roll back one wave at a time, in reverse order of the rollout.

A rollback only goes one step back. To go further, use `libdist-switch` with the release ID you want.

## Revoke a release

Revoking is a Security decision.

1. Run `libdist-revoke` with the release ID and the reason. The output lists the policies that still use it.
2. Switch each of those policies to an eligible release.
3. Runs that already started are not affected. Stopping them is an incident-response decision.

Do not delete the release folder. Runs that started before the switch still read it.

## Clean-up

`libdist-cleanup` follows the retention rules in `config/policy.yml`.

- **Wheelhouse releases** are deleted only when no site references them as the current or previous
  release, they are not the newest published release of their channel, and compute left them more
  than `retention_days` ago. A deleted release gets status `RETIRED`.
- **Staging candidates** older than `staging_retention_days` are deleted unless they are being
  built or are waiting for approval or publication.

Without `apply=true` the job only reports what it would delete. Read the report before the first real run.

## Serverless

A serverless wave lists workspace base environments instead of compute policies. A switch rewrites
the environment's YAML file under `<wheelhouse>/environments/` and refreshes the environment.

- New serverless sessions and runs use the new release. Notebook sessions that are already open
  keep the old one until they are restarted.
- If the refresh fails, the YAML file is put back and the event `SWITCH_FAILED` is written. Read
  the error in the workspace under Settings > Compute > Base environments.
- To add a serverless wave: `python src/run.py --config-dir config serverless-init --release-id <id> --wave <wave>`,
  then add the printed name to the wave in `channel.yml`.

## Several workspaces

Each workspace is a *site* (`settings.yml`: `site`). One site is the **hub**: it builds, approves,
publishes, rescans and cleans up. The others are **spokes**: their `libdist-promote` only rolls
published releases out to their own compute.

- `dependency_references` and `dependency_rollouts` carry the site. A rollback in one site does not touch another.
- A release is revoked for all sites at once. Alert A4 lists every site that still uses it.
- To bring a release into another metastore, see IMPLEMENTATION.md stage 10 (`import-release`).

## Where to look

| Question | Where |
|---|---|
| Why did this build fail? | The job run output; `dependency_releases.status_reason`; `dependency_events` |
| What changed in this release? | `manifest.json` in the release folder: `changes_from_previous` |
| Which findings does a release have? | `dependency_findings`; `security-findings.json` in the release folder |
| Who approved it? | `dependency_releases.approved_by`; the `APPROVED` event |
| Which release does a policy or base environment use? | `dependency_references`; the policy's Libraries tab; the YAML under `<wheelhouse>/environments/` |
| Where is a rollout? | `dependency_rollouts`; events `ROLLOUT_*` |
| Who read or wrote the wheelhouse? | Unity Catalog audit logs for the volume |
| Is a release intact? | `python src/run.py --config-dir config verify --release-id <id>` |
