import logging

import pytest

from openbot.db.models import Thread
from openbot.runtime.delivery import create_thread, human_actor, post_message
from openbot.runtime.providers import chat_model
from tests.factories import bot_actor
from tests.fakes import ai


@pytest.fixture(autouse=True)
async def _no_actor_runs(services):
    await services.actors.stop()


async def seed(services, *actors):
    async with services.session_factory() as session:
        session.add_all(actors)
        await session.commit()
        return actors


async def test_auto_rename_after_third_message_and_only_once(services, scripts):
    await seed(services, bot_actor("chief_of_staff"), bot_actor("eng"))
    scripts["chief_of_staff"] = [ai('  "Purposeful title"  ')]
    async with services.bus.subscribe(None) as queue:
        async with services.session_factory() as session:
            you = await human_actor(session)
            thread = await create_thread(services, session, title="Initial", handles=["eng"], created_by=you)
            for content in ("first", "second", "third"):
                await post_message(services, session, thread_id=thread.id, sender=you, content=content)

        async with services.session_factory() as session:
            renamed = await session.get(Thread, thread.id)
            assert renamed.title == "Purposeful title"
            assert renamed.auto_renamed is True
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
    updates = [event for event in events if event["event"] == "thread.updated"]
    assert len(updates) == 1
    assert updates[0]["data"]["title"] == "Purposeful title"


async def test_auto_rename_claim_remains_consumed_when_provider_fails(services, scripts):
    await seed(services, bot_actor("chief_of_staff"), bot_actor("eng"))
    scripts["chief_of_staff"] = [RuntimeError("provider unavailable")]
    async with services.session_factory() as session:
        you = await human_actor(session)
        thread = await create_thread(services, session, title="Initial", handles=["eng"], created_by=you)
        for content in ("first", "second", "third"):
            await post_message(services, session, thread_id=thread.id, sender=you, content=content)
    async with services.session_factory() as session:
        renamed = await session.get(Thread, thread.id)
        assert renamed.title == "Initial"
        assert renamed.auto_renamed is True


async def test_auto_rename_fails_silently_with_no_provider_configured(services, caplog):
    """The `services` fixture always scripts model_factory, so this wires up the real chat_model() to
    exercise the case #196's setup work actually cares about: a claude-code-only install has no API
    provider for renaming to fall back to, and chat_model() raises before any model call is even made."""
    for k in ("openai_api_key", "anthropic_api_key", "openrouter_api_key", "xai_api_key", "ollama_base_url"):
        setattr(services.settings, k, None)
    services.model_factory = lambda actor: chat_model(actor.bot, services.settings)
    await seed(services, bot_actor("chief_of_staff", provider="auto", model=""), bot_actor("eng"))
    caplog.set_level(logging.ERROR)
    async with services.session_factory() as session:
        you = await human_actor(session)
        thread = await create_thread(services, session, title="Initial", handles=["eng"], created_by=you)
        for content in ("first", "second", "third"):
            await post_message(services, session, thread_id=thread.id, sender=you, content=content)
    async with services.session_factory() as session:
        renamed = await session.get(Thread, thread.id)
        assert renamed.title == "Initial" and renamed.auto_renamed is True   # claimed, never crashed, never renamed
    assert any("automatic thread rename failed" in r.message for r in caplog.records)
