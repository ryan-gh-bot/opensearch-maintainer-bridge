"""Parse maintainer invocations from GitHub comment bodies.

The bot accepts two invocation forms, both anchored to the first non-blank
line of the comment and both prefixed by an @-mention of the bot:

  Slash form (fast path, exact intent):
      @ryan-gh-bot /rca
      @ryan-gh-bot /reproduce 2.18
      @ryan-gh-bot /fix

  Natural-language form (engineer-style):
      @ryan-gh-bot please look into this regression
      @ryan-gh-bot can you fix this once you've finished the rca
      @ryan-gh-bot is this still a problem on 2.x?

Rules:
  - The mention MUST be on the first non-blank line.
  - The mention MUST match the configured bot username (case-insensitive).
  - Anything after the mention on the same line is the request.
  - If the request begins with `/<word>`, we treat it as a slash command.
  - Otherwise, we treat it as a natural-language request to be triaged by the
    agent (the synthetic command name is "@triage" — distinguishable from any
    real slash command, since command names cannot start with @).
  - Empty-after-mention is rejected (a bare `@<bot>` mention is not a request).

Why require the mention?
  - Explicit namespacing: this is for THIS bot, not someone else's bot.
  - Avoids accidental triggers from @-mentions in nested quotes or context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

# Two-stage match: first the mention, then either a slash-command or freeform.
# Permissive about what comes between the mention and the request — comma,
# colon, em dash, etc. are all natural punctuation in human writing
# ("@bot, please..." or "@bot: do X" or "@bot — can you..."). We require
# at least one whitespace character before the request body, but we allow
# punctuation chars to precede that whitespace.
_MENTION_RE = re.compile(
    r"""^
    @(?P<mention>[A-Za-z0-9][A-Za-z0-9-]*)        # @username
    [,.;:!?\u2013\u2014\-]*                       # optional trailing punctuation
    \s+                                           # at least one whitespace
    (?P<rest>\S.*?)                               # at least one non-space char
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Within `rest`, does the first token look like a slash-command?
_SLASH_RE = re.compile(
    r"""^
    /(?P<cmd>[a-z][a-z-]*)                       # /command (lowercased on match)
    (?:\s+(?P<args>.*?))?                        # optional args
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

NATURAL_LANGUAGE_COMMAND = "@triage"


@dataclass
class ParsedCommand:
    command: str           # "/rca", "/reproduce", "/fix", or "@triage" for NL.
    args: List[str]        # whitespace-split args; may be empty.
    raw_line: str          # the original first-line text, for logging.
    mention: str           # the @-username before the request.
    request_text: str      # full text after the mention (NL invocations use this).


def parse_command(body: str, bot_username: Optional[str] = None) -> Optional[ParsedCommand]:
    """Return the parsed invocation from the first non-blank line, or None.

    If `bot_username` is provided, the @-mention must match it (case-insensitive)
    or the line is rejected.
    """
    if not body:
        return None
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue

        m = _MENTION_RE.match(line)
        if not m:
            return None

        mention = m.group("mention")
        if bot_username and mention.lower() != bot_username.lower():
            return None  # not for us

        rest = m.group("rest").strip()

        # Slash-command path?
        sm = _SLASH_RE.match(rest)
        if sm:
            cmd_word = sm.group("cmd").lower()
            args_str = (sm.group("args") or "").strip()
            args = args_str.split() if args_str else []
            return ParsedCommand(
                command=f"/{cmd_word}",
                args=args,
                raw_line=line,
                mention=mention,
                request_text=rest,
            )

        # Natural-language path: anything else with content gets triaged.
        return ParsedCommand(
            command=NATURAL_LANGUAGE_COMMAND,
            args=[],
            raw_line=line,
            mention=mention,
            request_text=rest,
        )

    return None
