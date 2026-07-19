"""Tests for SynthesizerToolsConfig parsing and defaults."""

from __future__ import annotations

from ai_council.config import AICouncilConfig, ModelConfig, Provider


def _base_models():
    return [
        ModelConfig(name="A", provider=Provider.CUSTOM, model_id="a", base_url="http://x", api_key="k"),
        ModelConfig(name="B", provider=Provider.CUSTOM, model_id="b", base_url="http://x", api_key="k"),
    ]


def test_default_synthesizer_tools_disabled():
    cfg = AICouncilConfig(models=_base_models())
    assert cfg.synthesizer_tools.enabled is False
    assert cfg.synthesizer_tools.max_tool_iterations == 8
    assert "read_file" in cfg.synthesizer_tools.allowed_tools


def test_synthesizer_tools_parsed_from_dict():
    cfg = AICouncilConfig(
        models=_base_models(),
        synthesizer_tools={
            "enabled": True,
            "workspace_root": "/tmp",
            "max_tool_iterations": 4,
            "allowed_tools": ["think"],
        },
    )
    assert cfg.synthesizer_tools.enabled is True
    assert cfg.synthesizer_tools.workspace_root == "/tmp"
    assert cfg.synthesizer_tools.max_tool_iterations == 4
    assert cfg.synthesizer_tools.allowed_tools == ["think"]


def test_synthesizer_tools_max_iterations_bounds():
    import pytest

    with pytest.raises(Exception):
        AICouncilConfig(
            models=_base_models(),
            synthesizer_tools={"max_tool_iterations": 0},
        )
    with pytest.raises(Exception):
        AICouncilConfig(
            models=_base_models(),
            synthesizer_tools={"max_tool_iterations": 999},
        )


def test_anonymous_perspectives_default_false():
    cfg = AICouncilConfig(models=_base_models())
    assert cfg.anonymous_perspectives is False


def test_anonymous_perspectives_set_true():
    cfg = AICouncilConfig(models=_base_models(), anonymous_perspectives=True)
    assert cfg.anonymous_perspectives is True


def test_max_concurrent_consultants_default():
    cfg = AICouncilConfig(models=_base_models())
    assert cfg.max_concurrent_consultants == 3


def test_max_concurrent_consultants_set():
    cfg = AICouncilConfig(models=_base_models(), max_concurrent_consultants=10)
    assert cfg.max_concurrent_consultants == 10


def test_max_concurrent_consultants_bounds():
    import pytest

    with pytest.raises(Exception):
        AICouncilConfig(models=_base_models(), max_concurrent_consultants=0)
    with pytest.raises(Exception):
        AICouncilConfig(models=_base_models(), max_concurrent_consultants=99)