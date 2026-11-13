-- Approve or reject a candidate release. Run by a SECURITY APPROVER, as themselves,
-- in the SQL editor. current_user() records who approved.
--
-- Replace ${catalog}, ${schema} and :release_id.

-- 1. Review the candidate ------------------------------------------------------
SELECT release_id, channel, runtime_profile, status, gate_status, test_passed,
       previous_release_id, upgrade_packages, staging_path, created_at
FROM ${catalog}.${schema}.dependency_releases
WHERE release_id = :release_id;

-- Every finding recorded for it (also in <staging_path>/security-findings.json)
SELECT package, version, issue_id, cves, severity, severity_source, fixed_versions, is_violation
FROM ${catalog}.${schema}.dependency_findings
WHERE release_id = :release_id
ORDER BY CASE severity WHEN 'Critical' THEN 1 WHEN 'High' THEN 2 WHEN 'Medium' THEN 3
                       WHEN 'Low' THEN 4 ELSE 0 END, package;

-- Every wheel must have scan_status = SCANNED
SELECT scan_status, COUNT(*) AS wheels
FROM ${catalog}.${schema}.dependency_artifacts
WHERE release_id = :release_id
GROUP BY scan_status;

-- What changed from the previous release is in <staging_path>/manifest.json
-- under "changes_from_previous".

-- 2a. Approve --------------------------------------------------------------------
-- Only a candidate that is READY_FOR_APPROVAL, passed the gate and passed the tests can be approved.
UPDATE ${catalog}.${schema}.dependency_releases
SET status = 'APPROVED', approved_by = current_user(), approved_at = current_timestamp(),
    updated_at = current_timestamp()
WHERE release_id = :release_id
  AND status = 'READY_FOR_APPROVAL' AND gate_status = 'PASS' AND test_passed = true;

INSERT INTO ${catalog}.${schema}.dependency_events
SELECT uuid(), current_timestamp(), current_user(), 'APPROVED', release_id, channel, '{}'
FROM ${catalog}.${schema}.dependency_releases
WHERE release_id = :release_id AND status = 'APPROVED' AND approved_by = current_user();

-- 2b. Or reject ------------------------------------------------------------------
-- UPDATE ${catalog}.${schema}.dependency_releases
-- SET status = 'FAILED', status_reason = 'rejected by ' || current_user() || ': <reason>',
--     updated_at = current_timestamp()
-- WHERE release_id = :release_id AND status = 'READY_FOR_APPROVAL';

-- 3. Confirm ---------------------------------------------------------------------
SELECT release_id, status, approved_by, approved_at
FROM ${catalog}.${schema}.dependency_releases
WHERE release_id = :release_id;
