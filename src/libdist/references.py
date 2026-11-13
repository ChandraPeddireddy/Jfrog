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
