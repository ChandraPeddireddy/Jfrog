-- Queries for the operations dashboard and for Databricks SQL alerts.
-- Replace ${catalog} and ${schema}. Each alert query returns rows only when
-- something needs attention: set the alert to trigger when the row count is above 0.

-- ============================== ALERTS ==========================================

-- A1. A build failed, was quarantined, or found no safe version (last 24 hours)
SELECT release_id, channel, status, status_reason, created_at
FROM ${catalog}.${schema}.dependency_releases
WHERE status IN ('FAILED', 'QUARANTINED')
  AND created_at >= current_timestamp() - INTERVAL 24 HOURS
ORDER BY created_at DESC;

-- A2. New blocking finding in a release that compute is using (open grace period)
SELECT e.release_id, e.channel, e.package, e.version, e.issue_id, e.severity,
       e.opened_at, e.deadline_at,
       ROUND((unix_timestamp(e.deadline_at) - unix_timestamp(current_timestamp())) / 3600, 1) AS hours_left
FROM ${catalog}.${schema}.dependency_exceptions e
WHERE e.status = 'OPEN' AND e.kind = 'GRACE'
ORDER BY e.deadline_at;

-- A3. Grace deadline within 3 days, or already passed  (escalate to Security: decision D7)
SELECT release_id, channel, package, issue_id, severity, status, deadline_at
FROM ${catalog}.${schema}.dependency_exceptions
WHERE (status = 'OPEN' AND deadline_at <= current_timestamp() + INTERVAL 3 DAYS)
   OR status = 'EXPIRED'
ORDER BY deadline_at;

-- A4. Compute still points at a release that is revoked
SELECT r.site, r.channel, r.wave, r.reference_type, r.reference_id, r.release_id, rel.revoked_at, rel.status_reason
FROM ${catalog}.${schema}.dependency_references r
JOIN ${catalog}.${schema}.dependency_releases rel USING (release_id)
WHERE rel.status = 'REVOKED';

-- A5. A candidate has been waiting for approval for more than 8 hours
SELECT release_id, channel, upgrade_packages, created_at
FROM ${catalog}.${schema}.dependency_releases
WHERE status = 'READY_FOR_APPROVAL' AND created_at < current_timestamp() - INTERVAL 8 HOURS;

-- A6. The rescan failed or a rollback was refused (last 24 hours)
SELECT event_time, action, release_id, channel, details
FROM ${catalog}.${schema}.dependency_events
WHERE action IN ('RESCAN_FAILED', 'ROLLBACK_REFUSED', 'PUBLISH_FAILED', 'NO_SAFE_VERSION')
  AND event_time >= current_timestamp() - INTERVAL 24 HOURS
ORDER BY event_time DESC;

-- A7. The reassess job has not completed a rescan in 8 hours (the schedule may be paused or failing)
SELECT MAX(event_time) AS last_rescan_completed
FROM ${catalog}.${schema}.dependency_events
WHERE action = 'RESCAN_COMPLETED'
HAVING MAX(event_time) IS NULL OR MAX(event_time) < current_timestamp() - INTERVAL 8 HOURS;

-- A8. A rollout was rolled back, halted, or is held waiting for runs
SELECT rollout_id, site, channel, release_id, status, wave_name, wave_switched_at, detail
FROM ${catalog}.${schema}.dependency_rollouts
WHERE status IN ('ROLLED_BACK', 'HALTED') AND updated_at >= current_timestamp() - INTERVAL 24 HOURS
   OR (status = 'IN_PROGRESS' AND detail LIKE 'held%');

-- A9. The promote job has not completed in 2 hours (only if its schedule is switched on)
SELECT MAX(event_time) AS last_promote_completed
FROM ${catalog}.${schema}.dependency_events
WHERE action = 'PROMOTE_COMPLETED'
HAVING MAX(event_time) IS NULL OR MAX(event_time) < current_timestamp() - INTERVAL 2 HOURS;

-- A10. A waiver request is waiting in JFrog, or a waiver request failed
SELECT 'pending' AS state, channel, package, version, opened_at AS at
FROM ${catalog}.${schema}.dependency_exceptions WHERE kind = 'WAIVER' AND status = 'OPEN'
UNION ALL
SELECT 'failed', channel, get_json_object(details, '$.package'), get_json_object(details, '$.version'), event_time
FROM ${catalog}.${schema}.dependency_events
WHERE action = 'WAIVER_FAILED' AND event_time >= current_timestamp() - INTERVAL 24 HOURS;

-- ============================== CANARY HEALTH ===================================

-- H1. Run failure rate of the canary jobs since a switch, against the 14 days before it.
-- Replace the job ids and :switch_time (the SWITCH event time). Needs SELECT on system.lakeflow.
SELECT
  CASE WHEN period_end_time >= :switch_time THEN 'canary' ELSE 'baseline' END AS window,
  COUNT(DISTINCT run_id) AS runs,
  COUNT(DISTINCT CASE WHEN result_state IN ('FAILED', 'ERROR', 'TIMED_OUT') THEN run_id END) AS failed_runs
FROM system.lakeflow.job_run_timeline
WHERE job_id IN ('<job id 1>', '<job id 2>')
  AND result_state IS NOT NULL
  AND period_end_time >= :switch_time - INTERVAL 14 DAYS
GROUP BY 1;

-- ============================== DASHBOARD =======================================

-- D1. Which release each compute policy and base environment uses now
SELECT r.site, r.channel, r.wave, r.reference_type, r.reference_id, r.release_id, r.previous_release_id,
       r.updated_at, r.updated_by, rel.status
FROM ${catalog}.${schema}.dependency_references r
LEFT JOIN ${catalog}.${schema}.dependency_releases rel USING (release_id)
ORDER BY r.site, r.channel, r.wave;

-- D2. Release history
SELECT release_id, channel, status, gate_status, test_passed, upgrade_packages,
       approved_by, created_at, published_at, revoked_at, status_reason
FROM ${catalog}.${schema}.dependency_releases
ORDER BY created_at DESC
LIMIT 200;

-- D3. Findings in releases that are in use, by severity
SELECT f.severity, COUNT(DISTINCT f.issue_id) AS findings, COUNT(DISTINCT f.package) AS packages
FROM ${catalog}.${schema}.dependency_findings f
WHERE f.release_id IN (SELECT release_id FROM ${catalog}.${schema}.dependency_references)
GROUP BY f.severity;

-- D4. Audit trail for one release
-- SELECT event_time, actor, action, details FROM ${catalog}.${schema}.dependency_events
-- WHERE release_id = :release_id ORDER BY event_time;

-- D5. Migration report: library requests that still point outside the wheelhouse
SELECT object_type, object_name, object_id, owner, library_kind, library_spec
FROM ${catalog}.${schema}.dependency_consumers
WHERE NOT (library_kind = 'requirements' AND library_spec LIKE '%/wheelhouse/releases/%')
ORDER BY object_type, object_name;

-- D6. Most requested libraries (input for channel.in)
SELECT lower(regexp_extract(library_spec, '^([A-Za-z0-9._-]+)', 1)) AS library, COUNT(*) AS uses
FROM ${catalog}.${schema}.dependency_consumers
WHERE library_kind IN ('pypi', 'pip_install', 'environment')
GROUP BY 1 ORDER BY uses DESC LIMIT 100;

-- D7. Rollout history
SELECT rollout_id, channel, release_id, status, wave_name, started_at, updated_at, detail
FROM ${catalog}.${schema}.dependency_rollouts
ORDER BY started_at DESC LIMIT 100;

-- D8. Migration report: will existing library requests work on the channel's release?
-- (filled by:  run.py migration-report --channel <channel>)
SELECT result, COUNT(*) AS requirements, COUNT(DISTINCT object_id) AS objects
FROM ${catalog}.${schema}.dependency_migration
GROUP BY result;

-- D9. Migration report: what to fix before the egress block
SELECT result, requirement, pinned_version, COUNT(DISTINCT object_id) AS objects,
       array_join(slice(collect_set(object_name), 1, 5), ', ') AS examples
FROM ${catalog}.${schema}.dependency_migration
WHERE result IN ('VERSION_CONFLICT', 'NOT_IN_RELEASE')
GROUP BY result, requirement, pinned_version
ORDER BY objects DESC;
