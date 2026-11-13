# Databricks notebook source
# MAGIC %md
# MAGIC # Consumer check
# MAGIC Run this notebook on compute that uses a channel's compute policy (POC tests P1, P6, P8).
# MAGIC It checks that every package in the release's lock file is installed at the pinned version,
# MAGIC and shows whether pip can still reach a package index from this compute.

# COMMAND ----------

dbutils.widgets.text("install_txt", "", "Path of the release install.txt")  # noqa: F821
install_txt = dbutils.widgets.get("install_txt")  # noqa: F821
assert install_txt.endswith("/install.txt"), "Give the full path of the release's install.txt"
release_dir = install_txt.rsplit("/", 1)[0]

# COMMAND ----------

import re
from importlib import metadata


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


pins = {}
with open(f"{release_dir}/requirements.lock") as handle:
    for line in handle:
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==([^\s;\\]+)", line.strip())
        if match:
            pins[norm(match.group(1))] = match.group(2)

installed = {norm(d.metadata["Name"]): d.version for d in metadata.distributions() if d.metadata["Name"]}
wrong = {name: (want, installed.get(name)) for name, want in pins.items() if installed.get(name) != want}

print(f"release folder : {release_dir}")
print(f"pinned packages: {len(pins)}")
print(f"mismatches     : {len(wrong)}")
for name, (want, got) in sorted(wrong.items()):
    print(f"  {name}: lock has {want}, compute has {got}")
assert not wrong, "This compute does not match the release. Check the compute policy and the library status."

# COMMAND ----------

# MAGIC %md
# MAGIC ## Can this compute still reach a package index? (POC test P7)
# MAGIC After egress rules are in place, the download below must fail.

# COMMAND ----------

import subprocess
import sys
import tempfile

probe = subprocess.run(
    [sys.executable, "-m", "pip", "download", "--no-deps", "--no-cache-dir", "-d", tempfile.mkdtemp(), "six"],
    capture_output=True, text=True, timeout=180,
)
print("index reachable:", probe.returncode == 0)
print((probe.stdout + probe.stderr)[-800:])
