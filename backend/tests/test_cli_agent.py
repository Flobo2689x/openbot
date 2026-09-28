"""Bots on the claude-code provider (#196), run against tests/fake_claude.py instead of the real CLI.

tests/fixtures/claude_turn.jsonl follows the documented `--output-format stream-json --verbose
--include-partial-messages` format: init, text deltas, assistant text and tool_use, tool_result (one of
them a permission denial), and the final result with usage and modelUsage.
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import select

from openbot.config import Settings
from openbot.db.models import ActivityLog, Actor, BotProfile, InboxItem, Message, Run, RunEvent
from openbot.runtime import cli_agent, memory
from openbot.runtime.cli_agent import (
    CLAUDE_CODE,
    StreamMapper,
    billing_mode,
    build_argv,
    child_env,
    claude_auth_status,
    claude_candidates,
    dontask_trim,
    find_claude,
    login_commands,
    render_messages,
    result_usage,
    settings_error,
    since_last_reply,
)
from openbot.runtime.cli_mcp import CliMcpServer
from openbot.runtime.delivery import create_thread, human_actor, post_message
from openbot.runtime.prompt import build_system_prompt
from openbot.runtime.providers import chat_model, effective_bot_profile, provider_status
from openbot.runtime.runner import TOOL_RESULT_CAP, Runner
from tests.conftest import build_test_services
from tests.factories import bot_actor

FIXTURE = Path(__file__).parent / "fixtures" / "claude_turn.jsonl"
SESSION = "4f7c2a0e-9b1d-4c3e-8a5f-0d2e6b7c9a11"
TURN = FIXTURE.read_text(encoding="utf-8").splitlines()
# A real `claude --print --output-format stream-json` capture (2.1.283, native Windows exe, a Read tool
# call, --setting-sources user, --model haiku), anonymized: usernames/paths replaced, session id fixed to
# the same SESSION as the engineered TURN above, personal skill/agent/MCP-server catalog trimmed to a
# short sample. claude_turn.jsonl above stays hand-built (it exercises a tool error and a handoff, which
# this single real capture does not have); this one is evidence that the mapper survives the real wire
# format, thinking blocks and noise events included.
REAL_TURN = (Path(__file__).parent / "fixtures" / "claude_turn_real.jsonl").read_text(encoding="utf-8").splitlines()
# A second real capture (same CLI, --model haiku, dontAsk, --setting-sources user), anonymized the same way:
# the bot calls OpenBot's memory over MCP (--mcp-config, --allowedTools mcp__openbot) and then tries a Bash
# command that dontAsk denies.
REAL_MCP_TURN = (Path(__file__).parent / "fixtures" / "claude_turn_mcp_real.jsonl").read_text(encoding="utf-8").splitlines()


def alive(pid: int) -> bool:
    if sys.platform == "win32":
        # os.kill(pid, 0) would terminate the process on Windows instead of probing it.
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return code.value == 259                                     # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def result_line(text: str, *, subtype: str = "success", is_error: bool = False, session: str = SESSION) -> str:
    return json.dumps({"type": "result", "subtype": subtype, "is_error": is_error, "num_turns": 1, "result": text,
                       "session_id": session, "usage": {"input_tokens": 5, "cache_read_input_tokens": 100, "output_tokens": 7}})


def init_line(session: str = SESSION) -> str:
    return json.dumps({"type": "system", "subtype": "init", "session_id": session, "cwd": "/work", "tools": []})


@pytest.fixture(autouse=True)
async def _stop_cli_mcp(monkeypatch):
    """Every claude-code run starts OpenBot's MCP listener; stop the ones a test started."""
    started = []
    original = CliMcpServer.__init__

    def record(self, services):
        original(self, services)
        started.append(self)

    monkeypatch.setattr(CliMcpServer, "__init__", record)
    yield
    for server in started:
        await server.stop()


def mcp_log(home: Path) -> list[dict]:
    f = home / "mcp.jsonl"
    return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """A `claude` executable that runs tests/fake_claude.py with this interpreter; returns (dir, path)."""
    home = tmp_path / "fake_claude"
    home.mkdir()
    script = Path(__file__).parent / "fake_claude.py"
    if sys.platform == "win32":
        exe = home / "claude.cmd"
        exe.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        exe = home / "claude"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        exe.chmod(0o755)
    monkeypatch.setenv("FAKE_CLAUDE_DIR", str(home))
    return home, exe


def script(home: Path, *steps: dict) -> None:
    (home / "script.json").write_text(json.dumps(list(steps)), encoding="utf-8")


def calls(home: Path) -> list[dict]:
    f = home / "calls.jsonl"
    return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []


async def make(settings, *, model: str = "", model_settings: dict | None = None, content: str = "please build it"):
    services = await build_test_services(settings, {})
    services.actors = None            # drive the runner directly
    services.runner = Runner(services)
    async with services.session_factory() as s:
        eng = Actor(kind="bot", handle="eng", name="Eng", description="builds",
                    bot=BotProfile(provider=CLAUDE_CODE, model=model, model_settings=model_settings or {}))
        rev = bot_actor("rev", description="reviews")
        s.add_all([eng, rev])
        await s.commit()
        you = await human_actor(s)
        t = await create_thread(services, s, title="t", handles=["eng", "rev"], created_by=you, default_bot_handle="eng")
    run = await queue(services, t.id, eng.id, content)
    return services, eng, t, run


async def queue(services, thread_id: str, bot_id: str, content: str) -> Run:
    """Post a human message to the bot and claim its inbox item for a new run, as the actor system would."""
    async with services.session_factory() as s:
        you = await human_actor(s)
        res = await post_message(services, s, thread_id=thread_id, sender=you, content=f"@eng {content}")
        item = next(i for i in res.items if i.actor_id == bot_id)
        run = Run(actor_id=bot_id, thread_id=thread_id)
        s.add(run)
        await s.flush()
        item.run_id, item.status = run.id, "processing"
        await s.commit()
        return run


async def get(services, model, id_):
    async with services.session_factory() as s:
        return await s.get(model, id_)


async def events(services, run_id):
    async with services.session_factory() as s:
        return (await s.execute(select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq))).scalars().all()


async def thread_messages(services, thread_id):
    async with services.session_factory() as s:
        return (await s.execute(select(Message).where(Message.thread_id == thread_id).order_by(Message.created_at))).scalars().all()


# --- finding the CLI -------------------------------------------------------------------------------------------

def test_candidates_search_path_then_install_dirs_on_windows():
    env = {"PATH": r"C:\tools;C:\bin", "PATHEXT": ".EXE;.CMD", "APPDATA": r"C:\Users\a\AppData\Roaming",
           "LOCALAPPDATA": r"C:\Users\a\AppData\Local"}
    c = claude_candidates(platform="win32", env=env, home=r"C:\Users\a")
    assert c[:4] == [r"C:\tools\claude.exe", r"C:\tools\claude.cmd", r"C:\bin\claude.exe", r"C:\bin\claude.cmd"]
    assert r"C:\Users\a\.local\bin\claude.exe" in c and r"C:\Users\a\AppData\Roaming\npm\claude.cmd" in c


def test_candidates_search_path_then_install_dirs_on_posix():
    c = claude_candidates(platform="linux", env={"PATH": "/usr/bin:/opt/x"}, home="/home/a")
    assert c[:2] == ["/usr/bin/claude", "/opt/x/claude"]
    assert c[2:] == ["/home/a/.local/bin/claude", "/home/a/.claude/local/claude", "/home/a/.npm-global/bin/claude",
                     "/opt/homebrew/bin/claude", "/usr/local/bin/claude"]


def test_find_claude_uses_the_setting_or_the_first_runnable_candidate(settings, fake, tmp_path):
    _home, exe = fake
    assert find_claude(settings, candidates=[str(tmp_path / "missing"), str(exe)]) == str(exe)
    assert find_claude(settings, candidates=[str(tmp_path / "missing")]) is None
    settings.claude_code_path = str(exe)
    assert find_claude(settings, candidates=[]) == str(exe)
    # An explicit path is the only one tried: a typo must not silently pick another install.
    settings.claude_code_path = str(tmp_path / "nope")
    assert find_claude(settings, candidates=[str(exe)]) is None


def test_child_env_keeps_openbot_settings_and_keys_out():
    env = {"PATH": "/bin", "HOME": "/h", "OPENROUTER_API_KEY": "k", "SECRET_KEY": "s", "MCP_TOKEN_KEY": "m",
           "OPENBOT_API_KEY": "a", "TELEGRAM_BOT_TOKEN": "t", "DATABASE_URL": "d", "PUBLIC_URL": "u",
           "LANGSMITH_API_KEY": "l", "FOO": "bar"}
    assert child_env(env) == {"PATH": "/bin", "HOME": "/h", "FOO": "bar"}


SYSTEM_ENV = {"PATH": "p", "Path": "p", "HOME": "h", "USERPROFILE": "u", "APPDATA": "a", "LOCALAPPDATA": "l",
              "TEMP": "t", "TMP": "t", "SystemRoot": "s"}


def test_child_env_keeps_what_the_cli_needs_to_run(monkeypatch):
    assert child_env(SYSTEM_ENV) == SYSTEM_ENV
    # Even if OpenBot ever gets a setting with one of these names, the CLI still finds git, node and its config.
    monkeypatch.setattr(cli_agent, "_openbot_env_names",
                        lambda: {"PATH", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "SYSTEMROOT", "SECRET_KEY"})
    assert child_env({**SYSTEM_ENV, "SECRET_KEY": "k"}) == SYSTEM_ENV


def test_an_anthropic_key_reaches_the_cli_and_shows_as_api_billing():
    # The CLI may be set up to authenticate with it, so it is passed on, and only its presence is reported.
    env = child_env({"PATH": "/bin", "ANTHROPIC_API_KEY": "k"})
    assert env == {"PATH": "/bin", "ANTHROPIC_API_KEY": "k"}
    assert billing_mode(env) == "api" and billing_mode({"PATH": "/bin"}) == "subscription"


def test_settings_error():
    assert settings_error("", {}) is None
    assert settings_error("sonnet", {"permission_mode": "acceptEdits", "allowed_tools": ["Read", "Bash(git diff *)"],
                                     "timeout_seconds": 60}) is None
    assert "permission_mode" in settings_error("", {"permission_mode": "bypassPermissions"})
    assert "allowed_tools" in settings_error("", {"allowed_tools": "Read"})
    assert "allowed_tools" in settings_error("", {"allowed_tools": ["--dangerously-skip-permissions"]})
    assert "model" in settings_error("--x", {})
    assert "timeout_seconds" in settings_error("", {"timeout_seconds": 0})
    assert "trust_project_settings" in settings_error("", {"trust_project_settings": "yes"})
    assert "project_instructions" in settings_error("", {"project_instructions": 1})


def test_argv_is_conservative_by_default_and_never_carries_the_prompt():
    argv = build_argv("claude", model="", model_settings={}, system_prompt_file="/tmp/p.md", session_id=None)
    assert argv == ["claude", "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
                    "--append-system-prompt-file", "/tmp/p.md", "--permission-mode", "dontAsk", "--setting-sources", "user"]
    argv = build_argv("claude", model="opus", model_settings={"permission_mode": "acceptEdits", "allowed_tools": ["Read", "Bash(a, b)"]},
                      system_prompt_file="/tmp/p.md", session_id=SESSION)
    assert argv[9:] == ["acceptEdits", "--setting-sources", "user", "--model", "opus", "--resume", SESSION,
                        "--allowedTools", "Read", "Bash(a, b)"]
    # A bot that trusts the directory gets its hooks, CLAUDE.md and .mcp.json servers.
    argv = build_argv("claude", model="", model_settings={"trust_project_settings": True}, system_prompt_file="/tmp/p.md",
                      session_id=None)
    assert "--setting-sources" not in argv


def test_disallowed_tools_are_named_last_and_never_touch_mcp():
    argv = build_argv("claude", model="", model_settings={}, system_prompt_file="/tmp/p.md", session_id=None,
                      extra_allowed=("mcp__openbot",), extra_disallowed=("Edit", "Write", "NotebookEdit", "WebSearch"))
    assert argv[argv.index("--disallowedTools") + 1:] == ["Edit", "Write", "NotebookEdit", "WebSearch"]
    assert argv[argv.index("--allowedTools") + 1] == "mcp__openbot"


def test_dontask_trim_only_when_default_mode_and_no_custom_allowed_tools():
    # Doc-backed: File modification (Edit/Write/NotebookEdit) and Web search are always denied under
    # dontAsk with no exception, unlike Bash (read-only commands) and WebFetch (preapproved domains).
    assert dontask_trim(None) == cli_agent.DONTASK_ALWAYS_DENIED
    assert dontask_trim({}) == cli_agent.DONTASK_ALWAYS_DENIED
    assert dontask_trim({"permission_mode": "dontAsk"}) == cli_agent.DONTASK_ALWAYS_DENIED
    assert dontask_trim({"permission_mode": "acceptEdits"}) == ()
    assert dontask_trim({"allowed_tools": ["Edit"]}) == ()                # the bot's own choice wins
    assert set(cli_agent.DONTASK_ALWAYS_DENIED) == {"Edit", "Write", "NotebookEdit", "WebSearch"}
    assert "Bash" not in cli_agent.DONTASK_ALWAYS_DENIED and "WebFetch" not in cli_agent.DONTASK_ALWAYS_DENIED


def test_claude_profile_dir_is_a_named_subfolder_of_the_shared_data_dir(settings, monkeypatch):
    from openbot.runtime import appdirs
    monkeypatch.setattr(appdirs, "openbot_data_dir", lambda **kw: Path("/data"))
    assert cli_agent.claude_profile_dir(settings) == Path("/data/claude-profile")


def test_login_commands_use_the_resolved_claude_path_in_every_shell_syntax():
    commands = login_commands(r"C:\claude\claude.exe", Path(r"C:\data\claude-profile"))
    assert commands["powershell"] == '$env:CLAUDE_CONFIG_DIR = "C:\\data\\claude-profile"; & "C:\\claude\\claude.exe" auth login'
    assert commands["cmd"] == 'set "CLAUDE_CONFIG_DIR=C:\\data\\claude-profile" && "C:\\claude\\claude.exe" auth login'
    assert commands["posix"] == 'CLAUDE_CONFIG_DIR="C:\\data\\claude-profile" "C:\\claude\\claude.exe" auth login'


async def test_claude_auth_status_reads_only_the_exit_code(fake):
    home, exe = fake
    (home / "auth_exit.txt").write_text("0")
    status = await claude_auth_status(str(exe), child_env())
    assert status == {"checked": True, "logged_in": True, "auth_method": "claudeai"}
    (home / "auth_exit.txt").write_text("1")
    status = await claude_auth_status(str(exe), child_env())
    assert status["checked"] is True and status["logged_in"] is False


def test_child_env_sets_claude_config_dir_only_when_a_profile_is_given():
    assert "CLAUDE_CONFIG_DIR" not in child_env({"PATH": "/bin"})
    assert child_env({"PATH": "/bin"}, profile_dir=Path("/data/claude-profile"))["CLAUDE_CONFIG_DIR"] == str(Path("/data/claude-profile"))


# --- the stream ------------------------------------------------------------------------------------------------

def test_mapper_turns_the_recorded_stream_into_run_actions():
    m = StreamMapper()
    actions = [a for line in TURN for a in m.feed(json.loads(line))]
    kinds = [k for k, _ in actions]
    assert kinds == ["session", "init", "delta", "delta", "text", "tool_call", "tool_result", "tool_call", "tool_result",
                     "delta", "text", "result"]
    assert m.session_id == SESSION
    assert actions[1][1] == {"model": "claude-sonnet-5", "permissionMode": "dontAsk", "apiKeySource": "none"}
    assert actions[5][1] == {"id": "toolu_01", "name": "Read", "args": {"file_path": "README.md"}}
    assert actions[6][1]["name"] == "Read" and actions[6][1]["status"] == "success" and "# Demo" in actions[6][1]["content"]
    assert actions[8][1] == {"tool_call_id": "toolu_02", "name": "Bash", "status": "error",
                             "content": "Permission to use Bash has been denied."}


def test_mapper_keeps_subagent_text_out_of_the_reply():
    m = StreamMapper()
    sub = {"type": "assistant", "parent_tool_use_id": "toolu_9", "session_id": SESSION,
           "message": {"content": [{"type": "text", "text": "subagent notes"}, {"type": "tool_use", "id": "t1", "name": "Grep", "input": {}}]}}
    assert [k for k, _ in m.feed(sub)] == ["session", "tool_call"]
    assert m.feed({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                                              "content": [{"type": "text", "text": "a"}, {"type": "image"}]}]}}) == [
        ("tool_result", {"tool_call_id": "t1", "name": "Grep", "status": "success", "content": "a\n[image]"})]


def test_mapper_survives_a_real_captured_stream():
    """claude_turn_real.jsonl (see its header comment above) was never hand-built: it is what the
    2.1.283 Windows exe actually wrote for `claude -p --output-format stream-json --verbose
    --include-partial-messages --append-system-prompt-file ... --permission-mode dontAsk
    --setting-sources user --model haiku` given "Lies notes.md und fasse sie in einem Satz zusammen."
    It carries thinking blocks, a `system/status` and `system/task_summary` noise events, and a trailing
    `system/task_summary` after the result -- none of which the engineered TURN above has."""
    m = StreamMapper()
    actions = [a for line in REAL_TURN for a in m.feed(json.loads(line))]
    kinds = [k for k, _ in actions]
    # Exactly one of each: thinking-only assistant events and the system/* noise produce nothing.
    assert kinds.count("init") == 1 and kinds.count("session") == 1 and kinds.count("result") == 1
    assert kinds.count("tool_call") == 1 and kinds.count("tool_result") == 1 and kinds.count("text") == 1
    assert m.session_id == SESSION
    tool_call = actions[kinds.index("tool_call")][1]
    assert tool_call["name"] == "Read" and tool_call["args"]["file_path"].endswith("notes.md")
    tool_result = actions[kinds.index("tool_result")][1]
    assert tool_result["status"] == "success" and "Team notes" in tool_result["content"]
    text = actions[kinds.index("text")][1]
    assert text.startswith("Freitags wird deployed")
    result = m.result
    assert (result["type"], result["subtype"], result["is_error"]) == ("result", "success", False)
    assert result_usage(result) == {"prompt_tokens": 17 + 39993 + 38316, "completion_tokens": 378,
                                    "cache_read_tokens": 38316, "total_tokens": 17 + 39993 + 38316 + 378,
                                    "model_calls": 2}


def test_mapper_on_a_real_run_with_openbot_mcp_tools_and_a_denied_bash_call():
    """What the real CLI emits for OpenBot's MCP tools and for a tool dontAsk denies. A denial arrives
    twice: as a `system/permission_denied` event and as an ordinary tool_result with is_error. The mapper
    records the tool_result (so the run card's call resolves as an error) and ignores the system event,
    so the denial shows up once; the result lists it again in permission_denials."""
    m = StreamMapper()
    events = [json.loads(line) for line in REAL_MCP_TURN]
    init = next(e for e in events if e.get("subtype") == "init")
    assert {"name": "openbot", "status": "connected", "source": "dynamic"} in init["mcp_servers"]
    assert {"mcp__openbot__manage_memory", "mcp__openbot__search_memory", "mcp__openbot__read_history"} <= set(init["tools"])
    denial = next(e for e in events if e.get("subtype") == "permission_denied")
    assert denial["tool_name"] == "Bash" and denial["decision_reason_type"] == "mode"

    actions = [a for e in events for a in m.feed(e)]
    calls_ = [p for k, p in actions if k == "tool_call"]
    results = {p["tool_call_id"]: p for k, p in actions if k == "tool_result"}
    names = [c["name"] for c in calls_]
    assert names == ["ToolSearch", "mcp__openbot__manage_memory", "mcp__openbot__search_memory", "Bash"]
    assert len(results) == len(calls_)                                   # every call resolves, the denial once
    by_name = {c["name"]: results[c["id"]] for c in calls_}
    assert by_name["mcp__openbot__manage_memory"]["status"] == "success"
    assert "Release freeze starts Thursday 6pm" in by_name["mcp__openbot__search_memory"]["content"]
    bash = by_name["Bash"]
    assert bash["status"] == "error" and "Permission to use Bash has been denied" in bash["content"]
    assert denial["tool_use_id"] == bash["tool_call_id"]
    result = m.result
    assert result["subtype"] == "success" and [d["tool_name"] for d in result["permission_denials"]] == ["Bash"]


def test_result_usage_prefers_model_usage_and_counts_cache_as_prompt():
    result = json.loads(TURN[-1])
    assert result_usage(result) == {"prompt_tokens": 18 + 1200 + 29500 + 300, "completion_tokens": 95 + 20,
                                    "cache_read_tokens": 29500, "total_tokens": 18 + 1200 + 29500 + 300 + 115,
                                    "model_calls": 3}
    del result["modelUsage"]
    assert result_usage(result)["prompt_tokens"] == 18 + 1200 + 29500


def test_render_and_since_last_reply():
    history = [HumanMessage(content="[You]: hi"), AIMessage(content="hello"), HumanMessage(content="[You] (new): build it")]
    assert render_messages(history, "Eng") == "[You]: hi\n\n[Eng (you)]: hello\n\n[You] (new): build it"
    assert since_last_reply(history) == history[2:]
    assert since_last_reply(history[:1]) == history[:1]


def test_cli_prompt_does_not_offer_openbot_tools():
    bot = Actor(id="a1", kind="bot", handle="eng", name="Eng", description="", bot=BotProfile(provider=CLAUDE_CODE, instructions="build"))
    other = bot_actor("rev", description="reviews")
    other.id = "a2"
    kw = {"bot": bot, "all_bots": [bot, other], "participants": ["You"], "memories": [], "workspace_root": "/w",
          "older_count": 5, "tool_names": [], "default_bot_handle": "eng"}
    cli, std = build_system_prompt(**kw, cli=True), build_system_prompt(**kw)
    for name in ("ask_human", "schedule_message", "manage_memory", "read_history"):
        assert name in std and name not in cli
    assert "@rev" in cli and "Your working directory in this thread is /w" in cli


def test_providers_list_claude_code_only_as_configured_when_found(settings, fake):
    _home, exe = fake
    settings.claude_code_path = str(exe.parent / "missing")
    assert provider_status(settings)[-1] == {"id": CLAUDE_CODE, "configured": False, "models": ["sonnet", "opus", "haiku"],
                                             "default_model": ""}
    settings.claude_code_path = str(exe)
    assert provider_status(settings)[-1]["configured"] is True


def test_claude_code_bots_are_never_rerouted_and_side_tasks_use_auto(settings):
    settings.openrouter_api_key = "k"
    profile = BotProfile(provider=CLAUDE_CODE, model="opus")
    assert effective_bot_profile(profile, settings) == (CLAUDE_CODE, "opus")
    assert chat_model(profile, settings).model_name == "openai/gpt-4o-mini"


# --- runs ------------------------------------------------------------------------------------------------------

async def test_run_spawns_claude_streams_events_and_hands_off(settings, fake):
    home, exe = fake
    settings.claude_code_path = str(exe)
    script(home, {"lines": TURN})
    services, eng, t, run = await make(settings)
    await services.runner.execute(run.id)

    run = await get(services, Run, run.id)
    assert run.status == "completed", run.error
    assert (run.prompt_tokens, run.completion_tokens, run.cache_read_tokens, run.model_calls) == (31018, 115, 29500, 3)
    [call] = calls(home)
    assert Path(call["cwd"]).resolve() == settings.workspace_root.resolve()
    assert call["argv"][:5] == ["-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
    assert call["argv"][call["argv"].index("--permission-mode") + 1] == "dontAsk" and "--resume" not in call["argv"]
    assert "[You] (new): @eng please build it" in call["stdin"]
    assert "You are Eng (@eng)" in call["system_prompt"] and "@rev (Rev)" in call["system_prompt"]
    assert "FAKE_CLAUDE_DIR" in call["env"] and "SECRET_KEY" not in call["env"]
    assert call["argv"][call["argv"].index("--setting-sources") + 1] == "user"
    assert [e.type for e in await events(services, run.id)] == ["cli_session", "text", "tool_call", "tool_result",
                                                                "tool_call", "tool_result", "text", "message"]
    reply = (await thread_messages(services, t.id))[-1]
    assert reply.sender_actor_id == eng.id and reply.run_id == run.id and reply.content.endswith("@rev - please review the change.")
    async with services.session_factory() as s:
        rev = (await s.execute(select(Actor).where(Actor.handle == "rev"))).scalar_one()
        woken = (await s.execute(select(InboxItem.actor_id).where(InboxItem.message_id == reply.id))).scalars().all()
    assert rev.id in woken and eng.id not in woken
    [session] = [e.payload for e in await events(services, run.id) if e.type == "cli_session"]
    assert session["session_id"] == SESSION and session["billing"] == "subscription"


async def test_an_anthropic_key_in_the_environment_is_recorded_as_api_billing(settings, fake, monkeypatch):
    home, exe = fake
    settings.claude_code_path = str(exe)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    script(home, {"lines": [json.dumps({**json.loads(init_line()), "apiKeySource": "ANTHROPIC_API_KEY"}), result_line("ok")]})
    services, _eng, _t, run = await make(settings)
    await services.runner.execute(run.id)
    assert "ANTHROPIC_API_KEY" in calls(home)[0]["env"]
    [session] = [e.payload for e in await events(services, run.id) if e.type == "cli_session"]
    assert session["billing"] == "api"
    async with services.session_factory() as s:
        row = (await s.execute(select(ActivityLog).where(ActivityLog.run_id == run.id,
                                                         ActivityLog.event == "run.cli_session"))).scalar_one()
    assert row.detail["billing"] == "api" and row.detail["api_key_source"] == "ANTHROPIC_API_KEY"
    assert "sk-test" not in json.dumps(row.detail) and "sk-test" not in row.summary


async def test_lines_far_past_the_default_stream_limit_are_read_whole(settings, fake):
    # asyncio's default is 64 KiB per line; one file read in a tool_result is easily more.
    home, exe = fake
    settings.claude_code_path = str(exe)
    big = "Gr\u00f6\u00dfe: " + "x" * 300_000
    lines = [init_line(),
             json.dumps({"type": "assistant", "session_id": SESSION, "parent_tool_use_id": None,
                         "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "big.txt"}}]}}),
             json.dumps({"type": "user", "session_id": SESSION, "parent_tool_use_id": None,
                         "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": big}]}}, ensure_ascii=False),
             result_line("Die Datei ist gro\u00df.")]
    script(home, {"lines": lines})
    services, _eng, t, run = await make(settings)
    await services.runner.execute(run.id)
    assert (await get(services, Run, run.id)).status == "completed"
    [result] = [e.payload for e in await events(services, run.id) if e.type == "tool_result"]
    assert result["content"] == big[:TOOL_RESULT_CAP]
    assert (await thread_messages(services, t.id))[-1].content == "Die Datei ist gro\u00df."


async def test_follow_up_resumes_the_session_with_only_new_messages(settings, fake):
    home, exe = fake
    settings.claude_code_path = str(exe)
    script(home, {"lines": TURN}, {"lines": [init_line(), result_line("Tests added.")]})
    services, eng, t, run = await make(settings)
    await services.runner.execute(run.id)
    second = await queue(services, t.id, eng.id, "now add tests")
    await services.runner.execute(second.id)

    assert (await get(services, Run, second.id)).status == "completed"
    _first_call, second_call = calls(home)
    assert second_call["argv"][second_call["argv"].index("--resume") + 1] == SESSION
    assert "now add tests" in second_call["stdin"] and "please build it" not in second_call["stdin"]
    # The system prompt is sent again each turn: the roster and memories may have changed since.
    assert "You are Eng (@eng)" in second_call["system_prompt"]
    assert (await thread_messages(services, t.id))[-1].content == "Tests added."
    # The version is checked once per install, not on every turn.
    assert (home / "versions.txt").read_text() == "x"


async def test_a_session_the_cli_no_longer_knows_starts_over_with_the_thread(settings, fake):
    home, exe = fake
    settings.claude_code_path = str(exe)
    script(home, {"lines": TURN},
           {"stderr": f"No conversation found with session ID: {SESSION}\n", "exit": 1},
           {"lines": [init_line("0e1d2c3b-4a59-4687-9a0b-c1d2e3f4a5b6"), result_line("Fresh start.", session="0e1d2c3b-4a59-4687-9a0b-c1d2e3f4a5b6")]})
    services, eng, t, run = await make(settings)
    await services.runner.execute(run.id)
    second = await queue(services, t.id, eng.id, "again")
    await services.runner.execute(second.id)

    assert (await get(services, Run, second.id)).status == "completed"
    _first, gone, fresh = calls(home)
    assert "--resume" in gone["argv"] and "--resume" not in fresh["argv"]
    assert "please build it" in fresh["stdin"] and "again" in fresh["stdin"]
    stored = [e.payload for e in await events(services, second.id) if e.type == "cli_session"]
    assert [p["session_id"] for p in stored] == ["0e1d2c3b-4a59-4687-9a0b-c1d2e3f4a5b6"]


@pytest.mark.parametrize(("model_settings", "expected"), [
    ({}, True),                                        # on by default
    ({"project_instructions": False}, False),          # turned off for this bot
    ({"trust_project_settings": True}, False),         # the CLI loads CLAUDE.md itself then; no second copy
])
async def test_the_projects_claude_md_is_appended_to_the_system_prompt(settings, fake, model_settings, expected):
    home, exe = fake
    settings.claude_code_path = str(exe)
    settings.workspace_root.mkdir(parents=True, exist_ok=True)
    (settings.workspace_root / "CLAUDE.md").write_text("Run the tests with make check.", encoding="utf-8")
    script(home, {"lines": [init_line(), result_line("ok")]})
    services, _eng, _t, run = await make(settings, model_settings=model_settings)
    await services.runner.execute(run.id)
    assert (await get(services, Run, run.id)).status == "completed"
    prompt = calls(home)[0]["system_prompt"]
    assert ("# Project instructions" in prompt and "Run the tests with make check." in prompt) is expected
    assert "You are Eng (@eng)" in prompt


async def test_default_dontask_bots_get_disallowed_tools_trimmed_in_a_real_run(settings, fake):
    home, exe = fake
    settings.claude_code_path = str(exe)
    script(home, {"lines": [init_line(), result_line("ok")]})
    services, _eng, _t, run = await make(settings)  # default model_settings: dontAsk, no allowed_tools
    await services.runner.execute(run.id)
    assert (await get(services, Run, run.id)).status == "completed"
    argv = calls(home)[0]["argv"]
    assert argv[argv.index("--disallowedTools") + 1:] == ["Edit", "Write", "NotebookEdit", "WebSearch"]


async def test_isolated_profile_sets_claude_config_dir_when_logged_in(settings, fake):
    home, exe = fake
    settings.claude_code_path = str(exe)
    settings.claude_code_own_profile = True
    (home / "auth_exit.txt").write_text("0")
    script(home, {"lines": [init_line(), result_line("ok")]})
    services, _eng, _t, run = await make(settings)
    await services.runner.execute(run.id)
    run = await get(services, Run, run.id)
    assert run.status == "completed", run.error
    assert "CLAUDE_CONFIG_DIR" in calls(home)[0]["env"]


async def test_isolated_profile_without_login_fails_clearly_before_spawning_the_real_run(settings, fake):
    home, exe = fake
    settings.claude_code_path = str(exe)
    settings.claude_code_own_profile = True
    (home / "auth_exit.txt").write_text("1")
    script(home, {"lines": [init_line(), result_line("should not run")]})
    services, _eng, t, run = await make(settings)
    await services.runner.execute(run.id)
    run = await get(services, Run, run.id)
    assert run.status == "failed"
    assert "isolated Claude Code profile is not logged in" in run.error and "auth login" in run.error
    assert calls(home) == []  # never got to the actual turn
    assert "auth login" in (await thread_messages(services, t.id))[-1].content


async def test_a_session_from_another_working_directory_is_not_resumed(settings, fake):
    home, exe = fake
    settings.claude_code_path = str(exe)
    script(home, {"lines": [init_line(), result_line("ok")]})
    services, _eng, _t, run = await make(settings)
    async with services.session_factory() as s:
        s.add(RunEvent(run_id=run.id, seq=99, type="cli_session", payload={"session_id": SESSION, "cwd": "/elsewhere"}))
        await s.commit()
    await services.runner.execute(run.id)
    assert "--resume" not in calls(home)[0]["argv"]


async def test_a_cli_too_old_for_setting_sources_fails_before_it_runs(settings, fake, monkeypatch):
    # Without --setting-sources the CLI would load the directory's hooks and .mcp.json.
    home, exe = fake
    settings.claude_code_path = str(exe)
    monkeypatch.setenv("FAKE_CLAUDE_VERSION", "2.1.100 (Claude Code)")
    script(home, {"lines": [init_line(), result_line("should not run")]})
    services, _eng, _t, run = await make(settings)
    await services.runner.execute(run.id)
    run = await get(services, Run, run.id)
    assert run.status == "failed"
    assert run.error == "error: Claude Code 2.1.100 is too old; OpenBot needs 2.1.210 or later. Update it with `claude update`."
    assert calls(home) == []


async def test_an_unreadable_version_fails_the_run(settings, fake, monkeypatch):
    home, exe = fake
    settings.claude_code_path = str(exe)
    monkeypatch.setenv("FAKE_CLAUDE_VERSION", "unknown")
    script(home, {"lines": [init_line(), result_line("should not run")]})
    services, _eng, _t, run = await make(settings)
    await services.runner.execute(run.id)
    run = await get(services, Run, run.id)
    assert run.status == "failed" and run.error.startswith("error: could not read the Claude Code version") and calls(home) == []


async def test_missing_claude_fails_the_run_with_a_clear_error(settings, tmp_path):
    settings.claude_code_path = str(tmp_path / "no-claude")
    services, _eng, t, run = await make(settings)
    await services.runner.execute(run.id)
    run = await get(services, Run, run.id)
    assert run.status == "failed" and run.error.startswith("error: Claude Code CLI (claude) not found")
    assert (await thread_messages(services, t.id))[-1].content.startswith("@eng failed: error: Claude Code CLI")


async def test_an_error_result_fails_the_run_and_keeps_its_usage(settings, fake, monkeypatch):
    home, exe = fake
    settings.claude_code_path = str(exe)
    script(home, {"lines": [init_line(), result_line("Credit balance is too low", subtype="error_during_execution", is_error=True)],
                  "exit": 1})
    services, _eng, _t, run = await make(settings)
    dropped = []

    async def fake_drop(run_id):
        dropped.append(run_id)

    monkeypatch.setattr(services.runner, "_drop_checkpoint", fake_drop)
    await services.runner.execute(run.id)
    run = await get(services, Run, run.id)
    assert run.status == "failed" and run.error == "error: claude run failed (error_during_execution): Credit balance is too low"
    assert run.prompt_tokens == 105 and run.completion_tokens == 7
    # A CLI-agent run never checkpoints a LangGraph agent; a failure must not try to drop one either.
    assert dropped == []


async def test_a_crash_without_result_reports_stderr(settings, fake):
    home, exe = fake
    settings.claude_code_path = str(exe)
    script(home, {"stderr": "error: unknown option '--include-partial-messages'\n", "exit": 2})
    services, _eng, _t, run = await make(settings)
    await services.runner.execute(run.id)
    run = await get(services, Run, run.id)
    assert run.status == "failed" and "exited with code 2" in run.error and "unknown option" in run.error


async def wait_for_pids(home: Path) -> tuple[int, int]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        f = home / "pids.txt"
        if f.exists() and len(f.read_text().split()) == 2:
            a, b = f.read_text().split()
            return int(a), int(b)
        await asyncio.sleep(0.05)
    raise AssertionError("the fake claude never started its child")


async def wait_dead(*pids: int) -> bool:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not any(alive(p) for p in pids):
            return True
        await asyncio.sleep(0.05)
    return False


async def test_cancelling_a_run_stops_the_cli_and_its_children(settings, fake, monkeypatch):
    home, exe = fake
    settings.claude_code_path = str(exe)
    script(home, {"lines": [init_line()], "hang": True})
    services, _eng, _t, run = await make(settings)
    # A CLI-agent run never checkpoints a LangGraph agent; cancelling one must not try to drop one either.
    dropped = []

    async def fake_drop(run_id):
        dropped.append(run_id)

    monkeypatch.setattr(services.runner, "_drop_checkpoint", fake_drop)
    task = asyncio.create_task(services.runner.execute(run.id))
    parent, child = await wait_for_pids(home)
    assert alive(parent) and alive(child)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await wait_dead(parent, child)
    assert (await get(services, Run, run.id)).status == "cancelled"
    assert dropped == []


async def test_a_run_past_its_timeout_is_stopped(settings, fake):
    home, exe = fake
    settings.claude_code_path = str(exe)
    script(home, {"lines": [init_line()], "hang": True})
    services, _eng, _t, run = await make(settings, model_settings={"timeout_seconds": 3})
    await services.runner.execute(run.id)
    parent, child = await wait_for_pids(home)
    assert await wait_dead(parent, child)
    run = await get(services, Run, run.id)
    assert run.status == "failed" and run.error == "error: claude did not finish within 3s and was stopped"


# --- API -------------------------------------------------------------------------------------------------------

async def test_api_accepts_claude_code_bots_without_a_model_and_checks_their_settings(client):
    r = await client.post("/api/v1/bots", json={"handle": "coder", "name": "Coder", "provider": "claude-code"})
    assert r.status_code == 201 and r.json()["provider"] == "claude-code" and r.json()["model"] == ""
    bad = await client.patch(f"/api/v1/bots/{r.json()['id']}", json={"model_settings": {"permission_mode": "bypassPermissions"}})
    assert bad.status_code == 422 and "permission_mode" in bad.text
    ok = await client.patch(f"/api/v1/bots/{r.json()['id']}", json={"model_settings": {"permission_mode": "acceptEdits",
                                                                                "allowed_tools": ["Read", "Edit"]}})
    assert ok.status_code == 200
    providers = (await client.get("/api/v1/providers")).json()["providers"]
    assert providers[-1]["id"] == "claude-code"


def test_settings_default_timeout():
    assert Settings(_env_file=None).claude_code_timeout == 1800
    assert cli_agent.DEFAULT_PERMISSION_MODE == "dontAsk"


# --- OpenBot's tools over MCP (runtime/cli_mcp.py) ------------------------------------------------------------

async def test_the_cli_reaches_this_bots_memory_over_mcp_with_a_token_that_dies_with_the_run(settings, fake):
    home, exe = fake
    settings.claude_code_path = str(exe)
    script(home, {"mcp_calls": [{"tool": "manage_memory", "args": {"action": "create", "content": "deploys on Fridays"}},
                                {"tool": "search_memory", "args": {"query": "deploy"}}],
                  "lines": [init_line(), result_line("noted")]})
    services, eng, _t, run = await make(settings)
    await services.runner.execute(run.id)
    assert (await get(services, Run, run.id)).status == "completed"

    argv = calls(home)[0]["argv"]
    config_file = argv[argv.index("--mcp-config") + 1]
    allowed_end = argv.index("--disallowedTools") if "--disallowedTools" in argv else len(argv)
    assert argv[argv.index("--allowedTools") + 1:allowed_end] == ["mcp__openbot"]  # the only rule
    # No custom allowed_tools and the default dontAsk mode: the guaranteed-denied tools are trimmed too.
    assert argv[argv.index("--disallowedTools") + 1:] == list(cli_agent.DONTASK_ALWAYS_DENIED)
    assert not Path(config_file).exists()                                 # it held the token; gone after the run
    first, created, searched = mcp_log(home)
    server = first["config"]["mcpServers"]["openbot"]
    assert server["url"].startswith("http://127.0.0.1:") and server["headers"]["Authorization"].startswith("Bearer ")
    assert created["status"] == 200 and searched["status"] == 200
    assert "deploys on Fridays" in json.dumps(searched["body"])
    assert await memory.relevant_memories(services.store, eng.id, "deploy") == ["deploys on Fridays"]
    # The token was revoked when the run ended.
    token = server["headers"]["Authorization"].removeprefix("Bearer ")
    assert services.cli_mcp.lookup(token) is None
    prompt = calls(home)[0]["system_prompt"]
    assert "mcp__openbot__manage_memory" in prompt and "mcp__openbot__search_memory" in prompt


async def test_a_bot_can_turn_openbots_tools_off(settings, fake):
    home, exe = fake
    settings.claude_code_path = str(exe)
    script(home, {"lines": [init_line(), result_line("ok")]})
    services, _eng, _t, run = await make(settings, model_settings={"openbot_tools": False, "allowed_tools": ["Read"]})
    await services.runner.execute(run.id)
    call = calls(home)[0]
    assert "--mcp-config" not in call["argv"] and "mcp__openbot" not in call["argv"]
    assert call["argv"][call["argv"].index("--allowedTools") + 1:] == ["Read"]
    assert "mcp__openbot" not in call["system_prompt"] and services.cli_mcp is None


async def test_user_rules_come_first_and_openbots_rule_is_added(settings, fake):
    home, exe = fake
    settings.claude_code_path = str(exe)
    script(home, {"lines": [init_line(), result_line("ok")]})
    services, _eng, _t, run = await make(settings, model_settings={"allowed_tools": ["Read", "Bash(git diff *)"]})
    await services.runner.execute(run.id)
    argv = calls(home)[0]["argv"]
    assert argv[argv.index("--allowedTools") + 1:] == ["Read", "Bash(git diff *)", "mcp__openbot"]


async def test_a_failed_run_revokes_its_token_too(settings, fake):
    home, exe = fake
    settings.claude_code_path = str(exe)
    script(home, {"mcp_calls": [{"tool": "search_memory", "args": {"query": "x"}}], "stderr": "boom\n", "exit": 3})
    services, _eng, _t, run = await make(settings)
    await services.runner.execute(run.id)
    assert (await get(services, Run, run.id)).status == "failed"
    token = mcp_log(home)[0]["config"]["mcpServers"]["openbot"]["headers"]["Authorization"].removeprefix("Bearer ")
    assert services.cli_mcp.lookup(token) is None


@pytest.mark.parametrize(("api_key", "expected"), [("k", True), (None, False)])
async def test_memory_reflection_follows_a_cli_run_only_with_a_chat_provider(settings, fake, api_key, expected):
    home, exe = fake
    settings.claude_code_path = str(exe)
    settings.openrouter_api_key = api_key
    script(home, {"lines": [init_line(), result_line("done and dusted")]})
    services, eng, _t, run = await make(settings)
    scheduled = []
    services.reflector.schedule = lambda bot, messages, thread_id: scheduled.append((bot.id, messages))
    await services.runner.execute(run.id)
    assert bool(scheduled) is expected
    if expected:
        bot_id, messages = scheduled[0]
        assert bot_id == eng.id and messages[-1].content == "done and dusted"
