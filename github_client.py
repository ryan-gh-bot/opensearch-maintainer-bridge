"""GitHub REST API wrapper for the bridge.

Only the operations the bridge needs:
  - list_issue_comments(repo, since)    — GET repo issue-comments with ?since=
  - get_issue(repo, number)             — GET a single issue (for title/body)
  - post_comment(repo, number, body)    — POST a comment on an issue/PR
  - add_reaction(repo, comment_id, rx)  — POST a reaction on a comment

Why not use PyGithub or ghapi? The bridge needs ~5 endpoints. A thin wrapper
over `requests` is fewer moving parts, easier to reason about, and doesn't
pin us to a library's release cadence.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Iterable, Optional

import requests

logger = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"
DEFAULT_TIMEOUT_S = 30


@dataclass
class Comment:
    id: int
    issue_number: int  # applies to both issues and PRs (PRs are issues on this endpoint)
    user: str
    body: str
    created_at: str  # ISO-8601
    updated_at: str
    html_url: str
    # Derived from html_url — whether the comment is on a pull request or an issue.
    # We don't rely on this today but exposing it keeps future commands simpler.
    is_pull_request: bool
    # If this Comment was synthesized from a PR line-anchored review comment,
    # the original review comment's id. Used by the bridge to post replies in
    # the same review-comment thread (via the /pulls/{n}/comments/{id}/replies
    # endpoint) instead of the conversation tab. None for normal comments and
    # for top-level review wrappers (which have no per-line thread).
    review_comment_id: Optional[int] = None


class GitHubError(Exception):
    """Raised when a GitHub API call returns a non-successful status."""

    def __init__(self, message: str, status_code: int, body: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class GitHubClient:
    def __init__(self, token: str, user_agent: str = "opensearch-maintainer-bot/0.1") -> None:
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": user_agent,
            }
        )

    # ---- public API ----

    def list_issue_comments(
        self,
        repo: str,
        since_iso: Optional[str] = None,
        per_page: int = 100,
    ) -> Iterable[Comment]:
        """Yield issue comments (newest last) across both issues and PRs.

        GitHub's /repos/{repo}/issues/comments endpoint returns comments for BOTH
        issues and pull requests (PRs are issues in their REST model). Filtering
        by ?since= returns comments whose updated_at >= since_iso, sorted ascending.
        """
        url = f"{API_ROOT}/repos/{repo}/issues/comments"
        params = {"per_page": str(per_page), "sort": "updated", "direction": "asc"}
        if since_iso:
            params["since"] = since_iso
        # Follow pagination via Link header.
        while url:
            resp = self._get(url, params=params)
            for raw in resp.json():
                yield _comment_from_api(raw)
            url = _next_link(resp.headers.get("Link", ""))
            params = {}  # next-link URL already has them

    def post_comment(self, repo: str, issue_number: int, body: str) -> Comment:
        """Post a comment on an issue/PR. Returns the created Comment."""
        url = f"{API_ROOT}/repos/{repo}/issues/{issue_number}/comments"
        resp = self._post(url, json={"body": body})
        return _comment_from_api(resp.json())

    def create_review_comment_reply(
        self,
        repo: str,
        pr_number: int,
        review_comment_id: int,
        body: str,
    ) -> dict:
        """Post a reply inside a PR review-comment thread (the line-anchored
        thread shown in the Files-changed tab and on the conversation timeline
        as a threaded reply rather than a top-level comment).

        Uses POST /repos/{repo}/pulls/{pull_number}/comments/{comment_id}/replies.

        Returns the new review-comment dict (same shape as list_pr_review_comments
        entries). The caller should use the returned `id` to advance any cursors
        if needed — though in practice the bridge skips bot-authored entries on
        re-poll, so this isn't strictly necessary.
        """
        url = (
            f"{API_ROOT}/repos/{repo}/pulls/{pr_number}/comments/"
            f"{review_comment_id}/replies"
        )
        resp = self._post(url, json={"body": body})
        return resp.json()

    def add_reaction(self, repo: str, comment_id: int, reaction: str = "eyes") -> None:
        """Add a reaction emoji to an issue/PR conversation comment. Valid
        reactions per GitHub docs: +1, -1, laugh, confused, heart, hooray,
        rocket, eyes.
        """
        url = f"{API_ROOT}/repos/{repo}/issues/comments/{comment_id}/reactions"
        self._post(url, json={"content": reaction})

    def add_pr_review_comment_reaction(
        self, repo: str, review_comment_id: int, reaction: str = "eyes"
    ) -> None:
        """Add a reaction emoji to a PR line-anchored review comment. The
        endpoint is distinct from add_reaction (which is for issue/PR
        conversation comments). Valid reactions are the same set."""
        url = (
            f"{API_ROOT}/repos/{repo}/pulls/comments/"
            f"{review_comment_id}/reactions"
        )
        self._post(url, json={"content": reaction})

    def get_issue(self, repo: str, issue_number: int) -> dict:
        """Return the full issue/PR JSON as a dict."""
        url = f"{API_ROOT}/repos/{repo}/issues/{issue_number}"
        resp = self._get(url)
        return resp.json()

    def list_comments_on_issue(
        self,
        repo: str,
        issue_number: int,
        per_page: int = 100,
    ) -> list:
        """Return all comments on a single issue/PR conversation, oldest-first.

        Uses /repos/{repo}/issues/{n}/comments — note this endpoint covers
        BOTH issue conversations and PR conversation comments (PRs are issues
        in GitHub's REST model). Inline review comments on PR diffs are a
        separate endpoint that we don't use here.

        Returns Comment objects with full body, author, timestamps. Caller is
        responsible for any truncation when embedding in a prompt.
        """
        url = f"{API_ROOT}/repos/{repo}/issues/{issue_number}/comments"
        params = {"per_page": str(per_page)}
        out: list = []
        while url:
            resp = self._get(url, params=params)
            for raw in resp.json():
                out.append(_comment_from_api(raw))
            url = _next_link(resp.headers.get("Link", ""))
            params = {}  # next-link URL has params baked in
        return out

    # ---- pull request reviews and review comments ----
    #
    # GitHub has THREE distinct comment-shaped endpoints on a PR:
    #   1. /issues/{n}/comments — the PR's "conversation" tab (covered above).
    #   2. /pulls/{n}/reviews — the review wrapper objects (Approve, Request
    #      Changes, Comment) with optional body text.
    #   3. /pulls/{n}/comments — line-anchored review comments inside reviews.
    # We need (2) and (3) for the bot to see PR review feedback.

    def list_pr_reviews(self, repo: str, pr_number: int, per_page: int = 100) -> list:
        """Return PR review wrappers (Approve/RequestChanges/Comment), oldest-first.

        Each entry includes: id, user, body (may be empty for line-only reviews),
        state ('APPROVED', 'CHANGES_REQUESTED', 'COMMENTED', 'DISMISSED'),
        submitted_at (ISO-8601), html_url.
        """
        url = f"{API_ROOT}/repos/{repo}/pulls/{pr_number}/reviews"
        params = {"per_page": str(per_page)}
        out: list = []
        while url:
            resp = self._get(url, params=params)
            for raw in resp.json():
                out.append({
                    "id": int(raw.get("id") or 0),
                    "user": (raw.get("user") or {}).get("login", ""),
                    "body": raw.get("body") or "",
                    "state": raw.get("state") or "",
                    "submitted_at": raw.get("submitted_at") or "",
                    "html_url": raw.get("html_url") or "",
                })
            url = _next_link(resp.headers.get("Link", ""))
            params = {}
        return out

    def list_pr_review_comments(self, repo: str, pr_number: int, per_page: int = 100) -> list:
        """Return line-anchored review comments on a PR, oldest-first.

        Each entry includes: id, user, body, path (file), line (or original_line
        if the diff has shifted), diff_hunk, created_at, html_url, in_reply_to_id.
        """
        url = f"{API_ROOT}/repos/{repo}/pulls/{pr_number}/comments"
        params = {"per_page": str(per_page)}
        out: list = []
        while url:
            resp = self._get(url, params=params)
            for raw in resp.json():
                out.append({
                    "id": int(raw.get("id") or 0),
                    "user": (raw.get("user") or {}).get("login", ""),
                    "body": raw.get("body") or "",
                    "path": raw.get("path") or "",
                    "line": raw.get("line") or raw.get("original_line"),
                    "diff_hunk": raw.get("diff_hunk") or "",
                    "created_at": raw.get("created_at") or "",
                    "html_url": raw.get("html_url") or "",
                    "in_reply_to_id": raw.get("in_reply_to_id"),
                })
            url = _next_link(resp.headers.get("Link", ""))
            params = {}
        return out

    def get_pull_request(self, repo: str, pr_number: int) -> dict:
        """Return the PR JSON. Notable fields: head.ref, head.sha, base.ref,
        head.repo.full_name (the source repo, e.g., bot's fork)."""
        url = f"{API_ROOT}/repos/{repo}/pulls/{pr_number}"
        resp = self._get(url)
        return resp.json()

    # ---- forks ----

    def get_repo(self, owner: str, name: str) -> Optional[dict]:
        """Return the repo JSON, or None if it doesn't exist (404)."""
        url = f"{API_ROOT}/repos/{owner}/{name}"
        try:
            return self._get(url).json()
        except GitHubError as e:
            if e.status_code == 404:
                return None
            raise

    def fork_exists_for_target(self, target_repo: str, bot_login: str) -> bool:
        """Check whether the bot has a usable fork for `target_repo`.

        We just check whether `<bot_login>/<repo-name>` exists. If it does, we
        assume it can serve as the source for PRs to `target_repo` — this is
        true as long as they share git history, which is the case when the
        bot's fork and the target are both descended from the same upstream
        (which is how forks work on GitHub).

        Note: we intentionally do NOT verify `parent.full_name == target_repo`,
        because the bot's fork has only one `parent` (its original source),
        but it can serve PRs to any repo it shares history with. For example,
        `ryan-gh-bot/sql` (forked from `opensearch-project/sql`) can serve
        PRs to BOTH `opensearch-project/sql` and `RyanL1997/sql` because
        all three share commits.
        """
        _, repo_name = target_repo.split("/", 1)
        return self.get_repo(bot_login, repo_name) is not None

    def create_fork(self, target_repo: str) -> dict:
        """POST /repos/{target_repo}/forks. Returns the partial fork data.

        GitHub's fork creation is async — it returns 202 Accepted with the new
        repo's metadata, but the actual fork content may take a few seconds to
        become readable. The caller is responsible for polling
        `fork_exists_for_target` until True before relying on it.
        """
        url = f"{API_ROOT}/repos/{target_repo}/forks"
        resp = self._post(url, json={})
        return resp.json()

    # ---- pull requests ----

    def create_pull_request(
        self,
        target_repo: str,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool = False,
        maintainer_can_modify: bool = True,
    ) -> dict:
        """POST /repos/{target_repo}/pulls.

        Args:
            target_repo: the repo to open the PR against, e.g. "RyanL1997/sql".
            head: the branch to merge from, in `owner:branch` form for cross-
                repo PRs (e.g., "ryan-gh-bot:bot-fix-...").
            base: the branch to merge into, e.g. "main".

        Returns the PR JSON. Notable fields: `html_url`, `number`.
        """
        url = f"{API_ROOT}/repos/{target_repo}/pulls"
        payload = {
            "title": title,
            "body": body,
            "head": head,
            "base": base,
            "draft": draft,
            "maintainer_can_modify": maintainer_can_modify,
        }
        resp = self._post(url, json=payload)
        return resp.json()

    # ---- internals ----

    def _get(self, url: str, params: Optional[dict] = None) -> requests.Response:
        return self._request("GET", url, params=params)

    def _post(self, url: str, json: dict) -> requests.Response:
        return self._request("POST", url, json=json)

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict] = None,
        json: Optional[dict] = None,
    ) -> requests.Response:
        # Simple retry with backoff on 5xx/connection errors (up to 3 attempts).
        last_exc: Optional[BaseException] = None
        for attempt in range(3):
            try:
                resp = self._session.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    timeout=DEFAULT_TIMEOUT_S,
                )
            except requests.RequestException as e:
                last_exc = e
                sleep_s = 2 ** attempt
                logger.warning("GitHub %s %s failed (%s), retrying in %ds", method, url, e, sleep_s)
                time.sleep(sleep_s)
                continue
            if resp.status_code >= 500:
                logger.warning(
                    "GitHub %s %s returned %d, retrying", method, url, resp.status_code
                )
                time.sleep(2 ** attempt)
                continue
            if resp.status_code >= 400:
                raise GitHubError(
                    f"{method} {url} failed: HTTP {resp.status_code}",
                    resp.status_code,
                    resp.text,
                )
            # Check rate limit headers; log if running low.
            remaining = resp.headers.get("X-RateLimit-Remaining")
            if remaining is not None and int(remaining) < 100:
                reset = resp.headers.get("X-RateLimit-Reset")
                logger.warning(
                    "GitHub rate limit remaining=%s reset=%s", remaining, reset
                )
            return resp
        assert last_exc is not None
        raise last_exc


# ---- helpers ----


def _comment_from_api(raw: dict) -> Comment:
    # The issue number is embedded in issue_url: .../issues/<number>
    issue_url = raw.get("issue_url", "") or ""
    issue_number = 0
    if issue_url:
        try:
            issue_number = int(issue_url.rsplit("/", 1)[1])
        except (ValueError, IndexError):
            issue_number = 0
    html_url = raw.get("html_url", "") or ""
    return Comment(
        id=int(raw["id"]),
        issue_number=issue_number,
        user=(raw.get("user") or {}).get("login", ""),
        body=raw.get("body", "") or "",
        created_at=raw.get("created_at", ""),
        updated_at=raw.get("updated_at", ""),
        html_url=html_url,
        # PR review comments have a different URL path; issue/PR general comments
        # on this endpoint all look like /<repo>/pull/<n>#... or /<repo>/issues/<n>#...
        is_pull_request="/pull/" in html_url,
    )


def _next_link(link_header: str) -> Optional[str]:
    """Parse the standard Link header and return the `next` URL or None."""
    if not link_header:
        return None
    for part in link_header.split(","):
        segments = part.strip().split(";")
        if len(segments) < 2:
            continue
        url_seg = segments[0].strip()
        rel = next((s.strip() for s in segments[1:] if "rel=" in s), "")
        if rel == 'rel="next"':
            if url_seg.startswith("<") and url_seg.endswith(">"):
                return url_seg[1:-1]
    return None
