"""One git worktree per thread, so several threads (and their bots) can work in the same repository at once.

Opt-in when a thread is created, and only when its working directory is inside a git repository:

- The worktree lives outside the repository, in a per-user directory (WORKTREES_DIR; by default
  %LOCALAPPDATA%\\OpenBot\\wt on Windows, $XDG_DATA_HOME/openbot/wt or ~/.local/share/openbot/wt elsewhere),
  named <repo, at most 20 characters>-<first 8 characters of the thread id>: short, for Windows path limits.
- On a new branch openbot/<thread id, 8 characters>, from the commit the repository has checked out.
- The thread's working directory becomes the worktree (plus the subdirectory the thread was pointed at), so
  run_shell, the file tools and claude-code bots all work there, and a CLI session resumes where it was made.
- Deleting the thread removes the worktree and its branch only when nothing would be lost: no uncommitted
  changes and no commit that exists nowhere else (not in the base commit, another branch or a remote). Anything
  else needs the explicit discard, which is the only path that uses --force or branch -D.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from pathlib import Path

from openbot.runtime.appdirs import openbot_data_dir

log = logging.getLogger(__name__)

BRANCH_PREFIX = "openbot/"
GIT_TIMEOUT = 60.0
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class WorktreeError(Exception):
    """A worktree could not be created or removed, for a reason the user can act on."""


def default_worktrees_dir(**kwargs) -> Path:
    return openbot_data_dir(**kwargs) / "wt"


def worktrees_dir(settings) -> Path:
    return Path(settings.worktrees_dir).expanduser() if settings.worktrees_dir else default_worktrees_dir()


async def git(*args: str, cwd: Path | str) -> tuple[int, str, str]:
    exe = shutil.which("git")
    if exe is None:
        raise WorktreeError("git was not found on PATH; an isolated worktree needs git")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
    proc = await asyncio.create_subprocess_exec(exe, *args, cwd=str(cwd), env=env, stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), GIT_TIMEOUT)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise WorktreeError(f"git {args[0]} did not finish within {int(GIT_TIMEOUT)}s") from None
    return proc.returncode, out.decode("utf-8", "replace").strip(), err.decode("utf-8", "replace").strip()


async def repo_root(directory: Path) -> Path | None:
    """The top of the git repository containing `directory` (its main working tree), or None."""
    if not directory.is_dir():
        return None
    try:
        code, out, _ = await git("rev-parse", "--show-toplevel", cwd=directory)
    except WorktreeError:
        return None
    return Path(out).resolve() if code == 0 and out else None


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


async def create_worktree(settings, directory: Path, thread_id: str) -> dict:
    """Make the thread's worktree and return what the thread stores about it (Thread.worktree)."""
    directory = directory.resolve()
    root = await repo_root(directory)
    if root is None:
        raise WorktreeError(f"{directory} is not inside a git repository, so it cannot get an isolated worktree")
    short = thread_id.replace("-", "")[:8]
    name = f"{_UNSAFE.sub('-', root.name)[:20].strip('-') or 'repo'}-{short}"
    base_dir = worktrees_dir(settings)
    path = base_dir / name
    if _inside(path, root):
        raise WorktreeError(f"the worktrees directory {base_dir} is inside the repository {root}; set WORKTREES_DIR elsewhere")
    if path.exists():
        raise WorktreeError(f"{path} already exists")
    code, head, err = await git("rev-parse", "--verify", "HEAD", cwd=root)
    if code != 0:
        raise WorktreeError(f"the repository has no commit to branch from yet: {err}")
    code, ref, _ = await git("symbolic-ref", "--short", "-q", "HEAD", cwd=root)
    base_ref = ref if code == 0 and ref else None                          # None: detached HEAD
    branch = f"{BRANCH_PREFIX}{short}"
    base_dir.mkdir(parents=True, exist_ok=True)
    code, _, err = await git("-c", "core.longpaths=true", "worktree", "add", "-b", branch, str(path), head, cwd=root)
    if code != 0:
        # Leave nothing half made: the branch may exist even when the checkout failed.
        await git("worktree", "prune", cwd=root)
        await git("branch", "-d", branch, cwd=root)
        raise WorktreeError(f"git worktree add failed: {err}")
    subdir = directory.relative_to(root).as_posix() if directory != root else ""
    log.info("thread %s: worktree %s on %s from %s (%s)", thread_id, path, branch, base_ref or "detached HEAD", head[:12])
    return {"repo": str(root), "path": str(path), "branch": branch, "base_ref": base_ref, "base_commit": head,
            "subdir": subdir}


def working_directory_of(info: dict) -> str:
    """The thread's working directory inside its worktree: the worktree, or the subdirectory it was made for."""
    path = Path(info["path"])
    return str(path / info["subdir"]) if info.get("subdir") else str(path)


async def worktree_status(info: dict) -> dict:
    """What deleting the worktree would lose: uncommitted changes, and commits that exist nowhere else."""
    path, root, branch = Path(info["path"]), Path(info["repo"]), info["branch"]
    exists = path.is_dir()
    dirty: list[str] = []
    if exists:
        code, out, _ = await git("status", "--porcelain", cwd=path)
        dirty = [line for line in out.splitlines() if line.strip()] if code == 0 else ["(git status failed)"]
    unpushed: list[str] = []
    code, _, _ = await git("rev-parse", "--verify", "-q", f"refs/heads/{branch}", cwd=root)
    branch_exists = code == 0
    if branch_exists:
        # Commits on the branch not reachable from the base commit, any other local branch, or any remote.
        # (--exclude before --branches takes the short name: a refs/heads/ pattern silently matches nothing.)
        code, out, err = await git("rev-list", "--oneline", branch, "--not", info["base_commit"],
                                   f"--exclude={branch}", "--branches", "--remotes", cwd=root)
        unpushed = out.splitlines() if code == 0 else [f"(git rev-list failed: {err})"]
    return {"path": str(path), "branch": branch, "exists": exists, "branch_exists": branch_exists,
            "uncommitted": len(dirty), "unpushed": len(unpushed), "unpushed_commits": unpushed[:20],
            "clean": not dirty and not unpushed}


def loss_summary(status: dict) -> str:
    parts = []
    if status["unpushed"]:
        n = status["unpushed"]
        parts.append(f"{n} unpushed commit{'s' if n != 1 else ''} on {status['branch']}")
    if status["uncommitted"]:
        parts.append(f"uncommitted changes in {status['uncommitted']} file{'s' if status['uncommitted'] != 1 else ''}")
    return " and ".join(parts)


async def remove_worktree(info: dict, *, discard: bool = False) -> dict:
    """Remove the worktree and its branch. Without `discard`, only when nothing would be lost (else it raises
    and leaves both in place); with it, whatever is there -- the one path that forces anything."""
    status = await worktree_status(info)
    if not discard and not status["clean"]:
        raise WorktreeError(f"the worktree at {status['path']} has {loss_summary(status)}; push or discard it first")
    root, path, branch = Path(info["repo"]), Path(info["path"]), info["branch"]
    if status["exists"]:
        args = ["worktree", "remove", *(["--force", "--force"] if discard else []), str(path)]
        code, _, err = await git(*args, cwd=root)
        if code != 0:
            raise WorktreeError(f"git worktree remove failed: {err}")
    await git("worktree", "prune", cwd=root)
    if status["branch_exists"]:
        code, _, err = await git("branch", "-D" if discard else "-d", branch, cwd=root)
        if code != 0:
            # -d refuses a branch that is not merged into HEAD or its upstream even when its commits are
            # safe elsewhere; keep it rather than force, and say so.
            log.warning("worktree %s removed but branch %s kept: %s", path, branch, err)
            return {**status, "removed": True, "branch_kept": True}
    return {**status, "removed": True, "branch_kept": False}
