"""Parse slash-commands from GitHub comment bodies.

Rules:
  - The command MUST be on the first non-blank line of the comment.
  - The line MUST begin with an @-mention of the configured bot username.
  - The @-mention is followed by whitespace, then a /<command> and optional args.
  - The command word contains only [a-z-] (case-insensitive; we lowercase).
  - Any other form (bare /cmd, @ someone-else /cmd, prose with /cmd) is rejected.
  - We do not support multi-command comments. Only the first one counts.

Why require the @-mention?
- Explicit namespacing: "this command is for THIS bot, not some other slash-bot".
- Natural UX: matches how users already address @dependabot, @copilot, etc.
- Defense against cross-bot interference if opensearch-project/sql ever installs
  other slash-command bots.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

# Matches "@botname /cmd optional args" anchored to the whole line.
# The @-mention is REQUIRED. Anything without it is not a command to us.
_COMMAND_RE = re.compile(
    r"""^
    @(?P<mention>[A-Za-z0-9][A-Za-z0-9-]*)\s+    # REQUIRED @username prefix
    /(?P<cmd>[a-z][a-z-]*)                       # /command (lowercased on match)
    (?:\s+(?P<args>.*?))?                        # optional args
    \s*$                                         # trailing whitespace only
    """,
    re.IGNORECASE | re.VERBOSE,
)


@dataclass
class ParsedCommand:
    command: str           # e.g. "/rca" (leading slash preserved, lowercased)
    args: List[str]        # whitespace-split args; may be empty
    raw_line: str          # the original first-line text, for logging
    mention: str           # the @-username before the command (always present)


def parse_command(body: str, bot_username: Optional[str] = None) -> Optional[ParsedCommand]:
    """Return the parsed command from the first non-blank line, or None.

    If `bot_username` is provided, the @-mention must match it (case-insensitive)
    or the line is rejected.
    """
    if not body:
        return None
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _COMMAND_RE.match(line)
        if not m:
            return None

        mention = m.group("mention")
        if bot_username and mention.lower() != bot_username.lower():
            # Line addresses someone else; not for us.
            return None

        cmd_word = m.group("cmd").lower()
        args_str = (m.group("args") or "").strip()
        args = args_str.split() if args_str else []
        return ParsedCommand(
            command=f"/{cmd_word}",
            args=args,
            raw_line=line,
            mention=mention,
        )
    return None
