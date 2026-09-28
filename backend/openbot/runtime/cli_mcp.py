"""OpenBot's own tools for a claude-code bot, served to its CLI as an MCP server (#196).

A claude-code bot runs with the CLI's tools, not OpenBot's. This gives it back the few that only OpenBot
can provide -- its long-term memory and the thread's history -- over MCP (`--mcp-config`):

- A separate listener on 127.0.0.1 and a port the OS picks, started on the first claude-code run. Not a
  route on the main app: that one may listen on every interface, and the backend does not reliably know
  its own port (uvicorn's --port is set from outside, PUBLIC_URL may be a proxy).
- It serves exactly one path, /mcp (streamable HTTP, stateless). No docs, no other routes, no access log,
  and nothing here logs a header: the bearer token must never reach a log file.
- Each run gets its own random token, bound to that bot, thread and run, and revoked when the run ends
  however it ends. The table lives in memory only: a backend restart drops every token, which is right,
  since no CLI process of the old backend survives it either.
- Every tool takes the bot and thread from the token, never from its arguments, so a run can reach only
  its own bot's memory and its own thread.
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from typing import Any, Literal

from mcp.server.fastmcp import Context, FastMCP

from openbot.runtime import memory
from openbot.tools.builtin.core import thread_history

log = logging.getLogger(__name__)

SERVER_NAME = "openbot"
# One allow rule for every tool of this server and nothing else (permission rule syntax: mcp__<server>).
ALLOW_RULE = f"mcp__{SERVER_NAME}"
MCP_PATH = "/mcp"
START_TIMEOUT = 10.0
MAX_HISTORY = 100
_LOOPBACK = {"127.0.0.1", "::1"}


@dataclass(frozen=True)
class Grant:
    bot_id: str
    thread_id: str
    run_id: str


def _bearer(headers) -> str | None:
    """The token from an `Authorization: Bearer <token>` header, from ASGI or Starlette headers."""
    for name, value in headers:
        if (name.decode("latin-1") if isinstance(name, bytes) else name).lower() == "authorization":
            value = value.decode("latin-1") if isinstance(value, bytes) else value
            scheme, _, token = value.partition(" ")
            return token.strip() if scheme.lower() == "bearer" and token.strip() else None
    return None


class CliMcpServer:
    def __init__(self, services) -> None:
        self.s = services
        self._grants: dict[str, Grant] = {}
        self._lock = asyncio.Lock()
        self._server = None
        self._task: asyncio.Task | None = None
        self.port: int | None = None

    # --- grants --------------------------------------------------------------------------------------

    def grant(self, *, bot_id: str, thread_id: str, run_id: str) -> str:
        token = secrets.token_urlsafe(32)
        self._grants[token] = Grant(bot_id, thread_id, run_id)
        return token

    def revoke(self, token: str) -> None:
        self._grants.pop(token, None)

    def lookup(self, token: str | None) -> Grant | None:
        if not token:
            return None
        # Constant-time comparison against every live token (there are a handful at most).
        for known, grant in list(self._grants.items()):
            if hmac.compare_digest(known, token):
                return grant
        return None

    def config(self, token: str) -> dict:
        """The --mcp-config document for one run: this server, over HTTP, with that run's token."""
        return {"mcpServers": {SERVER_NAME: {"type": "http", "url": f"http://127.0.0.1:{self.port}{MCP_PATH}",
                                             "headers": {"Authorization": f"Bearer {token}"}}}}

    # --- the MCP app ---------------------------------------------------------------------------------

    def _grant_of(self, ctx: Context) -> Grant:
        request = ctx.request_context.request
        grant = self.lookup(_bearer(request.headers.items()) if request is not None else None)
        if grant is None:
            raise PermissionError("this run's OpenBot token is no longer valid")
        return grant

    def build_app(self):
        mcp = FastMCP(SERVER_NAME, stateless_http=True, json_response=True, streamable_http_path=MCP_PATH,
                      log_level="WARNING")

        @mcp.tool()
        async def manage_memory(ctx: Context, action: Literal["create", "update", "delete"] = "create",
                                content: str | None = None, id: str | None = None) -> str:
            """Create, update or delete one of your long-term memories: durable facts, preferences and
            decisions that stay true beyond this thread. Pass the memory's id to update or delete it."""
            grant = self._grant_of(ctx)
            tool = memory.memory_tools(grant.bot_id, self.s.store)[0]
            args: dict[str, Any] = {"action": action}
            if content is not None:
                args["content"] = content
            if id is not None:
                args["id"] = id
            return str(await tool.ainvoke(args))

        @mcp.tool()
        async def search_memory(ctx: Context, query: str, limit: int = 10) -> str:
            """Search your long-term memories."""
            grant = self._grant_of(ctx)
            tool = memory.memory_tools(grant.bot_id, self.s.store)[1]
            return str(await tool.ainvoke({"query": query, "limit": max(1, min(limit, 50))}))

        @mcp.tool()
        async def read_history(ctx: Context, before_message_id: str | None = None, limit: int = 20) -> str:
            """Read messages of this OpenBot thread, oldest first; pass a message id to page further back."""
            grant = self._grant_of(ctx)
            return await thread_history(self.s, grant.thread_id, before_message_id, max(1, min(limit, MAX_HISTORY)))

        inner = mcp.streamable_http_app()

        async def app(scope, receive, send):
            # Loopback and a live token before anything reaches the MCP layer; lifespan passes through so
            # the session manager starts and stops with the listener.
            if scope["type"] == "http":
                client = (scope.get("client") or ("", 0))[0]
                if client not in _LOOPBACK or self.lookup(_bearer(scope.get("headers") or [])) is None:
                    await _deny(send, 401 if client in _LOOPBACK else 403)
                    return
            await inner(scope, receive, send)

        return app

    # --- the listener --------------------------------------------------------------------------------

    async def ensure_started(self) -> int:
        async with self._lock:
            if self._task is not None and not self._task.done() and self.port is not None:
                return self.port
            import uvicorn

            config = uvicorn.Config(self.build_app(), host="127.0.0.1", port=0, lifespan="on",
                                    log_level="warning", access_log=False)
            server = uvicorn.Server(config)
            # The main server owns SIGINT/SIGTERM; a second one must not replace its handlers.
            server.capture_signals = contextlib.nullcontext  # type: ignore[method-assign]
            self._server = server
            self._task = asyncio.create_task(server.serve(), name="cli-mcp-listener")
            loop = asyncio.get_running_loop()
            deadline = loop.time() + START_TIMEOUT
            while not server.started:
                if self._task.done() or loop.time() > deadline:
                    raise RuntimeError("the OpenBot MCP listener for claude-code bots did not start")
                await asyncio.sleep(0.02)
            self.port = server.servers[0].sockets[0].getsockname()[1]
            log.info("OpenBot MCP listener for claude-code bots on 127.0.0.1:%d", self.port)
            return self.port

    async def stop(self) -> None:
        self._grants.clear()
        if self._server is not None and self._task is not None:
            self._server.should_exit = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._task, timeout=5)
        self._server, self._task, self.port = None, None, None


async def _deny(send, status: int) -> None:
    body = b'{"error":"forbidden"}' if status == 403 else b'{"error":"unauthorized"}'
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
