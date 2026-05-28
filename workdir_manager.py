"""Workdir preparation for the agent.

Before dispatching a command to the agent, the bridge ensures the tenant's
repo checkout is on a clean state at the right ref. For now we just do:
  - git fetch upstream --prune
  - git checkout upstream/main (detached HEAD)
  - git clean -fdx to remove leftover .bot-run.log etc.

For /fix specifically, the bridge also ensures a `target` remote is configured
pointing at the repo where the issue lives — that's where the PR will be
opened. See `ensure_target_remote()`.
"""

from __future__ import annotations

import logging
import subprocess
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class WorkdirError(Exception):
    pass


def _run(workdir: Path, cmd: list[str], check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
    logger.debug("workdir %s$ %s", workdir, " ".join(cmd))
    # Inherit the environment, but ensure git never blocks on an interactive
    # credential prompt (would hang on a headless dev desktop). The workdir
    # should have credential.helper configured locally to supply the bot's PAT
    # for github.com pushes; if that lookup fails for any reason, we want git
    # to fail fast rather than wait for stdin.
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    cp = subprocess.run(
        cmd, cwd=workdir, capture_output=True, text=True, timeout=timeout, env=env,
    )
    if check and cp.returncode != 0:
        raise WorkdirError(
            f"command failed: {' '.join(cmd)}\nstdout: {cp.stdout}\nstderr: {cp.stderr}"
        )
    return cp


def prepare(workdir: str, default_branch: str = "main", upstream_remote: str = "upstream") -> str:
    """Prepare the workdir and return the short sha of the resolved ref.

    Raises WorkdirError on any failure.
    """
    wd = Path(workdir)
    if not (wd / ".git").exists():
        raise WorkdirError(f"not a git checkout: {wd}")

    # Fetch latest
    _run(wd, ["git", "fetch", upstream_remote, "--prune"])

    # Reset to clean state — detached HEAD at upstream/<default_branch>.
    _run(wd, ["git", "checkout", f"{upstream_remote}/{default_branch}"])

    # Clean up any leftover files from a prior run (e.g., .bot-run.log,
    # .bot-response.md, .bot-pr-body.md). -d: also remove untracked
    # directories. -x: include ignored files too.
    _run(wd, ["git", "clean", "-fdx"])

    # Return the resolved sha for logging.
    cp = _run(wd, ["git", "rev-parse", "--short", "HEAD"])
    return cp.stdout.strip()


def prepare_for_revision(workdir: str, branch_name: str) -> str:
    """Prepare the workdir to add commits to an existing branch on `origin`
    (the bot's fork). Used when the bot is asked to revise a PR it previously
    authored — additional commits land on the same branch and the existing PR
    auto-updates.

    Steps:
      - git fetch origin --prune  (refresh the bot's fork)
      - git fetch target --prune  (so the agent can rebase / diff against
                                   the current target base if it wants)
      - git checkout -f origin/<branch>  (detached HEAD on the branch tip)
      - git clean -fdx            (remove .bot-response.md / .bot-pr-body.md
                                   from any prior crashed run)

    Detached HEAD is intentional. The agent makes commits on top, and the
    bridge pushes them via `git push origin HEAD:refs/heads/<branch>`,
    which works the same whether the agent did a plain commit (fast-forward)
    or a `git rebase target/<base>` (in which case the bridge uses
    --force-with-lease to update the remote branch).

    Returns the short sha of the resolved branch tip. Raises WorkdirError if
    the branch can't be fetched (e.g., it's been deleted on the bot's fork)
    or the workdir isn't a git checkout.
    """
    wd = Path(workdir)
    if not (wd / ".git").exists():
        raise WorkdirError(f"not a git checkout: {wd}")

    _run(wd, ["git", "fetch", "origin", "--prune"])
    # target may not be configured yet on first revision after a bridge restart;
    # check=False so we don't fail. The agent can re-fetch it later if needed.
    _run(wd, ["git", "fetch", "target", "--prune"], check=False)

    cp = _run(wd, ["git", "rev-parse", "--verify", f"origin/{branch_name}"], check=False)
    if cp.returncode != 0:
        raise WorkdirError(
            f"branch `{branch_name}` not found on origin — has it been deleted? "
            f"Cannot revise; either restore the branch or invoke /fix on the "
            f"original issue to create a new branch."
        )

    _run(wd, ["git", "checkout", "-f", f"origin/{branch_name}"])
    _run(wd, ["git", "clean", "-fdx"])

    cp = _run(wd, ["git", "rev-parse", "--short", "HEAD"])
    return cp.stdout.strip()


def ensure_target_remote(workdir: str, target_repo: str, remote_name: str = "target") -> None:
    """Idempotently configure a `<remote_name>` remote pointing at `target_repo`.

    Used for /fix: the bridge needs to know where the PR will be opened so the
    agent can fetch the target's base branch and the bridge can resolve cross-
    repo PR head/base. The remote is named `target` (not `upstream`) so it
    doesn't collide with the existing `upstream` remote that /rca and
    /reproduce expect.
    """
    wd = Path(workdir)
    if not (wd / ".git").exists():
        raise WorkdirError(f"not a git checkout: {wd}")
    target_url = f"https://github.com/{target_repo}.git"

    # Is the remote already configured?
    cp = _run(wd, ["git", "remote", "get-url", remote_name], check=False)
    if cp.returncode == 0:
        existing_url = cp.stdout.strip()
        if existing_url == target_url:
            return  # already correct
        # Different URL — replace it.
        _run(wd, ["git", "remote", "set-url", remote_name, target_url])
    else:
        _run(wd, ["git", "remote", "add", remote_name, target_url])
    # Belt-and-suspenders: disable pushes to this remote to prevent accidents.
    _run(wd, ["git", "remote", "set-url", "--push", remote_name, "DISABLED"], check=False)


def fetch_and_checkout(workdir: str, remote: str, branch: str) -> str:
    """Fetch `<remote>` and check out `<remote>/<branch>` detached. Returns short sha."""
    wd = Path(workdir)
    _run(wd, ["git", "fetch", remote, "--prune"])
    _run(wd, ["git", "checkout", f"{remote}/{branch}"])
    cp = _run(wd, ["git", "rev-parse", "--short", "HEAD"])
    return cp.stdout.strip()


def has_unpushed_commits(workdir: str, base_ref: str) -> bool:
    """Return True if HEAD has commits ahead of `base_ref`. Used to verify the
    agent actually committed before the bridge attempts to push.
    """
    wd = Path(workdir)
    cp = _run(wd, ["git", "rev-list", "--count", f"{base_ref}..HEAD"], check=False)
    if cp.returncode != 0:
        # If the rev-list fails (e.g., base_ref unresolvable), conservatively
        # say "no commits to push" rather than push something half-broken.
        return False
    try:
        return int(cp.stdout.strip()) > 0
    except ValueError:
        return False


def is_ancestor(workdir: str, ancestor_ref: str, descendant_ref: str = "HEAD") -> bool:
    """Return True if `ancestor_ref` is an ancestor of `descendant_ref` —
    i.e., descendant_ref is reachable from ancestor_ref via parent links.

    Used to decide whether a push can be a fast-forward (plain push) or
    requires history rewrite (force-with-lease). For a revision flow:
      - Agent made additive commits on top of the existing branch tip:
        existing_tip is an ancestor of HEAD → fast-forward, plain push works.
      - Agent rebased or reset onto a different base: existing_tip is NOT an
        ancestor of HEAD → force-with-lease required.
    """
    wd = Path(workdir)
    cp = _run(
        wd,
        ["git", "merge-base", "--is-ancestor", ancestor_ref, descendant_ref],
        check=False,
    )
    return cp.returncode == 0


def push_branch(workdir: str, remote: str, branch_name: str, force: bool = False) -> None:
    """Push HEAD to `<remote>/<branch_name>`. If `force=False` (default) and
    the remote branch already exists, the push will be rejected — the bridge
    must handle that as an error and tell the maintainer.
    """
    wd = Path(workdir)
    args = ["git", "push", remote, f"HEAD:refs/heads/{branch_name}"]
    if force:
        args.insert(2, "--force-with-lease")
    _run(wd, args, timeout=180)


def remote_branch_exists(workdir: str, remote: str, branch_name: str) -> bool:
    """Check whether `<remote>/<branch_name>` already exists by ls-remote."""
    wd = Path(workdir)
    cp = _run(wd, ["git", "ls-remote", "--heads", remote, branch_name], check=False)
    if cp.returncode != 0:
        return False
    return bool(cp.stdout.strip())


def latest_commit_subject(workdir: str) -> str:
    """Return the subject (first line) of the latest commit on HEAD."""
    wd = Path(workdir)
    cp = _run(wd, ["git", "log", "-1", "--pretty=%s"])
    return cp.stdout.strip()
