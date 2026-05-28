"""Invoke the kiro-cli agent and retrieve the final GitHub comment it produced.

Design: the agent does NOT return the comment via stdout. Instead, the agent
writes the raw markdown comment to a fixed file inside the workdir, and the
bridge reads that file after the subprocess exits.

Why: kiro-cli's non-interactive mode renders the agent's markdown output for
a terminal (ANSI bold, Unicode box-drawing horizontal rules, syntax-highlighted
but fenceless code blocks, italic escapes, ...). Reversing that rendering to
recover the original markdown is fragile and has repeatedly failed in subtle
ways. Writing to a file via `fs_write` bypasses the renderer entirely — the
bytes the agent wrote are exactly the bytes the bridge reads.

The file path is stable and scoped to the workdir: `<workdir>/.bot-response.md`.
The agent's SOP instructs it to write here. The workdir is git-cleaned at the
start of each run by the bridge, so stale files from prior runs are removed
before the agent executes.

Stream handling: kiro-cli's stdout/stderr are still streamed line-by-line to
the bridge log so demos can show the agent's tool calls in real time. We just
don't rely on stdout for the final comment body any more.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# A dedicated logger for agent stream output. The bridge can tune its level
# separately if it wants to hide agent chatter at INFO and only show at DEBUG.
agent_stream_logger = logging.getLogger("agent.stream")

# Full ANSI CSI escape pattern (ECMA-48). We only use this for cleaning up
# the live stream that goes to the bridge log — NEVER on the comment body.
_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _strip_ansi(s: str) -> str:
    """Strip ANSI escapes. Used only for human-readable log lines."""
    return _ANSI_RE.sub("", s)


# The fixed filename the agent must write its final comment to.
# Relative to the repo workdir (the agent's CWD).
RESPONSE_FILENAME = ".bot-response.md"


class AgentRunError(Exception):
    pass


@dataclass
class AgentResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    response_body: Optional[str]  # contents of .bot-response.md, or None if missing


def _stream_reader(
    stream,
    sink: List[str],
    logger_method,
    prefix: str,
) -> None:
    """Consume a pipe line-by-line, appending to `sink` and logging each line."""
    try:
        for raw in iter(stream.readline, ""):
            if not raw:
                break
            sink.append(raw)
            clean = _strip_ansi(raw).rstrip()
            if clean:
                logger_method("%s %s", prefix, clean)
    finally:
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill `proc` and every descendant by sending SIGTERM/SIGKILL to its
    process group. Requires that proc was launched with start_new_session=True
    so it has its own pgid.

    Two-phase: SIGTERM, brief grace period, then SIGKILL on anything still
    alive. We don't bother polling — gradle/java sometimes ignores SIGTERM
    if it's mid-test, and we want to release the workdir promptly.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        # Process already dead.
        return
    for sig_name, sig in (("SIGTERM", signal.SIGTERM), ("SIGKILL", signal.SIGKILL)):
        try:
            os.killpg(pgid, sig)
            logger.info("sent %s to process group %d", sig_name, pgid)
        except ProcessLookupError:
            return  # nothing left
        except OSError as e:
            logger.warning("killpg(%d, %s) failed: %s", pgid, sig_name, e)
            return
        # Give the group up to 5s to drain after SIGTERM before escalating.
        if sig is signal.SIGTERM:
            time.sleep(5)


def run_agent(
    prompt: str,
    workdir: str,
    agent_name: str = "opensearch-maintainer-agent",
    timeout_s: int = 600,
    extra_env: Optional[dict] = None,
) -> AgentResult:
    """Run the agent against `prompt` with CWD=workdir.

    Streams stdout/stderr to the `agent.stream` logger. After the subprocess
    exits, reads `<workdir>/.bot-response.md` if it exists and returns its
    contents in `response_body`.

    Raises AgentRunError if kiro-cli is not on PATH or the workdir is invalid.
    """
    wd = Path(workdir)
    if not wd.exists():
        raise AgentRunError(f"workdir does not exist: {wd}")

    response_path = wd / RESPONSE_FILENAME
    # Defensive: make sure no stale file from a crashed prior run exists.
    # The bridge also does a git clean -fdx before each invocation, but we
    # belt-and-suspenders here.
    if response_path.exists():
        try:
            response_path.unlink()
        except OSError as e:
            logger.warning("could not remove stale %s: %s", response_path, e)

    env = os.environ.copy()
    env.setdefault("JAVA_HOME", "/usr/lib/jvm/java-21-amazon-corretto")
    if extra_env:
        env.update(extra_env)

    cmd = [
        "kiro-cli",
        "chat",
        "--agent",
        agent_name,
        "--no-interactive",
        "--trust-all-tools",
    ]
    logger.info("agent: %s (cwd=%s, timeout=%ds)", " ".join(cmd), wd, timeout_s)

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(wd),
            env=env,
            # Put the subprocess (and any descendants like gradle/java) in
            # its own process group so we can kill the whole tree on timeout.
            # Without this, proc.kill() only kills kiro-cli; gradle keeps
            # running in the background and holds the workdir.
            start_new_session=True,
        )
    except FileNotFoundError as e:
        raise AgentRunError(f"kiro-cli not found on PATH: {e}") from e

    assert proc.stdin is not None
    try:
        proc.stdin.write(prompt)
        proc.stdin.flush()
    finally:
        proc.stdin.close()

    stdout_buf: List[str] = []
    stderr_buf: List[str] = []

    t_out = threading.Thread(
        target=_stream_reader,
        args=(proc.stdout, stdout_buf, agent_stream_logger.info, "[agent]"),
        daemon=True,
    )
    t_err = threading.Thread(
        target=_stream_reader,
        args=(proc.stderr, stderr_buf, agent_stream_logger.warning, "[agent.err]"),
        daemon=True,
    )
    t_out.start()
    t_err.start()

    timed_out = False
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            exit_code = proc.wait(timeout=1.0)
            break
        except subprocess.TimeoutExpired:
            if time.monotonic() >= deadline:
                logger.error("agent exceeded %ds timeout, killing process group", timeout_s)
                _kill_process_tree(proc)
                timed_out = True
                try:
                    exit_code = proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    exit_code = -1
                break

    t_out.join(timeout=5.0)
    t_err.join(timeout=5.0)

    stdout = "".join(stdout_buf)
    stderr = "".join(stderr_buf)

    # Read the response file the agent (should have) written.
    response_body: Optional[str] = None
    if response_path.exists():
        try:
            response_body = response_path.read_text(encoding="utf-8")
            logger.info("read response file: %s (%d chars)", response_path, len(response_body))
        except OSError as e:
            logger.error("failed to read %s: %s", response_path, e)
    else:
        logger.warning(
            "agent did not write %s — stdout will be used as a fallback", response_path
        )

    return AgentResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        timed_out=timed_out,
        response_body=response_body,
    )


def extract_final_comment(result: "AgentResult") -> str:
    """Return the comment body to post to GitHub.

    Preference order:
      1. The contents of .bot-response.md written by the agent (authoritative).
      2. Fallback: de-ANSI'd stdout trimmed at the first status tag and before
         the kiro-cli credits line. Only used when the agent forgot to write
         the file; visible degradation surfaces the failure for debugging.
    """
    if result.response_body is not None:
        return result.response_body.strip()

    # Fallback path — best-effort salvage from stdout.
    stdout = _strip_ansi(result.stdout)
    stdout = re.sub(r"^\s*>\s+", "", stdout, count=1)
    stdout = re.sub(r"(?m)^> ", "", stdout)

    valid_tags = ("[Reproduced]", "[Not reproduced]", "[Partial reproduction]",
                  "[RCA]", "[Revised RCA]", "[Note]",
                  "[Error]", "[Needs info]", "[Out of scope]")
    best_pos = -1
    for tag in valid_tags:
        idx = -1
        searchfrom = 0
        while True:
            found = stdout.find(tag, searchfrom)
            if found < 0:
                break
            at_start = found == 0 or stdout[found - 1] == "\n"
            if at_start:
                idx = found
            searchfrom = found + 1
        if idx > best_pos:
            best_pos = idx
    body = stdout if best_pos < 0 else stdout[best_pos:]

    # Trim trailing kiro-cli "▸ Credits: ..." line.
    lines = body.splitlines()
    cutoff = len(lines)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if "Credits:" in stripped and ("Time:" in stripped or "•" in stripped):
            cutoff = i
            break
    body = "\n".join(lines[:cutoff]).strip()

    # Prepend a warning so maintainers can see something went wrong.
    return (
        "[Error] Agent did not write the expected response file. "
        "Best-effort fallback from stdout follows.\n\n"
        + body
    )


def truncate_comment(body: str, max_len: int) -> str:
    """Truncate to max_len chars, preserving the final footer if possible."""
    if len(body) <= max_len:
        return body
    marker = "\n\n<!-- ... response truncated by bridge ... -->\n\n"
    footer_start = body.rfind("\n---\n")
    if footer_start > 0 and len(body) - footer_start < 800:
        footer = body[footer_start:]
        head_budget = max_len - len(marker) - len(footer)
        if head_budget > 200:
            return body[:head_budget].rstrip() + marker + footer
    return body[: max_len - len(marker)].rstrip() + marker
