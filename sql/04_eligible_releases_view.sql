-- Optional: a view that lists the releases compute may use right now.
-- Used by examples/startup_gate.py for jobs that need a hard stop at start (decision D12).
-- Run after 02 and 03. Same placeholders as 01_setup_uc.sql.
--
-- A release is eligible when it is PUBLISHED and no exception on it is past its deadline.

CREATE OR REPLACE VIEW ${catalog}.${schema}.eligible_releases AS
SELECT r.release_id, r.channel, r.runtime_profile, r.release_path, r.published_at
FROM ${catalog}.${schema}.dependency_releases r
WHERE r.status = 'PUBLISHED'
  AND NOT EXISTS (
    SELECT 1 FROM ${catalog}.${schema}.dependency_exceptions e
    WHERE e.release_id = r.release_id
      AND (e.status = 'EXPIRED'
           OR (e.status IN ('OPEN', 'APPROVED') AND e.deadline_at < current_timestamp()))
  );

-- Workloads may read the view. They get no access to the registry tables themselves.
GRANT SELECT ON VIEW ${catalog}.${schema}.eligible_releases TO `${consumers_group}`;
