"""Workdir preparation for the agent.

Before dispatching a command to the agent, the bridge ensures the tenant's
repo checkout is on a clean state at the right ref. For now we just do:
  - git fetch upstream --prune
  - git checkout upstream/main (detached HEAD)
  - git clean -fdx to remove leftover .bot-run.log etc.

If the issue body mentions a version branch, the agent itself can switch to
it (per /rca and /reproduce SOPs). We only get the workdir into a clean
known state as a baseline.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class WorkdirError(Exception):
    pass


def prepare(workdir: str, default_branch: str = "main", upstream_remote: str = "upstream") -> str:
    """Prepare the workdir and return the short sha of the resolved ref.

    Raises WorkdirError on any failure.
    """
    wd = Path(workdir)
    if not (wd / ".git").exists():
        raise WorkdirError(f"not a git checkout: {wd}")

    def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
        logger.debug("workdir %s$ %s", wd, " ".join(cmd))
        cp = subprocess.run(
            cmd, cwd=wd, capture_output=True, text=True, timeout=120
        )
        if check and cp.returncode != 0:
            raise WorkdirError(
                f"command failed: {' '.join(cmd)}\nstdout: {cp.stdout}\nstderr: {cp.stderr}"
            )
        return cp

    # Fetch latest
    run(["git", "fetch", upstream_remote, "--prune"])

    # Reset to clean state — detached HEAD at upstream/<default_branch>.
    run(["git", "checkout", f"{upstream_remote}/{default_branch}"])

    # Clean up any leftover files from a prior run (e.g., .bot-run.log).
    # -d: also remove untracked directories. -x: include ignored files too.
    # Be careful: we intentionally use -x here because the agent's scratch log
    # is gitignored.
    run(["git", "clean", "-fdx"])

    # Return the resolved sha for logging.
    cp = run(["git", "rev-parse", "--short", "HEAD"])
    return cp.stdout.strip()
