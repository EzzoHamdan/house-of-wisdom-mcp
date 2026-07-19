"""Model-selection semantics: max_models caps the DEFAULT fan-out only.

Regression for the bug where an explicitly named, enabled model sitting past
position ``max_models`` in the YAML was silently unreachable, because the cap
was applied inside ``get_enabled_models`` before the explicit-subset filter ran.
"""

from __future__ import annotations

from ai_council.config import AICouncilConfig, ModelConfig, Provider


def _config(max_models: int):
    # Five enabled models; the interesting one (E) sits well past max_models.
    models = [
        ModelConfig(name=n, provider=Provider.CUSTOM, model_id=n.lower(),
                    base_url="http://x", api_key="k")
        for n in ("A", "B", "C", "D", "E")
    ]
    return AICouncilConfig(models=models, max_models=max_models)


def test_default_fanout_is_capped_by_max_models():
    config = _config(max_models=2)
    names = [m.name for m in config.get_enabled_models()]
    assert names == ["A", "B"]


def test_full_enabled_list_ignores_max_models():
    config = _config(max_models=2)
    names = [m.name for m in config.get_enabled_models(limit=False)]
    assert names == ["A", "B", "C", "D", "E"]


def test_named_model_past_cap_is_reachable():
    # E is enabled but sits at position 5 with max_models=2. Resolving an
    # explicit request must use the full list, so E is selectable.
    config = _config(max_models=2)
    enabled_all = config.get_enabled_models(limit=False)
    requested = {"E"}
    selected = [m.name for m in enabled_all if m.name in requested]
    assert selected == ["E"]


def test_model_manager_delegates_limit_flag():
    from ai_council.models import ModelManager

    config = _config(max_models=2)
    manager = ModelManager(config)
    assert [m.name for m in manager.get_enabled_models()] == ["A", "B"]
    assert [m.name for m in manager.get_enabled_models(limit=False)] == [
        "A", "B", "C", "D", "E",
    ]
