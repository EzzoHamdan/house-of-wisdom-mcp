"""Per-call `tool_budget` / `timeout` overrides (v0.8.0).

The caller of `consult` is an agent that cannot edit config.yaml, so these are
its only depth dials beyond the three mode presets. Both are CLAMPED to the
operator's configured ceilings — a caller can dial down, never up. Bad values
are the caller's error (INVALID_INPUT), never a crash.
"""

from __future__ import annotations

import asyncio

import pytest

from ai_council.config import AICouncilConfig, ModelConfig, Provider
from ai_council.models import ModelManager, ConsultantResult
from ai_council.synthesis import ResponseSynthesizer


def _models(n=2):
    return [
        ModelConfig(name=c, model_id=c.lower(), provider=Provider.CUSTOM,
                    base_url="http://x/v1", api_key="k")
        for c in ["A", "B", "C"][:n]
    ]


def _synth(monkeypatch, **config_kw):
    cfg = AICouncilConfig(models=_models(), **config_kw)
    mgr = ModelManager(config=cfg)
    calls = {}

    async def fake_scribe(models, ctx, q, **kw):
        calls["path"] = "scribe"
        calls["kw"] = kw
        return [ConsultantResult("ok", True) for _ in models]

    async def fake_agentic(models, ctx, q, **kw):
        calls["path"] = "agentic"
        calls["kw"] = kw
        return [ConsultantResult("ok", True) for _ in models]

    monkeypatch.setattr(mgr, "call_models_parallel", fake_scribe)
    monkeypatch.setattr(mgr, "call_models_parallel_agentic", fake_agentic)
    return ResponseSynthesizer(mgr), cfg, calls


def _run(synth, cfg, **kw):
    return asyncio.run(synth.collect_perspectives("ctx", "q", cfg.models, **kw))


# --- tool_budget clamping ------------------------------------------------------
def test_budget_below_mode_ceiling_is_honored(monkeypatch):
    synth, cfg, calls = _synth(
        monkeypatch,
        synthesizer_tools={"enabled": True, "scholar_max_tool_iterations": 64},
    )
    _run(synth, cfg, mode="scholar", tool_budget=20)
    assert calls["kw"]["max_iterations"] == 20


def test_budget_above_mode_ceiling_is_clamped(monkeypatch):
    synth, cfg, calls = _synth(
        monkeypatch,
        synthesizer_tools={"enabled": True, "scholar_max_tool_iterations": 64},
    )
    _run(synth, cfg, mode="scholar", tool_budget=500)
    assert calls["kw"]["max_iterations"] == 64


def test_budget_ceiling_follows_the_resolved_mode(monkeypatch):
    # Translator's ceiling is max_tool_iterations (8 built-in), so the same
    # tool_budget=20 that scholar honors gets clamped here.
    synth, cfg, calls = _synth(
        monkeypatch,
        synthesizer_tools={"enabled": True, "max_tool_iterations": 8},
    )
    _run(synth, cfg, mode="translator", tool_budget=20)
    assert calls["kw"]["max_iterations"] == 8


def test_no_budget_override_keeps_config_value(monkeypatch):
    synth, cfg, calls = _synth(
        monkeypatch,
        synthesizer_tools={"enabled": True, "max_tool_iterations": 12},
    )
    _run(synth, cfg, mode="translator")
    assert calls["kw"]["max_iterations"] == 12


# --- timeout clamping ----------------------------------------------------------
def test_timeout_below_config_is_honored_on_both_paths(monkeypatch):
    synth, cfg, calls = _synth(
        monkeypatch, parallel_timeout=60,
        synthesizer_tools={"enabled": True},
    )
    _run(synth, cfg, mode="translator", timeout=10)
    assert calls["kw"]["timeout"] == 10
    _run(synth, cfg, mode="scribe", timeout=10)
    assert calls["path"] == "scribe"
    assert calls["kw"]["timeout"] == 10


def test_timeout_above_config_is_clamped(monkeypatch):
    synth, cfg, calls = _synth(
        monkeypatch, parallel_timeout=60,
        synthesizer_tools={"enabled": True},
    )
    _run(synth, cfg, mode="translator", timeout=600)
    assert calls["kw"]["timeout"] == 60


def test_no_timeout_override_passes_none(monkeypatch):
    """None means 'use config' downstream — the config value must be applied
    at dispatch time, not frozen in at collection time."""
    synth, cfg, calls = _synth(monkeypatch, synthesizer_tools={"enabled": True})
    _run(synth, cfg, mode="translator")
    assert calls["kw"]["timeout"] is None


def test_agentic_dispatch_forwards_timeout_per_consultant(monkeypatch):
    """The override must reach each consultant's own tool loop, not just the
    batch deadline — parallel_timeout has always applied to both."""
    cfg = AICouncilConfig(models=_models(), parallel_timeout=60)
    mgr = ModelManager(config=cfg)
    seen = {}

    async def fake_loop(model_config, **kw):
        seen[model_config.name] = kw.get("timeout")
        return ConsultantResult("ok", True)

    monkeypatch.setattr(mgr, "call_model_with_tools", fake_loop)
    asyncio.run(mgr.call_models_parallel_agentic(
        cfg.models, "ctx", "q",
        tool_schemas=[], tool_registries=[object(), object()],
        timeout=15,
    ))
    assert seen == {"A": 15, "B": 15}


# --- argument validation at the MCP boundary ------------------------------------
def _server(monkeypatch):
    from ai_council.main import AICouncilServer

    cfg = AICouncilConfig(models=_models())
    server = AICouncilServer(config=cfg)
    captured = {}

    async def fake_collect(context, question, models, **kw):
        captured.update(kw)
        return [{
            "label": m.name, "model_name": m.name, "code_name": m.code_name,
            "analysis": "ok", "status": "ok", "mode": "scribe",
        } for m in models]

    monkeypatch.setattr(server.synthesizer, "collect_perspectives", fake_collect)
    return server, captured


@pytest.mark.parametrize("bad_args", [
    {"tool_budget": 0},
    {"tool_budget": -3},
    {"tool_budget": "abc"},
    {"tool_budget": 2.5},
    {"tool_budget": True},
    {"timeout": 4},
    {"timeout": "soon"},
])
def test_bad_override_is_invalid_input(monkeypatch, bad_args):
    server, _ = _server(monkeypatch)
    result = asyncio.run(server._process_ai_council({"question": "q", **bad_args}))
    assert result.status == "error"
    assert result.error.code == "INVALID_INPUT"


def test_valid_overrides_reach_the_synthesizer(monkeypatch):
    server, captured = _server(monkeypatch)
    result = asyncio.run(server._process_ai_council({
        "question": "q", "tool_budget": "20", "timeout": 30,
    }))
    assert result.status == "success"
    assert captured["tool_budget"] == 20  # digit strings tolerated, like `agentic`
    assert captured["timeout"] == 30


def test_omitted_overrides_are_none(monkeypatch):
    server, captured = _server(monkeypatch)
    result = asyncio.run(server._process_ai_council({"question": "q"}))
    assert result.status == "success"
    assert captured["tool_budget"] is None
    assert captured["timeout"] is None
