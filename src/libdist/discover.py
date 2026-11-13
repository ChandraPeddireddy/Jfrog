"""Discover which libraries jobs, clusters, policies and notebooks use today."""

from __future__ import annotations

import base64
import re
from typing import Iterable

from .util import log, utcnow

_PIP_LINE = re.compile(r"^\s*(?:#\s*MAGIC\s+)?[%!]pip\s+install\s+(.+?)\s*$", re.MULTILINE)


def pip_install_lines(source: str) -> list[str]:
    """The arguments of every ``%pip install`` (or ``!pip install``) line in notebook source."""
    return [m.strip() for m in _PIP_LINE.findall(source or "")]


def library_specs(libraries: Iterable[dict] | None) -> list[tuple[str, str]]:
    """Flatten the Databricks ``libraries`` structure into ``(kind, spec)`` pairs."""
    out = []
    for lib in libraries or []:
        if not isinstance(lib, dict):
            continue
        if "pypi" in lib:
            pypi = lib["pypi"] or {}
            spec = pypi.get("package", "")
            if pypi.get("repo"):
                spec += f" (repo: {pypi['repo']})"
            out.append(("pypi", spec))
        for kind in ("requirements", "whl", "jar", "egg"):
            if kind in lib:
                out.append((kind, str(lib[kind])))
        if "maven" in lib:
            out.append(("maven", str((lib["maven"] or {}).get("coordinates", ""))))
    return out


def _row(now, host, object_type, object_id, name, owner, policy_id, kind, spec) -> dict:
    return {"snapshot_at": now, "workspace_host": host, "object_type": object_type,
            "object_id": str(object_id), "object_name": name or "", "owner": owner or "",
            "policy_id": policy_id or "", "library_kind": kind, "library_spec": spec}


def discover(workspace_client, notebook_root: str | None = None, max_notebooks: int = 2000) -> list[dict]:
    """Snapshot of library use in one workspace. Read-only."""
    w = workspace_client
    host = w.config.host or ""
    now = utcnow()
    rows: list[dict] = []
    api = w.api_client

    # Compute policies and their enforced libraries
    for policy in api.do("GET", "/api/2.0/policies/clusters/list").get("policies", []):
        for kind, spec in library_specs(policy.get("libraries")):
            rows.append(_row(now, host, "compute_policy", policy["policy_id"], policy.get("name"),
                             policy.get("creator_user_name"), policy["policy_id"], kind, spec))

    # Jobs: task libraries and serverless environment dependencies
    page_token = None
    while True:
        query = {"limit": 100, "expand_tasks": "true"}
        if page_token:
            query["page_token"] = page_token
        page = api.do("GET", "/api/2.2/jobs/list", query=query)
        for job in page.get("jobs", []):
            settings = job.get("settings", {})
            owner = job.get("creator_user_name")
            policy_by_cluster = {c.get("job_cluster_key"): (c.get("new_cluster") or {}).get("policy_id")
                                 for c in settings.get("job_clusters", [])}
            for task in settings.get("tasks", []):
                policy_id = (task.get("new_cluster") or {}).get("policy_id") or \
                    policy_by_cluster.get(task.get("job_cluster_key"))
                for kind, spec in library_specs(task.get("libraries")):
                    rows.append(_row(now, host, "job_task", f"{job['job_id']}:{task.get('task_key')}",
                                     settings.get("name"), owner, policy_id, kind, spec))
            for env in settings.get("environments", []):
                for dep in (env.get("spec") or {}).get("dependencies", []) or []:
                    rows.append(_row(now, host, "job_environment",
                                     f"{job['job_id']}:{env.get('environment_key')}",
                                     settings.get("name"), owner, None, "environment", str(dep)))
        page_token = page.get("next_page_token")
        if not page_token:
            break

    # All-purpose clusters: installed cluster libraries
    statuses = api.do("GET", "/api/2.0/libraries/all-cluster-statuses").get("statuses", [])
    for status in statuses:
        libs = [s.get("library") for s in status.get("library_statuses", [])
                if not s.get("is_library_for_all_clusters")]
        for kind, spec in library_specs(libs):
            rows.append(_row(now, host, "cluster", status.get("cluster_id"), "", "", None, kind, spec))

    # Notebooks: %pip install lines (optional, can be slow)
    if notebook_root:
        count = 0
        for obj in w.workspace.list(notebook_root, recursive=True):
            if str(getattr(obj, "object_type", "")).endswith("NOTEBOOK"):
                count += 1
                if count > max_notebooks:
                    log.warning("stopped after %d notebooks; narrow --notebook-root", max_notebooks)
                    break
                try:
                    exported = api.do("GET", "/api/2.0/workspace/export",
                                      query={"path": obj.path, "format": "SOURCE"})
                    source = base64.b64decode(exported.get("content", "")).decode("utf-8", "replace")
                except Exception as exc:
                    log.warning("could not read %s: %s", obj.path, exc)
                    continue
                for line in pip_install_lines(source):
                    rows.append(_row(now, host, "notebook", obj.path, obj.path.rsplit("/", 1)[-1],
                                     "", None, "pip_install", line))
    return rows
