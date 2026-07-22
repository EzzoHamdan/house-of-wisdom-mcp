"""Tests for the read-only synthesizer tools and sandbox enforcement."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ai_council.tools import ToolRegistry, filter_schemas, TOOL_SCHEMAS


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sample_repo"


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry(
        workspace_root=str(FIXTURE_ROOT),
        allowed_tools=["read_file", "list_dir", "glob_search", "think"],
    )


def test_read_file_inside_sandbox(registry: ToolRegistry):
    out = registry.read_file("src/math.py")
    assert "def add" in out
    assert "def divide" in out


def test_read_file_missing(registry: ToolRegistry):
    out = registry.read_file("src/nope.py")
    assert "not found" in out.lower()


def test_list_dir_lists_entries(registry: ToolRegistry):
    out = registry.list_dir(".")
    assert "README.md" in out
    assert "src/" in out


def test_glob_search_finds_python_files(registry: ToolRegistry):
    out = registry.glob_search("**/*.py")
    assert "src/math.py" in out


def test_think_echoes(registry: ToolRegistry):
    out = registry.think("step 1: compare answers")
    assert "step 1" in out


def test_sandbox_rejects_outside_path(registry: ToolRegistry):
    from ai_council.tools import SandboxViolation

    with pytest.raises(SandboxViolation):
        registry.read_file("../../../../etc/passwd")


def test_sandbox_rejects_absolute_outside_path(registry: ToolRegistry):
    from ai_council.tools import SandboxViolation

    with pytest.raises(SandboxViolation):
        registry.read_file("/etc/passwd")


def test_call_dispatches_by_name(registry: ToolRegistry):
    out = registry.call("read_file", {"path": "README.md"})
    assert "Sample Repo" in out


def test_call_rejects_disallowed_tool():
    reg = ToolRegistry(
        workspace_root=str(FIXTURE_ROOT),
        allowed_tools=["think"],  # read_file not allowed
    )
    out = reg.call("read_file", {"path": "README.md"})
    assert "not in allowed_tools" in out


def test_call_rejects_unknown_tool():
    # allowed_tools=None => no allowlist gating, so the dispatch path is reached
    reg = ToolRegistry(workspace_root=str(FIXTURE_ROOT), allowed_tools=None)
    out = reg.call("bogus_tool", {})
    assert "unknown tool" in out.lower()


def test_call_empty_allowlist_permits_nothing():
    # [] is an empty allowlist: every tool is gated out (not "all allowed").
    reg = ToolRegistry(workspace_root=str(FIXTURE_ROOT), allowed_tools=[])
    out = reg.call("read_file", {"path": "README.md"})
    assert "not in allowed_tools" in out


def test_call_none_allowlist_permits_all():
    # None => no allowlist => the tool dispatches normally.
    reg = ToolRegistry(workspace_root=str(FIXTURE_ROOT), allowed_tools=None)
    out = reg.call("read_file", {"path": "README.md"})
    assert "Sample Repo" in out


def test_filter_schemas_subset():
    filtered = filter_schemas(["read_file", "think"])
    names = {s["function"]["name"] for s in filtered}
    assert names == {"read_file", "think"}


def test_filter_schemas_none_returns_all():
    # None => no allowlist => every schema.
    assert len(filter_schemas(None)) == len(TOOL_SCHEMAS)


def test_filter_schemas_empty_returns_none():
    # [] => empty allowlist => no schema (previously wrongly returned all).
    assert filter_schemas([]) == []


def test_workspace_root_must_exist():
    with pytest.raises(ValueError):
        ToolRegistry(workspace_root="/nonexistent/path/xyz")


def test_relative_path_resolved_against_root(registry: ToolRegistry):
    # No leading slash — should resolve inside sandbox
    out = registry.read_file("src/math.py")
    assert "def add" in out


def test_glob_search_does_not_escape_sandbox(registry: ToolRegistry):
    """glob_search must not enumerate paths outside workspace_root (B1).

    fixtures/sample_repo has a parent (fixtures/) with sibling entries; a
    '../*' pattern would leak their names before the boundary check was added.
    """
    out = registry.glob_search("../*")
    # No returned line may point above the root. At most the root itself (".")
    # survives — a sibling of sample_repo must never appear.
    assert ".." not in out
    for line in out.splitlines():
        assert line in (".", "") or not line.startswith(".."), f"leaked: {line}"


def test_glob_search_parent_recursive_blocked(registry: ToolRegistry):
    out = registry.glob_search("../**/*.py")
    # Any match must be inside the root; escaping matches are dropped.
    for line in out.splitlines():
        assert not line.startswith(".."), f"leaked outside path: {line}"


def test_read_file_max_bytes_not_overridable(registry: ToolRegistry):
    """The read cap is fixed; a model cannot raise it via tool args (B4)."""
    out = registry.call("read_file", {"path": "src/math.py", "max_bytes": 10_000_000})
    assert "bad arguments" in out.lower()


def test_read_file_truncation_marker_bytes(tmp_path: Path):
    """Truncation is decided by byte length, not decoded char count (B3)."""
    reg = ToolRegistry(workspace_root=str(tmp_path), allowed_tools=None)
    cap = ToolRegistry.MAX_READ_BYTES

    # Exactly at the cap, ASCII, NOT truncated -> no marker (was a false marker).
    (tmp_path / "exact.txt").write_bytes(b"a" * cap)
    assert "[truncated" not in reg.read_file("exact.txt")

    # Over the cap in bytes but fewer chars (2-byte UTF-8) -> marker present
    # (was silently truncated with no warning).
    (tmp_path / "multi.txt").write_bytes(("é" * cap).encode("utf-8"))  # 2*cap bytes
    out = reg.read_file("multi.txt")
    assert "[truncated" in out


def test_symlink_escape_blocked(tmp_path: Path, registry: ToolRegistry):
    """A symlink inside the sandbox pointing outside must be rejected."""
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    link = FIXTURE_ROOT / "escape.txt"
    try:
        if link.exists() or link.is_symlink():
            link.unlink()
        os.symlink(outside, link)
        from ai_council.tools import SandboxViolation

        with pytest.raises(SandboxViolation):
            registry.read_file("escape.txt")
    finally:
        if link.exists() or link.is_symlink():
            link.unlink()

# --- content_search (v0.9.0) --------------------------------------------------

def test_content_search_finds_lines_with_locations(registry: ToolRegistry):
    out = registry.content_search("def add")
    assert "src/math.py:" in out
    assert "def add" in out


def test_content_search_glob_filter(tmp_path: Path):
    (tmp_path / "a.py").write_text("needle in python\n")
    (tmp_path / "b.txt").write_text("needle in text\n")
    reg = ToolRegistry(workspace_root=str(tmp_path), allowed_tools=None)
    out = reg.content_search("needle", glob="**/*.py")
    assert "a.py:1:" in out
    assert "b.txt" not in out


def test_content_search_no_match(registry: ToolRegistry):
    out = registry.content_search("zzz_never_in_fixture_zzz")
    assert "No matches" in out


def test_content_search_invalid_regex(registry: ToolRegistry):
    out = registry.content_search("def (")
    assert "invalid regex" in out


def test_content_search_empty_pattern(registry: ToolRegistry):
    assert "Error" in registry.content_search("")


def test_content_search_match_cap_with_marker(tmp_path: Path):
    lines = "\n".join(f"needle line {i}" for i in range(150))
    (tmp_path / "big.txt").write_text(lines)
    reg = ToolRegistry(workspace_root=str(tmp_path), allowed_tools=None)
    out = reg.content_search("needle")
    assert out.count("big.txt:") == ToolRegistry.MAX_GREP_MATCHES
    assert "stopped at" in out


def test_content_search_skips_binary(tmp_path: Path):
    (tmp_path / "blob.bin").write_bytes(b"needle\x00needle")
    (tmp_path / "plain.txt").write_text("needle\n")
    reg = ToolRegistry(workspace_root=str(tmp_path), allowed_tools=None)
    out = reg.content_search("needle")
    assert "plain.txt:1:" in out
    assert "blob.bin" not in out


def test_content_search_skips_hidden_dirs_not_hidden_files(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("needle in vcs internals\n")
    (tmp_path / ".env.example").write_text("needle in a dotfile\n")
    reg = ToolRegistry(workspace_root=str(tmp_path), allowed_tools=None)
    out = reg.content_search("needle")
    assert ".env.example:1:" in out
    assert ".git/" not in out


def test_content_search_symlink_escape_excluded(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("needle secret\n")
    root = tmp_path / "root"
    root.mkdir()
    (root / "ok.txt").write_text("needle fine\n")
    os.symlink(outside / "secret.txt", root / "leak.txt")
    reg = ToolRegistry(workspace_root=str(root), allowed_tools=None)
    out = reg.content_search("needle")
    assert "ok.txt:1:" in out
    assert "leak" not in out
    assert "secret" not in out


def test_content_search_clips_long_lines(tmp_path: Path):
    (tmp_path / "long.txt").write_text("needle " + "x" * 1000 + "\n")
    reg = ToolRegistry(workspace_root=str(tmp_path), allowed_tools=None)
    out = reg.content_search("needle")
    line = out.splitlines()[0]
    assert len(line) < 400
    assert line.endswith("…")


def test_content_search_via_dispatch_and_default_config(tmp_path: Path):
    from ai_council.config import SynthesizerToolsConfig

    assert "content_search" in SynthesizerToolsConfig().allowed_tools
    assert any(
        s["function"]["name"] == "content_search" for s in TOOL_SCHEMAS
    )
    (tmp_path / "f.txt").write_text("needle\n")
    reg = ToolRegistry(workspace_root=str(tmp_path), allowed_tools=None)
    out = reg.call("content_search", {"pattern": "needle"})
    assert "f.txt:1:" in out
    assert reg.call_counts["content_search"] == 1
