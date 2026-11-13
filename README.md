# Secure Python library distribution on Databricks

This repository implements the design in
[`docs/Library_Distribution_Proposed_Solution.docx`](docs/Library_Distribution_Proposed_Solution.docx).

**What it does.** A release pipeline downloads Python libraries through JFrog ahead of time,
scans them with Xray, tests them, and stores each approved set as a read-only *release* in a
Unity Catalog volume (the *approved wheelhouse*). Databricks compute installs from the wheelhouse,
offline. A job start no longer calls JFrog, so a new Curation or Xray block cannot fail it.

**Scope.** The code covers all three phases of the proposal: the pilot (manual approval, manual
waves), the shared service (automatic approval by rules, automatic rollouts with a health check,
Curation waiver requests, serverless), and scale-out (several workspaces and metastores, clean-up).
Every automation is a switch that ships **off**. You turn them on stage by stage.

**Status.** The code is written and has 83 passing tests on a developer machine. It has **not**
been run against a real Databricks workspace, JFrog Artifactory or Xray. The parts that talk to
those systems were written from their documentation, and some will need changes once they run.
[`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) lists those points stage by stage, with the
change each outcome needs. [`docs/STATUS.md`](docs/STATUS.md) lists what is and is not proven.

## Where to start

| You are | Read |
|---|---|
| Anyone receiving this package | [`docs/HANDOVER.md`](docs/HANDOVER.md): what is in it, who needs what, what to do first |
| Ops, implementing the solution | [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md): the master plan, 11 stages |
| Ops, doing the first deployment | [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md): step-by-step commands |
| Ops, running it day to day | [`docs/RUNBOOK.md`](docs/RUNBOOK.md) |
| Running the proof of concept | [`docs/POC_TESTS.md`](docs/POC_TESTS.md) |
| Security, reviewing the rules | [`config/policy.yml`](config/policy.yml), [`sql/approve_release.sql`](sql/approve_release.sql) |
| A team that needs a library | [Add a library](#add-a-library) below |

## How it works

```
 channel.in ──► libdist-build (builder) ──► staging volume ──► approval
 (libraries)     resolve → scan → test        candidate         by Security (SQL), or by the rules
                      ▲                                                  │
                      │                                                  ▼
                JFrog Artifactory                          libdist-promote (promoter)
                Curation + Xray                            publish: copy, verify every hash
                      ▲                                                  │
                      │                                                  ▼
 libdist-reassess ────┘                                         wheelhouse volume
 every 4 hours: rescan releases in use,                      releases/<release_id>/
 open a grace period for a new finding,                                  │
 start a replacement build, retry until                                  ▼
 one is on the way                                 rollout, wave by wave, with a health check:
                                                   compute policies (classic)
                                                   base environments (serverless)
                                                                         │
                                                                         ▼
                                                      compute installs the release, offline
```

One release folder looks like this and is never changed after it is published:

```
/Volumes/<catalog>/<schema>/wheelhouse/releases/ds-basics-20261112-1405/
    wheels/*.whl
    requirements.lock        every library pinned, with sha256 hashes
    install.txt              pip options: --no-index, --find-links, --require-hashes, -r requirements.lock
    manifest.json            packages, changes from the previous release, blocked versions, gate result
    security-findings.json   scan result for every wheel
    test-results.json        offline install and import test
    COMPLETE                 written last, after every hash is verified
```

A compute policy points at one release with a single library entry:

```json
{ "requirements": "/Volumes/<catalog>/<schema>/wheelhouse/releases/ds-basics-20261112-1405/install.txt" }
```

## Repository layout

| Path | Content |
|---|---|
| `databricks.yml`, `resources/jobs.yml` | Databricks Asset Bundle: eleven jobs |
| `config/settings.yml` | Catalog, schema, volumes, JFrog connection |
| `config/policy.yml` | Security rules as code: blocked severities, grace periods |
| `config/channels/<name>/` | One folder per channel: `channel.in`, `constraints.txt`, `channel.yml` |
| `src/run.py`, `src/libdist/` | Pipeline code |
| `sql/` | Unity Catalog setup, registry tables, grants, approval, monitoring queries |
| `examples/` | Example compute policy, consumer check notebook, start-up gate, P0 reproduction script |
| `tests/` | Unit and local end-to-end tests |
| `docs/` | Implementation guide, deployment steps, runbook, POC tests, status, the proposed solution |

## The jobs

| Job | Runs as | What it does |
|---|---|---|
| `libdist-build` | builder | Builds one candidate release for a channel. Result: `READY_FOR_APPROVAL`, `QUARANTINED` or `FAILED`. Can request a Curation waiver. |
| `libdist-reassess` | builder | Every 4 hours: rescans releases in use, opens grace periods, starts and retries replacement builds. |
| `libdist-discover` | builder | Daily: records which libraries jobs, clusters, policies and notebooks use. |
| `libdist-promote` | promoter | Every 30 minutes: approves by the rules (if allowed), publishes approved releases, starts rollouts and moves them wave by wave, rolls back an unhealthy wave. |
| `libdist-publish` | promoter | Manual: copies one `APPROVED` release to the wheelhouse and verifies every hash. |
| `libdist-switch` | promoter | Manual: points one wave at a published release. |
| `libdist-rollback` | promoter | Manual: points a wave back at its previous release, if that release is still eligible. |
| `libdist-revoke` | promoter | Marks a release `REVOKED` and lists the compute that still uses it. |
| `libdist-cleanup` | promoter | Weekly: reports, and with `apply=true` deletes, old candidates and unused releases. |
| `libdist-run-builder`, `libdist-run-promoter` | builder, promoter | Run any other command once, as the right identity. |

The manual jobs always work. `libdist-promote` does the same steps on a schedule once you switch it on.

In manual approval mode a security approver runs [`sql/approve_release.sql`](sql/approve_release.sql)
as themselves, so the registry records who approved.

Commands without a job of their own (`serverless-init`, `rollout-start`, `migration-report`,
`import-release`, `waiver`, `verify`, `status`) run through `libdist-run-builder` or
`libdist-run-promoter`:

```bash
databricks bundle run -t prod libdist_run_promoter --params 'command=status --channel ds-basics'
```

## Add a library

1. Add one line to `config/channels/<channel>/channel.in`. Write a name or a range, not an exact version.
2. Open a pull request. After it is merged and deployed, run `libdist-build` for the channel.
3. The pipeline pins the library and all of its hidden dependencies, scans and tests them.
4. After approval and publication, the next compute start has the library.

## Run the tests

The tests need Python 3.10 or later, `pytest`, `pyyaml` and `uv`. They do not need Databricks or JFrog.

```bash
pip install pytest pyyaml uv
python -m pytest
```

The end-to-end tests run the real resolver (uv) and the real downloader and installer (pip)
against a folder of small hand-made wheels. JFrog, Xray and the compute policy API are replaced by fakes.

### Dry run on a laptop

You can run the whole flow locally without Databricks or JFrog. This uses a folder of wheels as
the index, a JSON file as the registry, and **does not scan anything**:

```bash
pip download requests -d /tmp/index                          # any folder of wheels
cp -r config /tmp/cfg
cat >> /tmp/cfg/settings.yml <<'EOF'
find_links: /tmp/index
volumes_root: /tmp/Volumes
EOF
RUN="python src/run.py --config-dir /tmp/cfg --local-registry /tmp/registry.json --local-test-scanner"
$RUN build --channel ds-basics
$RUN status --channel ds-basics                               # note the release_id
$RUN approve-local --release-id <release_id> --approver me@example.com
$RUN publish --release-id <release_id>
pip install -r /tmp/Volumes/main/libs/wheelhouse/releases/<release_id>/install.txt --target /tmp/site
```
