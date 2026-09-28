from sqlalchemy import select

from openbot.config import Settings
from openbot.db.models import Actor
from openbot.runtime.providers import effective_bot_profile
from openbot.seed import CLAUDE_CODE_MODEL_SETTINGS, DEMO_BOTS, ensure_cron_actor, seed_demo_bots
from tests.conftest import build_test_services


def _fake_claude(tmp_path) -> str:
    p = tmp_path / "claude"
    p.write_text("#!/bin/sh\n")
    p.chmod(0o755)
    return str(p)


async def test_seed_once(services):
    services.settings.openrouter_api_key = "k"
    assert await seed_demo_bots(services) == 4
    assert await seed_demo_bots(services) == 0
    async with services.session_factory() as s:
        bots = {a.handle: a for a in (await s.execute(select(Actor).where(Actor.kind == "bot"))).scalars()}
    assert set(bots) == {"chief_of_staff", "engineer", "reviewer", "qa"}
    # Demo bots default to provider "auto" so they keep working as keys are added/removed/changed.
    assert bots["engineer"].bot.provider == "auto"
    assert effective_bot_profile(bots["engineer"].bot, services.settings) == ("openrouter", "openai/gpt-4o-mini")
    assert "run_shell" in bots["engineer"].bot.tool_names and "write_file" not in bots["reviewer"].bot.tool_names
    assert all(services.registry.has(t) for b in DEMO_BOTS for t in b["tool_names"])


async def test_seed_skipped_without_provider(services):
    assert await seed_demo_bots(services) == 0


async def test_seed_uses_configured_bot_model(services):
    services.settings.openrouter_api_key = "k"
    services.settings.bot_model = "google/gemini-2.0-flash-001"
    assert await seed_demo_bots(services) == 4
    async with services.session_factory() as s:
        bots = {a.handle: a for a in (await s.execute(select(Actor).where(Actor.kind == "bot"))).scalars()}
    assert bots["chief_of_staff"].bot.provider == "auto"
    assert effective_bot_profile(bots["chief_of_staff"].bot, services.settings) == ("openrouter", "google/gemini-2.0-flash-001")


async def test_seed_uses_claude_code_when_selected_and_no_api_provider_is_configured(services, tmp_path):
    services.settings.claude_code_selected = True
    services.settings.claude_code_path = _fake_claude(tmp_path)
    services.settings.claude_code_model = "opus"
    assert await seed_demo_bots(services) == 4
    async with services.session_factory() as s:
        bots = {a.handle: a.bot for a in (await s.execute(select(Actor).where(Actor.kind == "bot"))).scalars()}
    assert all((b.provider, b.model) == ("claude-code", "opus") for b in bots.values())
    # dontAsk for now (CLAUDE_CODE_MODEL_SETTINGS is empty for every demo bot): no permission_mode key.
    assert all("permission_mode" not in b.model_settings for b in bots.values())
    assert bots["chief_of_staff"].model_settings.get("max_model_calls") == 6            # spec-level settings survive the merge


async def test_seed_prefers_an_api_provider_over_claude_code(services, tmp_path):
    services.settings.openrouter_api_key = "k"
    services.settings.claude_code_selected = True
    services.settings.claude_code_path = _fake_claude(tmp_path)
    assert await seed_demo_bots(services) == 4
    async with services.session_factory() as s:
        bots = {a.handle: a.bot for a in (await s.execute(select(Actor).where(Actor.kind == "bot"))).scalars()}
    assert all(b.provider == "auto" for b in bots.values())


async def test_seed_skipped_when_claude_code_selected_but_the_cli_is_not_found(services, tmp_path):
    services.settings.claude_code_selected = True
    services.settings.claude_code_path = str(tmp_path / "not-there")
    assert await seed_demo_bots(services) == 0


async def test_claude_code_seed_has_the_same_instructions_as_the_auto_seed(services, tmp_path):
    services.settings.openrouter_api_key = "k"
    assert await seed_demo_bots(services) == 4
    async with services.session_factory() as s:
        auto = {a.handle: a.bot.instructions for a in (await s.execute(select(Actor).where(Actor.kind == "bot"))).scalars()}

    cli_dir = tmp_path / "cli"
    cli_dir.mkdir()
    cli_settings = Settings(database_url=f"sqlite+aiosqlite:///{cli_dir}/test.db", workspace_root=cli_dir / "workspace",
                            tools_dir=cli_dir / "tools", log_file=cli_dir / "logs" / "openbot.log", seed_demo_bots=False,
                            mcp_config=cli_dir / "mcp.json", mcp_token_key_file=cli_dir / "mcp_token.key",
                            secret_key_file=cli_dir / "secret.key", claude_code_selected=True,
                            claude_code_path=_fake_claude(tmp_path), _env_file=None)
    cli_services = await build_test_services(cli_settings)
    assert await seed_demo_bots(cli_services) == 4
    async with cli_services.session_factory() as s:
        cli = {a.handle: a.bot.instructions for a in (await s.execute(select(Actor).where(Actor.kind == "bot"))).scalars()}

    assert auto == cli and set(auto) == {"chief_of_staff", "engineer", "reviewer", "qa"}


def test_claude_code_model_settings_covers_every_demo_bot_and_starts_conservative():
    """One dict entry per demo bot is the single place to loosen a specific bot's claude-code
    permissions later (see seed_demo_bots/sync_demo_bots); empty means the conservative default."""
    assert set(CLAUDE_CODE_MODEL_SETTINGS) == {b["handle"] for b in DEMO_BOTS}
    assert all(v == {} for v in CLAUDE_CODE_MODEL_SETTINGS.values())


async def test_ensure_cron_actor_idempotent(services):
    # The `services` fixture already seeds it; a second call must not create a duplicate handle.
    actor = await ensure_cron_actor(services)
    assert actor.kind == "system" and actor.handle == "cron"
    async with services.session_factory() as s:
        rows = (await s.execute(select(Actor).where(Actor.handle == "cron"))).scalars().all()
        assert [r.id for r in rows] == [actor.id]


def test_chief_of_staff_delegates_in_the_same_thread():
    chief = next(b for b in DEMO_BOTS if b["handle"] == "chief_of_staff")
    assert "in this same thread" in chief["instructions"]


async def test_sync_updates_existing_demo_bots_without_touching_the_rest(services):
    """The seed skips existing rows, so a redesigned team never reaches a running install. Sync
    rewrites the demo bots' instructions, tools, approval tools and model settings from DEMO_BOTS,
    leaves their provider/model pin alone, and ignores bots that are not part of the demo team."""
    from openbot.db.models import Actor, BotProfile
    from openbot.seed import sync_demo_bots
    services.settings.openrouter_api_key = "k"
    assert await seed_demo_bots(services) == 4
    async with services.session_factory() as s:
        chief = (await s.execute(select(Actor).where(Actor.handle == "chief_of_staff"))).scalar_one()
        chief.bot.instructions, chief.bot.tool_names, chief.bot.model_settings = "old", ["read_file"], {}
        chief.bot.provider, chief.bot.model = "ollama", "qwen3.6"
        s.add(Actor(kind="bot", handle="custom", name="Custom", bot=BotProfile(instructions="mine", tool_names=["run_shell"])))
        await s.commit()
    assert await sync_demo_bots(services) == 4
    async with services.session_factory() as s:
        chief = (await s.execute(select(Actor).where(Actor.handle == "chief_of_staff"))).scalar_one()
        spec = next(b for b in DEMO_BOTS if b["handle"] == "chief_of_staff")
        assert chief.bot.instructions == spec["instructions"] and chief.bot.tool_names == spec["tool_names"]
        assert chief.bot.model_settings == spec["model_settings"]
        assert (chief.bot.provider, chief.bot.model) == ("ollama", "qwen3.6")      # the operator's pin survives
        custom = (await s.execute(select(Actor).where(Actor.handle == "custom"))).scalar_one()
        assert custom.bot.instructions == "mine" and custom.bot.tool_names == ["run_shell"]


async def test_sync_reapplies_the_claude_code_override_for_a_claude_code_demo_bot(services, tmp_path, monkeypatch):
    """A future non-empty CLAUDE_CODE_MODEL_SETTINGS entry must survive sync_demo_bots, not be wiped
    back to the spec's own (provider-neutral) model_settings."""
    from openbot.seed import sync_demo_bots
    monkeypatch.setitem(CLAUDE_CODE_MODEL_SETTINGS, "engineer", {"permission_mode": "acceptEdits"})
    services.settings.claude_code_selected = True
    services.settings.claude_code_path = _fake_claude(tmp_path)
    assert await seed_demo_bots(services) == 4
    async with services.session_factory() as s:
        engineer = (await s.execute(select(Actor).where(Actor.handle == "engineer"))).scalar_one()
        assert engineer.bot.model_settings == {"permission_mode": "acceptEdits"}
        engineer.bot.model_settings = {}  # simulate drift, e.g. an operator edit
        await s.commit()
    assert await sync_demo_bots(services) == 4
    async with services.session_factory() as s:
        engineer = (await s.execute(select(Actor).where(Actor.handle == "engineer"))).scalar_one()
        assert engineer.bot.model_settings == {"permission_mode": "acceptEdits"}
