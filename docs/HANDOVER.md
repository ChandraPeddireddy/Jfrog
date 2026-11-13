# Handover

What this package is, who needs which part, and what happens first.

## What you are receiving

| Item | Where | For |
|---|---|---|
| The proposed solution, version 2.1 (design, decisions, POC tests) | `docs/Library_Distribution_Proposed_Solution.docx` | Architecture, Security, Platform |
| The implementation plan: 11 stages, checks, and the code change for each unexpected result | `docs/IMPLEMENTATION.md` | Ops |
| Step-by-step commands for the first deployment | `docs/DEPLOYMENT.md` | Ops |
| Day-to-day operation and what to do for each alert | `docs/RUNBOOK.md` | Ops, on-call |
| How to run each POC test | `docs/POC_TESTS.md` | Ops, Platform |
| What is built, tested and not yet proven; limits Security must know | `docs/STATUS.md` | Everyone, before planning |
| Pipeline code and its tests | `src/`, `tests/` | Ops, Platform engineers |
| Job definitions | `databricks.yml`, `resources/jobs.yml` | Ops |
| Configuration: environment, security rules, channels | `config/` | Ops, Security, channel owners |
| SQL: setup, tables, grants, approval, alerts and dashboard | `sql/` | Ops, Security |
| Example policy, check notebooks, P0 script | `examples/` | Ops |

## Read this first: the state of the code

The code implements the whole design and passes its tests on a developer machine. It has **never
run against a Databricks workspace, JFrog Artifactory or Xray**. Parts of it were written from
vendor documentation and will need small changes when they first run. A few outcomes would need
larger changes. `docs/IMPLEMENTATION.md` says what to check at each stage and what to change.

Plan the work as an implementation project with a proof of concept at the front, not as an
installation.

## What Ops needs from other teams before starting

Nothing in the repository can supply these. Ask for them first.

| From | Needed | Used in |
|---|---|---|
| JFrog admin | Service account with read access to the curated PyPI repository and permission to run Xray on-demand scans | Deployment step 3 |
| JFrog admin | The repository keys: the one to resolve from, and the curated remote repository | `config/settings.yml` |
| JFrog admin | The JFrog CLI (`jf`) Linux amd64 binary, in an approved version | Deployment step 4 |
| JFrog admin | Names of the Xray Watches to scan against (optional) | `config/settings.yml` |
| JFrog admin | Whether the Curation policy allows waiver requests, and who approves them | Stage 7 |
| Network team | A route from the pipeline cluster to the JFrog host; the enterprise CA on the cluster | Deployment step 8 |
| Network team | Later: rules that stop workload compute from reaching package indexes | Stage 9 |
| Databricks admins | Workspace admin and metastore admin time; a non-production workspace | Deployment steps 1, 2, 6 |
| Security | Blocked severities and the severity source | `config/policy.yml` |
| Security | Grace period per severity (decision D1) | Stage 4 |
| Security | Who approves releases; later, whether rules may approve (decision D2) | Deployment step 8b; stage 6 |
| Security | Review of the two service principals' privileges (see STATUS.md, limits 1 and 2) | Deployment step 1 |
| One application team | A real job, its library list, and people to test the pilot | Deployment steps 5 and 10 |

## The first two weeks

1. **Day 1.** Read `docs/STATUS.md` and the stage table in `docs/IMPLEMENTATION.md`. Send the requests above.
2. **As soon as there is a JFrog token.** Run stage 1 (`examples/p0_reproduce.py`). It needs no
   Databricks setup and settles several code details early.
3. **Put the repository in your own Git.** All configuration changes go through pull requests.
   Run `python -m pytest` there to confirm the tests pass in your environment.
4. **Stages 2 and 3.** Follow `docs/DEPLOYMENT.md` in a non-production workspace until a release
   is installed on a pilot cluster. Expect to make the first code changes here.
5. **Record every result** in the results log at the end of `docs/IMPLEMENTATION.md`.

Do not switch on any automation (`libdist-promote`, `rollout.auto`, `approval_mode`,
`waiver_mode`) before stage 5 is complete.

## The proposed solution document and the code

`docs/Library_Distribution_Proposed_Solution.docx` is version 2.1. It was updated after the code
was written, so the two agree on release names, release states, registry tables, the serverless
install path and the discovery step. Section 16 of the document describes this package: its
parts, the automation switches with the decision each one belongs to, and how the three phases
map to the eleven stages of the implementation guide.

If the document and the code ever disagree, the code and `docs/STATUS.md` describe what exists.
Update the document when a decision or a POC result changes the design.

## Who to ask

| Question | Owner |
|---|---|
| A step in the guides fails or is unclear | The platform team that owns this repository |
| What may be approved, grace periods, exceptions, waivers | Security |
| JFrog errors, tokens, policies, the CLI | JFrog admin |
| A library is missing or a version is wrong | The channel owner named in `channel.yml` |
