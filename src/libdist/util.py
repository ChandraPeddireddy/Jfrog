"""Small shared helpers: subprocess, hashing, time, logging, redaction."""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

log = logging.getLogger("libdist")


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
        force=True,
    )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_release_id(channel: str, now: datetime | None = None) -> str:
    """Release ids look like ``ds-basics-20261112-1405`` (UTC)."""
    now = now or utcnow()
    return f"{channel}-{now:%Y%m%d-%H%M}"


def sha256_file(path: str | Path, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


_URL_CREDENTIALS = re.compile(r"(https?://)([^/\s:@]+):([^@\s/]+)@")


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Remove credentials from text before it is logged or stored.

    Strips ``user:password@`` from URLs and replaces any literal secret value.
    """
    if not text:
        return text
    out = _URL_CREDENTIALS.sub(r"\1***:***@", text)
    for secret in secrets:
        if secret:
            out = out.replace(secret, "***")
    return out


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return f"{self.stdout}\n{self.stderr}"


def run(
    cmd: Sequence[str],
    env: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    secrets: Iterable[str] = (),
    input_text: str | None = None,
    timeout: int = 3600,
) -> CommandResult:
    """Run a command and return its redacted output. Never raises on non-zero exit."""
    secrets = list(secrets)
    log.info("run: %s", redact(" ".join(cmd), secrets))
    proc = subprocess.run(
        list(cmd),
        env=dict(env) if env is not None else None,
        cwd=str(cwd) if cwd else None,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return CommandResult(
        proc.returncode,
        redact(proc.stdout, secrets),
        redact(proc.stderr, secrets),
    )
