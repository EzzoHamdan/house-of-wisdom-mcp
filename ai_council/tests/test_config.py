"""Tests for SynthesizerToolsConfig parsing and defaults."""

from __future__ import annotations

import pytest

from ai_council.config import (
    AICouncilConfig,
    ModelConfig,
    Provider,
    _load_dotenv_files,
    load_config,
)


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


# --- Bug 1: custom endpoint base_url is required even when api_key is present -

def test_custom_endpoint_without_base_url_rejected():
    """A custom model with an api_key but no base_url must be rejected — it
    previously slipped through and defaulted to api.openai.com."""
    import pytest

    with pytest.raises(ValueError, match="base_url"):
        AICouncilConfig(models=[
            ModelConfig(name="A", provider=Provider.CUSTOM, model_id="a", api_key="k"),  # no base_url
            ModelConfig(name="B", provider=Provider.CUSTOM, model_id="b", base_url="http://x", api_key="k"),
        ])


# --- Bug 2: API keys come from the AI_COUNCIL_ prefix, not a bare env var -----

def test_openai_key_read_from_prefixed_env(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("AI_COUNCIL_OPENAI_API_KEY", "prefixed-value")
    cfg = AICouncilConfig(models=_base_models())
    assert cfg.openai_api_key == "prefixed-value"


def test_bare_openai_key_env_is_ignored(monkeypatch):
    """An ambient OPENAI_API_KEY must NOT be silently adopted."""
    monkeypatch.delenv("AI_COUNCIL_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-value")
    cfg = AICouncilConfig(models=_base_models())
    assert cfg.openai_api_key is None


# --- Bug 3: code-name assignment must not skip names or IndexError ------------

def test_code_names_assigned_in_order():
    cfg = AICouncilConfig(models=_base_models())
    assert [m.code_name for m in cfg.models] == ["Alpha", "Beta"]


def test_code_names_no_skip_when_earlier_model_is_explicit():
    """When an earlier model claims a name explicitly, the unassigned one must
    still get the first free name (Alpha), not skip to Gamma."""
    cfg = AICouncilConfig(models=[
        ModelConfig(name="A", provider=Provider.CUSTOM, model_id="a", base_url="http://x", api_key="k", code_name="Beta"),
        ModelConfig(name="B", provider=Provider.CUSTOM, model_id="b", base_url="http://x", api_key="k"),
    ])
    assert cfg.models[0].code_name == "Beta"
    assert cfg.models[1].code_name == "Alpha"


# --- ${ENV_VAR} interpolation keeps secrets out of the YAML --------------------

def test_env_placeholder_resolved_in_model_api_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-real-deepseek")
    m = ModelConfig(
        name="DS", provider=Provider.CUSTOM, model_id="deepseek-chat",
        base_url="https://api.deepseek.com/v1", api_key="${DEEPSEEK_API_KEY}",
    )
    assert m.api_key == "sk-real-deepseek"


def test_env_placeholder_resolved_in_base_url(monkeypatch):
    monkeypatch.setenv("LLM_HOST", "http://example.local:8000")
    m = ModelConfig(
        name="X", provider=Provider.CUSTOM, model_id="x",
        base_url="${LLM_HOST}/v1", api_key="k",
    )
    assert m.base_url == "http://example.local:8000/v1"


def test_env_placeholder_missing_raises_naming_the_var(monkeypatch):
    monkeypatch.delenv("NO_SUCH_KEY", raising=False)
    with pytest.raises(ValueError, match="NO_SUCH_KEY"):
        ModelConfig(
            name="X", provider=Provider.CUSTOM, model_id="x",
            base_url="http://x", api_key="${NO_SUCH_KEY}",
        )


def test_literal_key_with_stray_dollar_is_untouched():
    """A real key that merely contains '$' (but no ${...}) must pass through."""
    m = ModelConfig(
        name="X", provider=Provider.CUSTOM, model_id="x",
        base_url="http://x", api_key="sk-abc$def",
    )
    assert m.api_key == "sk-abc$def"


def test_env_placeholder_resolved_in_top_level_key(monkeypatch):
    monkeypatch.setenv("MY_OR_KEY", "sk-or-real")
    cfg = AICouncilConfig(models=_base_models(), openrouter_api_key="${MY_OR_KEY}")
    assert cfg.openrouter_api_key == "sk-or-real"


# --- .env file loading (config-dir + cwd, real env wins) ----------------------

def test_dotenv_loaded_next_to_config(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_COUNCIL_OPENAI_API_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "# a comment\n"
        'export AI_COUNCIL_OPENAI_API_KEY="sk-from-dotenv"\n'
    )
    _load_dotenv_files(str(tmp_path / "config.yaml"))
    import os
    assert os.environ["AI_COUNCIL_OPENAI_API_KEY"] == "sk-from-dotenv"


def test_dotenv_does_not_override_real_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_COUNCIL_OPENAI_API_KEY", "sk-from-real-env")
    (tmp_path / ".env").write_text("AI_COUNCIL_OPENAI_API_KEY=sk-from-dotenv\n")
    _load_dotenv_files(str(tmp_path / "config.yaml"))
    import os
    assert os.environ["AI_COUNCIL_OPENAI_API_KEY"] == "sk-from-real-env"


def test_load_config_reads_dotenv_and_applies_prefixed_key(tmp_path, monkeypatch):
    """End-to-end: a .env beside the config feeds the AI_COUNCIL_-prefixed key."""
    monkeypatch.delenv("AI_COUNCIL_OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".env").write_text("AI_COUNCIL_OPENROUTER_API_KEY=sk-or-fromenv\n")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n"
        "  - name: Claude\n"
        "    provider: openrouter\n"
        "    model_id: anthropic/claude-opus-4\n"
        "    enabled: true\n"
        "  - name: Gemini\n"
        "    provider: openrouter\n"
        "    model_id: google/gemini-2.5-pro\n"
        "    enabled: true\n"
    )
    cfg = load_config(str(config_path))
    assert cfg.openrouter_api_key == "sk-or-fromenv"


def test_ten_models_with_one_explicit_name_does_not_crash():
    """10 models with one hand-set code_name used to raise a bare IndexError."""
    models = [
        ModelConfig(name=f"M{i}", provider=Provider.CUSTOM, model_id=str(i),
                    base_url="http://x", api_key="k",
                    code_name="Zeta" if i == 0 else None)  # M0 claims a mid-pool name
        for i in range(10)
    ]
    cfg = AICouncilConfig(models=models)
    names = [m.code_name for m in cfg.models]
    assert names[0] == "Zeta"
    assert len(set(names)) == 10  # every model got a distinct name