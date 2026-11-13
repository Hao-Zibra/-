import json

import httpx
import pytest

from farewatch import github
from farewatch.config import ConfigError


def tracker(handler):
    return github.GitHubIssues("octo/fares", "t0ken", transport=httpx.MockTransport(handler))


def test_create_opens_an_issue_and_returns_its_number():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(
            method=request.method,
            url=str(request.url),
            authorization=request.headers["authorization"],
            sent=json.loads(request.content),
        )
        return httpx.Response(201, json={"number": 7})

    assert tracker(handler).create("fare found", "details") == 7
    assert seen == {
        "method": "POST",
        "url": "https://api.github.com/repos/octo/fares/issues",
        "authorization": "Bearer t0ken",
        "sent": {"title": "fare found", "body": "details"},
    }


def test_comment_posts_to_the_issue():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(method=request.method, url=str(request.url), sent=json.loads(request.content))
        return httpx.Response(201, json={"id": 1})

    tracker(handler).comment(7, "lower now")

    assert seen == {
        "method": "POST",
        "url": "https://api.github.com/repos/octo/fares/issues/7/comments",
        "sent": {"body": "lower now"},
    }


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(200, json={"state": "open"}), True),
        (httpx.Response(200, json={"state": "closed"}), False),
        (httpx.Response(404, json={"message": "Not Found"}), False),
        (httpx.Response(410, json={"message": "This issue was deleted"}), False),
    ],
)
def test_is_open(response, expected):
    assert tracker(lambda request: response).is_open(7) is expected


@pytest.mark.parametrize("call", [lambda t: t.create("a", "b"), lambda t: t.comment(7, "b"), lambda t: t.is_open(7)])
def test_a_refused_request_is_raised(call):
    forbidden = tracker(lambda request: httpx.Response(403, json={"message": "Resource not accessible"}))

    with pytest.raises(httpx.HTTPStatusError):
        call(forbidden)


def test_from_environment_needs_the_actions_variables(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo/fares")

    with pytest.raises(ConfigError, match="GITHUB_REPOSITORY and GITHUB_TOKEN"):
        github.from_environment()


def test_from_environment_uses_the_repository_of_the_run(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo/fares")

    assert isinstance(github.from_environment(), github.GitHubIssues)
