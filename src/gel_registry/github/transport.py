"""Transport configuration and host-scoped authentication for GitHub API access."""

from __future__ import annotations

import os
from collections.abc import Generator

import httpx

GITHUB_API = "https://api.github.com"
GITHUB_UPLOADS = "https://uploads.github.com"
GITHUB_HOSTS = frozenset({"api.github.com", "uploads.github.com"})

API_VERSION = "2022-11-28"
USER_AGENT = "gel-registry/1"

API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": API_VERSION,
    "User-Agent": USER_AGENT,
}

REQUEST_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=60.0)


class GitHubTokenAuth(httpx.Auth):
    """Attach the token strictly to GitHub-owned API and upload hosts.

    Redirects (such as release asset downloads redirected to
    objects.githubusercontent.com) and unrelated manifest or artifact hosts
    must never receive the authentication token.
    """

    def __init__(self, token: str) -> None:
        token = token.strip()
        if not token:
            raise ValueError("GitHub token must not be empty")
        self._token = token

    def auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response]:
        if request.url.host in GITHUB_HOSTS:
            request.headers["Authorization"] = f"Bearer {self._token}"
        else:
            request.headers.pop("Authorization", None)
        yield request


def resolve_github_token() -> str | None:
    """Read GH_TOKEN or GITHUB_TOKEN from the environment if present and nonempty."""
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token is not None:
        token = token.strip()
        return token if token else None
    return None


def create_github_client(
    token: str | None = None,
    *,
    timeout: httpx.Timeout = REQUEST_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    """Construct an httpx.Client configured for GitHub API access.

    If a token is provided or present in GH_TOKEN / GITHUB_TOKEN, host-scoped
    authentication is attached. Otherwise, an unauthenticated client is created
    for local public-repository inspection.
    """
    resolved_token = token.strip() if token is not None else resolve_github_token()
    auth = GitHubTokenAuth(resolved_token) if resolved_token else None
    return httpx.Client(
        auth=auth,
        headers=dict(API_HEADERS),
        timeout=timeout,
        transport=transport,
    )


__all__ = [
    "API_HEADERS",
    "API_VERSION",
    "GITHUB_API",
    "GITHUB_HOSTS",
    "GITHUB_UPLOADS",
    "GitHubTokenAuth",
    "REQUEST_TIMEOUT",
    "USER_AGENT",
    "create_github_client",
    "resolve_github_token",
]
