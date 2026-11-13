"""Compute references: the compute policies that point at a release.

A compute policy carries a ``libraries`` list. One entry is
``{"requirements": "<wheelhouse>/releases/<release_id>/install.txt"}``.
Switching a release replaces that one entry and leaves every other library
and every other policy setting unchanged.
"""

from __future__ import annotations

import re

# Fields of a policy that the edit API accepts. Everything else returned by GET
# (creator, timestamps, is_default) is read-only.
_EDITABLE = ["policy_id", "name", "definition", "description", "max_clusters_per_user",
             "policy_family_id", "policy_family_definition_overrides", "libraries"]


def install_txt_path(releases_root: str, release_id: str) -> str:
    return f"{releases_root.rstrip('/')}/{release_id}/install.txt"


def release_pattern(releases_root: str, channel: str) -> re.Pattern:
    """Matches this channel's install.txt paths only (not ``<channel>-ml`` and the like)."""
    root = re.escape(releases_root.rstrip("/"))
    return re.compile(rf"^{root}/({re.escape(channel)}-\d{{8}}-\d{{4}})/install\.txt$")


def repoint_libraries(libraries: list[dict] | None, releases_root: str, channel: str,
                      release_id: str) -> tuple[list[dict], str | None]:
    """Return the new libraries list and the release id that was referenced before."""
    pattern = release_pattern(releases_root, channel)
    new_entry = {"requirements": install_txt_path(releases_root, release_id)}
    out: list[dict] = []
    previous: str | None = None
    replaced = False
    for lib in libraries or []:
        match = pattern.match(str(lib.get("requirements", ""))) if isinstance(lib, dict) else None
        if match:
            previous = previous or match.group(1)
            if not replaced:
                out.append(new_entry)
                replaced = True
            continue  # drop duplicates of the same channel
        out.append(lib)
    if not replaced:
        out.append(new_entry)
    return out, previous


class PolicyClient:
    """Reads and edits compute policies through the Databricks REST API."""

    def __init__(self, workspace_client):
        self.api = workspace_client.api_client

    def get(self, policy_id: str) -> dict:
        return self.api.do("GET", "/api/2.0/policies/clusters/get", query={"policy_id": policy_id})

    def set_libraries(self, policy: dict, libraries: list[dict]) -> None:
        body = {k: policy[k] for k in _EDITABLE if k in policy and policy[k] not in (None, "")}
        body["libraries"] = libraries
        self.api.do("POST", "/api/2.0/policies/clusters/edit", body=body)


# ---------------------------------------------------------------------------
# Optional: make existing "%pip install" lines resolve from the release
# ---------------------------------------------------------------------------
PIP_ENV_KEYS = ("spark_env_vars.PIP_NO_INDEX", "spark_env_vars.PIP_FIND_LINKS")


def with_pip_redirect(policy: dict, wheels_path: str) -> dict:
    """Return a copy of the policy whose compute sets PIP_NO_INDEX and PIP_FIND_LINKS.

    A policy built from a policy family keeps its rules in
    ``policy_family_definition_overrides``; other policies keep them in ``definition``.
    Both are JSON strings.
    """
    import json

    field = "policy_family_definition_overrides" if policy.get("policy_family_id") else "definition"
    rules = json.loads(policy.get(field) or "{}")
    rules[PIP_ENV_KEYS[0]] = {"type": "fixed", "value": "1"}
    rules[PIP_ENV_KEYS[1]] = {"type": "fixed", "value": wheels_path}
    out = dict(policy)
    out[field] = json.dumps(rules)
    return out


# ---------------------------------------------------------------------------
# Serverless: workspace base environments
# ---------------------------------------------------------------------------
def render_environment_yaml(release_path: str, environment_version: str) -> str:
    """Environment specification for a serverless base environment.

    The pip options are listed directly, the documented form for this file,
    instead of pointing at install.txt.
    """
    release_path = release_path.rstrip("/")
    return (
        "# Managed by the library distribution pipeline. Do not edit by hand.\n"
        f"environment_version: '{environment_version}'\n"
        "dependencies:\n"
        '  - "--no-index"\n'
        f'  - "--find-links {release_path}/wheels"\n'
        '  - "--only-binary=:all:"\n'  # quoted: a bare value ending in ":" is not a YAML string
        '  - "--require-hashes"\n'
        f'  - "-r {release_path}/requirements.lock"\n'
    )


def release_in_environment_yaml(text: str, releases_root: str, channel: str) -> str | None:
    """The release id an environment YAML currently points at, if it is this channel's."""
    root = re.escape(releases_root.rstrip("/"))
    match = re.search(rf"-r\s+{root}/({re.escape(channel)}-\d{{8}}-\d{{4}})/requirements\.lock", text or "")
    return match.group(1) if match else None


def environment_version_in_yaml(text: str) -> str | None:
    match = re.search(r"^environment_version:\s*['\"]?([^'\"\s]+)", text or "", re.MULTILINE)
    return match.group(1) if match else None


class BaseEnvironmentClient:
    """Workspace base environments API (``/api/environments/v1``)."""

    PREFIX = "/api/environments/v1/"

    def __init__(self, workspace_client, poll_seconds: int = 15, timeout_seconds: int = 1800):
        self.api = workspace_client.api_client
        self.poll_seconds, self.timeout_seconds = poll_seconds, timeout_seconds

    def get(self, name: str) -> dict:
        return self.api.do("GET", self.PREFIX + name)

    def _wait(self, operation: dict) -> dict:
        import time

        deadline = time.time() + self.timeout_seconds
        while not operation.get("done"):
            if time.time() > deadline:
                raise TimeoutError(f"operation {operation.get('name')} did not finish in time")
            time.sleep(self.poll_seconds)
            operation = self.api.do("GET", self.PREFIX + operation["name"])
        if operation.get("error"):
            raise RuntimeError(f"base environment operation failed: {operation['error']}")
        return operation

    def create(self, environment_id: str, display_name: str, filepath: str) -> dict:
        body = {"display_name": display_name, "filepath": filepath, "base_environment_type": "CPU"}
        return self._wait(self.api.do("POST", self.PREFIX + "workspace-base-environments",
                                      query={"workspace_base_environment_id": environment_id}, body=body))

    def refresh(self, name: str) -> dict:
        """Rebuild the environment after its YAML file changed."""
        return self._wait(self.api.do("POST", self.PREFIX + name + "/refresh", body={}))
