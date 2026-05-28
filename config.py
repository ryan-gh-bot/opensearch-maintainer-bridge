"""Configuration loader for the github-bridge daemon.

Loads:
  - Credentials (GITHUB_BOT_TOKEN, GITHUB_BOT_USERNAME) from
    ~/.config/opensearch-sql-bot/credentials (a simple KEY=value file).
  - Runtime config from ~/.config/opensearch-maintainer-bot/config.yaml
    (the schema is documented in config.example.yaml).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

import yaml

CREDENTIALS_PATH = Path.home() / ".config" / "opensearch-sql-bot" / "credentials"
CONFIG_PATH = Path.home() / ".config" / "opensearch-maintainer-bot" / "config.yaml"


class ConfigError(Exception):
    """Raised when config is missing or malformed."""


@dataclass
class Config:
    # Credentials
    github_bot_token: str
    github_bot_username: str

    # Runtime config
    watched_repos: List[str]
    repo_tenant_map: Dict[str, str]
    tenant_workdirs: Dict[str, str]
    allowlist: List[str]
    commands: Dict[str, str]
    poll_interval_seconds: int
    agent_timeout_seconds: int
    fix_agent_timeout_seconds: int
    acknowledgment_mode: str
    max_comment_length: int
    dry_run: bool

    # Derived
    allowlist_lower: List[str] = field(default_factory=list)
    bot_username_lower: str = ""

    def __post_init__(self) -> None:
        self.allowlist_lower = [u.lower() for u in self.allowlist]
        self.bot_username_lower = self.github_bot_username.lower()

    def tenant_for(self, repo: str) -> str | None:
        return self.repo_tenant_map.get(repo)

    def workdir_for(self, tenant: str) -> str | None:
        return self.tenant_workdirs.get(tenant)

    def sop_for(self, command: str) -> str | None:
        return self.commands.get(command)

    def is_allowed(self, username: str) -> bool:
        return username.lower() in self.allowlist_lower

    def is_self(self, username: str) -> bool:
        return username.lower() == self.bot_username_lower


def _parse_env_file(path: Path) -> Dict[str, str]:
    """Parse a KEY=value file. Blank lines and lines starting with # are ignored."""
    if not path.exists():
        raise ConfigError(f"credentials file not found: {path}")
    out: Dict[str, str] = {}
    line_re = re.compile(r"^([A-Z_][A-Z0-9_]*)=(.*)$")
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = line_re.match(line)
            if not m:
                raise ConfigError(f"malformed line in {path}: {raw!r}")
            key, val = m.group(1), m.group(2)
            # Strip optional surrounding quotes
            if val.startswith(('"', "'")) and val.endswith(val[0]) and len(val) >= 2:
                val = val[1:-1]
            out[key] = val
    return out


def _load_credentials(path: Path = CREDENTIALS_PATH) -> Dict[str, str]:
    env = _parse_env_file(path)
    for required in ("GITHUB_BOT_TOKEN", "GITHUB_BOT_USERNAME"):
        if required not in env or not env[required]:
            raise ConfigError(f"{required} missing or empty in {path}")
    return env


def _load_yaml(path: Path = CONFIG_PATH) -> Dict:
    if not path.exists():
        raise ConfigError(
            f"config file not found: {path}. Copy config.example.yaml to this path."
        )
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ConfigError(f"top-level YAML in {path} must be a mapping")
    return data


def load_config(
    credentials_path: Path = CREDENTIALS_PATH,
    config_path: Path = CONFIG_PATH,
) -> Config:
    env = _load_credentials(credentials_path)
    cfg = _load_yaml(config_path)

    required_keys = [
        "repo_tenant_map",
        "tenant_workdirs",
        "allowlist",
        "commands",
        "poll_interval_seconds",
        "agent_timeout_seconds",
        "acknowledgment_mode",
        "max_comment_length",
        "dry_run",
    ]
    missing = [k for k in required_keys if k not in cfg]
    if missing:
        raise ConfigError(f"missing required keys in {config_path}: {missing}")

    # Accept either:
    #   watched_repos: [a/b, c/d]     (preferred)
    #   watched_repo:  "a/b"          (legacy, single repo)
    if "watched_repos" in cfg:
        watched_repos = list(cfg["watched_repos"])
    elif "watched_repo" in cfg:
        watched_repos = [cfg["watched_repo"]]
    else:
        raise ConfigError(
            f"config must define `watched_repos` (list) or `watched_repo` (string) in {config_path}"
        )
    if not watched_repos:
        raise ConfigError("`watched_repos` must not be empty")

    # Every watched repo must route to a known tenant with a valid workdir.
    for repo in watched_repos:
        if repo not in cfg["repo_tenant_map"]:
            raise ConfigError(
                f"watched repo {repo!r} is not in repo_tenant_map"
            )
        tenant = cfg["repo_tenant_map"][repo]
        if tenant not in cfg["tenant_workdirs"]:
            raise ConfigError(
                f"tenant {tenant!r} for watched repo {repo!r} is not in tenant_workdirs"
            )
        workdir = Path(cfg["tenant_workdirs"][tenant])
        if not workdir.exists():
            raise ConfigError(
                f"workdir for tenant {tenant!r} does not exist: {workdir}"
            )
        if not (workdir / ".git").exists():
            raise ConfigError(
                f"workdir for tenant {tenant!r} is not a git checkout: {workdir}"
            )

    if cfg["acknowledgment_mode"] not in ("comment", "reaction", "none"):
        raise ConfigError(
            f"acknowledgment_mode must be one of comment|reaction|none, got "
            f"{cfg['acknowledgment_mode']!r}"
        )

    return Config(
        github_bot_token=env["GITHUB_BOT_TOKEN"],
        github_bot_username=env["GITHUB_BOT_USERNAME"],
        watched_repos=watched_repos,
        repo_tenant_map=dict(cfg["repo_tenant_map"]),
        tenant_workdirs=dict(cfg["tenant_workdirs"]),
        allowlist=list(cfg["allowlist"]),
        commands=dict(cfg["commands"]),
        poll_interval_seconds=int(cfg["poll_interval_seconds"]),
        agent_timeout_seconds=int(cfg["agent_timeout_seconds"]),
        # Per-command override for /fix; defaults to 4x the standard timeout
        # because /fix typically runs build + tests in addition to investigation.
        fix_agent_timeout_seconds=int(
            cfg.get("fix_agent_timeout_seconds", int(cfg["agent_timeout_seconds"]) * 4)
        ),
        acknowledgment_mode=cfg["acknowledgment_mode"],
        max_comment_length=int(cfg["max_comment_length"]),
        dry_run=bool(cfg["dry_run"]),
    )
