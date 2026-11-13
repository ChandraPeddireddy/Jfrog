# Databricks notebook source
# MAGIC %md
# MAGIC # Start-up gate (optional, decision D12)
# MAGIC Put this at the start of a job that must not run on a revoked or expired release.
# MAGIC It finds the release this compute installed and fails the run if that release is no
# MAGIC longer eligible. It reads only the `eligible_releases` view (sql/04_eligible_releases_view.sql).
# MAGIC
# MAGIC If the registry cannot be read, the gate fails closed. Change `FAIL_CLOSED` only with
# MAGIC Security's agreement.

# COMMAND ----------

import os
import re

REGISTRY = "main.libs"          # CHANGE-ME: <catalog>.<schema> of the pipeline
RELEASES_ROOT = "/Volumes/main/libs/wheelhouse/releases"  # CHANGE-ME
FAIL_CLOSED = True


def installed_release() -> str | None:
    """The release id this compute was started with, from pip's environment or the cluster libraries."""
    candidates = [os.environ.get("PIP_FIND_LINKS", "")]
    try:
        cluster_id = spark.conf.get("spark.databricks.clusterUsageTags.clusterId")  # noqa: F821
        from databricks.sdk import WorkspaceClient

        for status in WorkspaceClient().libraries.cluster_status(cluster_id):
            candidates.append(getattr(status.library, "requirements", "") or "")
    except Exception as exc:  # serverless, or no permission to read the cluster's libraries
        print(f"could not read cluster libraries: {exc}")
    for text in candidates:
        match = re.search(re.escape(RELEASES_ROOT) + r"/([^/]+)/", text)
        if match:
            return match.group(1)
    return None


release_id = installed_release()
print("release on this compute:", release_id)
if release_id is None:
    raise RuntimeError("Start-up gate: this compute does not use a wheelhouse release.")

try:
    eligible = spark.sql(  # noqa: F821
        f"SELECT 1 FROM {REGISTRY}.eligible_releases WHERE release_id = :rid", args={"rid": release_id}
    ).count() > 0
except Exception as exc:
    if FAIL_CLOSED:
        raise RuntimeError(f"Start-up gate: the release registry cannot be read ({exc}).") from exc
    print(f"WARNING: registry not readable, continuing: {exc}")
    eligible = True

if not eligible:
    raise RuntimeError(f"Start-up gate: release {release_id} is revoked or past a grace deadline. "
                       "Restart the compute to pick up the current release, or contact the platform team.")
print("release is eligible")
