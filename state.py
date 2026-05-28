"""Persistent state for the github-bridge.

Tracks, per watched repo, the last-processed comment id and poll timestamp
so that after a restart the bridge doesn't replay comments it already
handled. Tracks a single shared `posted_comment_ids` set across all repos,
which is fine because GitHub comment ids are globally unique.

Also tracks PRs the bot authored (one entry per PR, per repo) so the
polling loop knows which PRs to check for review activity. Adding new
review/review-comment polling to every PR in a watched repo would be
prohibitively expensive on large repos; restricting it to bot-authored
PRs is bounded and matches the actual use case.

State file lives at ~/.local/state/opensearch-maintainer-bot/state.json.

JSON schema (v2):
  {
    "version": 2,
    "per_repo": {
      "<owner>/<repo>": {
        "last_seen_comment_id":   <int | null>,
        "last_seen_at":           "<ISO-8601 | null>",
        "bot_authored_prs":       {
          "<pr_number>": {
            "branch":                       "<branch-name>",
            "last_seen_review_id":          <int | null>,
            "last_seen_review_comment_id":  <int | null>
          },
          ...
        }
      },
      ...
    },
    "posted_comment_ids": [<int>, ...]
  }

The `bot_authored_prs` field was added after initial v2 release; older v2
state files without it load fine (the field defaults to {}).

A v1 state file (flat, single-repo) is auto-migrated on load.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

STATE_DIR = Path.home() / ".local" / "state" / "opensearch-maintainer-bot"
STATE_FILE = STATE_DIR / "state.json"


@dataclass
class AuthoredPR:
    """A pull request the bot authored. The branch lives on the bot's fork
    (e.g., ryan-gh-bot/sql:bot-fix-...). The two `last_seen_*` cursors track
    where polling left off, so we don't redispatch the same review repeatedly.
    """
    branch: str
    last_seen_review_id: Optional[int] = None
    last_seen_review_comment_id: Optional[int] = None

    def as_dict(self) -> dict:
        return {
            "branch": self.branch,
            "last_seen_review_id": self.last_seen_review_id,
            "last_seen_review_comment_id": self.last_seen_review_comment_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AuthoredPR":
        return cls(
            branch=d.get("branch") or "",
            last_seen_review_id=d.get("last_seen_review_id"),
            last_seen_review_comment_id=d.get("last_seen_review_comment_id"),
        )


@dataclass
class RepoState:
    last_seen_comment_id: Optional[int] = None
    last_seen_at: Optional[str] = None
    # Map of pr_number -> AuthoredPR. Only PRs created by the bot via /fix
    # land here; the bot doesn't react to reviews on other PRs.
    bot_authored_prs: Dict[int, AuthoredPR] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "last_seen_comment_id": self.last_seen_comment_id,
            "last_seen_at": self.last_seen_at,
            "bot_authored_prs": {
                str(pr_num): ap.as_dict()
                for pr_num, ap in self.bot_authored_prs.items()
            },
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RepoState":
        prs: Dict[int, AuthoredPR] = {}
        for pr_str, pr_d in (d.get("bot_authored_prs") or {}).items():
            try:
                pr_num = int(pr_str)
            except (TypeError, ValueError):
                continue
            if isinstance(pr_d, dict):
                prs[pr_num] = AuthoredPR.from_dict(pr_d)
        return cls(
            last_seen_comment_id=d.get("last_seen_comment_id"),
            last_seen_at=d.get("last_seen_at"),
            bot_authored_prs=prs,
        )


@dataclass
class State:
    per_repo: Dict[str, RepoState] = field(default_factory=dict)
    # Global: ids of comments we posted ourselves. Prevents self-reply loops
    # across all repos. Comment ids are globally unique.
    posted_comment_ids: List[int] = field(default_factory=list)

    # Held only during a v1→v2 migration so we can attach the legacy scalar
    # state to whichever repo the bridge first polls.
    _legacy_last_seen_comment_id: Optional[int] = None
    _legacy_last_seen_at: Optional[str] = None

    def get_repo(self, repo: str) -> RepoState:
        if repo not in self.per_repo:
            # Adopt legacy scalar state for the first repo we see, if present.
            rs = RepoState(
                last_seen_comment_id=self._legacy_last_seen_comment_id,
                last_seen_at=self._legacy_last_seen_at,
            )
            self.per_repo[repo] = rs
            # Consume the legacy values so subsequent repos start clean.
            self._legacy_last_seen_comment_id = None
            self._legacy_last_seen_at = None
        return self.per_repo[repo]

    def record_posted(self, comment_id: int) -> None:
        if comment_id not in self.posted_comment_ids:
            self.posted_comment_ids.append(comment_id)
            if len(self.posted_comment_ids) > 1000:
                self.posted_comment_ids = self.posted_comment_ids[-1000:]

    def record_authored_pr(self, repo: str, pr_number: int, branch: str) -> None:
        """Mark a PR as bot-authored so the polling loop will fetch its
        reviews and review-comments going forward."""
        rs = self.get_repo(repo)
        if pr_number not in rs.bot_authored_prs:
            rs.bot_authored_prs[pr_number] = AuthoredPR(branch=branch)
        else:
            # Update branch in case it ever changes (shouldn't, but defensive).
            rs.bot_authored_prs[pr_number].branch = branch


def load_state(path: Path = STATE_FILE) -> State:
    if not path.exists():
        return State()
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        # Corrupt state file — start fresh. Previous state is preserved as .bak.
        path.rename(path.with_suffix(path.suffix + ".bak"))
        return State()

    # v2 (current) schema
    if data.get("version") == 2:
        per_repo = {
            repo: RepoState.from_dict(rs)
            for repo, rs in (data.get("per_repo") or {}).items()
        }
        return State(
            per_repo=per_repo,
            posted_comment_ids=list(data.get("posted_comment_ids", [])),
        )

    # v1 (legacy) schema — single-repo scalar fields at top level.
    # Adopt them into the first repo the bridge polls.
    return State(
        per_repo={},
        posted_comment_ids=list(data.get("posted_comment_ids", [])),
        _legacy_last_seen_comment_id=data.get("last_seen_comment_id"),
        _legacy_last_seen_at=data.get("last_seen_at"),
    )


def save_state(state: State, path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": 2,
        "per_repo": {repo: rs.as_dict() for repo, rs in state.per_repo.items()},
        "posted_comment_ids": list(state.posted_comment_ids),
    }
    # Atomic write: write to tmpfile in the same dir, then rename.
    fd, tmppath = tempfile.mkstemp(
        prefix="state-", suffix=".json.tmp", dir=str(path.parent)
    )
    try:
        with open(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
        Path(tmppath).replace(path)
    except Exception:
        Path(tmppath).unlink(missing_ok=True)
        raise
