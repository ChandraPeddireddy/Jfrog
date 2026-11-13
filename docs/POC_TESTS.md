# POC tests

How to run each proof-of-concept test from the proposed solution (section 13) with this repository.
Record the result of every test: the date, the environment, what you ran, and what happened.

| # | Test | How to run it | Pass when |
|---|---|---|---|
| P0 | Reproduce the problem | `python examples/p0_reproduce.py --old-version <a version Xray should block>` on a machine that reaches JFrog, with `PIP_INDEX_URL` and `UV_DEFAULT_INDEX` set | You have one result row per requirement style and client. Compare with the JFrog overview and correct it where they differ. |
| P1 | Offline install through a compute policy | DEPLOYMENT steps 8 to 10 | `examples/consumer_check.py` reports `mismatches: 0` |
| P2 | Offline install through a serverless base environment | Not automated. Create a base environment YAML whose `dependencies` list has one line: `-r /Volumes/.../releases/<id>/install.txt`. Attach a serverless notebook and run the first cell of `consumer_check.py`. | Same as P1 |
| P3 | pip options in `install.txt` are honoured | On the pilot cluster, look at the driver log for the library install. Then publish a test release, change one byte of a wheel in a **copy** of it, point a test policy at the copy, and start a cluster. | The library install fails with a hash error |
| P4 | Blocked versions are excluded automatically | Add a library to `channel.in` whose newest version is younger than the Curation window, then run `libdist-build` | The summary lists the version under `blocked` with a reason, and the lock pins an older version; or the result is a clean `NO_SAFE_VERSION` |
| P5 | `uv --exclude-newer` | On the build cluster: `uv pip compile config/channels/ds-basics/channel.in --exclude-newer <date 10 days ago>` with `UV_DEFAULT_INDEX` set | uv resolves and skips younger versions. If it reports missing upload times, the option cannot be used with this Artifactory. |
| P6 | Rollback | Publish and switch two releases, then run `libdist-rollback` | The policy points at the first release; a new cluster passes `consumer_check.py` for it |
| P7 | Run-time installs are blocked | DEPLOYMENT step 12 | `index reachable: False` |
| P8 | Autoscaling after a switch | Start a job cluster with autoscaling on release N-1. Switch the policy to N while it runs. Force a scale-up. Check the new node's library status. | New nodes install N-1, the same as the driver |
| P9 | Serverless switch while runs are in flight | Manual, after P2 | In-flight runs keep their environment; new runs get the new one. Record the delay. |
| P10 | Load | Start about 50 job clusters at once on one policy | All install. Record the install time and compare it with today's install time from JFrog. |
| P11 | Existing `%pip install` lines | On a test policy add `spark_env_vars.PIP_NO_INDEX` = `1` and `spark_env_vars.PIP_FIND_LINKS` = `<release>/wheels`. Run a notebook with existing `%pip install` lines. | Satisfied requirements are a no-op; others fail with "no matching distribution"; no traffic to JFrog |
| P12 | Curation waiver through the API | `python src/run.py --config-dir config waiver --package <p> --version <blocked version> --reason "POC test P12"` as the builder | A request appears in JFrog under Curation > Waiver Requests. After approval, `libdist-build` can download that version. |

## Notes

- **P3 is the most important early test.** The whole install path relies on a compute policy
  accepting a requirements file that contains pip options (`--no-index`, `--find-links`,
  `--require-hashes`, `-r`). If it fails, the fallback is to list the wheel files directly in the
  policy's `libraries`, which the promoter can generate from the lock file.
- **P4 depends on how JFrog words a 403.** The pipeline reads the blocked file name from the pip
  or uv error text, and reads the reason from the 403 response body. If your JFrog version words
  them differently, adjust `find_blocked` and `make_block_classifier` in `src/libdist/resolve.py`.
  Both have unit tests.
- **P11 has a known gap.** `PIP_FIND_LINKS` would have to change at every switch. The switch job
  does not do that yet (see [STATUS.md](STATUS.md)).
- **P12 needs a policy change first** if the Curation policy has waiver requests set to forbidden.
