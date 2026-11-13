# Status: what is built, what is tested, what is proven

Read this before planning the work. It is the honest state of the code in this repository.

## Summary

- The code covers **all three phases** of the proposed solution. Every automation is a switch
  that ships off.
- **83 automated tests pass** on a developer machine.
- **Nothing has been run against a real Databricks workspace, JFrog Artifactory or Xray.**
  Every part that talks to those systems is written from public documentation. Some of it will
  need changes when it first runs. [IMPLEMENTATION.md](IMPLEMENTATION.md) lists the known points
  and the change each outcome needs.

So "full code" here means: every feature of the design has code and tests for its logic. It does
not mean "proven in your environment". That proof is the eleven stages in IMPLEMENTATION.md.

## What is built

| Capability (proposal section) | Code | Switch |
|---|---|---|
| Resolve with automatic exclusion of blocked versions (6, component 2) | `resolve.py` | always on |
| Smallest-change security rebuild (8) | `resolve.py`, `build.py` | `upgrade_package` |
| Xray scan, coverage check, security gate (6, component 3) | `scan.py`, `gate.py` | always on |
| Offline install and import test, team smoke tests (6, component 4) | `tester.py` | always on |
| Publish with hash verification, releases never overwritten (6, component 5) | `publish.py` | always on |
| Switch, rollback with eligibility rule, revoke (8, 9) | `promote.py` | manual jobs |
| Classic compute policies (6, component 6) | `references.py` | `policy_ids` |
| Serverless base environments (6, component 6) | `references.py`, `promote.py` | `base_environments` |
| Rescan every 4 hours, grace periods (7) | `reassess.py` | schedule |
| Replacement builds, retried until one is on the way (7) | `reassess.py` | schedule |
| Curation waiver request for a fix that is too new (7, decision D10) | `waiver.py`, `build.py` | `waiver_mode` |
| Accepting a finding with no fix for a limited time (7, outcome C) | `build.py`, `gate.py` | exception row by Security |
| Revoke at the grace deadline (decision D7) | `reassess.py` | `on_grace_expiry` |
| Approval by rules (decision D2) | `orchestrate.py` | `approval_mode` |
| Progressive rollout with canary health check and automatic rollback (8) | `orchestrate.py`, `health.py` | `rollout.auto` |
| Shorter canary for security rebuilds (decision D8) | `orchestrate.py` | `urgent_soak_hours` |
| Discovery of current library use, per workspace (6, component 1) | `discover.py` | schedule |
| Migration report for existing requirements (10) | `migrate.py` | command |
| Existing `%pip` lines resolve from the release (10) | `references.py` | `pip_redirect` |
| Start-up gate for jobs that need a hard stop (decision D12) | `examples/startup_gate.py`, `sql/04_eligible_releases_view.sql` | opt-in per job |
| Retention and clean-up (8, decision D9) | `cleanup.py` | schedule, `apply` |
| Several workspaces on one metastore (6, component 8) | `registry.py` (site), `orchestrate.py` (role) | `site`, `role` |
| Another metastore or region (6, component 8) | `transfer.py` | `import-release` command |
| Registry, audit events, alert and dashboard queries (6, component 8) | `registry.py`, `sql/` | always on |

## Tested locally

The tests run the real resolver (uv) and the real downloader and installer (pip) against small
hand-made wheels. JFrog, Xray, the compute policy API, the base environment API and the system
tables are **fakes**. A 403 is simulated by returning pip's error text.

| Behaviour | Test file |
|---|---|
| Lock parsing, hashes, release diff, `install.txt` | `tests/test_units.py` |
| 403 detection in pip and uv output | `tests/test_units.py` |
| Scan output parsing; a failed scan is never a pass | `tests/test_units.py` |
| Gate: blocked severity fails, unknown severity holds, missing coverage holds | `tests/test_units.py` |
| Resolve, exclusion and retry, no safe version, one-package rebuild | `tests/test_pipeline.py` |
| Offline install; a modified wheel fails | `tests/test_pipeline.py` |
| Build, publish, verify, switch, rollback, refused rollback, revoke | `tests/test_pipeline.py` |
| Grace period opened once, expired, extended by Security; a finding that becomes blocking later | `tests/test_pipeline.py` |
| Zero-touch flow from candidate to all waves; soak times; unhealthy canary rolled back and not retried | `tests/test_automation.py` |
| Too few canary runs: hold or proceed | `tests/test_automation.py` |
| Approval modes | `tests/test_automation.py` |
| Serverless switch; YAML restored when the refresh fails; YAML outside the wheelhouse refused | `tests/test_automation.py` |
| Waiver requested once, for the newest Curation-blocked fix, only for security rebuilds | `tests/test_automation.py` |
| Replacement build retried until a candidate exists; revoke at deadline only when configured | `tests/test_automation.py` |
| Clean-up deletes only unused releases past retention | `tests/test_automation.py` |
| Import verifies hashes and refuses a tampered source | `tests/test_automation.py` |
| Migration report; per-workspace snapshots; sites keep their own references and rollouts | `tests/test_automation.py` |

## Not proven: needs your environment

Each row is covered by a stage in IMPLEMENTATION.md, with the change to make if it behaves differently.

| Area | Code | Risk | Stage |
|---|---|---|---|
| Wording of JFrog's 403 errors and response bodies | `resolve.find_blocked`, `resolve.make_block_classifier` | Medium | 1 |
| Whether JFrog refuses or hides a blocked version | `resolve.py`, `build._request_waivers` | Medium. Changes how a held fix is detected. | 1, 7 |
| Bundle definition (`bundle validate` was not run) | `databricks.yml`, `resources/jobs.yml` | Low to medium | 2 |
| JFrog CLI flags and `simple-json` field names | `scan.JfScanner`, `scan.parse_simple_json` | Medium. The test fixture is illustrative, not a captured scan. | 2 |
| That the on-demand scan identifies wheel files | `scan.py` | **High.** If it does not, a different scanner class is needed. | 2 |
| Delta reads and writes with named SQL parameters | `registry.SparkRegistry` | Low to medium | 2 |
| File operations on Unity Catalog volumes | `util.copy_tree`, `publish.py`, `cleanup.py` | Low to medium | 2 |
| Compute policy accepts `install.txt` with pip options | `lockfile.render_install_txt` | **High.** The classic install path depends on it. | 3 |
| Compute policy get and edit calls | `references.PolicyClient` | Low to medium | 3 |
| A cluster keeps its release when the policy changes during a run | design | **High.** If not, switching must move jobs between policies. | 5 |
| `system.lakeflow.job_run_timeline` query and its delay | `health.py` | Medium | 6 |
| Curation waiver request body and authentication | `waiver.request_waiver` | Low to medium | 7 |
| Base environment API (Public Preview; jobs support in Beta) | `references.BaseEnvironmentClient` | Medium to high | 8 |
| pip options inside the environment YAML | `references.render_environment_yaml` | Medium | 8 |
| Behaviour of running serverless workloads at a refresh | design | Medium | 8 |
| Jobs, clusters and workspace API calls in discovery | `discover.discover` | Medium | 9 |
| `%pip` honours `PIP_NO_INDEX` and `PIP_FIND_LINKS` from the policy | `references.with_pip_redirect` | Medium | 9 |
| Install time when many clusters start at once | design | Unknown | 10 |

## Not built

| Item | Why | What to do instead |
|---|---|---|
| Reading the decision on a waiver request from JFrog | The status API is not documented in the sources used | Alert A10 and the runbook step |
| Moving jobs between per-release policies | Only needed if POC test P8 fails | IMPLEMENTATION stage 5 describes the change |
| Scanning through Artifactory instead of the CLI | Only needed if the on-demand scan does not identify wheels | IMPLEMENTATION stage 2 describes the change |
| Source builds | The customer design requires a separately secured build process | Publish built wheels to an internal repository |
| Dashboard and alert objects as code | Queries are provided in `sql/monitoring_queries.sql` | Create them by hand once |
| Prefect | Outside Databricks | The wheelhouse folder layout can be reused; needs its own design |
| Developer lock file refresh | Happens on developer machines, through JFrog | Developers can use a published `requirements.lock` as a constraints file |
| Copying releases between metastores | Depends on the cloud storage layout | Copy the folder with cloud tools, then `import-release` |

## Known limits to tell Security about

1. **The promoter service principal is a workspace admin.** Editing compute policies and base
   environments requires it. Its job definitions must be protected accordingly.
2. **Approval is recorded, not enforced by privileges.** The builder and promoter can write the
   `dependency_releases` table, so a compromised pipeline identity could mark a release approved.
   Unity Catalog audit logs show every write. A stricter design keeps approvals in a table only
   Security can write.
3. **Approval by rules means no person looks at a release.** `approval_mode` is `manual` until
   Security changes it. In the other modes the gate in `config/policy.yml` is the only control,
   so changes to that file need Security's review.
4. **A grace period lets jobs keep installing a library that Xray now blocks.** This is the
   explicit decision D1. Without it the pipeline cannot protect jobs from new Xray findings.
5. **A waiver request from the pipeline asks JFrog to release a version younger than the Curation
   window.** It is limited to packages named in a security rebuild, and the version is still
   scanned and tested. Whether JFrog approves it automatically is set in the Curation policy.
6. **Revocation does not stop running jobs** and does not uninstall anything from a running process.
   Only jobs that include the start-up gate are stopped at their next start.
7. **A notebook can still run `%pip install` from an index** until network egress rules block it.
   Serverless users can also add packages in the Environment panel.
8. **Grace hours in `config/policy.yml` are examples.** `grace_values_approved` is `false` and the
   reassess job logs a warning until Security sets real values.
9. **An imported release is not rescanned at import.** The receiving side's reassess job scans it
   on its next run.
