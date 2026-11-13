"""POC test P0: reproduce the JFrog Curation / Xray behaviour before building anything.

Runs each way of writing a requirement, with pip and with uv, against the JFrog
index, and prints one result row per case: the exact requirement, the client,
and what happened. Nothing is installed; packages are only resolved or
downloaded into a temporary folder.

Usage (on a build cluster or a laptop that can reach JFrog):

    export PIP_INDEX_URL="https://<user>:<token>@<host>/artifactory/api/pypi/<repo>/simple"
    export UV_DEFAULT_INDEX="$PIP_INDEX_URL"
    python examples/p0_reproduce.py --package certifi \
        --old-version 2024.2.2 --range ">=2026.1,<2027" --parent "requests>=2,<3"

Pick --old-version as a version Security expects Xray to block.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile


def run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    text = re.sub(r"(https?://)[^/\s:@]+:[^@\s/]+@", r"\1***:***@", proc.stdout + proc.stderr)
    return proc.returncode, text


def summarise(code: int, text: str) -> str:
    if code == 0:
        match = re.search(r"(?:Saved|Downloading|File was already downloaded)\s+\S*?([A-Za-z0-9_.]+-[0-9][^\s/]*\.whl)", text)
        pinned = re.findall(r"^([a-z0-9_.-]+==[^\s\\]+)", text, re.MULTILINE | re.IGNORECASE)
        return "OK  " + (match.group(1) if match else ", ".join(pinned[:6]))
    lines = [line.strip() for line in text.splitlines() if "403" in line or "No matching" in line
             or "No solution" in line or line.startswith(("ERROR", "error"))]
    return "FAIL  " + (lines[0][:220] if lines else text.strip().splitlines()[-1][:220])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--package", default="certifi")
    parser.add_argument("--old-version", required=True, help="a version Xray is expected to block")
    parser.add_argument("--range", default=">=2026.1,<2027")
    parser.add_argument("--parent", default="requests>=2,<3", help="a library that depends on --package")
    args = parser.parse_args()

    cases = {
        "bare name": args.package,
        "pinned old version": f"{args.package}=={args.old_version}",
        "range": f"{args.package}{args.range}",
        "hidden dependency (parent with a range)": args.parent,
    }
    uv = shutil.which("uv")
    results = []
    for label, requirement in cases.items():
        tmp = tempfile.mkdtemp()
        code, text = run([sys.executable, "-m", "pip", "download", "--no-cache-dir", "--only-binary=:all:",
                          "--disable-pip-version-check", "-d", tmp, requirement])
        results.append({"case": label, "requirement": requirement, "client": "pip download",
                        "result": summarise(code, text)})
        if uv:
            proc = subprocess.run([uv, "pip", "compile", "-", "--no-cache", "--no-header", "--no-annotate"],
                                  input=requirement + "\n", capture_output=True, text=True, timeout=600)
            text = re.sub(r"(https?://)[^/\s:@]+:[^@\s/]+@", r"\1***:***@", proc.stdout + proc.stderr)
            results.append({"case": label, "requirement": requirement, "client": "uv pip compile",
                            "result": summarise(proc.returncode, text)})

    width = max(len(r["requirement"]) for r in results)
    for r in results:
        print(f"{r['case']:<42} {r['requirement']:<{width}}  {r['client']:<15} {r['result']}")
    print("\nJSON:\n" + json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
