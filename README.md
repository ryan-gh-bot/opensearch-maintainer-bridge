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
3. For each PR the bot previously authored (tracked in state): also fetches new top-level review wrappers (`/pulls/N/reviews`) and line-anchored review comments (`/pulls/N/comments`). `@<bot>` mentions in either are dispatched to the same handlers as conversation comments.
4. Prepares the tenant's local repo workdir (fetch, checkout, clean) and configures a `target` remote pointing at the issue's source repo. For PR revisions, instead checks out the existing PR's branch (`origin/<branch>`) so additional commits land on the same branch.
5. For commands that may produce a PR (`/fix` and `@triage`): ensures the bot's fork of the target exists, lazy-creating it via the GitHub API on first use.
6. Fetches the full conversation thread on the issue/PR — including PR reviews and review-comments when applicable — and embeds it in the agent's prompt as a `<<<CONVERSATION` block annotated with `kind=`, `state=`, `file=`, `line=`, `author`, `maintainer`, `bot`, and `triggering` flags.
7. Invokes the agent (`kiro-cli chat --agent opensearch-maintainer-agent --no-interactive --trust-all-tools`), streaming stdout/stderr to the bridge log so a human tailing the log can watch the agent work in real time.
8. After the agent exits:
   - Reads `.bot-response.md` from the workdir as the comment to post (the agent writes here, not to stdout).
   - For `/fix` paths: if `.bot-pr-body.md` also exists and the agent committed, the bridge pushes the branch and either (a) opens a new cross-repo PR via the GitHub API or (b) for revisions, GitHub auto-updates the existing PR and the bridge posts a "pushed updates" follow-up. PR revisions push with `--force-with-lease` so the agent can rebase if needed.

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

## End-to-end setup

> **Heads up:** the bridge requires the companion `OpenSearchMaintainerAgent` AIM capabilities package to be installed and runnable on this same host. That package is currently built with Amazon-internal tooling (Brazil + AIM + kiro-cli). The bridge itself is generic Python and runs anywhere; the dependency is on having the agent reachable via the configured CLI invocation. See the agent package's README for its install path. If you're outside Amazon and want to run this end-to-end, you'll need to port the agent's install path to a public LLM-CLI runtime; the bridge code's `agent_runner.py` invokes the configured CLI via subprocess, so swapping the runtime is contained.

This setup walks through every step from a fresh Linux/Unix host. Follow in order; each step has a verification command.

### Prerequisites

- Linux or macOS host that can run a long-lived daemon. A developer desktop is fine.
- Python 3.10+ (`python3 --version`).
- Git 2.x (`git --version`).
- The [`OpenSearchMaintainerAgent`](https://github.com/ryan-gh-bot/opensearch-maintainer-agent) AIM capabilities package installed locally (the bridge invokes it as a subprocess).
- A second GitHub account dedicated to the bot — do **not** reuse a personal account. Throughout this README, examples use `ryan-gh-bot` as the bot account login and `RyanL1997` as the human-maintainer login; substitute your own.

### Step 1. Create the bot's GitHub account and PAT

1. Sign up on github.com with a new email and a username for the bot (e.g., `ryan-gh-bot`).
2. While logged in as that account, go to **Settings → Developer settings → Personal access tokens**.
3. Generate a new PAT.
   - **Classic** PAT: `repo` scope (covers public + private repos the account has access to).
   - **Fine-grained** PAT scoped to specific repos: enable **Issues** (read+write), **Pull requests** (read+write), **Contents** (read+write), **Metadata** (read).
4. Save the PAT string somewhere temporarily — you'll put it on disk in step 3, then discard the working copy.

Verify the bot account is the one you'll use:

```bash
curl -s -H "Authorization: token <paste-PAT-here>" https://api.github.com/user \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('login:', d.get('login'))"
```

Expected: prints the bot's login.

### Step 2. Clone the bridge

```bash
mkdir -p ~/workplace
cd ~/workplace
git clone https://github.com/ryan-gh-bot/opensearch-maintainer-bridge.git
cd opensearch-maintainer-bridge
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Verify imports work:

```bash
python3 -c "import bridge, agent_runner, config, github_client, command_parser, state, workdir_manager; print('imports ok')"
```

### Step 3. Save the bot credentials on disk

```bash
mkdir -p ~/.config/opensearch-sql-bot
chmod 700 ~/.config/opensearch-sql-bot

cat > ~/.config/opensearch-sql-bot/credentials <<'EOF'
GITHUB_BOT_TOKEN=<paste-the-PAT>
GITHUB_BOT_USERNAME=<bot-account-login>
EOF
chmod 600 ~/.config/opensearch-sql-bot/credentials
```

Verify the credentials work:

```bash
set -a; . ~/.config/opensearch-sql-bot/credentials; set +a
curl -s -H "Authorization: token $GITHUB_BOT_TOKEN" \
  https://api.github.com/user \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('login:', d.get('login'))"
```

Expected: prints the bot's login.

### Step 4. Save git push credentials in the same directory

The bridge's `git push` invocations need to authenticate as the bot. Create a separate `git-credentials` file (same secret, different format — git's credential helper expects URL-style entries):

```bash
( umask 077 && \
  printf 'https://%s:%s@github.com\n' "$GITHUB_BOT_USERNAME" "$GITHUB_BOT_TOKEN" \
  > ~/.config/opensearch-sql-bot/git-credentials \
)
chmod 600 ~/.config/opensearch-sql-bot/git-credentials
ls -l ~/.config/opensearch-sql-bot/git-credentials
```

Expected: file exists with mode `600`.

### Step 5. Configure the bridge

```bash
mkdir -p ~/.config/opensearch-maintainer-bot
cp config.example.yaml ~/.config/opensearch-maintainer-bot/config.yaml
$EDITOR ~/.config/opensearch-maintainer-bot/config.yaml
```

Edit these fields:

| Field | What to put |
|---|---|
| `watched_repos` | List of `owner/repo` strings to poll. For initial testing, use a fork you own (e.g., `RyanL1997/sql`); you can add the upstream (`opensearch-project/sql`) once the test repo works. |
| `repo_tenant_map` | Map each watched repo to a tenant identifier the agent knows (e.g., `sql`). |
| `tenant_workdirs` | Each tenant → absolute path of a local repo checkout you'll create in step 6. |
| `allowlist` | GitHub usernames of maintainers who can invoke the bot. **Include yourself.** |
| `commands` | Leave the defaults: `/rca`, `/reproduce`, `/fix`, `@triage`. |
| `dry_run` | Set to `true` for the first test run — the bridge will log what it would post without actually posting. Switch to `false` once verified. |

Verify the config loads cleanly:

```bash
cd ~/workplace/opensearch-maintainer-bridge
source .venv/bin/activate
python3 -c "from config import load_config; cfg = load_config(); print('OK:', cfg.watched_repos)"
```

Expected: prints `OK: [...]` with your watched repos. Errors here indicate missing config or referenced workdirs that don't exist yet (you'll create them next).

### Step 6. Pre-clone tenant workdirs

For each tenant in `tenant_workdirs`, create a git checkout. The `origin` of each workdir **must point at the bot's fork** (the bridge pushes there). The bot's fork can be lazy-created on first `/fix`, but for now we'll clone it to populate the workdir:

```bash
mkdir -p ~/opensearch-sql-bot-workdir
cd ~/opensearch-sql-bot-workdir
git clone https://github.com/$GITHUB_BOT_USERNAME/<repo>.git <tenant-name>
cd <tenant-name>

# Add an `upstream` remote pointing at the canonical repo (used by /rca and /reproduce)
git remote add upstream https://github.com/<canonical-owner>/<repo>.git
git remote set-url --push upstream DISABLED
git fetch upstream

# Configure the credential helper for git push from this workdir
git config --local credential.helper "store --file=$HOME/.config/opensearch-sql-bot/git-credentials"
```

Verify push auth works (no actual push — just dry run):

```bash
git push --dry-run origin
```

Expected: `Everything up-to-date` with no credential prompt. If git prompts for username/password, the credential helper is misconfigured — re-run the `git config --local credential.helper ...` command above.

If the bot doesn't yet have a fork of the repo, **either**:
- Pre-create the fork in the GitHub UI (visit the canonical repo as the bot, click Fork), or
- Skip the workdir clone for now; the bridge will lazy-create the fork on the first `/fix` invocation. In that case, configure `tenant_workdirs` to point at a path that exists as a git checkout of any related repo with the same name (the bridge's `prepare_workdir` will reset it).

The bot's fork can serve PRs to multiple targets that share git history — e.g., one `ryan-gh-bot/sql` fork can be the head for PRs into `RyanL1997/sql` AND `opensearch-project/sql`, since all three share base commits. You don't need a separate fork per target.

### Step 7. Smoke test (dry run)

With `dry_run: true` set in config, run a single poll cycle:

```bash
cd ~/workplace/opensearch-maintainer-bridge
source .venv/bin/activate
python3 bridge.py --once --debug
```

Expected output:

- `github-bridge starting`
- `config: bot=<bot-username> watched_repos=[...]`
- For each watched repo: `polling <repo> (since=<timestamp>)` and `poll <repo>: processed=0 skipped=0`
- Exit cleanly

If errors:
- `credentials file not found` → step 3
- `watched_repo X not in repo_tenant_map` → step 5
- `workdir for tenant X is not a git checkout` → step 6 (the workdir path in your config doesn't exist or isn't a git repo)
- `kiro-cli not found on PATH` → the agent isn't installed; see the agent package README

### Step 8. Live smoke test

Pick one of your watched repos and an issue you control. As an allowlisted maintainer, post a comment with exactly:

```
@<bot-username> /rca
```

Switch the bridge config to `dry_run: false`:

```bash
sed -i 's/^dry_run: true/dry_run: false/' ~/.config/opensearch-maintainer-bot/config.yaml
```

Run another single poll cycle:

```bash
python3 bridge.py --once --debug
```

Expected:

- `dispatching: repo=<repo> ... cmd=/rca`
- `ack comment posted: <id>` — a "Working on" comment appears on the issue
- `invoking agent (timeout=600s, ...)` — agent starts
- Stream of `[agent]` log lines as the agent reads files, runs git commands, etc.
- `agent finished: exit=0 ...`
- `posted comment on <repo>#N: <url>` — the real `[RCA]` comment appears on the issue

If the agent stream stalls or the bridge hangs on `git push`, see Troubleshooting below.

### Step 9. Run the bridge as a daemon

For ongoing operation, run the bridge in continuous polling mode:

```bash
cd ~/workplace/opensearch-maintainer-bridge
source .venv/bin/activate
nohup python3 bridge.py --debug > /tmp/bridge.out 2>&1 &
echo $! > /tmp/bridge.pid
```

Tail the log:

```bash
tail -f /tmp/bridge.out | grep -vE urllib3
```

Stop:

```bash
kill $(cat /tmp/bridge.pid)
```

For production-ish usage, wrap this in a systemd user service or a tmux session — the bridge has no built-in process manager.

## Configuration reference

See `config.example.yaml` for the full schema. Key knobs:

| Key | Default | Notes |
|---|---|---|
| `watched_repos` | (required) | List of `owner/repo` strings to poll. |
| `repo_tenant_map` | (required) | Maps each watched repo to a tenant identifier. Multiple repos can share a tenant. |
| `tenant_workdirs` | (required) | Absolute path on this host for each tenant's git checkout. |
| `allowlist` | (required) | Maintainer usernames permitted to invoke. Unauthorized comments are silently ignored. |
| `commands` | (required) | Maps `/<command>` and `@triage` to agent SOP names. |
| `poll_interval_seconds` | 30 | Polling cadence. |
| `agent_timeout_seconds` | 600 | Default subprocess timeout for `/rca` and `/reproduce`. |
| `fix_agent_timeout_seconds` | 2400 | Subprocess timeout for `/fix` and `@triage` (longer because they may run build + tests). |
| `acknowledgment_mode` | `comment` | `comment` posts a "Working on..." comment; `reaction` adds 👀; `none` is silent. |
| `max_comment_length` | 60000 | Hard cap; the bridge truncates with a notice if exceeded. |
| `dry_run` | false | If true, log what would be posted without actually posting. |

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

## Troubleshooting

**Bridge sees the comment but doesn't dispatch (`processed=0 skipped=1`).**
The parser likely rejected it. The first non-blank line of the comment must start with `@<bot-username>` (case-insensitive) followed by either `/cmd` or freeform text. Common gotchas: leading whitespace doesn't count toward "first non-blank line" content, but text *before* the mention does — `hi @bot, can you...` is rejected. Use `@bot, can you...` instead.

**Bridge dispatches but `git push` hangs and times out.**
Step 6's credential helper isn't configured for the workdir. Re-run the `git config --local credential.helper "store --file=..."` command **inside the workdir directory**, then verify with `git push --dry-run origin`.

**Agent invocation fails with "kiro-cli not found on PATH".**
The agent package isn't installed on this host. See the agent package's README for the install procedure.

**Agent times out at 600s on a `/fix` invocation.**
`/fix` runs builds + tests and the default 600s is too short. The bridge already uses `fix_agent_timeout_seconds: 2400` for `/fix` and `@triage`. If you've overridden it, raise it.

**Bot leaves orphan gradle/java processes after a timeout.**
Should not happen with the current bridge — `agent_runner.py` SIGKILLs the entire process group on timeout. If you see orphans, check that `start_new_session=True` is set in the subprocess invocation (it is in the shipped code) and that gradle isn't being launched outside the agent's process tree (it shouldn't be).

**The first `/fix` invocation fails to create the fork.**
Lazy fork creation calls `POST /repos/<target>/forks`. The PAT needs write access. For classic PATs, `repo` scope is enough; for fine-grained PATs, the bot account needs at least the **public_repo** permission level on the target repo (or the target must allow forks from any user, which is the default for public repos).

**State file gets out of sync (bridge re-processes comments after a restart).**
Edit `~/.local/state/opensearch-maintainer-bot/state.json` directly, or delete it to start fresh. The bridge will set `last_seen_at` to "now" if the file is missing.

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

- **Multi-author conversation citations.** The agent can reference prior comments by `[N]` (the index in the embedded `<<<CONVERSATION` block) but doesn't yet emit GitHub-permalink shortlinks like `https://github.com/.../#issuecomment-12345`. Useful for making `[Revised RCA]` comments self-linking.
- **Dynamic allowlist.** Today the allowlist is a static config list. Pulling from `MAINTAINERS.md` in each watched repo at startup (with periodic refresh) would auto-track maintainer changes.
- **Webhook delivery.** Polling at 30s is fine at this volume but a webhook receiver would reduce latency. Requires a publicly-reachable endpoint, which a dev-desktop deployment doesn't have.

## Companion package

- `OpenSearchMaintainerAgent` — the AIM agent capabilities package this bridge invokes (knowledge layer). The agent's SOPs document each command's behavior in detail. Hosted internally; not currently mirrored to GitHub.
