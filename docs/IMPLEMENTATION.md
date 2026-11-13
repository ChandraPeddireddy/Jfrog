# Implementation guide

This is the master document for Ops. It takes the pipeline from an empty workspace to the full
design, in eleven stages. Each stage says:

- **Do**: the steps, with links to the detailed procedure;
- **Check**: what proves the stage worked;
- **If the result is different**: the code or configuration change to make, and where.

The third part matters. The code was written from public documentation and tested on a developer
machine. It has never run against your Databricks workspace, your JFrog Artifactory or your Xray.
Some behaviour can only be learned by running it. This guide lists every such point that is known,
and the change each outcome needs.

## How to use this guide

1. Work through the stages in order. Do not switch on a later stage's automation early.
2. At every **Check**, write down the date, what you ran and what happened, in the results log
   at the end of this file.
3. When a result matches a row under **If the result is different**, make that change in a pull
   request, add or update the unit test named in the row, run `python -m pytest`, and redeploy
   with `databricks bundle deploy -t <target>`.
4. A result that matches no row is new information. Stop, record it, and decide before going on.

## Stages at a glance

| Stage | Goal | Automation switched on | Proposal phase |
|---|---|---|---|
| 0 | People, access and decisions are in place | none | |
| 1 | The JFrog behaviour is known (POC test P0) | none | 1 |
| 2 | The pipeline is deployed and builds a candidate | none | 1 |
| 3 | A release is installed on pilot compute | none | 1 |
| 4 | New findings are detected and tracked | `libdist-reassess` schedule | 1 |
| 5 | Rollback and revocation are proven | none | 2 |
| 6 | Releases move without a person starting each step | `libdist-promote`, `rollout.auto`, `approval_mode` | 2 |
| 7 | Fix versions inside the Curation window get a waiver | `waiver_mode` | 2 |
| 8 | Serverless compute uses the wheelhouse | serverless waves | 2 |
| 9 | Existing notebooks and jobs are moved | `libdist-discover`, `pip_redirect`, egress rules | 2 to 3 |
| 10 | More channels, workspaces and metastores | `libdist-cleanup`, spoke sites | 3 |
| 11 | Handover to steady-state operation | `on_grace_expiry` (optional) | 3 |

Everything that changes behaviour is a switch in `config/policy.yml` or a channel's `channel.yml`.
All switches ship in the **off** position, which is the phase 1 behaviour: manual approval, manual
waves, no waiver requests, no automatic revocation.

---

## Stage 0. Prepare

**Do**

1. Collect the people, access and tools listed at the top of [DEPLOYMENT.md](DEPLOYMENT.md).
2. Ask Security for first answers to decisions D1, D2, D7 and D10 (proposed solution, section 14).
   Phase 1 does not need them final, but stages 6, 7 and 11 do.
3. Ask the JFrog admin three questions now, because the answers can take time:
   - Does the Curation policy allow waiver requests (`waiver_request_config` of `manual` or `auto_approved`)?
   - Which Xray Watches should the pipeline scan against?
   - Can the build cluster reach `releases.jfrog.io`, or is there an Artifactory repository that proxies it?

**Check:** a named person for each role, and a non-production workspace to work in.

---

## Stage 1. Learn how JFrog behaves (POC test P0)

Do this before deploying anything. It takes an hour and decides several code details.

**Do**

On a machine that can reach JFrog, with `PIP_INDEX_URL` and `UV_DEFAULT_INDEX` set to the
authenticated index URL:

```bash
python examples/p0_reproduce.py --package certifi --old-version <a version Xray should block> \
    --range ">=2026.1,<2027" --parent "requests>=2,<3"
```

Keep the full output, including the error text of every failed case. Also save the body of one
403 response for a Curation block and one for an Xray block:

```bash
curl -s -u "<user>:<token>" -o /dev/null -w "%{http_code}\n" "<url of a blocked wheel>"
curl -s -u "<user>:<token>" "<url of a blocked wheel>" | head -c 2000
```

**Check:** you can state, for each requirement style and each client, whether it fails and with which message.

**If the result is different**

| What you see | Change | Where |
|---|---|---|
| The error text for a refused file does not contain `403` next to the file's URL | Add the pattern you see | `find_blocked` in `src/libdist/resolve.py`. Add the captured text as a case in `test_find_blocked_in_pip_output` or `test_find_blocked_in_uv_output_and_whole_package` (`tests/test_units.py`). |
| The 403 response body names the blocking control with other words than "curation" or "xray" | Match the words you see | `make_block_classifier` in `src/libdist/resolve.py` |
| The 403 body says nothing useful | Leave the classifier returning `unknown`. Stage 7 then needs the alternative in its table. | |
| JFrog does not return 403 for a young version but leaves it out of the index page | No change for building: the resolver simply picks an older version. Stage 7 needs the alternative in its table, because the pipeline never sees the blocked fix. | |
| The whole index page of a package is refused (`.../simple/<package>/` returns 403) | The pipeline reports `NO_SAFE_VERSION` for that package. Nothing can be excluded. This is a JFrog policy matter. | |
| A bare install succeeds and returns an allowed version | The problem is smaller than the JFrog overview states. Tell the design owners. No code change. | |
| uv fails with an error that pip does not show for the same requirement | Replace `uv pip compile` with `pip-compile` from pip-tools | `resolve` in `src/libdist/resolve.py`: the `compile_cmd` list. The options `--generate-hashes`, `-c`, `-o` and `--upgrade-package` are the same in pip-tools. Set `uv_path` to the pip-tools command. |
| The index pages carry no hashes, so the lock has pins without `--hash` | Preferred: enable hash metadata on the Artifactory repository. Otherwise compute the hashes from the downloaded wheels | `resolve`: after `pip download`, rewrite the lock from `sha256_file` of each wheel, and drop `--require-hashes` from `download_cmd` only. |

---

## Stage 2. Deploy and build the first candidate

**Do:** [DEPLOYMENT.md](DEPLOYMENT.md) steps 1 to 8a.

**Check:** `libdist-build` ends with `"status": "READY_FOR_APPROVAL"`, and the staging folder holds
`requirements.lock`, `wheels/`, `manifest.json`, `security-findings.json` and `test-results.json`.

**Then prove the scanner sees vulnerabilities.** A clean result means nothing until this passes:
add a library version that Security knows is vulnerable to a test channel's `channel.in`
(pinned with `==`), and build it. The build must end `QUARANTINED` and name the finding.

**If the result is different**

*Bundle*

| What you see | Change | Where |
|---|---|---|
| `bundle validate` rejects the `&pipeline_cluster` / `*pipeline_cluster` YAML anchor | Copy the `new_cluster` block into every job | `resources/jobs.yml` |
| `bundle validate` rejects `run_as` inside a job | Move to one `run_as` per target and deploy two bundles, one per service principal, or run all jobs as one identity during the pilot | `databricks.yml`, `resources/jobs.yml` |
| The task fails with `No module named 'libdist'` | `__file__` is not set for the task. Pass the source folder explicitly | `src/run.py`: replace the `HERE` line with `HERE = os.path.join(os.path.dirname(<config dir argument>), "src")`, taking the path that follows `--config-dir` in `sys.argv` |
| The cluster does not start for the service principal | Grant cluster creation, or add `policy_id` to the cluster block | `resources/jobs.yml` |

*Credentials and tools*

| What you see | Change | Where |
|---|---|---|
| `dbutils.secrets.get` is not available to the task | Read the secret through the API | `_jfrog_credentials` in `src/libdist/cli.py`: use `WorkspaceClient().secrets.get_secret(scope, key).value` and base64-decode it |
| `jf config add` rejects `--access-token-stdin` | Pass the token in the environment | `JfScanner.configure` in `src/libdist/scan.py`: drop the flag and `input_text`, add `JF_ACCESS_TOKEN` to `self.env`, or use `--access-token=<token>` (it is redacted from the logs) |
| The first scan fails while downloading the indexer | Set `jfrog.releases_repo` to the Artifactory repository that proxies `releases.jfrog.io` | `config/settings.yml` |
| uv cannot be installed from JFrog (it is itself blocked by Curation) | Place a `uv` binary in the tools volume | `config/settings.yml`: `uv_path`. Copy it to local disk first, as `_scanner` does for `jf`. |

*Scanning*

| What you see | Change | Where |
|---|---|---|
| The JSON has other key names than `vulnerabilities`, `securityViolations`, `severity`, `impactedPackageName`, `impactedPackageVersion`, `fixedVersions`, `cves[].id`, `issueId` | Map the names you see | `parse_simple_json` and `_row_to_finding` in `src/libdist/scan.py`. Replace the `SIMPLE_JSON` fixture in `tests/test_units.py` with a real captured output. |
| `jf scan` exits non-zero when it finds vulnerabilities, even with `--fail=false` | Treat that exit code as success when the output is valid JSON | `JfScanner.scan_file`: check the JSON first, then the exit code |
| The known-vulnerable wheel is **not** reported | The on-demand scan does not identify wheel files in your setup. Scan through Artifactory instead: upload candidate wheels to a local repository that Xray indexes, and read the result from the Xray summary API | Write a second scanner class with the same `scan_release(wheels_dir)` method and select it in `_scanner` (`src/libdist/cli.py`). The gate, build and reassess code do not change. |
| One scan per wheel is too slow | Scan the folder once and attribute findings by component name | `JfScanner.scan_release`: one `jf scan "<dir>/*.whl"`, then match `Finding.component` to the wheels. Keep the rule that a wheel with no result is `FAILED`. |
| The Watch scan returns nothing although violations exist | Check the Watch names; the CLI accepts only one of `--watches`, `--project`, `--repo-path` | `config/settings.yml`: `jfrog.watches`; `JfScanner.scan_violations` |

*Registry and files*

| What you see | Change | Where |
|---|---|---|
| `spark.sql(..., args=...)` is not supported | Use Databricks Runtime 13.3 LTS or later for the pipeline cluster | `databricks.yml`: `spark_version` |
| Writing to the staging volume fails with an "operation not supported" error | A file operation the volume does not support is still in use. Report which call failed | `copy_tree` in `src/libdist/util.py` and `publish` in `src/libdist/publish.py` copy bytes only; check the failing line |
| The import test fails for modules that are not meant to be imported | List them | `skip_imports` in the channel's `channel.yml` |
| The import test fails because the release replaces a runtime package | Hold that package at the runtime's version | the channel's `constraints.txt` |
| A package has no wheel for the runtime | The build fails by design. Build the wheel in a separately secured process and publish it to an internal repository | not in this pipeline |

---

## Stage 3. First release on pilot compute (POC tests P1, P3)

**Do:** [DEPLOYMENT.md](DEPLOYMENT.md) steps 8b to 10.

**Check:** `examples/consumer_check.py` reports `mismatches: 0` on a cluster that uses the pilot policy,
and the pilot team's job runs.

**Then run P3:** copy a release folder, change one byte of one wheel in the copy, point a test
policy at the copy's `install.txt` and start a cluster. The library install must fail.

**If the result is different**

| What you see | Change | Where |
|---|---|---|
| The library install fails because pip options are not accepted in `install.txt` | Point the policy at each wheel file instead: one `{"whl": "<path>"}` entry per wheel | `repoint_libraries` and `release_pattern` in `src/libdist/references.py`: match and replace the set of `whl` entries under `releases/<release_id>/wheels/`. A policy holds at most 500 libraries, so keep channels under that. Hash checking at install is lost; keep `verify` in the switch (it is already there). |
| The install succeeds but P3 does not fail (hashes are not checked) | The options are being ignored. Same change as the row above, and tell Security that integrity then rests on the volume permissions and the `verify` step. | |
| The cluster cannot read the volume | Grant `READ VOLUME` to the identity the cluster runs as, and check the access mode supports volume libraries | `sql/01_setup_uc.sql` |
| The policy edit call is rejected | Read the error: it names the field. Remove or add that field | `_EDITABLE` in `src/libdist/references.py` |
| A running all-purpose cluster does not change | Expected. It changes at restart. | |
| Packages installed by the release clash with runtime packages at import | Hold them at the runtime's version | the channel's `constraints.txt` |

---

## Stage 4. Detect new findings

**Do**

1. Security confirms `grace_hours` in `config/policy.yml` and sets `grace_values_approved: true`.
   The rule: each value must be longer than the time it takes to get a replacement release.
2. Unpause `libdist-reassess`. Create the alerts from `sql/monitoring_queries.sql`.
3. Exercise it: build and publish a release that contains a version with a *Low* finding, switch
   the pilot policy to it, then add `Low` to `block_severities` and run `libdist-reassess`.
   Afterwards remove `Low` again.

**Check:** `dependency_exceptions` has one `GRACE` row with a deadline, alert A2 fires, and a
`libdist-build` run was started with `upgrade_package` set.

**If the result is different**

| What you see | Change | Where |
|---|---|---|
| The replacement build is not started | The builder may not run the build job. Grant it `CAN_MANAGE_RUN` on `libdist-build` | job permissions in the workspace, or `permissions:` in `resources/jobs.yml` |
| `run_now` rejects `job_parameters` | Your workspace uses the older parameter style | `cmd_reassess` in `src/libdist/cli.py`: pass `python_params` instead |
| A rescan takes longer than 4 hours | Raise `scan_threads`, or lengthen the schedule | `config/settings.yml`; `resources/jobs.yml` |
| The same finding opens a second grace period | The finding's ID is not stable between scans. Use the CVE as the key | `_row_to_finding` in `src/libdist/scan.py`: prefer `cves[0]` over `issueId` |

---

## Stage 5. Rollback and revocation exercise (POC tests P6, P8)

**Do**

1. Publish a second release and switch the pilot wave to it. Run `libdist-rollback`.
2. Run `libdist-revoke` on a test release that a test policy points at. Confirm alert A4 fires,
   then switch the policy to an eligible release.
3. **P8:** start a job cluster with autoscaling on release N-1, switch the policy to N while it
   runs, force a scale-up, and read the new node's library status.

**Check:** after the rollback the policy points at the first release; the revoked release cannot
be switched to (`not eligible`); new nodes in P8 install N-1.

**If the result is different**

| What you see | Change | Where |
|---|---|---|
| **P8 fails:** a node added after the switch installs release N while the driver has N-1 | Editing a policy in place is not safe for running clusters. Switch by policy instead: create one policy per release and move jobs to the new policy at their next run | This is a design change, not a small fix. `switch` in `src/libdist/promote.py` would create a policy named `<channel>-<release_id>` and update the jobs' `policy_id`. Until then, avoid autoscaling on pilot job clusters, or switch only between runs. |
| Rollback says `no previous release is recorded` | The policy was switched by hand, outside the pipeline. Use `libdist-switch` with the release you want | |

---

## Stage 6. Switch on zero-touch promotion

Until now a person approved, published and switched. This stage hands those steps to the
`libdist-promote` job, one switch at a time.

**Do**

1. Run `sql/03_registry_grants.sql` again if you skipped the `system.lakeflow` grants.
2. In each channel's `channel.yml`, fill `job_ids` for the canary wave with jobs that run on the
   canary policy at least a few times a day.
3. Unpause `libdist-promote` with everything still off. It now only publishes releases that
   Security approved. Let it run for a few days.
4. Set `rollout.auto: true` for one channel. The job now starts the canary wave after a publish
   and moves to the next wave after `soak_hours` if the failure rate did not rise.
5. When Security agrees (decision D2), set `approval_mode` in `config/policy.yml`:
   `security_rebuilds` first, `automatic` later if wanted.

**Check:** a release goes from `READY_FOR_APPROVAL` to all waves with no manual job run, and
`dependency_rollouts` shows `COMPLETED`. Then publish a release whose import of a canary job's
library is broken on purpose in a test channel, and confirm the rollout ends `ROLLED_BACK`.

**If the result is different**

| What you see | Change | Where |
|---|---|---|
| The health query fails on a column name | Match your system table's columns | `failure_rate_sql` in `src/libdist/health.py`; query H1 in `sql/monitoring_queries.sql` |
| The health query returns no rows for job IDs you know ran | System tables can lag by hours. Lengthen `soak_hours`, or the IDs are from another workspace | the channel's `channel.yml` |
| Rollouts sit in `HELD` | The canary jobs run too rarely. Lower `min_runs`, pick busier canary jobs, or set `on_insufficient_data: proceed` | the channel's `channel.yml` |
| A rollout rolled back although the jobs fail for unrelated reasons | Raise `max_increase`, or choose steadier canary jobs | the channel's `channel.yml` |
| A newer release waits while an older rollout is still soaking | By design only one rollout runs per channel. For an urgent release, roll the running one back or finish it by hand with `libdist-switch`, then the job picks up the newer one | [RUNBOOK.md](RUNBOOK.md) |

---

## Stage 7. Curation waivers for fix versions (POC test P12)

**Do**

1. Confirm with Security how waiver requests from the pipeline are approved (decision D10) and
   that the Curation policy allows them.
2. Request one by hand:
   `python src/run.py --config-dir config waiver --package <p> --version <blocked version> --reason "POC test P12"`.
3. After it is approved, run `libdist-build` with `upgrade_package=<p>` and confirm the version is used.
4. Set `waiver_mode: request` in `config/policy.yml`.

**Check:** a security rebuild whose fix is inside the Curation window ends `FAILED` or
`QUARANTINED`, records `WAIVER_REQUESTED`, and a later run of `libdist-reassess` rebuilds
successfully once the waiver is approved. No second request is made for the same version.

**If the result is different**

| What you see | Change | Where |
|---|---|---|
| The request is rejected with a field error | Match the field names your JFrog version expects | `waiver_body` in `src/libdist/waiver.py`, and `test_waiver_body_matches_the_jfrog_api` |
| The request needs basic auth, not a bearer token | Switch the header | `request_waiver` in `src/libdist/waiver.py`: use `auth=(user, token)` |
| The block reason is always `unknown` (stage 1), or blocked versions are hidden instead of refused | The build cannot tell that a fix is being held. Take the candidate from the finding instead: request a waiver for the lowest version in the finding's `fixed_versions` that the resolver did not pick | `_request_waivers` in `src/libdist/build.py`: read `dependency_findings.fixed_versions` for the open `GRACE` exceptions of the channel, instead of the `blocked` list |
| A waiver is approved but the build still gets 403 | The waiver may apply to another repository key. Check `jfrog.curated_repo` | `config/settings.yml` |
| A waiver was rejected in JFrog | The pipeline does not read waiver status. Close the row by hand so alert A10 clears: `UPDATE dependency_exceptions SET status = 'CLOSED' WHERE kind = 'WAIVER' AND ...` | |

---

## Stage 8. Serverless compute (POC tests P2, P9)

**Do**

1. Publish a release for a channel whose `runtime_profile` is a serverless environment version.
   Build it on a cluster whose Python version matches that environment.
2. Create the base environment for the canary wave:
   `python src/run.py --config-dir config serverless-init --release-id <id> --wave canary`
   (as the promoter). It writes `<wheelhouse>/environments/<channel>-<wave>.yml` and registers it.
3. Add the printed name to `base_environments` of that wave in `channel.yml` and redeploy.
4. Attach a serverless notebook to the base environment and run the first cell of
   `examples/consumer_check.py`.
5. **P9:** switch the wave to a second release while a serverless job is running.

**Check:** the notebook reports `mismatches: 0`; after a switch, new sessions get the new release.

**If the result is different**

| What you see | Change | Where |
|---|---|---|
| The API returns 404 or "not enabled" | The base environment API is not available in your workspace. Create the environment in the UI (Settings > Compute > Base environments) from the YAML file, and refresh it there after each switch | `switch` in `src/libdist/promote.py` still rewrites the YAML; replace the `environments.refresh(name)` call with a notification to Ops |
| The environment build fails on the pip options | List the wheels directly | `render_environment_yaml` in `src/libdist/references.py`: write one line per wheel path instead of the five option lines, and adapt `release_in_environment_yaml`; update `test_environment_yaml_round_trip` |
| The build cannot read the volume | Serverless reads volumes as the user. Grant `READ VOLUME` to the users of the environment | `sql/01_setup_uc.sql` |
| **P9:** a running job changes libraries mid-run | Do not refresh while jobs run: give serverless waves a quiet window, or use one base environment per release (limit: 10 per workspace) | the channel's waves; `cmd_serverless_init` |
| Jobs cannot select a workspace base environment | Job support is in Beta. Reference the YAML file path in the job's environment instead | job definitions; no pipeline change |
| Users install other packages in the Environment panel | Expected: serverless has no compute policy. Only network egress control stops it | stage 9 |

---

## Stage 9. Move existing notebooks and jobs (POC tests P7, P11)

**Do**

1. Run `libdist-discover`, with `notebook_root` set if you want `%pip` lines included. Give the
   builder read access to the jobs, clusters and folders it should see.
2. Run the migration report and read dashboard queries D8 and D9:
   `python src/run.py --config-dir config migration-report --channel <channel>`.
3. Fix what it lists: add missing libraries to `channel.in`; ask owners to remove pins that conflict.
4. **P11:** set `pip_redirect: true` for a test channel, switch a test policy, and run a notebook
   with existing `%pip install` lines on it.
5. Attach compute to the channel's policies, team by team.
6. Last, add the network rules that stop run-time installs, and run the last cell of
   `examples/consumer_check.py` (P7).

**Check:** on migrated compute, existing `%pip install` lines either report "already satisfied"
or fail with "no matching distribution", and nothing reaches JFrog.

**If the result is different**

| What you see | Change | Where |
|---|---|---|
| Discovery misses jobs or clusters | The builder sees only what it may view. Run discovery as an identity with wider read access | `run_as` of `libdist_discover` in `resources/jobs.yml` |
| A discovery API call fails on a field or a path | Match your API version | `discover` in `src/libdist/discover.py` |
| **P11:** `%pip` ignores the environment variables | Notebook-scoped installs may not inherit them in your runtime. Use a cluster-scoped init script that writes `/etc/pip.conf` with `no-index` and `find-links`, managed by the policy | `with_pip_redirect` in `src/libdist/references.py` would set `init_scripts` instead of `spark_env_vars` |
| **P11:** the policy's own library install breaks when `PIP_NO_INDEX` is set | Other libraries on the policy still need an index. Move them into the channel, then retry | the channel's `channel.in` |
| Changing the policy definition restarts nothing but confuses users | Expected: environment variables apply at the next cluster start | |
| The migration report marks many lines `UNPARSED` | They are paths, URLs or shell syntax. Review them by hand | dashboard query D9 |

---

## Stage 10. More channels, workspaces and metastores

**Do**

*More channels.* Copy `config/channels/ds-basics/` to a new folder, edit the three files, redeploy,
and run stages 2 and 3 for it. One channel serves one runtime. Serverless allows 10 base
environments per workspace, so prefer a few shared channels.

*More workspaces on the same metastore.* Bind the catalog to the workspace. Then, for each
additional workspace:

1. Copy `config/` to `config-<site>/`. In its `settings.yml` set `site: <site>` and `role: spoke`.
   In its channels, put that workspace's policy IDs and job IDs.
2. Add a target in `databricks.yml` with that workspace's host and `config_dir: config-<site>`.
3. Deploy. In a spoke, unpause only `libdist-promote`. Leave `libdist-build`, `libdist-reassess`
   and `libdist-cleanup` paused: the hub runs them.

*Another metastore or region.* Copy published release folders to storage both sides can read
(for example an external volume), then on the receiving side run
`python src/run.py --config-dir config import-release --source-path <path>`. The receiving side
needs the channel configured with the same `runtime_profile`, and its own `libdist-reassess`.

*Clean-up.* Run `libdist-cleanup` and read its report. When the list is right, run it with
`apply=true`, then unpause the weekly schedule and set its `apply` parameter default to `true`.

**Check:** dashboard query D1 shows every site; a release published in the hub reaches a spoke's
compute; `libdist-cleanup` deletes only releases that no site references.

**If the result is different**

| What you see | Change | Where |
|---|---|---|
| A spoke cannot read the registry or the wheelhouse | Bind the catalog to that workspace and repeat the grants for its promoter | Catalog Explorer; `sql/01_setup_uc.sql`, `sql/03_registry_grants.sql` |
| Two workspaces publish the same release | Two sites have `role: hub`. Exactly one may | the spoke's `settings.yml` |
| Deleting from the volume fails | The promoter needs `WRITE VOLUME` on staging to delete candidates | `sql/01_setup_uc.sql`: add the grant, or run the staging part as the builder |
| The load test (P10) shows slow installs with many clusters starting at once | Record the numbers first. If volume reads are the limit, split large channels, or place the wheelhouse on storage in the same region as the compute | channel layout; storage location of the volume |

---

## Stage 11. Handover

**Do**

1. Decide `on_grace_expiry` with Security (decision D7). `alert` keeps a person in the loop;
   `revoke` marks the release revoked at the deadline.
2. Hand [RUNBOOK.md](RUNBOOK.md) to the on-call team and walk through each alert once.
3. Agree who reviews pull requests to `config/policy.yml` (Security) and to `channel.in` files (channel owners).
4. Update [STATUS.md](STATUS.md): move every item you proved from "not tested" to "proven", with the date.

**Check:** every row of the results log below is filled.

---

## Automation switches

| Switch | File | Values | Stage |
|---|---|---|---|
| `grace_values_approved` | `config/policy.yml` | `false`, `true` | 4 |
| `libdist-reassess` schedule | job | paused, unpaused | 4 |
| `libdist-promote` schedule | job | paused, unpaused | 6 |
| `rollout.auto` | `channel.yml` | `false`, `true` | 6 |
| `approval_mode` | `config/policy.yml` | `manual`, `security_rebuilds`, `automatic` | 6 |
| `waiver_mode` | `config/policy.yml` | `"off"`, `request` | 7 |
| `base_environments` in a wave | `channel.yml` | list of names | 8 |
| `libdist-discover` schedule | job | paused, unpaused | 9 |
| `pip_redirect` | `channel.yml` | `false`, `true` | 9 |
| `site`, `role` | `settings.yml` | name; `hub`, `spoke` | 10 |
| `libdist-cleanup` schedule and `apply` | job | paused, unpaused; `false`, `true` | 10 |
| `on_grace_expiry` | `config/policy.yml` | `alert`, `revoke` | 11 |

## Results log

Fill one row per check. Keep this file in Git.

| Stage | Check or POC test | Date | Environment | Result | Code change made (pull request) |
|---|---|---|---|---|---|
| 1 | P0: requirement styles, pip and uv | | | | |
| 1 | 403 response bodies captured | | | | |
| 2 | Bundle validates and deploys | | | | |
| 2 | First build reaches `READY_FOR_APPROVAL` | | | | |
| 2 | Known-vulnerable wheel is quarantined | | | | |
| 3 | P1: `mismatches: 0` on pilot compute | | | | |
| 3 | P3: modified wheel fails the install | | | | |
| 4 | New finding opens a grace period and starts a build | | | | |
| 5 | P6: rollback | | | | |
| 5 | Revocation exercise | | | | |
| 5 | P8: autoscaling after a switch | | | | |
| 6 | Zero-touch rollout completes | | | | |
| 6 | Broken release is rolled back | | | | |
| 7 | P12: waiver request and rebuild | | | | |
| 8 | P2: serverless install | | | | |
| 8 | P9: serverless switch during a run | | | | |
| 9 | P11: existing `%pip` lines | | | | |
| 9 | P7: run-time installs blocked | | | | |
| 10 | Second site receives a release | | | | |
| 10 | P10: load test | | | | |
| 10 | P4, P5: blocked-version exclusion, `--exclude-newer` | | | | |
