"""Bots whose turn runs through a local coding agent CLI instead of OpenBot's own model loop (#196).

Claude Code first: `claude -p --output-format stream-json` runs in the thread's working directory, its
event stream is mapped onto the run events the UI already shows, and its final result becomes the bot's
reply. OpenBot never reads the CLI's credentials: the CLI uses whatever login the user set up for it.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import ntpath
import os
import posixpath
import re
import signal
import subprocess
import sys
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage
from pydantic import AliasChoices

from openbot.config import Settings

log = logging.getLogger(__name__)

CLAUDE_CODE = "claude-code"
# Aliases the CLI resolves to its current models; an empty model means the CLI's own default.
CLAUDE_CODE_MODELS = ["sonnet", "opus", "haiku"]
# `dontAsk` denies anything that would prompt, which leaves reads in the working directory and read-only
# commands: the conservative default. bypassPermissions is deliberately not offered.
PERMISSION_MODES = ("dontAsk", "acceptEdits", "auto")
DEFAULT_PERMISSION_MODE = "dontAsk"
KILL_GRACE = 5.0                 # seconds between SIGTERM (claude stops its own Bash trees) and SIGKILL
STREAM_LIMIT = 32 * 1024 * 1024  # one stream-json line can carry a whole file read
STDERR_TAIL = 4000
NOT_FOUND = ("error: Claude Code CLI (claude) not found. Install it (https://code.claude.com), log in once in a "
             "terminal, then restart OpenBot, or set CLAUDE_CODE_PATH to the executable.")
_SESSION_ID = re.compile(r"^[0-9a-fA-F-]{8,64}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,119}$")
_UNKNOWN_SESSION = re.compile(r"no conversation found with session id|session .{0,80} not found|--resume requires a valid session",
                              re.IGNORECASE)
# The CLI inherits the server environment, minus OpenBot's own configuration and keys (the backend loads
# .env into os.environ): SECRET_KEY, MCP_TOKEN_KEY, OPENBOT_API_KEY, the provider and Telegram keys and so on.
# ANTHROPIC_API_KEY stays, because it is also how a user may have set up the CLI; billing_mode() makes
# its presence visible instead.
_EXTRA_PRIVATE_ENV = ("LANGSMITH_API_KEY",)
_CLI_AUTH_ENV = ("ANTHROPIC_API_KEY",)
# The block list is derived from every Settings field name, so a future field with a generic name
# (path, home, temp, user ...) would silently take the matching variable away, and the CLI would find
# neither git nor node, nor its own config. What a process needs to run always passes.
_SYSTEM_ENV = ("PATH", "PATHEXT", "HOME", "USER", "USERNAME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
               "TEMP", "TMP", "TMPDIR", "SYSTEMROOT", "WINDIR", "COMSPEC", "SHELL", "LANG", "LC_ALL")


class CliAgentError(Exception):
    """A CLI-agent run failed for a reason the user can act on; str() is the whole message. `usage` is
    what the CLI reported before failing, so a failed run still counts what it spent."""

    def __init__(self, message: str, usage: dict[str, int] | None = None) -> None:
        super().__init__(message)
        self.usage = usage


class UnknownSession(Exception):
    """The stored session no longer exists (the CLI's transcripts were cleared, or another machine)."""


def claude_profile_dir(settings) -> Path:
    """The isolated CLAUDE_CONFIG_DIR for OpenBot's own bots. OpenBot only ever sets this environment
    variable and creates the directory; it never reads a file inside it (credentials included)."""
    from openbot.runtime.appdirs import openbot_data_dir
    return openbot_data_dir() / "claude-profile"


def is_cli_agent(profile) -> bool:
    return getattr(profile, "provider", None) == CLAUDE_CODE


def openbot_tools_enabled(profile) -> bool:
    """Whether this claude-code bot gets OpenBot's memory and thread history over MCP (on by default)."""
    return (getattr(profile, "model_settings", None) or {}).get("openbot_tools", True) is not False


# --- finding the executable ----------------------------------------------------------------------------------

def claude_candidates(*, platform: str = sys.platform, env: Mapping[str, str] = os.environ,
                      home: str | None = None) -> list[str]:
    """Every place claude may live, in lookup order: PATH first, then where its installers put it.
    A GUI-launched app gets a minimal PATH, the same reason the desktop launcher searches for uv."""
    win = platform == "win32"
    p = ntpath if win else posixpath
    home = home or str(Path.home())
    names = (["claude" + ext.lower() for ext in (env.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD").split(";") if ext]
             if win else ["claude"])
    dirs = [d for d in (env.get("PATH") or env.get("Path") or "").split(";" if win else ":") if d]
    found = [p.join(d, n) for d in dirs for n in names]
    if win:
        found += [p.join(home, ".local", "bin", "claude.exe"), p.join(home, ".claude", "local", "claude.exe"),
                  p.join(home, ".claude", "local", "claude.cmd")]
        if env.get("APPDATA"):
            found.append(p.join(env["APPDATA"], "npm", "claude.cmd"))
        if env.get("LOCALAPPDATA"):
            found.append(p.join(env["LOCALAPPDATA"], "Microsoft", "WinGet", "Links", "claude.exe"))
        found.append(p.join(home, "scoop", "shims", "claude.exe"))
    else:
        found += [p.join(home, ".local", "bin", "claude"), p.join(home, ".claude", "local", "claude"),
                  p.join(home, ".npm-global", "bin", "claude"), "/opt/homebrew/bin/claude", "/usr/local/bin/claude"]
    return found


def _runnable(path: str) -> bool:
    return os.path.isfile(path) and (sys.platform == "win32" or os.access(path, os.X_OK))


def find_claude(settings: Settings, *, candidates: list[str] | None = None) -> str | None:
    """CLAUDE_CODE_PATH when set (and only that), else the first runnable candidate."""
    if settings.claude_code_path:
        path = str(Path(settings.claude_code_path).expanduser())
        return path if _runnable(path) else None
    return next((c for c in (candidates if candidates is not None else claude_candidates()) if _runnable(c)), None)


def _openbot_env_names() -> set[str]:
    """Every variable OpenBot reads its own settings from: field names and their aliases."""
    names = {n.upper() for n in Settings.model_fields} | set(_EXTRA_PRIVATE_ENV)
    for f in Settings.model_fields.values():
        alias = f.validation_alias
        if isinstance(alias, str):
            names.add(alias.upper())
        elif isinstance(alias, AliasChoices):
            names.update(a.upper() for a in alias.choices if isinstance(a, str))
    return names


def child_env(env: Mapping[str, str] = os.environ, *, profile_dir: Path | None = None) -> dict[str, str]:
    private = _openbot_env_names() - set(_CLI_AUTH_ENV) - set(_SYSTEM_ENV)
    out = {k: v for k, v in env.items() if k.upper() not in private}
    if profile_dir is not None:
        out["CLAUDE_CONFIG_DIR"] = str(profile_dir)
    return out


LOGIN_TIMEOUT = 15.0
# File-modification and web-search tools that dontAsk denies unconditionally (no read-only carve-out the
# way Bash and WebFetch have -- see the permission tiers table in the Claude Code permissions docs: "File
# modification" and "Web search" both say "Yes" with no exception, unlike "Bash commands" (except a
# built-in read-only set) and "Web fetch" (except preapproved documentation domains). Named individually
# with --disallowedTools, which removes exactly the tools it names from the model's context (real token
# savings, not just a denied call) and leaves everything else -- including mcp__openbot__* and any tool
# OpenBot doesn't know about -- untouched.
DONTASK_ALWAYS_DENIED = ("Edit", "Write", "NotebookEdit", "WebSearch")


def dontask_trim(model_settings: dict | None) -> tuple[str, ...]:
    """The --disallowedTools OpenBot adds on its own: only when the bot is on the default dontAsk mode and
    has not set its own allowed_tools (an explicit list means the bot's own choices apply, not this)."""
    ms = model_settings or {}
    if ms.get("permission_mode", DEFAULT_PERMISSION_MODE) != DEFAULT_PERMISSION_MODE or ms.get("allowed_tools"):
        return ()
    return DONTASK_ALWAYS_DENIED


async def claude_auth_status(executable: str, env: dict[str, str]) -> dict:
    """Runs `claude auth status` (JSON) in the given environment (with CLAUDE_CONFIG_DIR set, for the
    isolated profile). Exit code alone decides logged_in, per the CLI reference ("exits with code 0 if
    logged in, 1 if not"); the JSON body is read only best-effort for extra detail, never as the source of
    truth, and OpenBot never reads any file to answer this."""
    try:
        tree = await ProcessTree.spawn([executable, "auth", "status"], cwd=str(Path.home()), env=env)
    except OSError as e:
        return {"checked": False, "logged_in": None, "error": str(e)}
    try:
        out, _err = await asyncio.wait_for(tree.proc.communicate(), LOGIN_TIMEOUT)
    except TimeoutError:
        await tree.stop()
        return {"checked": False, "logged_in": None, "error": f"claude auth status did not answer within {int(LOGIN_TIMEOUT)}s"}
    finally:
        tree.close()
    code = tree.proc.returncode
    detail = None
    with contextlib.suppress(Exception):
        detail = json.loads(out.decode("utf-8", errors="replace")).get("authMethod")
    return {"checked": True, "logged_in": code == 0, "auth_method": detail}


def login_commands(executable: str, profile_dir: Path) -> dict[str, str]:
    """The one-time login command for the isolated profile, in every shell syntax OpenBot's UI or README
    might show: PowerShell (the operator's shell), cmd.exe, and POSIX (macOS/Linux). Uses the exact claude
    path OpenBot itself found, since it is frequently not on PATH (only CLAUDE_CODE_PATH is set)."""
    exe, d = str(executable), str(profile_dir)
    return {
        "powershell": f'$env:CLAUDE_CONFIG_DIR = "{d}"; & "{exe}" auth login',
        "cmd": f'set "CLAUDE_CONFIG_DIR={d}" && "{exe}" auth login',
        "posix": f'CLAUDE_CONFIG_DIR="{d}" "{exe}" auth login',
    }


def billing_mode(env: Mapping[str, str]) -> str:
    """"api" when the CLI will see ANTHROPIC_API_KEY (it then bills that key), else "subscription".
    Only the name is checked; the value is never read or logged."""
    return "api" if any(k.upper() in _CLI_AUTH_ENV for k in env) else "subscription"


# --- settings -------------------------------------------------------------------------------------------------

def settings_error(model: str, model_settings: dict | None) -> str | None:
    """Why this bot's CLI settings are unusable, or None. Checked on save and again before each run."""
    ms = model_settings or {}
    if model and not _MODEL.match(model):
        return f"invalid Claude Code model {model!r}"
    mode = ms.get("permission_mode", DEFAULT_PERMISSION_MODE)
    if mode not in PERMISSION_MODES:
        return f"permission_mode must be one of {', '.join(PERMISSION_MODES)}"
    tools = ms.get("allowed_tools", [])
    if not isinstance(tools, list) or not all(isinstance(t, str) and t.strip() and not t.startswith("-") for t in tools):
        return "allowed_tools must be a list of Claude Code permission rules, e.g. Read, Edit, Bash(git diff *)"
    for flag in ("trust_project_settings", "project_instructions", "openbot_tools"):
        if not isinstance(ms.get(flag, False), bool):
            return f"{flag} must be true or false"
    timeout = ms.get("timeout_seconds")
    if timeout is not None and (not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1):
        return "timeout_seconds must be a positive whole number"
    return None


def build_argv(executable: str, *, model: str, model_settings: dict | None, system_prompt_file: str,
               session_id: str | None, mcp_config_file: str | None = None, extra_allowed: tuple[str, ...] = (),
               extra_disallowed: tuple[str, ...] = ()) -> list[str]:
    """Thread messages go to stdin and the system prompt to a file, never onto the command line; the bot
    settings that do (model, rules) are checked by settings_error first. Each allowed-tools rule is its own
    argument, last, since a rule like `Bash(a, b)` may contain a comma.

    Unless the bot trusts project settings, `--setting-sources user` keeps the working directory's
    .claude/settings.json (hooks, env, helpers), its CLAUDE.md and its .mcp.json servers out: `claude -p`
    shows no trust dialog and would otherwise run them. --bare would too, but it ignores the subscription
    login.

    `mcp_config_file` is OpenBot's own MCP server for this run (runtime/cli_mcp.py): a file, since it holds
    the run's token. `extra_allowed` are allow rules OpenBot adds itself, e.g. that server's tools, which a
    dontAsk bot could not call otherwise; servers passed with --mcp-config load whatever --setting-sources is."""
    ms = model_settings or {}
    argv = [executable, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--append-system-prompt-file", system_prompt_file,
            "--permission-mode", ms.get("permission_mode", DEFAULT_PERMISSION_MODE)]
    if not ms.get("trust_project_settings", False):
        argv += ["--setting-sources", "user"]
    if model:
        argv += ["--model", model]
    if mcp_config_file:
        argv += ["--mcp-config", mcp_config_file]
    if session_id:
        argv += ["--resume", session_id]
    rules = [*(t.strip() for t in ms.get("allowed_tools") or []), *extra_allowed]
    if rules:
        argv += ["--allowedTools", *rules]
    if extra_disallowed:
        argv += ["--disallowedTools", *extra_disallowed]
    return argv


# --- the prompt -----------------------------------------------------------------------------------------------

def _content(m: BaseMessage) -> str:
    return m.content if isinstance(m.content, str) else str(m.content)


def render_messages(messages: list[BaseMessage], bot_name: str) -> str:
    """The thread as plain text: build_history already renders others as "[name] (new): text"."""
    return "\n\n".join(f"[{bot_name} (you)]: {_content(m)}" if isinstance(m, AIMessage) else _content(m)
                       for m in messages)


def since_last_reply(messages: list[BaseMessage]) -> list[BaseMessage]:
    """What a resumed session has not seen yet: everything after the bot's own latest reply."""
    last = max((i for i, m in enumerate(messages) if isinstance(m, AIMessage)), default=-1)
    return messages[last + 1:]


# --- the event stream -----------------------------------------------------------------------------------------

def result_usage(result: dict) -> dict[str, int]:
    """Usage in the run's USAGE_KEYS. `modelUsage` covers exactly this invocation, subagents included
    (the top-level `usage` misses those); prompt tokens count cache writes and reads, as for Anthropic."""
    def n(d: dict, k: str) -> int:
        v = d.get(k)
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0

    entries = [v for v in (result.get("modelUsage") or {}).values() if isinstance(v, dict)]
    if entries:
        fresh = sum(n(e, "inputTokens") + n(e, "cacheCreationInputTokens") for e in entries)
        cached = sum(n(e, "cacheReadInputTokens") for e in entries)
        out = sum(n(e, "outputTokens") for e in entries)
    else:
        u = result.get("usage") or {}
        fresh = n(u, "input_tokens") + n(u, "cache_creation_input_tokens")
        cached, out = n(u, "cache_read_input_tokens"), n(u, "output_tokens")
    prompt = fresh + cached
    return {"prompt_tokens": prompt, "completion_tokens": out, "cache_read_tokens": cached,
            "total_tokens": prompt + out, "model_calls": max(1, n(result, "num_turns"))}


def _tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) if isinstance(b, dict) and b.get("type") == "text"
                         else f"[{b.get('type', 'block')}]" if isinstance(b, dict) else str(b) for b in content)
    return "" if content is None else json.dumps(content, default=str)


@dataclass
class StreamMapper:
    """Turns stream-json events into run-level actions: ("session", id), ("delta", text), ("text", text),
    ("tool_call", {...}), ("tool_result", {...}), ("init", {...}) and ("result", event). Pure, so tests feed it lines."""

    session_id: str | None = None
    result: dict | None = None
    texts: list[str] = field(default_factory=list)
    _tool_names: dict[str, str] = field(default_factory=dict)

    def feed(self, event: dict) -> list[tuple[str, Any]]:
        out: list[tuple[str, Any]] = []
        sid = event.get("session_id")
        if isinstance(sid, str) and sid != self.session_id and _SESSION_ID.match(sid):
            self.session_id = sid
            out.append(("session", sid))
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            # What the CLI says about itself; apiKeySource names where its credential came from, not the key.
            out.append(("init", {k: event[k] for k in ("model", "permissionMode", "apiKeySource") if isinstance(event.get(k), str)}))
        # Messages of a subagent carry the id of the tool call that started it; its text is not the reply.
        main_chain = not event.get("parent_tool_use_id")
        if kind == "stream_event" and main_chain:
            ev = event.get("event") or {}
            delta = ev.get("delta") or {}
            if ev.get("type") == "content_block_delta" and delta.get("type") == "text_delta" and delta.get("text"):
                out.append(("delta", delta["text"]))
        elif kind == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and main_chain and str(block.get("text", "")).strip():
                    self.texts.append(block["text"])
                    out.append(("text", block["text"]))
                elif block.get("type") == "tool_use":
                    call_id, name = str(block.get("id", "")), str(block.get("name", "tool"))
                    self._tool_names[call_id] = name
                    out.append(("tool_call", {"id": call_id, "name": name, "args": block.get("input") or {}}))
        elif kind == "user":
            content = (event.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    call_id = str(block.get("tool_use_id", ""))
                    out.append(("tool_result", {"tool_call_id": call_id, "name": self._tool_names.get(call_id, "tool"),
                                                "status": "error" if block.get("is_error") else "success",
                                                "content": _tool_result_text(block.get("content"))}))
        elif kind == "result":
            self.result = event
            out.append(("result", event))
        return out


# --- the process ----------------------------------------------------------------------------------------------

class ProcessTree:
    """The CLI and everything it starts, so a cancel or a timeout stops all of it: a process group on
    macOS and Linux, a job object on Windows (with taskkill /T as the fallback if Windows refuses one)."""

    def __init__(self, proc: asyncio.subprocess.Process, job=None) -> None:
        self.proc, self._job = proc, job

    @classmethod
    async def spawn(cls, argv: list[str], *, cwd: str, env: dict[str, str]) -> ProcessTree:
        group: dict[str, Any] = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
                                 else {"start_new_session": True})
        proc = await asyncio.create_subprocess_exec(*argv, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                    limit=STREAM_LIMIT, **group)
        job = None
        if sys.platform == "win32":
            from openbot.tools.builtin.windows_job import WindowsJob
            job = WindowsJob.attach(proc.pid)
        return cls(proc, job)

    async def stop(self) -> None:
        if sys.platform == "win32":
            if self._job is not None:
                self._job.terminate()
            elif self.proc.returncode is None:
                killer = await asyncio.create_subprocess_exec("taskkill", "/pid", str(self.proc.pid), "/T", "/F",
                                                              stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await killer.wait()
            await self.proc.wait()
            return
        # start_new_session made the CLI a group leader, so its pid is the group id even after it exits.
        self._signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(self.proc.wait(), KILL_GRACE)
        except TimeoutError:
            pass
        self._signal(signal.SIGKILL)
        await self.proc.wait()

    def _signal(self, sig: int) -> None:
        try:
            os.killpg(self.proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def close(self) -> None:
        if self._job is not None:
            self._job.close()
            self._job = None


async def _tail(stream: asyncio.StreamReader | None) -> str:
    buf = ""
    if stream is None:
        return buf
    while chunk := await stream.read(65536):
        buf = (buf + chunk.decode(errors="replace"))[-STDERR_TAIL:]
    return buf


@dataclass
class TurnResult:
    text: str
    usage: dict[str, int]
    session_id: str | None


OnEvent = Callable[[str, Any], Awaitable[None]]


def _private_file(prefix: str, suffix: str, text: str) -> str:
    """A temp file only this user can read (mkstemp creates it 0600), for the prompt and the run's token."""
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return path


async def _turn(executable: str, *, cwd: str, env: dict[str, str], system_prompt: str, prompt: str, session_id: str | None,
                model: str, model_settings: dict | None, timeout: float, on_event: OnEvent,
                mcp_config: dict | None = None, extra_allowed: tuple[str, ...] = (),
                extra_disallowed: tuple[str, ...] = ()) -> TurnResult:
    prompt_file = _private_file("openbot-system-", ".md", system_prompt)
    mcp_file = _private_file("openbot-mcp-", ".json", json.dumps(mcp_config)) if mcp_config else None
    argv = build_argv(executable, model=model, model_settings=model_settings, system_prompt_file=prompt_file,
                      session_id=session_id, mcp_config_file=mcp_file, extra_allowed=extra_allowed,
                      extra_disallowed=extra_disallowed)
    mapper = StreamMapper()
    stderr_task: asyncio.Task | None = None
    try:
        tree = await ProcessTree.spawn(argv, cwd=cwd, env=env)
        stderr_task = asyncio.create_task(_tail(tree.proc.stderr))
        try:
            async with asyncio.timeout(timeout):
                try:
                    tree.proc.stdin.write(prompt.encode("utf-8"))
                    await tree.proc.stdin.drain()
                    tree.proc.stdin.close()
                except (BrokenPipeError, ConnectionResetError):
                    pass    # it exited before reading; stderr and the exit code say why
                while raw := await tree.proc.stdout.readline():
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        log.debug("claude: non-JSON output line: %s", line[:200])
                        continue
                    if isinstance(event, dict):
                        for kind, payload in mapper.feed(event):
                            await on_event(kind, payload)
                code = await tree.proc.wait()
        except TimeoutError:
            await tree.stop()
            raise CliAgentError(f"error: claude did not finish within {int(timeout)}s and was stopped",
                                usage=result_usage(mapper.result) if mapper.result else None) from None
        except BaseException:
            # Cancelled run, or a failure while handling an event: never leave the CLI running.
            await tree.stop()
            raise
        finally:
            tree.close()
        stderr = await stderr_task
    finally:
        if stderr_task is not None and not stderr_task.done():
            stderr_task.cancel()
        for path in (prompt_file, mcp_file):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
    result = mapper.result
    if result is None:
        if session_id and _UNKNOWN_SESSION.search(stderr):
            raise UnknownSession(session_id)
        detail = stderr.strip()[-1000:]
        raise CliAgentError(f"error: claude exited with code {code} without a result" + (f": {detail}" if detail else ""))
    text = str(result.get("result") or "")
    subtype = str(result.get("subtype") or "")
    if result.get("is_error") or subtype.startswith("error"):
        errors = " ".join(str(e) for e in result.get("errors") or [])
        if session_id and _UNKNOWN_SESSION.search(f"{text} {errors} {stderr}"):
            raise UnknownSession(session_id)
        detail = (text or errors or stderr.strip())[-1000:]
        raise CliAgentError(f"error: claude run failed ({subtype or 'error'})" + (f": {detail}" if detail else ""),
                            usage=result_usage(result))
    return TurnResult(text=text or (mapper.texts[-1] if mapper.texts else ""), usage=result_usage(result),
                      session_id=mapper.session_id)


# `--setting-sources user` is what keeps a directory's hooks and .mcp.json out; older CLIs do not have it.
MIN_VERSION = (2, 1, 210)
VERSION_TIMEOUT = 30.0
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
# (path, mtime) -> version, so `claude --version` runs once per install, and again after an update.
# Only touched from the event loop.
_versions: dict[tuple[str, float], tuple[int, int, int]] = {}


async def claude_version(executable: str, env: dict[str, str]) -> tuple[int, int, int]:
    try:
        key = (executable, os.path.getmtime(executable))
    except OSError:
        key = (executable, 0.0)
    if key in _versions:
        return _versions[key]
    tree = await ProcessTree.spawn([executable, "--version"], cwd=str(Path(executable).parent), env=env)
    try:
        out, _err = await asyncio.wait_for(tree.proc.communicate(), VERSION_TIMEOUT)
    except TimeoutError:
        await tree.stop()
        raise CliAgentError(f"error: `claude --version` did not answer within {int(VERSION_TIMEOUT)}s") from None
    except BaseException:
        await tree.stop()
        raise
    finally:
        tree.close()
    text = out.decode("utf-8", errors="replace").strip()
    m = _VERSION.search(text)
    if m is None:
        raise CliAgentError(f"error: could not read the Claude Code version from `claude --version`: {text[:200]!r}")
    _versions[key] = version = (int(m[1]), int(m[2]), int(m[3]))
    return version


async def check_version(executable: str, env: dict[str, str]) -> None:
    version = await claude_version(executable, env)
    if version < MIN_VERSION:
        raise CliAgentError(f"error: Claude Code {'.'.join(map(str, version))} is too old; OpenBot needs "
                            f"{'.'.join(map(str, MIN_VERSION))} or later. Update it with `claude update`.")


async def run_claude_code(executable: str, *, cwd: str, env: dict[str, str], system_prompt: str, full_prompt: str,
                          resume: tuple[str, str] | None, model: str, model_settings: dict | None, timeout: float,
                          on_event: OnEvent, mcp_config: dict | None = None,
                          extra_allowed: tuple[str, ...] = ()) -> TurnResult:
    """One bot turn. `resume` is (session id, the messages that session has not seen); if the CLI no longer
    knows that session, the turn starts over in a new one with the whole visible thread instead."""
    error = settings_error(model, model_settings)
    if error:
        raise CliAgentError(f"error: {error}")
    await check_version(executable, env)
    kwargs = {"cwd": cwd, "env": env, "system_prompt": system_prompt, "model": model, "model_settings": model_settings,
              "timeout": timeout, "on_event": on_event, "mcp_config": mcp_config, "extra_allowed": extra_allowed,
              "extra_disallowed": dontask_trim(model_settings)}
    if resume is not None and _SESSION_ID.match(resume[0]):
        try:
            return await _turn(executable, prompt=resume[1], session_id=resume[0], **kwargs)
        except UnknownSession:
            log.info("claude session %s is gone; starting a new one", resume[0])
    return await _turn(executable, prompt=full_prompt, session_id=None, **kwargs)
