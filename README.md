# opensearch-maintainer-bridge

A polling daemon that connects GitHub to the [`OpenSearchMaintainerAgent`](https://github.com/ryan-gh-bot/opensearch-maintainer-agent) — an AI maintainer agent for OpenSearch-project repositories. Maintainers address the bot in a GitHub comment (slash-command or natural language); the bridge fetches context, invokes the agent, and posts the response back. For `/fix` requests, the bridge also pushes a branch and opens a pull request from the bot's fork.

This bridge is the **operational layer**. The agent (a separate AIM capabilities package) is the **knowledge layer**. The split lets the agent ship as installable AI capabilities while the bridge handles GitHub-specific I/O, authentication, and repo state.

## What it does

On each polling cycle:

1. Fetches new issue/PR comments from each watched repo (paginated, since-timestamp filtered).
2. For each new comment whose first non-blank line is `@<bot> <something>`:
   - Drops it if the commenter isn't on the maintainer allowlist.
   - Drops it if the bot itself is the author (loop prevention).
   - Parses the rest as either a slash-command (`/rca`, `/reproduce`, `/fix`) or a natural-language `@triage` invocation.
3. Prepares the tenant's local repo workdir (fetch, checkout, clean) and configures a `target` remote pointing at the issue's source repo.
4. For commands that may produce a PR (`/fix` and `@triage`): ensures the bot's fork of the target exists, lazy-creating it via the GitHub API on first use.
5. Fetches the full conversation thread on the issue/PR and embeds it in the agent's prompt as a `<<<CONVERSATION` block annotated with author/maintainer/bot/triggering flags.
6. Invokes the agent (`kiro-cli chat --agent opensearch-maintainer-agent --no-interactive --trust-all-tools`), streaming stdout/stderr to the bridge log so a human tailing the log can watch the agent work in real time.
7. After the agent exits:
   - Reads `.bot-response.md` from the workdir as the comment to post (the agent writes here, not to stdout).
   - For `/fix` paths only: if `.bot-pr-body.md` also exists and the agent committed, the bridge pushes the branch to the bot's fork and opens a cross-repo PR via the GitHub API, then posts a follow-up comment on the original issue with the PR link.

The daemon is intentionally small, single-threaded, and reads-only-what-it-needs.

## Invocation forms

Both work; both require an `@<bot>` mention as the first non-blank line of the comment.

**Slash-command (precise, fast path):**

```
@ryan-gh-bot /rca
@ryan-gh-bot /reproduce
@ryan-gh-bot /fix
```

**Natural language (engineer-style):**

```
@ryan-gh-bot, can you root cause this?
@ryan-gh-bot please fix it based on your rca above
@ryan-gh-bot what did you find?
@ryan-gh-bot I disagree with your rca, the actual cause is X
```

The bridge's parser is permissive about punctuation between the mention and the request (`,` `.` `:` `;` `!` `?` `–` `—` `-`).

For natural language, the agent classifies intent against the conversation thread (see the agent's `triage.sop.md`). It can chain on prior bot output ("based on your rca above"), reply with a `[Note]` instead of redoing work, push back with `[Revised RCA]`, or decline as `[Out of scope]`.

## Files

- `bridge.py` — main entry point, polling loop, dispatch, prompt construction, post-agent flow (push, PR creation, follow-up comment).
- `command_parser.py` — extracts slash-command or natural-language `@triage` invocation from the first line of a comment.
- `agent_runner.py` — invokes `kiro-cli` with line-by-line stream readers (live log) and a process-group SIGKILL on timeout. Handles ANSI escape stripping for the log; the comment body comes from `.bot-response.md`, not stdout.
- `github_client.py` — thin REST API wrapper. Endpoints: list issue/PR comments (poll + thread fetch), get issue, post comment, add reaction, get repo, fork detection + creation, PR creation.
- `workdir_manager.py` — git operations on the tenant's repo workdir: prepare (fetch/clean/checkout), ensure target remote, fetch+checkout target/base, has-unpushed-commits check, push branch, remote-branch-exists, latest commit subject. All git invocations run with `GIT_TERMINAL_PROMPT=0` so any auth issue fails fast.
- `state.py` — atomic JSON state. Per-repo `last_seen_comment_id` and `last_seen_at` so multi-repo polling doesn't conflate ids. v1→v2 migration on load.
- `config.py` — loads credentials and runtime config. Validates allowlist, watched-repo / tenant-workdir routing, command map.
- `config.example.yaml` — documented config template with all knobs.
- `requirements.txt` — `requests`, `pyyaml`.

## Setup

### 1. Install dependencies

```bash
cd ~/workplace/opensearch-maintainer-bridge   # or wherever you cloned
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Set up credentials

Create `~/.config/opensearch-sql-bot/credentials` (mode 600) with the bot's GitHub PAT and username:

```
GITHUB_BOT_TOKEN=<bot's PAT — needs `public_repo` scope minimum>
GITHUB_BOT_USERNAME=<the bot's GitHub login>
```

### 3. Set up a git credential helper for the tenant workdir

The bridge runs `git push` from each tenant's workdir, not from this bridge directory. The workdir needs the same credential lookup. Create `~/.config/opensearch-sql-bot/git-credentials` (mode 600):

```
https://<bot-username>:<bot-PAT>@github.com
```

Then in each tenant workdir (e.g., `~/opensearch-sql-bot-workdir/sql`):

```bash
cd <tenant-workdir>
git config --local credential.helper "store --file=$HOME/.config/opensearch-sql-bot/git-credentials"
```

This is per-repo so it doesn't pollute global git config or affect other work on the same host.

### 4. Configure the bridge

```bash
cp config.example.yaml ~/.config/opensearch-maintainer-bot/config.yaml
# Edit:
#   - watched_repos: list of "owner/repo" strings to poll.
#   - repo_tenant_map: each watched repo → tenant identifier the agent uses.
#   - tenant_workdirs: each tenant → absolute path of the local repo checkout.
#     The workdir must already exist as a git checkout with origin pointing
#     at the bot's fork.
#   - allowlist: GitHub usernames permitted to invoke commands.
#   - commands: map of "/<command>" → SOP name (must match agent SOP files).
#     Include "@triage": "triage" for natural-language support.
#   - poll_interval_seconds (default 30).
#   - agent_timeout_seconds (600 default for /rca and /reproduce).
#   - fix_agent_timeout_seconds (2400 default — /fix runs build + tests).
#   - acknowledgment_mode: "comment" | "reaction" | "none".
#   - dry_run: true to log what would be posted without posting (useful for testing).
```

### 5. Pre-clone tenant workdirs

For each tenant, the workdir must be a git checkout with `origin` pointing at the bot's fork (the bridge will configure a `target` remote per-invocation):

```bash
mkdir -p ~/opensearch-sql-bot-workdir
cd ~/opensearch-sql-bot-workdir
git clone https://github.com/<bot>/<repo>.git <tenant-name>
cd <tenant-name>
# (optional — for /rca and /reproduce convenience) add an upstream remote:
git remote add upstream https://github.com/<canonical-owner>/<repo>.git
```

## Running

```bash
# One poll cycle, then exit (useful for testing)
python3 bridge.py --once

# Continuous polling
python3 bridge.py

# Verbose logging
python3 bridge.py --debug
```

Logs go to stderr and to `~/.local/state/opensearch-maintainer-bot/bridge.log`. State is persisted after every iteration so restarts don't replay processed comments.

Stop with Ctrl+C; the daemon finishes the current poll cycle and exits cleanly.

## Configuration reference

See `config.example.yaml` for the full schema with comments. Key knobs:

| Key | Default | Notes |
|---|---|---|
| `watched_repos` | (required) | List of `owner/repo` strings to poll. |
| `repo_tenant_map` | (required) | Maps each watched repo to a tenant identifier. Multiple repos can share a tenant (e.g. a fork and the upstream). |
| `tenant_workdirs` | (required) | Absolute path on this host for each tenant's git checkout. |
| `allowlist` | (required) | Maintainer usernames permitted to invoke. Unauthorized comments are silently ignored. |
| `commands` | (required) | Maps `/<command>` and `@triage` to agent SOP names. |
| `poll_interval_seconds` | 30 | Polling cadence. |
| `agent_timeout_seconds` | 600 | Default subprocess timeout for `/rca` and `/reproduce`. |
| `fix_agent_timeout_seconds` | 2400 | Subprocess timeout for `/fix` and `@triage` (longer because they may run build + tests). |
| `acknowledgment_mode` | `comment` | `comment` posts a "Working on..." comment; `reaction` adds a 👀 reaction; `none` is silent. |
| `max_comment_length` | 60000 | Hard cap; the bridge truncates with a notice if exceeded. |
| `dry_run` | false | If true, log what would be posted but don't actually post. |

## Security model

- **Allowlist-gated.** Only comments from maintainer usernames in `allowlist` trigger the bot. Anything else is silently ignored — no "not authorized" comment, to avoid leaking the bot's existence to spam-prone outsiders.
- **Loop-safe.** The bot's own comments are skipped by the bot username and a recently-posted ids set.
- **Mention-required.** The first non-blank line of a comment must start with `@<bot-username>`. Mentions of the bot inside threaded quotes or other contexts don't trigger; only first-line invocations do.
- **No arbitrary shell.** The bridge invokes `kiro-cli chat` with a fixed argv. Comment bodies and natural-language requests are passed via stdin inside fenced data blocks; they do not become command-line args.
- **Untrusted-content fencing.** The agent's prompt clearly labels issue bodies, prior comments, and natural-language requests as DATA in named fenced blocks. The agent's system prompt forbids treating fenced content as instructions. Prompt-injection resistance is imperfect but structurally encouraged.
- **Process-tree kill on timeout.** When the agent's timeout fires, the bridge sends SIGTERM/SIGKILL to the subprocess's process group, ensuring gradle/java subprocesses don't survive. Without this, `/fix`'s test runs could keep running after the bridge thought the agent was dead.
- **Fail-fast git auth.** All git invocations from the bridge run with `GIT_TERMINAL_PROMPT=0`, so an auth misconfiguration produces an immediate error instead of a silent hang.
- **Never shells the agent's own writes.** The agent writes its comment body to `.bot-response.md` (and PR body to `.bot-pr-body.md` for `/fix`); the bridge reads those files and posts them via the GitHub API. The agent has no GitHub token, no push capability, no PR-creation capability.
- **`/fix` writes only to the bot's fork.** The bridge pushes branches to the bot's fork (`<bot-username>/<repo>`), never to the upstream target. PRs are opened from the bot's fork as cross-repo PRs targeting the issue's source repo.

## State

`~/.local/state/opensearch-maintainer-bot/state.json`:

```jsonc
{
  "version": 2,
  "per_repo": {
    "RyanL1997/sql":          {"last_seen_comment_id": 4567848532, "last_seen_at": "2026-05-28T19:50:11Z"},
    "opensearch-project/sql": {"last_seen_comment_id": 4567852833, "last_seen_at": "2026-05-28T20:30:00Z"}
  },
  "posted_comment_ids": [4567658640, 4567729612, ...]
}
```

Per-repo state means high-traffic repos don't cause low-traffic repos to skip comments. Posted-ids set caps at 1000 entries (rolling).

## Roadmap / future work

- **PR revision flow.** When the bot is asked to update a PR it previously authored, push additional commits (or `--force-with-lease`) to the existing branch instead of refusing. Today `remote_branch_exists` causes `/fix` to refuse on re-invocation; the SOP recognizes the "address review feedback" pattern but the bridge's push path doesn't yet handle it.
- **Inline PR review comments.** Fetch comments from `/repos/{repo}/pulls/{n}/comments` (the line-anchored review comment endpoint, distinct from the issue conversation endpoint we already use) and surface them to the agent for "address the inline comment on file X line Y" requests.
- **Multi-author conversation citations.** The agent can reference prior comments by `[N]` (the index in the embedded `<<<CONVERSATION` block) but doesn't yet emit GitHub-permalink shortlinks like `https://github.com/.../#issuecomment-12345`. Useful for making `[Revised RCA]` comments self-linking.
- **Dynamic allowlist.** Today the allowlist is a static config list. Pulling from `MAINTAINERS.md` in each watched repo at startup (with periodic refresh) would auto-track maintainer changes.
- **Webhook delivery.** Polling at 30s is fine at this volume but a webhook receiver would reduce latency. Requires a publicly-reachable endpoint, which a dev-desktop deployment doesn't have.

## Companion package

- `OpenSearchMaintainerAgent` — the AIM agent capabilities package this bridge invokes (knowledge layer). The agent's SOPs document each command's behavior in detail. Hosted internally; not currently mirrored to GitHub.
