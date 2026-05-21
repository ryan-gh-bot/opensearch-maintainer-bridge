"""Persistent state for the github-bridge.

Tracks, per watched repo, the last-processed comment id and poll timestamp
so that after a restart the bridge doesn't replay comments it already
handled. Tracks a single shared `posted_comment_ids` set across all repos,
which is fine because GitHub comment ids are globally unique.

State file lives at ~/.local/state/opensearch-maintainer-bot/state.json.

JSON schema (v2):
  {
    "version": 2,
    "per_repo": {
      "<owner>/<repo>": {
        "last_seen_comment_id": <int | null>,
        "last_seen_at":        "<ISO-8601 | null>"
      },
      ...
    },
    "posted_comment_ids": [<int>, ...]
  }

A v1 state file (flat, single-repo) is auto-migrated on load. The
migrated entries are associated with whichever repo the caller names
when calling `get_repo_state(repo)` with no prior entry — if there's
a pre-migration scalar, it's moved to that repo's slot on first access.
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
class RepoState:
    last_seen_comment_id: Optional[int] = None
    last_seen_at: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "last_seen_comment_id": self.last_seen_comment_id,
            "last_seen_at": self.last_seen_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RepoState":
        return cls(
            last_seen_comment_id=d.get("last_seen_comment_id"),
            last_seen_at=d.get("last_seen_at"),
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
