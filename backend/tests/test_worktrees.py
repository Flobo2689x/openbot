"""Isolated per-thread git worktrees (runtime/worktrees.py), against real git repositories in tmp_path."""
import asyncio
import subprocess
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy import select

from openbot.db.models import Thread
from openbot.db.session import _alembic_config, make_engine, run_migrations
from openbot.runtime.worktrees import (
    WorktreeError,
    create_worktree,
    default_worktrees_dir,
    remove_worktree,
    working_directory_of,
    worktree_status,
)
from tests.factories import bot_actor


def run_git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    run_git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("hello\n")
    (path / "pkg").mkdir()
    (path / "pkg" / "code.py").write_text("x = 1\n")
    run_git(path, "add", ".")
    run_git(path, "commit", "-q", "-m", "init")
    return path


def commit_in(path: Path, name: str):
    (path / name).write_text(name)
    run_git(path, "add", name)
    run_git(path, "commit", "-q", "-m", f"add {name}")


@pytest.fixture
def repo(tmp_path, settings):
    settings.worktrees_dir = tmp_path / "wt"
    return make_repo(tmp_path / "project")


def test_default_location_is_per_user_and_outside_any_repository(tmp_path):
    assert default_worktrees_dir(platform="win32", env={"LOCALAPPDATA": r"C:\Users\a\AppData\Local"}) \
        == Path(r"C:\Users\a\AppData\Local") / "OpenBot" / "wt"
    assert default_worktrees_dir(platform="linux", env={"XDG_DATA_HOME": "/x"}) == Path("/x/openbot/wt")
    assert default_worktrees_dir(platform="linux", env={}, home=Path("/home/a")) == Path("/home/a/.local/share/openbot/wt")


async def test_creates_a_worktree_and_branch_outside_the_repository(repo, settings):
    info = await create_worktree(settings, repo / "pkg", "12345678-aaaa-bbbb-cccc-000000000000")
    path = Path(info["path"])
    assert path == settings.worktrees_dir / "project-12345678" and (path / "pkg" / "code.py").is_file()
    assert info["branch"] == "openbot/12345678" and info["base_ref"] == "main" and info["subdir"] == "pkg"
    assert info["base_commit"] == run_git(repo, "rev-parse", "HEAD")
    assert working_directory_of(info) == str(path / "pkg")
    assert run_git(path, "rev-parse", "--abbrev-ref", "HEAD") == "openbot/12345678"
    assert run_git(repo, "status", "--porcelain") == ""                    # the main checkout is untouched


async def test_refuses_what_is_not_a_repository_or_would_land_inside_it(tmp_path, repo, settings):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(WorktreeError, match="not inside a git repository"):
        await create_worktree(settings, plain, "abcdef12-0000")
    settings.worktrees_dir = repo / ".worktrees"
    with pytest.raises(WorktreeError, match="inside the repository"):
        await create_worktree(settings, repo, "abcdef12-0000")


async def test_status_tells_what_would_be_lost(tmp_path, repo, settings):
    info = await create_worktree(settings, repo, "aaaabbbb-0000")
    path = Path(info["path"])
    assert (await worktree_status(info))["clean"] is True
    (path / "README.md").write_text("changed\n")
    status = await worktree_status(info)
    assert status["uncommitted"] == 1 and status["unpushed"] == 0 and not status["clean"]
    run_git(path, "commit", "-q", "-am", "work")
    status = await worktree_status(info)
    assert status["uncommitted"] == 0 and status["unpushed"] == 1
    # Pushed to a remote, the same commit is safe again.
    remote = tmp_path / "remote.git"
    run_git(tmp_path, "init", "-q", "--bare", str(remote))
    run_git(repo, "remote", "add", "origin", str(remote))
    run_git(path, "push", "-q", "origin", "openbot/aaaabbbb")
    assert (await worktree_status(info))["clean"] is True


async def test_removal_never_loses_work_without_discard(repo, settings):
    info = await create_worktree(settings, repo, "ccccdddd-0000")
    path = Path(info["path"])
    commit_in(path, "feature.txt")
    with pytest.raises(WorktreeError, match="1 unpushed commit on openbot/ccccdddd"):
        await remove_worktree(info)
    assert path.is_dir() and run_git(repo, "branch", "--list", "openbot/ccccdddd")
    result = await remove_worktree(info, discard=True)
    assert result["removed"] and not path.exists() and not run_git(repo, "branch", "--list", "openbot/ccccdddd")


async def test_a_clean_worktree_goes_with_its_branch(repo, settings):
    info = await create_worktree(settings, repo, "eeeeffff-0000")
    result = await remove_worktree(info)
    assert result["removed"] and not result["branch_kept"] and not Path(info["path"]).exists()
    assert not run_git(repo, "branch", "--list", "openbot/eeeeffff")


# --- threads -------------------------------------------------------------------------------------------------

async def seed_bot(services):
    async with services.session_factory() as s:
        s.add(bot_actor("eng"))
        await s.commit()


async def test_a_thread_works_in_its_own_worktree(client, services, repo):
    await seed_bot(services)
    listing = (await client.get("/api/v1/workspace/directories", params={"path": str(repo)})).json()
    assert listing["in_git_repo"] is True
    r = await client.post("/api/v1/threads", json={"handles": ["eng"], "working_directory": str(repo / "pkg"),
                                                   "isolated_worktree": True})
    assert r.status_code == 201, r.text
    t = r.json()
    wt = t["worktree"]
    assert t["working_directory"] == str(Path(wt["path"]) / "pkg") and wt["branch"].startswith("openbot/")
    assert Path(wt["origin"]) == (repo / "pkg").resolve()


async def test_isolated_needs_a_repository(client, services, tmp_path, settings):
    await seed_bot(services)
    plain = tmp_path / "plain"
    plain.mkdir()
    assert (await client.get("/api/v1/workspace/directories", params={"path": str(plain)})).json()["in_git_repo"] is False
    r = await client.post("/api/v1/threads", json={"handles": ["eng"], "working_directory": str(plain), "isolated_worktree": True})
    assert r.status_code == 422 and "not inside a git repository" in r.text
    async with services.session_factory() as s:
        assert (await s.execute(select(Thread).where(Thread.title != ""))).scalars().all() == []


async def test_deleting_a_thread_takes_a_clean_worktree_along(client, services, repo):
    await seed_bot(services)
    t = (await client.post("/api/v1/threads", json={"handles": ["eng"], "working_directory": str(repo),
                                                    "isolated_worktree": True})).json()
    assert (await client.delete(f"/api/v1/threads/{t['id']}")).status_code == 204
    assert not Path(t["worktree"]["path"]).exists() and not run_git(repo, "branch", "--list", t["worktree"]["branch"])


async def test_deleting_a_thread_with_unpushed_work_needs_a_decision(client, services, repo):
    await seed_bot(services)
    t = (await client.post("/api/v1/threads", json={"handles": ["eng"], "working_directory": str(repo),
                                                    "isolated_worktree": True})).json()
    path, branch = Path(t["worktree"]["path"]), t["worktree"]["branch"]
    commit_in(path, "work.txt")
    r = await client.delete(f"/api/v1/threads/{t['id']}")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["worktree"]["unpushed"] == 1 and f"1 unpushed commit on {branch}" in detail["message"]
    assert (await client.get(f"/api/v1/threads/{t['id']}")).status_code == 200      # still there
    status = (await client.get(f"/api/v1/threads/{t['id']}/worktree")).json()
    assert status["unpushed"] == 1 and status["clean"] is False
    # Without discard the worktree endpoint refuses too; with it, the work is thrown away on purpose.
    assert (await client.delete(f"/api/v1/threads/{t['id']}/worktree")).status_code == 409
    r = await client.delete(f"/api/v1/threads/{t['id']}/worktree", params={"discard": "true"})
    assert r.status_code == 200 and not path.exists() and not run_git(repo, "branch", "--list", branch)
    after = (await client.get(f"/api/v1/threads/{t['id']}")).json()
    assert after["worktree"] is None and Path(after["working_directory"]) == repo.resolve()


async def test_a_thread_can_be_deleted_while_keeping_its_worktree(client, services, repo):
    await seed_bot(services)
    t = (await client.post("/api/v1/threads", json={"handles": ["eng"], "working_directory": str(repo),
                                                    "isolated_worktree": True})).json()
    commit_in(Path(t["worktree"]["path"]), "keep.txt")
    assert (await client.delete(f"/api/v1/threads/{t['id']}", params={"keep_worktree": "true"})).status_code == 204
    assert Path(t["worktree"]["path"]).is_dir() and run_git(repo, "branch", "--list", t["worktree"]["branch"])


async def test_purging_a_bot_leaves_worktrees_alone(client, services, repo):
    await seed_bot(services)
    t = (await client.post("/api/v1/threads", json={"handles": ["eng"], "working_directory": str(repo),
                                                    "isolated_worktree": True})).json()
    bot_id = next(p["actor_id"] for p in t["participants"] if p["handle"] == "eng")
    assert (await client.post(f"/api/v1/bots/{bot_id}/purge")).status_code == 200
    assert Path(t["worktree"]["path"]).is_dir()
    assert (await client.get(f"/api/v1/threads/{t['id']}")).json()["worktree"]["path"] == t["worktree"]["path"]


async def test_worktree_migration_repairs_a_database_stamped_with_the_old_0016(tmp_path):
    """The worktree migration was 0016 until upstream's model catalog took that number. A database
    migrated back then is stamped 0016 with the worktree column but no model_catalog table; upgrading
    must add the table and leave the column alone instead of failing on a duplicate."""
    url = f"sqlite+aiosqlite:///{tmp_path}/old.db"
    cfg = _alembic_config(url)
    await asyncio.to_thread(command.upgrade, cfg, "0015")
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.execute(sa.text("ALTER TABLE threads ADD COLUMN worktree JSON"))
    await engine.dispose()
    await asyncio.to_thread(command.stamp, cfg, "0016")

    await run_migrations(url)
    engine = make_engine(url)
    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda c: set(sa.inspect(c).get_table_names()))
        cols = await conn.run_sync(lambda c: [col["name"] for col in sa.inspect(c).get_columns("threads")])
        version = (await conn.execute(sa.text("SELECT version_num FROM alembic_version"))).scalar()
    await engine.dispose()
    assert "model_catalog" in tables
    assert cols.count("worktree") == 1
    assert version == "0017"
