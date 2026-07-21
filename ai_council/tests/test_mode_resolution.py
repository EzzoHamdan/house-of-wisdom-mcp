"""Mode resolution precedence and the empty-content nudge (E2 coverage).

Precedence (highest first): explicit `mode` arg > legacy `agentic` bool >
config `synthesizer_tools.enabled`. SCHOLAR is only reachable explicitly.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ai_council.config import AICouncilConfig, ModelConfig, Provider
from ai_council.models import ModelManager, ConsultantResult
from ai_council.synthesis import CouncilMode, ResponseSynthesizer


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
