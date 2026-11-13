-- Grants on the registry tables. Run after 02_registry_tables.sql.
-- Same placeholders as 01_setup_uc.sql.

-- Builder and promoter write pipeline state.
GRANT SELECT, MODIFY ON TABLE ${catalog}.${schema}.dependency_releases   TO `${builder_sp}`;
GRANT SELECT, MODIFY ON TABLE ${catalog}.${schema}.dependency_artifacts  TO `${builder_sp}`;
GRANT SELECT, MODIFY ON TABLE ${catalog}.${schema}.dependency_findings   TO `${builder_sp}`;
GRANT SELECT, MODIFY ON TABLE ${catalog}.${schema}.dependency_exceptions TO `${builder_sp}`;
GRANT SELECT, MODIFY ON TABLE ${catalog}.${schema}.dependency_consumers  TO `${builder_sp}`;
GRANT SELECT, MODIFY ON TABLE ${catalog}.${schema}.dependency_events     TO `${builder_sp}`;
GRANT SELECT         ON TABLE ${catalog}.${schema}.dependency_references TO `${builder_sp}`;
GRANT SELECT         ON TABLE ${catalog}.${schema}.dependency_rollouts   TO `${builder_sp}`;
GRANT SELECT, MODIFY ON TABLE ${catalog}.${schema}.dependency_migration  TO `${builder_sp}`;

GRANT SELECT, MODIFY ON TABLE ${catalog}.${schema}.dependency_releases   TO `${promoter_sp}`;
GRANT SELECT, MODIFY ON TABLE ${catalog}.${schema}.dependency_references TO `${promoter_sp}`;
GRANT SELECT, MODIFY ON TABLE ${catalog}.${schema}.dependency_events     TO `${promoter_sp}`;
GRANT SELECT, MODIFY ON TABLE ${catalog}.${schema}.dependency_rollouts   TO `${promoter_sp}`;
GRANT SELECT, MODIFY ON TABLE ${catalog}.${schema}.dependency_exceptions TO `${promoter_sp}`;
GRANT SELECT, MODIFY ON TABLE ${catalog}.${schema}.dependency_artifacts  TO `${promoter_sp}`;  -- import-release
GRANT SELECT         ON TABLE ${catalog}.${schema}.dependency_findings   TO `${promoter_sp}`;

-- Security approvers: read everything, approve releases, manage exceptions.
GRANT SELECT ON SCHEMA ${catalog}.${schema} TO `${security_group}`;
GRANT MODIFY ON TABLE ${catalog}.${schema}.dependency_releases   TO `${security_group}`;
GRANT MODIFY ON TABLE ${catalog}.${schema}.dependency_exceptions TO `${security_group}`;
GRANT MODIFY ON TABLE ${catalog}.${schema}.dependency_events     TO `${security_group}`;

-- The promote job reads the jobs system tables for the canary health check.
-- (Also grant this to the Ops group if they run query H1 by hand.)
GRANT USE CATALOG ON CATALOG system TO `${promoter_sp}`;
GRANT USE SCHEMA ON SCHEMA system.lakeflow TO `${promoter_sp}`;
GRANT SELECT ON TABLE system.lakeflow.job_run_timeline TO `${promoter_sp}`;
