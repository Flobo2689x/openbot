"""A project's CLAUDE.md, read by OpenBot itself for a claude-code bot (#196).

A claude-code bot runs with `--setting-sources user` unless it trusts the directory, which keeps the
project's hooks and .mcp.json out -- but also its CLAUDE.md. This reads a safe subset of what Claude Code
itself would load, so the bot still knows the project's conventions, and hands it over as part of the
system prompt:

- CLAUDE.md and .claude/CLAUDE.md in the working directory and each directory above it, up to the root of
  the git repository containing it (just the working directory when there is none), root first.
- AGENTS.md / .claude/AGENTS.md instead, only when none of those exist (Claude Code's own default).
- `@path` imports outside code spans and fences, relative to the importing file, up to four hops.
- Never: CLAUDE.local.md (personal, not the project's), .claude/rules, and anything whose real path --
  symlinks resolved -- lies outside that root. Every file and the whole section are size-capped.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

INSTRUCTION_FILES = ("CLAUDE.md", ".claude/CLAUDE.md")
AGENTS_FILES = ("AGENTS.md", ".claude/AGENTS.md")
MAX_IMPORT_DEPTH = 4
MAX_FILE_CHARS = 20_000
MAX_TOTAL_CHARS = 40_000
HEADER = "# Project instructions"
_FENCE = re.compile(r"^\s*(```|~~~)")
_CODE_SPAN = re.compile(r"`[^`]*`")
_IMPORT = re.compile(r"(?<![\w@`])@([^\s`<>()\[\]{}\"',;]+)")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def _repo_root(start: Path) -> Path:
    """The nearest ancestor (or start itself) holding .git -- a directory, or a file in a git worktree."""
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return d
    return start


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _read(path: Path, root: Path) -> str | None:
    """The file's text if it is a regular file whose real path lies inside root, else None."""
    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not real.is_file() or not _inside(real, root):
        if real.is_file():
            log.info("project instructions: skipping %s, it resolves outside %s", path, root)
        return None
    try:
        text = real.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if len(text) > MAX_FILE_CHARS:
        text = text[:MAX_FILE_CHARS] + f"\n[... truncated, {len(text) - MAX_FILE_CHARS} more characters]"
    return text


def _imports(text: str) -> list[str]:
    """`@path` references outside fenced code blocks and inline code spans, in order, deduplicated."""
    found: list[str] = []
    fenced = False
    for line in text.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        for m in _IMPORT.finditer(_CODE_SPAN.sub("", line)):
            ref = m.group(1).rstrip(".:!?")
            if ref and ref not in found:
                found.append(ref)
    return found


def _expand(path: Path, root: Path, seen: set[Path], depth: int, out: list[tuple[Path, str]]) -> None:
    text = _read(path, root)
    if text is None:
        return
    real = path.resolve()
    if real in seen:
        return
    seen.add(real)
    out.append((real, _COMMENT.sub("", text).strip()))
    if depth >= MAX_IMPORT_DEPTH:
        return
    for ref in _imports(text):
        target = Path(os.path.expanduser(ref)) if ref.startswith("~") else Path(ref)
        if not target.is_absolute():
            target = real.parent / target
        # _read rejects anything outside root, so a `@~/...` or `@../../elsewhere` import is simply dropped.
        _expand(target, root, seen, depth + 1, out)


def load_project_instructions(cwd: Path) -> str:
    """The section to append to a claude-code bot's system prompt, or "" when the project has none."""
    try:
        cwd = cwd.resolve(strict=True)
    except (OSError, RuntimeError):
        return ""
    root = _repo_root(cwd)
    dirs = [d for d in (cwd, *cwd.parents) if _inside(d, root)]
    dirs.reverse()                                           # root first, the working directory last
    candidates = [d / name for d in dirs for name in INSTRUCTION_FILES]
    if not any(p.is_file() for p in candidates):
        candidates = [d / name for d in dirs for name in AGENTS_FILES]
    files: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for p in candidates:
        if p.is_file():
            _expand(p, root, seen, 0, files)
    parts, total = [], 0
    for real, text in files:
        if not text:
            continue
        rel = real.relative_to(root).as_posix()
        block = f"## {rel}\n\n{text}"
        if total + len(block) > MAX_TOTAL_CHARS:
            parts.append(f"## {rel}\n\n[omitted: the project instructions exceed {MAX_TOTAL_CHARS} characters]")
            break
        parts.append(block)
        total += len(block)
    if not parts:
        return ""
    return (f"{HEADER}\n\nThe project in your working directory keeps these instructions for anyone working in it "
            f"(read by OpenBot from its CLAUDE.md files; follow them unless your own instructions above say "
            f"otherwise).\n\n" + "\n\n".join(parts))
