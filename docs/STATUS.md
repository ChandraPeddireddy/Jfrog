# Status: what is tested, what is not, what is missing

Read this before planning a deployment. It is the honest state of the code in this repository.

## Summary

- The pipeline logic is implemented for **phase 1** of the proposed solution.
- **54 automated tests pass** on a developer machine.
- **Nothing has been run against a real Databricks workspace, JFrog Artifactory or Xray.**
  Every part that talks to those systems is written from their public documentation and must be
  proven in the POC.

## Tested locally

These run in the test suite with the real tools (uv and pip) against hand-made wheels.

| Behaviour | Test |
|---|---|
| Hidden dependencies are pinned with sha256 hashes | `test_resolve_pins_hidden_dependencies_and_downloads_wheels` |
| A version refused with 403 is excluded and the resolve is retried | `test_resolve_excludes_a_blocked_version_and_retries` |
| "No safe version" when every version is refused | `test_resolve_reports_no_safe_version_when_every_version_is_blocked` |
| A security rebuild changes only the named package | `test_security_rebuild_changes_only_the_named_package` |
| Offline install from the wheelhouse, with hash checking | `test_offline_install_and_import`, `test_published_release_installs_from_its_own_install_txt` |
| A modified wheel fails the install | `test_offline_install_fails_when_a_wheel_is_modified` |
| A blocking finding quarantines the release | `test_build_quarantines_a_blocking_finding` |
| A failed scan holds the release and it cannot be published | `test_build_holds_when_a_scan_fails_and_never_passes_it` |
| Unknown severity is held, never treated as low | `test_gate_holds_unknown_severity_and_failed_watch_scan` |
| Only an approved release can be published; releases are never overwritten | `test_publish_requires_approval_and_verifies_the_copy` |
| A candidate changed after the build cannot be published | `test_publish_refuses_a_candidate_changed_after_the_build` |
| A switch replaces only this channel's library entry | `test_repoint_replaces_only_this_channels_entry` |
| Rollback is refused when the previous release is not eligible | `test_rollback_is_refused_when_the_previous_release_is_not_eligible` |
| A new finding opens a grace period once; the deadline expires it; nothing is revoked automatically | `test_reassess_opens_a_grace_period_and_expires_it` |

In these tests JFrog, Xray and the compute policy API are **fakes**. A 403 is simulated by
returning pip's error text.

## Not tested: must be proven in the POC

| Area | File | Risk | POC test |
|---|---|---|---|
| Compute policy accepts `install.txt` with pip options | `lockfile.render_install_txt` | **High.** The install path depends on it. | P3 |
| Wording of JFrog's 403 errors in pip and uv output | `resolve.find_blocked` | Medium. The patterns are from pip's and uv's usual messages. | P4 |
| Reading "Curation" or "Xray" from the 403 response body | `resolve.make_block_classifier` | Medium. Falls back to `unknown`. | P4 |
| `jf config add --access-token-stdin` and `jf scan --format=simple-json` flags | `scan.JfScanner` | Medium. Depends on the CLI version. | Step 8a |
| Field names in the `simple-json` scan output | `scan.parse_simple_json` | Medium. The fixture in the tests is illustrative, not captured from a real scan. | Step 8a |
| That a scan with no findings means Xray identified the wheel | `gate.evaluate` | Medium. The gate checks that each wheel was scanned without error. It cannot yet prove Xray recognised the package. | Step 8a |
| Delta table reads and writes with named SQL parameters | `registry.SparkRegistry` | Low to medium. Needs DBR 13.3 or later. | Step 7, 8 |
| Compute policy get and edit REST calls | `references.PolicyClient` | Low to medium. | Step 9 |
| Jobs, clusters and workspace API calls in discovery | `discover.discover` | Medium. Pagination and field names are from the API reference. | Step 11 |
| Curation waiver request body | `waiver.request_waiver` | Low. Taken from a JFrog knowledge-base article; the response format is not documented there. | P12 |
| `system.lakeflow.job_run_timeline` query | `health.py`, query H1 | Low. | Runbook step 6 |
| Bundle definition (`databricks.yml`, `resources/jobs.yml`) | | Low to medium. The YAML parses; `databricks bundle validate` has not been run. | Step 6 |
| Installing into `--target` reflects a real cluster install | `tester.run_test` | Medium. It does not detect a release that replaces a runtime package in a harmful way. | P1 |

## Not built yet

| Item | Proposed solution | Note |
|---|---|---|
| Serverless base environments | Section 6, component 6 | The switch job handles compute policies only. |
| Automatic canary health check and automatic wave progression | Section 8 | Phase 1 is manual: Ops runs query H1 and starts each wave. `health.py` has the logic. |
| Automatic waiver request from the build | Section 7 | The `waiver` command exists; the build does not call it. Decide D10 first. |
| Automatic approval | Decision D2 | Approval is manual by design in phase 1. |
| Registry check at job start (hard revocation) | Decision D12 | Revocation works by re-pointing policies. |
| `PIP_FIND_LINKS` on compute for existing `%pip` lines | Section 10 | The switch job does not update policy environment variables. |
| Clean-up of old releases | Section 8, retention | Manual, described in the runbook. |
| Scan of non-wheel artifacts, source builds | | Wheels only. A package with no wheel fails the build. |
| Multi-metastore replication | Section 6, component 8 | One pipeline per metastore. |
| Dashboard and alert definitions as code | | Queries are provided; the dashboard and alerts are created by hand. |

## Known limits to tell Security about

1. **The promoter service principal is a workspace admin.** Editing compute policies requires it.
   Its token or job definition must be protected accordingly.
2. **Approval is recorded, not enforced by privileges.** The builder and promoter can write the
   `dependency_releases` table, so a compromised pipeline identity could mark a release approved.
   Unity Catalog audit logs show every write. A stricter design keeps approvals in a table only
   Security can write.
3. **A grace period lets jobs keep installing a library that Xray now blocks.** This is the
   explicit decision D1. Without it the pipeline cannot protect jobs from new Xray findings.
4. **Revocation does not stop running jobs** and does not uninstall anything from a running process.
5. **A notebook can still run `%pip install`** until network egress rules block the index (step 12).
6. **Grace hours in `config/policy.yml` are examples.** `grace_values_approved` is `false` and the
   reassess job logs a warning until Security sets real values.
