# Deployment guide

This guide has the commands for the **first deployment**: from an empty workspace to a first
release installed on a pilot cluster, with manual approval. It is stages 2 to 4 of
[IMPLEMENTATION.md](IMPLEMENTATION.md), which is the master plan and also tells you what to change
in the code when a step behaves differently from what is described here.

Do the steps in order. Each step ends with a check. Use a **non-production workspace** first.

> The code has not been run against a real workspace or a real JFrog instance yet
> (see [STATUS.md](STATUS.md)). Expect to adjust details during the first deployment. When a
> step fails, look up the stage in IMPLEMENTATION.md before changing anything.

## What you need before you start

### People

| Who | Needed for |
|---|---|
| Databricks workspace admin | Service principals, secret scope, compute policy, bundle deployment |
| Metastore admin or catalog owner | Schema, volumes, grants |
| JFrog admin | Service account token, Xray scan permission, CLI binary |
| Security approver | Approving the first release; confirming `config/policy.yml` |
| One application team | A real job and its library list for the pilot |

### Access and tools

| Item | Detail |
|---|---|
| Databricks CLI | A current version of the new CLI (the one with `databricks bundle`), authenticated to the workspace (`databricks auth login`). Check with `databricks bundle --help`. |
| Git | To clone this repository |
| JFrog service account | Read access to the PyPI repository that Curation protects; permission to run Xray on-demand scans |
| JFrog CLI (`jf`) | Linux amd64 binary, the version your JFrog admin approves |
| Network | The build cluster must reach the JFrog host over HTTPS. The enterprise CA must be trusted on the cluster. |

### Decisions to confirm

These come from the proposed solution, section 14. Phase 1 can start with the defaults below.

| Decision | Phase 1 default in this repository | Where it is set |
|---|---|---|
| D1 Grace period per severity | Example values, marked not approved | `config/policy.yml` |
| D2 Promotion mode | Manual approval by Security | `sql/approve_release.sql` |
| D4 Channels | One channel, `ds-basics` | `config/channels/` |
| D11 Severity source | Label `xray` | `config/policy.yml` |
| Blocked severities | Critical and High | `config/policy.yml` |

---

## Step 1. Create the two service principals

Two identities keep "download and build" apart from "publish and switch".

```bash
databricks service-principals create --display-name libdist-builder
databricks service-principals create --display-name libdist-promoter
```

Note the `applicationId` of each. Then:

1. Give the person who deploys the bundle the **Service Principal User** role on both.
2. Give both service principals the **Allow unrestricted cluster creation** entitlement, or a
   compute policy they may use for the single-node pipeline cluster.
3. Make `libdist-promoter` a **workspace admin**. Editing compute policies and managing
   serverless base environments are admin actions. This is a high privilege: Security should
   review it (see [STATUS.md](STATUS.md)).

**Check:** `databricks service-principals list` shows both.

## Step 2. Create the schema, volumes and grants

1. Open `sql/01_setup_uc.sql`. Replace the six placeholders listed in its header.
2. Run it in the SQL editor as a metastore admin or the catalog owner.

This creates three volumes:

| Volume | Holds | Who writes | Who reads |
|---|---|---|---|
| `staging` | Candidate releases | builder | promoter, Security |
| `wheelhouse` | Approved releases | promoter | workloads, builder, Security |
| `tools` | The JFrog CLI binary | Ops | builder |

**Check:** `SHOW VOLUMES IN <catalog>.<schema>` lists `staging`, `wheelhouse` and `tools`.

## Step 3. Store the JFrog credentials

```bash
databricks secrets create-scope libdist
databricks secrets put-secret libdist jfrog-user      # prompts for the value
databricks secrets put-secret libdist jfrog-token     # prompts for the value
databricks secrets put-acl libdist <builder-application-id> READ
```

Only the builder gets `READ`. Do not grant the scope to the promoter or to users.

**Check:** `databricks secrets list-acls libdist` shows the builder with `READ`.

## Step 4. Place the JFrog CLI binary

1. Get the `jf` Linux amd64 binary from your JFrog admin (or from your Artifactory proxy of
   `releases.jfrog.io`).
2. Upload it to the tools volume, as an identity that may write to it (the schema owner can;
   otherwise grant yourself `WRITE VOLUME` on `tools`):

   ```bash
   databricks fs cp ./jf dbfs:/Volumes/<catalog>/<schema>/tools/jf
   ```

The pipeline copies it to local disk and makes it executable at run time.

On its first scan the CLI downloads the Xray indexer (about 100 MB). If the build cluster has no
route to `releases.jfrog.io`, set `jfrog.releases_repo` in `config/settings.yml` to the Artifactory
repository that proxies it.

**Check:** `databricks fs ls dbfs:/Volumes/<catalog>/<schema>/tools/` lists `jf`.

## Step 5. Edit the configuration

All values to change are marked `CHANGE-ME`. Remove each marker as you set the value, so the
check at the end of this step is meaningful.

| File | Change |
|---|---|
| `config/settings.yml` | `catalog`, `schema`, `jfrog.platform_url`, `jfrog.pypi_repo`, `jfrog.curated_repo`, `jfrog.cli_path`, optionally `jfrog.watches` |
| `config/policy.yml` | Confirm `block_severities` with Security. Leave `grace_values_approved: false` until decision D1 is made. |
| `config/channels/ds-basics/channel.in` | The pilot application's libraries: names or ranges, one per line |
| `config/channels/ds-basics/constraints.txt` | Optional limits, for example to keep a library at the runtime's version |
| `config/channels/ds-basics/channel.yml` | `runtime_profile`, `owner`. Leave `policy_ids` empty for now (step 9). |
| `databricks.yml` | For your target: `workspace.host`, `builder_sp`, `promoter_sp`, `notification_email`, `node_type_id`, and `spark_version` if not 15.4 LTS |

`spark_version` must be the runtime the pilot compute uses. A release built on one runtime is
for that runtime only.

Commit the changes to your own Git repository. Do not commit tokens.

**Check:** `grep -rn CHANGE-ME config databricks.yml` shows only the target you are not deploying
and the `policy_ids` line of `channel.yml` (filled in step 9).

## Step 6. Deploy the bundle

```bash
databricks bundle validate -t dev
databricks bundle deploy -t dev
```

This uploads the code and creates eleven jobs (see the README). The four scheduled jobs are created paused.

**Check:** the Jobs page lists `libdist-build`, `libdist-reassess`, `libdist-discover`,
`libdist-promote`, `libdist-publish`, `libdist-switch`, `libdist-rollback`, `libdist-revoke`, `libdist-cleanup`,
`libdist-run-builder` and `libdist-run-promoter`. In `dev` mode the
names carry a `[dev <your name>]` prefix.

## Step 7. Create the registry tables

1. Open `sql/02_registry_tables.sql`, replace `${catalog}` and `${schema}`, and run it as the
   catalog or schema owner. (The command `init-registry` creates the same tables, but the
   service principals do not have the right to create tables, by design.)
2. Open `sql/03_registry_grants.sql`, replace the placeholders, and run it.

3. Optional, for jobs that need a hard stop at start: run `sql/04_eligible_releases_view.sql`.

**Check:** `SHOW TABLES IN <catalog>.<schema>` lists nine `dependency_*` tables.

## Step 8. Build, approve and publish the first release

### 8a. Build

```bash
databricks bundle run -t dev libdist_build --params channel=ds-basics
```

The run takes roughly 10 to 30 minutes, mostly cluster start and scanning. Read the task output:
it ends with a JSON summary.

| Summary `status` | Meaning | Next |
|---|---|---|
| `READY_FOR_APPROVAL` | Resolved, every wheel scanned, gate passed, tests passed | Go to 8b |
| `QUARANTINED` | A blocking finding, or a scan that failed or was incomplete | Read `reason`; see RUNBOOK "A build is quarantined" |
| `FAILED` with `NO_SAFE_VERSION` | Curation and Xray block every usable version of a library | See RUNBOOK "No safe version" |
| `FAILED` (other) | Resolver error, missing wheel, or test failure | Read `reason` |

A task that does not reach `READY_FOR_APPROVAL` is marked failed on purpose, so that the failure email is sent.

**First-run problems to expect**

| Symptom | Likely cause |
|---|---|
| `jf config add failed` | Wrong `platform_url`, wrong token, or the CLI version does not accept `--access-token-stdin` |
| Every wheel has scan status `FAILED` | The CLI could not download the Xray indexer, or the token lacks scan permission |
| `uv pip compile failed` with a TLS error | The enterprise CA is not trusted on the cluster |
| `pins without a sha256 hash` | The Artifactory repository does not serve hashes in its index pages |
| Import test fails for one module | Add it to `skip_imports` in `channel.yml` if it is not meant to be imported directly |

### 8b. Approve (Security)

A security approver opens `sql/approve_release.sql`, replaces the placeholders, reviews the
three result sets, and runs the `UPDATE`. The release status becomes `APPROVED`.

### 8c. Publish

```bash
databricks bundle run -t dev libdist_publish --params release_id=<release_id>
```

**Check:** the folder `/Volumes/<catalog>/<schema>/wheelhouse/releases/<release_id>/` exists and
contains `COMPLETE`. The release status is `PUBLISHED`.

## Step 9. Create the pilot compute policy and switch it

1. Edit `examples/consumer-policy.json`: the policy name and the `spark_version` value.
2. Create the policy and note its `policy_id`:

   ```bash
   databricks cluster-policies create --json @examples/consumer-policy.json
   ```

3. Give the pilot team `CAN_USE` on the policy.
4. Put the policy ID into `config/channels/ds-basics/channel.yml` under the `canary` wave, commit, and redeploy:

   ```bash
   databricks bundle deploy -t dev
   ```

5. Switch the canary wave to the release:

   ```bash
   databricks bundle run -t dev libdist_switch --params release_id=<release_id>,wave=canary
   ```

**Check:** the policy's **Libraries** tab shows one entry: the release's `install.txt`.

## Step 10. Verify on pilot compute

1. Create a cluster with the pilot policy. Confirm on its **Libraries** tab that `install.txt` reaches status *Installed*.
2. Import `examples/consumer_check.py` as a notebook, attach it to the cluster, set the
   `install_txt` widget to the path from step 9, and run it.

The first cell must report `mismatches: 0`. This is POC test P1. If the library fails to install
because the pip options in `install.txt` are not accepted, that is POC test P3 failing: stop and
record the error message.

3. Run the pilot team's real job on the policy.

## Step 11. Turn on the schedules

Only after step 10 passes:

1. Unpause `libdist-reassess` (every 4 hours).
2. Unpause `libdist-discover` (daily), if the builder may list jobs and clusters. It records only
   what the builder service principal is allowed to see.
3. Create SQL alerts from `sql/monitoring_queries.sql` (queries A1 to A7) and send them to the Ops channel.
4. Create a dashboard from queries D1 to D6.

**Check:** after the first `libdist-reassess` run, `dependency_events` has a `RESCAN_COMPLETED` row.

## Step 12. Block run-time installs (last)

This step is for the **pilot compute only**. Blocking run-time installs for everyone else is
stage 9 of IMPLEMENTATION.md, after the migration report. Do this only when the pilot jobs no
longer install anything from JFrog at run time:

1. Add network rules so pilot compute cannot reach the JFrog host or the internet package indexes.
2. Run the last cell of `examples/consumer_check.py`. It must print `index reachable: False`.

This is POC test P7. The pipeline cluster must keep its route to JFrog.

---

## What comes next

You now have the phase 1 pilot. Continue with [IMPLEMENTATION.md](IMPLEMENTATION.md):

| Stage | Adds |
|---|---|
| 5 | Rollback and revocation exercise |
| 6 | `libdist-promote`: automatic publish, rollouts with a health check, approval by rules |
| 7 | Curation waiver requests for fix versions |
| 8 | Serverless compute |
| 9 | Moving existing notebooks and jobs; blocking run-time installs |
| 10 | More channels, workspaces and metastores; clean-up |

## Deploying to production

1. Repeat steps 1 to 4 in the production workspace (or reuse the metastore objects if the
   workspaces share a metastore and the catalog is bound to both).
2. Fill the `prod` target in `databricks.yml`.
3. `databricks bundle deploy -t prod`. In production mode the jobs have no name prefix and the
   schedules stay paused until you unpause them.
4. Repeat steps 7 to 12.

## Updating the pipeline

Change the code or configuration in Git, then run `databricks bundle deploy -t <target>` again.
A configuration change (for example a new line in `channel.in`) takes effect at the next build.

## Removing the pipeline

1. Point the compute policies back to their previous libraries, or remove the `install.txt` entry.
2. `databricks bundle destroy -t <target>` removes the jobs and the uploaded files.
3. The volumes, tables, secret scope and service principals stay until you drop them by hand.
   Keep the registry tables if you need the audit trail.
