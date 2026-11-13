-- Registry tables for the library distribution pipeline.
-- Run once as the catalog or schema owner, after 01_setup_uc.sql.
-- Replace ${catalog} and ${schema} (the SQL editor prompts for them as parameters,
-- or use find-and-replace). The same statements are run by:  run.py init-registry
--
-- This file is generated from src/libdist/registry.py. A unit test keeps them in step.

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.dependency_releases (
  release_id STRING NOT NULL,
  channel STRING,
  runtime_profile STRING,
  status STRING,
  status_reason STRING,
  staging_path STRING,
  release_path STRING,
  lock_sha256 STRING,
  previous_release_id STRING,
  upgrade_packages STRING,
  gate_status STRING,
  test_passed BOOLEAN,
  created_at TIMESTAMP,
  created_by STRING,
  approved_by STRING,
  approved_at TIMESTAMP,
  published_at TIMESTAMP,
  revoked_at TIMESTAMP,
  updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.dependency_artifacts (
  release_id STRING NOT NULL,
  package STRING,
  version STRING,
  filename STRING,
  sha256 STRING,
  scan_status STRING,
  scanned_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.dependency_findings (
  release_id STRING NOT NULL,
  package STRING,
  version STRING,
  filename STRING,
  issue_id STRING,
  cves STRING,
  severity STRING,
  severity_source STRING,
  fixed_versions STRING,
  component STRING,
  is_violation BOOLEAN,
  summary STRING,
  source STRING,
  first_seen_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.dependency_exceptions (
  exception_id STRING NOT NULL,
  release_id STRING,
  channel STRING,
  package STRING,
  version STRING,
  issue_id STRING,
  severity STRING,
  kind STRING,
  status STRING,
  opened_at TIMESTAMP,
  deadline_at TIMESTAMP,
  approver STRING,
  rationale STRING,
  closed_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.dependency_references (
  site STRING,
  channel STRING NOT NULL,
  wave STRING,
  reference_type STRING,
  reference_id STRING NOT NULL,
  release_id STRING,
  previous_release_id STRING,
  updated_at TIMESTAMP,
  updated_by STRING
);

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.dependency_consumers (
  snapshot_at TIMESTAMP,
  workspace_host STRING,
  object_type STRING,
  object_id STRING,
  object_name STRING,
  owner STRING,
  policy_id STRING,
  library_kind STRING,
  library_spec STRING
);

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.dependency_rollouts (
  rollout_id STRING NOT NULL,
  site STRING,
  channel STRING,
  release_id STRING,
  status STRING,
  wave_index INT,
  wave_name STRING,
  wave_switched_at TIMESTAMP,
  started_at TIMESTAMP,
  updated_at TIMESTAMP,
  detail STRING
);

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.dependency_migration (
  snapshot_at TIMESTAMP,
  workspace_host STRING,
  object_type STRING,
  object_id STRING,
  object_name STRING,
  owner STRING,
  requirement STRING,
  channel STRING,
  release_id STRING,
  result STRING,
  pinned_version STRING
);

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.dependency_events (
  event_id STRING NOT NULL,
  event_time TIMESTAMP,
  actor STRING,
  action STRING,
  release_id STRING,
  channel STRING,
  details STRING
);
