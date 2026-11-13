"""Delivers alerts as GitHub issues, so GitHub's own email and mobile notifications carry them."""

from __future__ import annotations

import os

import httpx

from farewatch.config import ConfigError


class GitHubIssues:
    def __init__(
        self,
        repository: str,
        token: str,
        *,
        api_url: str = "https://api.github.com",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=f"{api_url}/repos/{repository}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
            transport=transport,
        )

    def is_open(self, number: int) -> bool:
        response = self._client.get(f"/issues/{number}")
        if response.status_code in (404, 410):
            return False
        response.raise_for_status()
        return response.json()["state"] == "open"

    def create(self, title: str, body: str) -> int:
        response = self._client.post("/issues", json={"title": title, "body": body})
        response.raise_for_status()
        return response.json()["number"]

    def comment(self, number: int, body: str) -> None:
        self._client.post(f"/issues/{number}/comments", json={"body": body}).raise_for_status()


def from_environment() -> GitHubIssues:
    """The issue tracker of the repository a GitHub Actions run belongs to."""
    repository, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not repository or not token:
        raise ConfigError("--github-issues needs GITHUB_REPOSITORY and GITHUB_TOKEN in the environment")
    return GitHubIssues(repository, token)
