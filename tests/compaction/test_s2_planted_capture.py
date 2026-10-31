"""Terminal capture with the pinned real MCP stack; no model or Docker calls.

Run with uv run --no-sync --with fastmcp==4.1.3 pytest -q <this file>.
"""
import asyncio
import importlib.metadata
import threading

import pytest

from compaction.s2.native import common as N


class NoSimulator:
    def ask(self, question):
        pytest.fail("a terminal capture must never consult the simulator")


def test_capture_is_preserved_before_callback_and_callback_cannot_mutate_evidence():
    def captured(row):
        assert server.captured_asks[0]["text"] == "original question"
        row["text"] = "mutated"
        raise RuntimeError("stop failed")

    server = N.SimServer(NoSimulator(), bind="127.0.0.1", capture_ask=captured)
    with pytest.raises(RuntimeError, match="stop failed"):
        server.ask("original question")
    assert server.captured_asks[0]["text"] == "original question"
    assert server.capture_errors[0]["error"] == "RuntimeError: stop failed"
    assert server.rows == []


def pinned_client(server):
    pytest.importorskip("fastmcp")
    assert importlib.metadata.version("fastmcp") == N.FASTMCP_VERSION
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    return Client(StreamableHttpTransport(
        f"http://127.0.0.1:{server.port}/mcp", headers={N.TOKEN_HEADER: server.token}))


@pytest.mark.parametrize("fail_stop", [False, True])
def test_real_mcp_terminal_capture_is_not_tool_error_but_stop_failure_is(fail_stop):
    callback_rows = []

    def captured(row):
        callback_rows.append(row)
        assert server.captured_asks == callback_rows
        if fail_stop:
            raise RuntimeError("capture stop failed")

    server = N.SimServer(NoSimulator(), bind="127.0.0.1", capture_ask=captured)
    client = pinned_client(server)
    server.start()
    try:
        async def request():
            async with client:
                result = await client.call_tool("ask_user", {"question": "terminal question"},
                                                raise_on_error=False)
                assert result.is_error is fail_stop
                if not fail_stop:
                    assert result.content == []
                    assert result.structured_content == {"result": ""}
                    # A duplicate cannot create another observation or invoke the callback.
                    second = await client.call_tool("ask_user", {"question": "duplicate"})
                    assert not second.is_error and second.content == []
        asyncio.run(request())
        assert len(callback_rows) == len(server.captured_asks) == 1
        assert bool(server.capture_errors) is fail_stop
        assert server.rows == []
    finally:
        assert server.stop() == []


def test_real_mcp_cancellation_retains_capture_and_waits_for_stop_callback():
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()

    def captured(row):
        entered.set()
        assert release.wait(5), "test did not release the stop callback"
        stopped.set()

    server = N.SimServer(NoSimulator(), bind="127.0.0.1", capture_ask=captured)
    client = pinned_client(server)
    server.start()
    try:
        async def request():
            async with client:
                pending = asyncio.create_task(client.call_tool("ask_user", {"question": "captured"}))
                assert await asyncio.to_thread(entered.wait, 5)
                assert server.captured_asks[0]["text"] == "captured"
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
                release.set()
                assert await asyncio.to_thread(stopped.wait, 5)
        asyncio.run(request())
        assert len(server.captured_asks) == 1
        assert server.rows == server.capture_errors == []
    finally:
        release.set()
        assert server.stop() == []
