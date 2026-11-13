"""JFrog Curation waiver requests for fix versions that are still inside the Curation window.

Endpoint and body follow the JFrog article "How to Use Curation Waiver Requests
to Unblock Packages". The Curation policy must allow waiver requests
(``waiver_request_config`` of ``manual`` or ``auto_approved``).
"""

from __future__ import annotations

import requests

ENDPOINT = "/xray/api/v1/curation/waiver_requests"


def waiver_body(package: str, version: str, repo_key: str, reason: str) -> dict:
    return {"pkg_type": "pypi", "pkg_name": package, "pkg_version": version,
            "repo_key": repo_key, "reason": reason}


def request_waiver(platform_url: str, token: str, package: str, version: str, repo_key: str,
                   reason: str, timeout: int = 60) -> dict:
    """Create the request. Approval happens in JFrog; check the status there."""
    response = requests.post(
        platform_url.rstrip("/") + ENDPOINT,
        json=waiver_body(package, version, repo_key, reason),
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {"text": response.text[:500]}
    if response.status_code >= 300:
        raise RuntimeError(f"waiver request failed with HTTP {response.status_code}: {payload}")
    return payload
