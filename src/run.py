"""Job entry point. Each Databricks job task runs this file with one command.

The folder that holds this file is put on the path so ``libdist`` can be
imported without building a wheel.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from libdist.cli import main  # noqa: E402

if __name__ == "__main__":
    code = main()
    if code:
        # A non-zero exit marks the Databricks task as failed, which is what alerts on.
        sys.exit(code)
