"""OpenBot's MCP server for claude-code bots (runtime/cli_mcp.py): loopback, per-run tokens, one bot's
memory and one thread's history only."""
import json

import httpx
import pytest

from openbot.runtime import memory
from openbot.runtime.cli_mcp import ALLOW_RULE, MCP_PATH, CliMcpServer
from openbot.runtime.delivery import create_thread, human_actor, post_message
from tests.factories import bot_actor

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
async def server(services):
    s = CliMcpServer(services)
    await s.ensure_started()
    yield s
    await s.stop()


async def call(server, token, tool, args=None, *, path=MCP_PATH):
    headers = dict(HEADERS)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args or {}}}
    async with httpx.AsyncClient() as c:
        return await c.post(f"http://127.0.0.1:{server.port}{path}", headers=headers, content=json.dumps(body))


def text_of(response) -> str:
    result = response.json()["result"]
    return "".join(part.get("text", "") for part in result["content"])


async def test_listens_on_loopback_and_hands_out_a_config_for_one_run(server):
    token = server.grant(bot_id="b1", thread_id="t1", run_id="r1")
    cfg = server.config(token)["mcpServers"]["openbot"]
    assert cfg == {"type": "http", "url": f"http://127.0.0.1:{server.port}/mcp", "headers": {"Authorization": f"Bearer {token}"}}
    assert ALLOW_RULE == "mcp__openbot"
    assert await server.ensure_started() == server.port                  # started once, reused


async def test_never_logs_the_token(server, caplog):
    caplog.set_level("DEBUG")
    token = server.grant(bot_id="b1", thread_id="t1", run_id="r1")
    assert (await call(server, token, "search_memory", {"query": "x"})).status_code == 200
    assert (await call(server, token + "x", "search_memory", {"query": "x"})).status_code == 401
    assert token not in caplog.text


async def test_requires_a_live_token(server):
    assert (await call(server, None, "search_memory", {"query": "x"})).status_code == 401
    assert (await call(server, "guessed", "search_memory", {"query": "x"})).status_code == 401
    token = server.grant(bot_id="b1", thread_id="t1", run_id="r1")
    assert (await call(server, token, "search_memory", {"query": "x"})).status_code == 200
    server.revoke(token)                                                   # the run ended
    assert (await call(server, token, "search_memory", {"query": "x"})).status_code == 401


async def test_serves_nothing_but_the_mcp_path(server):
    token = server.grant(bot_id="b1", thread_id="t1", run_id="r1")
    async with httpx.AsyncClient() as c:
        for path in ("/docs", "/openapi.json", "/", "/api/v1/bots"):
            r = await c.get(f"http://127.0.0.1:{server.port}{path}", headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 404, path


async def test_rejects_a_non_loopback_client(services):
    s = CliMcpServer(services)
    token = s.grant(bot_id="b1", thread_id="t1", run_id="r1")
    transport = httpx.ASGITransport(app=s.build_app(), client=("10.0.0.5", 4242))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
        r = await c.post(MCP_PATH, headers={**HEADERS, "Authorization": f"Bearer {token}"}, content="{}")
    assert r.status_code == 403


async def test_memory_is_the_granted_bots_own(server, services):
    alice = server.grant(bot_id="alice", thread_id="t1", run_id="r1")
    bob = server.grant(bot_id="bob", thread_id="t2", run_id="r2")
    r = await call(server, alice, "manage_memory", {"action": "create", "content": "prefers squash merges"})
    assert r.status_code == 200 and "created memory" in text_of(r)
    # The same namespace the normal bot uses, so the memory shows up for it (and in the Memory view) too.
    assert await memory.relevant_memories(services.store, "alice", "merge") == ["prefers squash merges"]
    assert "prefers squash merges" in text_of(await call(server, alice, "search_memory", {"query": "merge"}))
    assert "prefers squash merges" not in text_of(await call(server, bob, "search_memory", {"query": "merge"}))
    # A bot id in the arguments is ignored: the tools take the bot from the token only.
    r = await call(server, bob, "search_memory", {"query": "merge", "bot_id": "alice"})
    assert "prefers squash merges" not in r.text


async def test_history_is_the_granted_threads_own(server, services):
    async with services.session_factory() as s:
        s.add(bot_actor("eng"))
        await s.commit()
        you = await human_actor(s)
        mine = await create_thread(services, s, title="mine", handles=["eng"], created_by=you)
        other = await create_thread(services, s, title="other", handles=["eng"], created_by=you)
        await post_message(services, s, thread_id=mine.id, sender=you, content="in my thread")
        await post_message(services, s, thread_id=other.id, sender=you, content="SOMEONE ELSE'S THREAD")
    token = server.grant(bot_id="b1", thread_id=mine.id, run_id="r1")
    out = text_of(await call(server, token, "read_history", {"limit": 50}))
    assert "in my thread" in out and "SOMEONE ELSE'S THREAD" not in out


async def test_stop_drops_every_token(services):
    s = CliMcpServer(services)
    await s.ensure_started()
    token = s.grant(bot_id="b1", thread_id="t1", run_id="r1")
    await s.stop()
    assert s.lookup(token) is None and s.port is None
