# github-bridge

Polling daemon that translates GitHub slash-commands into kiro-cli agent invocations.

## What it does

1. Polls a configured GitHub repo for new issue/PR comments.
2. For comments from allowlisted maintainers that start with a slash-command, dispatches to the `opensearch-maintainer-agent` via `kiro-cli chat --no-interactive`.
3. Posts the agent's response back to the same issue/PR as a GitHub comment.

It is intentionally small, single-threaded, and reads-only-what-it-needs.

## Files

- `bridge.py` — main entry point and polling loop.
- `github_client.py` — thin GitHub REST API wrapper (comments list/post, reactions).
- `command_parser.py` — extracts `/cmd` from comment bodies.
- `agent_runner.py` — invokes `kiro-cli chat --no-interactive`, captures stdout, enforces timeout.
- `workdir_manager.py` — prepares the tenant's repo checkout (`git fetch upstream`, checkout).
- `config.py` — loads credentials, allowlist, repo→tenant routing.
- `state.py` — read/write last-processed comment id.
- `config.example.yaml` — documented config template.
- `requirements.txt` — dependencies (just `requests` and `pyyaml`).

## Setup

```bash
cd ~/workplace/github-bridge
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Credentials
# Already set up at ~/.config/opensearch-sql-bot/credentials (GITHUB_BOT_TOKEN, GITHUB_BOT_USERNAME)

# Config
cp config.example.yaml ~/.config/opensearch-maintainer-bot/config.yaml
# Edit to pick the watched repo, tenant routing, allowlist
```

## Running

```bash
# One-shot test (processes whatever's new, exits)
python3 bridge.py --once

# Continuous polling loop
python3 bridge.py
```

Logs go to stderr and to `~/.local/state/opensearch-maintainer-bot/bridge.log`.

## Stopping cleanly

Ctrl+C. The state file is persisted after every iteration, so restarts don't replay processed comments.

## Security model

- The bridge will ignore any comment from anyone not in the configured allowlist.
- The bridge will ignore its own comments (the `bot_username` from credentials).
- The bridge does NOT execute arbitrary shell supplied by a comment — it only invokes `kiro-cli chat` with a fixed set of parameters.
- The kiro-cli agent itself runs with filesystem access to the tenant's repo workdir only, per the agent spec's `allowedTools`.
