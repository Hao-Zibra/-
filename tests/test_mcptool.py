import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from farewatch import mcptool
from farewatch.model import SourceError


def tool_result(text="", *, structured=None, is_error=False):
    return SimpleNamespace(content=[SimpleNamespace(text=text)], structured_content=structured, is_error=is_error)


# --- reading a tool's answer -----------------------------------------------------------


def test_answer_prefers_structured_content():
    assert mcptool.payload(tool_result("ignored", structured={"currency": "USD"}), "Kiwi") == {"currency": "USD"}


def test_answer_falls_back_to_json_text():
    assert mcptool.payload(tool_result('{"currency": "USD"}'), "Kiwi") == {"currency": "USD"}


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (tool_result("departureDate must be in the future", is_error=True), "Kiwi rejected the search: departureDate"),
        (tool_result("<html>busy</html>"), "Kiwi response is not JSON"),
        (tool_result("[1, 2]"), "Kiwi response is not a JSON object"),
    ],
)
def test_results_that_are_not_an_answer_are_raised(result, message):
    with pytest.raises(SourceError, match=message):
        mcptool.payload(result, "Kiwi")


def test_describe_names_the_error_inside_nested_groups():
    nested = ExceptionGroup("task group", [ExceptionGroup("inner", [TimeoutError("no answer")])])

    assert mcptool.describe(nested) == "TimeoutError: no answer"
    assert mcptool.describe(ValueError("plain")) == "ValueError: plain"


# --- calling a tool many times ---------------------------------------------------------


class FakeServer:
    """Stands in for an MCP server: `answer(arguments, attempt)` returns a tool result or raises."""

    def __init__(self, answer, *, reachable=True):
        self.answer = answer
        self.reachable = reachable
        self.calls = []  # (tool, arguments)
        self.busiest = 0
        self._busy = 0

    def install(self, monkeypatch):
        server = self

        @asynccontextmanager
        async def client(url):
            if not server.reachable:
                raise ConnectionError(f"cannot connect to {url}")
            yield ("read stream", "write stream")

        class Session:
            def __init__(self, read, write):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            async def initialize(self):
                pass

            async def call_tool(self, tool, arguments):
                server.calls.append((tool, arguments))
                attempt = sum(1 for _, seen in server.calls if seen == arguments)
                server._busy += 1
                server.busiest = max(server.busiest, server._busy)
                try:
                    await asyncio.sleep(0)  # let the other calls start
                    return server.answer(arguments, attempt)
                finally:
                    server._busy -= 1

        async def no_wait(seconds):
            pass

        monkeypatch.setattr(mcptool, "streamable_http_client", client)
        monkeypatch.setattr(mcptool, "ClientSession", Session)
        monkeypatch.setattr(mcptool.asyncio, "sleep", _yield_once)
        return self


async def _yield_once(seconds, _sleep=asyncio.sleep):
    await _sleep(0)


def call_many(argument_sets, **options):
    return asyncio.run(mcptool.call_many("https://mcp.example.test", "search", argument_sets, who="Example", **options))


def test_answers_come_back_in_the_order_asked(monkeypatch):
    server = FakeServer(lambda arguments, attempt: tool_result(structured={"echo": arguments["n"]})).install(monkeypatch)

    answers = call_many([{"n": n} for n in range(5)], concurrency=3)

    assert answers == [{"echo": n} for n in range(5)]
    assert {tool for tool, _ in server.calls} == {"search"}


def test_no_more_calls_run_at_once_than_allowed(monkeypatch):
    server = FakeServer(lambda arguments, attempt: tool_result(structured={})).install(monkeypatch)

    call_many([{"n": n} for n in range(10)], concurrency=3)

    assert server.busiest == 3


def test_a_search_the_server_rejects_is_not_asked_again(monkeypatch):
    def answer(arguments, attempt):
        if arguments["n"] == 1:
            return tool_result("origin is not an airport", is_error=True)
        return tool_result(structured={"ok": arguments["n"]})

    server = FakeServer(answer).install(monkeypatch)

    first, second, third = call_many([{"n": 0}, {"n": 1}, {"n": 2}], concurrency=2)

    assert (first, third) == ({"ok": 0}, {"ok": 2})
    assert isinstance(second, SourceError) and "Example rejected the search: origin is not an airport" in str(second)
    assert len(server.calls) == 3


def test_a_call_that_breaks_is_tried_again(monkeypatch):
    def answer(arguments, attempt):
        if attempt == 1:
            raise ConnectionResetError("connection reset")
        return tool_result(structured={"ok": True})

    server = FakeServer(answer).install(monkeypatch)

    assert call_many([{"n": 0}]) == [{"ok": True}]
    assert len(server.calls) == 2


def test_a_call_that_keeps_breaking_ends_as_an_error_for_that_search_only(monkeypatch):
    def answer(arguments, attempt):
        if arguments["n"] == 0:
            raise ConnectionResetError("connection reset")
        return tool_result(structured={"ok": True})

    server = FakeServer(answer).install(monkeypatch)

    broken, fine = call_many([{"n": 0}, {"n": 1}], attempts=2)

    assert isinstance(broken, SourceError) and "Example search failed: ConnectionResetError: connection reset" in str(broken)
    assert fine == {"ok": True}
    assert len(server.calls) == 3


def test_an_unreachable_server_is_raised(monkeypatch):
    FakeServer(lambda arguments, attempt: tool_result(structured={}), reachable=False).install(monkeypatch)

    with pytest.raises(SourceError, match="Example could not be reached: ConnectionError: cannot connect"):
        call_many([{"n": 0}])
