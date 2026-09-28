"""OpenBot's own, safe reading of a project's CLAUDE.md for claude-code bots (runtime/project_instructions.py)."""
import os

import pytest

from openbot.runtime import project_instructions as pi
from openbot.runtime.project_instructions import HEADER, load_project_instructions


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def test_nothing_to_load_gives_an_empty_section(tmp_path):
    assert load_project_instructions(tmp_path) == ""
    assert load_project_instructions(tmp_path / "missing") == ""


def test_walks_from_the_repo_root_down_to_the_working_directory(tmp_path):
    write(tmp_path / "CLAUDE.md", "ABOVE THE REPO")                     # outside the repository: never read
    root = repo(tmp_path)
    write(root / "CLAUDE.md", "root rules")
    write(root / ".claude" / "CLAUDE.md", "dot-claude rules")
    write(root / "pkg" / "CLAUDE.md", "package rules")
    write(root / "pkg" / "CLAUDE.local.md", "PERSONAL")                  # someone's own notes: never read
    write(root / "pkg" / "deeper" / "CLAUDE.md", "BELOW THE CWD")        # Claude Code loads these lazily; we don't
    out = load_project_instructions(root / "pkg")
    assert out.startswith(HEADER)
    assert out.index("## CLAUDE.md") < out.index("## .claude/CLAUDE.md") < out.index("## pkg/CLAUDE.md")
    assert "root rules" in out and "dot-claude rules" in out and "package rules" in out
    for absent in ("ABOVE THE REPO", "PERSONAL", "BELOW THE CWD"):
        assert absent not in out


def test_without_a_repository_only_the_working_directory_counts(tmp_path):
    write(tmp_path / "CLAUDE.md", "parent")
    write(tmp_path / "work" / "CLAUDE.md", "here")
    out = load_project_instructions(tmp_path / "work")
    assert "here" in out and "parent" not in out


def test_agents_md_is_the_fallback_only_when_there_is_no_claude_md(tmp_path):
    root = repo(tmp_path)
    write(root / "AGENTS.md", "agents rules")
    assert "agents rules" in load_project_instructions(root)
    write(root / "CLAUDE.md", "claude rules")
    out = load_project_instructions(root)
    assert "claude rules" in out and "agents rules" not in out


def test_imports_resolve_relative_to_the_importing_file_and_stay_inside_the_repo(tmp_path):
    write(tmp_path / "secret.md", "OUTSIDE SECRET")
    root = repo(tmp_path)
    write(root / "docs" / "style.md", "style guide")
    write(root / "CLAUDE.md", "\n".join([
        "See @docs/style.md for style.",
        "Not an import: `@docs/never.md` and user@example.com.",
        "```", "@docs/fenced.md", "```",
        "Escape attempt: @../secret.md and @" + str(tmp_path / "secret.md"),
    ]))
    write(root / "docs" / "never.md", "IN A CODE SPAN")
    write(root / "docs" / "fenced.md", "IN A FENCE")
    out = load_project_instructions(root)
    assert "## docs/style.md\n\nstyle guide" in out
    for absent in ("OUTSIDE SECRET", "IN A CODE SPAN", "IN A FENCE"):
        assert absent not in out


def test_imports_stop_after_four_hops_and_survive_cycles(tmp_path):
    root = repo(tmp_path)
    write(root / "CLAUDE.md", "start @a.md")
    for name, nxt in (("a", "b"), ("b", "c"), ("c", "d"), ("d", "e"), ("e", "a")):
        write(root / f"{name}.md", f"file {name} @{nxt}.md")
    out = load_project_instructions(root)
    assert all(f"file {n}" in out for n in "abcd") and "file e" not in out


def test_html_comments_are_stripped_and_sizes_are_capped(tmp_path, monkeypatch):
    root = repo(tmp_path)
    write(root / "CLAUDE.md", "keep <!-- maintainer note --> this\n" + "x" * 300)
    monkeypatch.setattr(pi, "MAX_FILE_CHARS", 100)
    out = load_project_instructions(root)
    assert "maintainer note" not in out and "keep  this" in out and "[... truncated," in out
    monkeypatch.setattr(pi, "MAX_FILE_CHARS", 20_000)
    monkeypatch.setattr(pi, "MAX_TOTAL_CHARS", 50)
    write(root / ".claude" / "CLAUDE.md", "second file")
    out = load_project_instructions(root)
    assert "[omitted: the project instructions exceed 50 characters]" in out and "second file" not in out


def test_a_symlink_out_of_the_repository_is_not_followed(tmp_path):
    write(tmp_path / "outside.md", "LINKED SECRET")
    root = repo(tmp_path)
    try:
        os.symlink(tmp_path / "outside.md", root / "CLAUDE.md")
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks needs extra privileges here")
    assert "LINKED SECRET" not in load_project_instructions(root)
