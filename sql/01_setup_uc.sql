-- Unity Catalog objects and grants for the library distribution pipeline.
-- Run once, as a metastore admin or the catalog owner.
--
-- Replace before running:
--   ${catalog}             catalog name (settings.yml: catalog)
--   ${schema}              schema name  (settings.yml: schema)
--   ${builder_sp}          application ID of the builder service principal
--   ${promoter_sp}         application ID of the promoter service principal
--   ${consumers_group}     group whose compute installs releases (start with the pilot team)
--   ${security_group}      group that approves releases and exceptions
--
-- Volume privileges apply to a whole volume, not to one folder. That is why
-- candidates (staging) and approved releases (wheelhouse) are separate volumes.

CREATE SCHEMA IF NOT EXISTS ${catalog}.${schema}
  COMMENT 'Secure Python library distribution: volumes and registry tables';

CREATE VOLUME IF NOT EXISTS ${catalog}.${schema}.staging
  COMMENT 'Candidate releases. No workload access.';
CREATE VOLUME IF NOT EXISTS ${catalog}.${schema}.wheelhouse
  COMMENT 'Approved releases. Workloads read only. Only the promoter writes.';
CREATE VOLUME IF NOT EXISTS ${catalog}.${schema}.tools
  COMMENT 'Pipeline tools placed by Ops (JFrog CLI binary).';

-- Builder: reads JFrog, writes candidates. No write access to the wheelhouse.
GRANT USE CATALOG ON CATALOG ${catalog} TO `${builder_sp}`;
GRANT USE SCHEMA  ON SCHEMA  ${catalog}.${schema} TO `${builder_sp}`;
GRANT READ VOLUME, WRITE VOLUME ON VOLUME ${catalog}.${schema}.staging    TO `${builder_sp}`;
GRANT READ VOLUME               ON VOLUME ${catalog}.${schema}.wheelhouse TO `${builder_sp}`;
GRANT READ VOLUME               ON VOLUME ${catalog}.${schema}.tools      TO `${builder_sp}`;

-- Promoter: reads candidates, writes approved releases.
GRANT USE CATALOG ON CATALOG ${catalog} TO `${promoter_sp}`;
GRANT USE SCHEMA  ON SCHEMA  ${catalog}.${schema} TO `${promoter_sp}`;
GRANT READ VOLUME               ON VOLUME ${catalog}.${schema}.staging    TO `${promoter_sp}`;
GRANT READ VOLUME, WRITE VOLUME ON VOLUME ${catalog}.${schema}.wheelhouse TO `${promoter_sp}`;

-- Workloads: read approved releases only.
GRANT USE CATALOG ON CATALOG ${catalog} TO `${consumers_group}`;
GRANT USE SCHEMA  ON SCHEMA  ${catalog}.${schema} TO `${consumers_group}`;
GRANT READ VOLUME ON VOLUME ${catalog}.${schema}.wheelhouse TO `${consumers_group}`;

-- Security approvers: read candidates and their evidence.
GRANT USE CATALOG ON CATALOG ${catalog} TO `${security_group}`;
GRANT USE SCHEMA  ON SCHEMA  ${catalog}.${schema} TO `${security_group}`;
GRANT READ VOLUME ON VOLUME ${catalog}.${schema}.staging    TO `${security_group}`;
GRANT READ VOLUME ON VOLUME ${catalog}.${schema}.wheelhouse TO `${security_group}`;
