"""Mode resolution precedence and the empty-content nudge (E2 coverage).

Precedence (highest first): explicit `mode` arg > legacy `agentic` bool >
config `synthesizer_tools.enabled`. SCHOLAR is only reachable explicitly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_council.config import AICouncilConfig, ModelConfig, Provider
from ai_council.models import ModelManager, ConsultantResult
from ai_council.synthesis import CouncilMode, ResponseSynthesizer, WorkspaceRootError


def _models(n=2):
    return [
        ModelConfig(name=c, model_id=c.lower(), provider=Provider.CUSTOM,
                    base_url="http://x/v1", api_key="k")
        for c in ["A", "B", "C"][:n]
    ]


def _synth(monkeypatch, tools_enabled: bool):
    cfg = AICouncilConfig(
        models=_models(),
        synthesizer_tools={"enabled": tools_enabled, "workspace_root": None},
    )
    mgr = ModelManager(config=cfg)
    calls = {}

    async def fake_scribe(models, ctx, q, **kw):
        calls["path"] = "scribe"
        return [ConsultantResult("ok", True) for _ in models]

    async def fake_agentic(models, ctx, q, **kw):
        calls["path"] = "agentic"
        calls["kw"] = kw
        return [ConsultantResult("ok", True) for _ in models]

    monkeypatch.setattr(mgr, "call_models_parallel", fake_scribe)
    monkeypatch.setattr(mgr, "call_models_parallel_agentic", fake_agentic)
    return ResponseSynthesizer(mgr), cfg, calls


def _run(synth, cfg, **kw):
    return asyncio.run(synth.collect_perspectives(
        "ctx", "q", cfg.models, **kw
    ))


# --- pure back-compat mapping ------------------------------------------------
def test_from_agentic_mapping():
    assert CouncilMode.from_agentic(False, True) == CouncilMode.SCRIBE
    assert CouncilMode.from_agentic(True, False) == CouncilMode.TRANSLATOR
    assert CouncilMode.from_agentic(None, True) == CouncilMode.TRANSLATOR
    assert CouncilMode.from_agentic(None, False) == CouncilMode.SCRIBE


# --- precedence through collect_perspectives ---------------------------------
def test_explicit_mode_wins_over_agentic(monkeypatch):
    synth, cfg, calls = _synth(monkeypatch, tools_enabled=False)
    persp = _run(synth, cfg, mode="scholar", agentic_override=False)
    assert calls["path"] == "agentic"          # scholar is agentic despite agentic=False
    assert persp[0]["mode"] == "scholar"


def test_scribe_mode_forces_no_tools(monkeypatch):
    synth, cfg, calls = _synth(monkeypatch, tools_enabled=True)
    persp = _run(synth, cfg, mode="scribe")
    assert calls["path"] == "scribe"
    assert persp[0]["mode"] == "scribe"


def test_agentic_bool_used_when_no_mode(monkeypatch):
    synth, cfg, calls = _synth(monkeypatch, tools_enabled=False)
    persp = _run(synth, cfg, agentic_override=True)
    assert calls["path"] == "agentic"
    assert persp[0]["mode"] == "translator"


def test_config_default_used_when_neither_passed(monkeypatch):
    synth, cfg, calls = _synth(monkeypatch, tools_enabled=True)
    persp = _run(synth, cfg)
    assert calls["path"] == "agentic"
    assert persp[0]["mode"] == "translator"


def test_unknown_mode_falls_back_to_config(monkeypatch):
    synth, cfg, calls = _synth(monkeypatch, tools_enabled=False)
    persp = _run(synth, cfg, mode="nonsense")
    assert calls["path"] == "scribe"          # fell through to config default (disabled)
    assert persp[0]["mode"] == "scribe"


def test_scholar_budget_passed_to_agentic(monkeypatch):
    synth, cfg, calls = _synth(monkeypatch, tools_enabled=True)
    _run(synth, cfg, mode="scholar")
    assert calls["kw"]["max_iterations"] == cfg.synthesizer_tools.scholar_max_tool_iterations


# --- workspace_root validation and honest degradation (v0.7.1) ---------------
def test_bad_workspace_root_fails_loudly(monkeypatch):
    """A nonexistent root used to raise inside ToolRegistry construction, get
    swallowed by the degradation fallback, and run without tools while still
    tagged translator. Now it fails the call before any consultant fires."""
    synth, cfg, calls = _synth(monkeypatch, tools_enabled=True)
    with pytest.raises(WorkspaceRootError):
        _run(synth, cfg, mode="translator",
             workspace_root_override="/definitely/not/a/real/dir")
    assert "path" not in calls


def test_cwd_fallback_to_home_is_refused(monkeypatch):
    """MCP clients launch servers from directories the user never chose; an
    implicit fallback to $HOME grants read access to everything under it."""
    synth, cfg, calls = _synth(monkeypatch, tools_enabled=True)
    monkeypatch.setattr("ai_council.synthesis.os.getcwd", lambda: str(Path.home()))
    with pytest.raises(WorkspaceRootError):
        _run(synth, cfg, mode="translator")
    assert "path" not in calls


def test_explicit_home_workspace_root_is_allowed(monkeypatch):
    """Only the IMPLICIT fallback is refused — naming $HOME is a deliberate choice."""
    synth, cfg, calls = _synth(monkeypatch, tools_enabled=True)
    _run(synth, cfg, mode="translator", workspace_root_override=str(Path.home()))
    assert calls["path"] == "agentic"


def test_scribe_never_validates_workspace_root(monkeypatch):
    """Scribe has no sandbox to root, so a hostile cwd must not fail it."""
    synth, cfg, calls = _synth(monkeypatch, tools_enabled=False)
    monkeypatch.setattr("ai_council.synthesis.os.getcwd", lambda: str(Path.home()))
    persp = _run(synth, cfg, mode="scribe")
    assert calls["path"] == "scribe"
    assert persp[0]["mode"] == "scribe"


def test_degraded_agentic_run_is_stamped_scribe(monkeypatch, tmp_path):
    """If agentic setup fails for an unexpected reason, the fallback runs
    without tools — the perspectives must be stamped with the mode that
    actually ran, not the one that was asked for."""
    synth, cfg, calls = _synth(monkeypatch, tools_enabled=True)

    def boom(allowed):
        raise RuntimeError("schema construction bug")

    monkeypatch.setattr("ai_council.synthesis.filter_schemas", boom)
    persp = _run(synth, cfg, mode="scholar", workspace_root_override=str(tmp_path))
    assert calls["path"] == "scribe"
    assert persp[0]["mode"] == "scribe"


def test_consult_maps_workspace_error_to_invalid_input():
    """End-to-end through _process_ai_council: a bad root is the CALLER's
    error (INVALID_INPUT), not an INTERNAL_ERROR."""
    from ai_council.main import AICouncilServer

    cfg = AICouncilConfig(
        models=_models(),
        synthesizer_tools={"enabled": True, "workspace_root": None},
    )
    server = AICouncilServer(config=cfg)
    result = asyncio.run(server._process_ai_council({
        "question": "q",
        "mode": "translator",
        "workspace_root": "/definitely/not/a/real/dir",
    }))
    assert result.status == "error"
    assert result.error.code == "INVALID_INPUT"
    assert "workspace_root" in result.error.details


# --- empty-content nudge in the scribe call path -----------------------------
class _Msg(SimpleNamespace):
    pass


class _NudgeCompletions:
    """First reply empty, second reply has content (Ollama thinking pattern)."""

    def __init__(self, replies):
        self.replies = replies
        self.i = 0

    async def create(self, **kwargs):
        msg = self.replies[min(self.i, len(self.replies) - 1)]
        self.i += 1
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _mgr_with(monkeypatch, replies):
    cfg = AICouncilConfig(models=_models())
    mgr = ModelManager(config=cfg)
    client = SimpleNamespace(chat=SimpleNamespace(completions=_NudgeCompletions(replies)))
    monkeypatch.setattr(mgr, "_get_client_for_model", lambda mc: client)
    return mgr, cfg


def test_empty_then_content_nudged_ok(monkeypatch):
    mgr, cfg = _mgr_with(monkeypatch, [
        _Msg(content=""),                 # empty first
        _Msg(content="the real answer"),  # nudge produces content
    ])
    res = asyncio.run(mgr.call_model(cfg.models[0], "ctx", "question"))
    assert res.ok
    assert "real answer" in res.text


def test_empty_twice_fails(monkeypatch):
    mgr, cfg = _mgr_with(monkeypatch, [_Msg(content=""), _Msg(content="")])
    res = asyncio.run(mgr.call_model(cfg.models[0], "ctx", "question"))
    assert not res.ok
