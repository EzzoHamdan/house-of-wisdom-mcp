"""Client caching (E4) and SCRIBE concurrency cap (E3)."""

from __future__ import annotations

import asyncio

from ai_council.config import AICouncilConfig, ModelConfig, Provider
from ai_council.models import ModelManager, ConsultantResult


def _custom(name, base_url="http://x/v1", key="k"):
    return ModelConfig(name=name, model_id=name.lower(), provider=Provider.CUSTOM,
                       base_url=base_url, api_key=key)


# --- E4: client cache --------------------------------------------------------
def test_client_reused_for_same_model():
    cfg = AICouncilConfig(models=[_custom("A"), _custom("B")])
    mgr = ModelManager(config=cfg)
    c1 = mgr._get_client_for_model(cfg.models[0])
    c2 = mgr._get_client_for_model(cfg.models[0])
    assert c1 is c2  # not rebuilt on the second call


def test_client_shared_across_same_endpoint():
    # A and B share base_url + key -> same cached client.
    cfg = AICouncilConfig(models=[_custom("A"), _custom("B")])
    mgr = ModelManager(config=cfg)
    assert mgr._get_client_for_model(cfg.models[0]) is mgr._get_client_for_model(cfg.models[1])


def test_client_distinct_per_endpoint():
    cfg = AICouncilConfig(models=[
        _custom("A", base_url="http://one/v1"),
        _custom("B", base_url="http://two/v1"),
    ])
    mgr = ModelManager(config=cfg)
    assert mgr._get_client_for_model(cfg.models[0]) is not mgr._get_client_for_model(cfg.models[1])


# --- E3: SCRIBE concurrency cap ---------------------------------------------
def _peak_concurrency(monkeypatch, n_models, cap):
    cfg = AICouncilConfig(
        models=[_custom(c) for c in ["A", "B", "C", "D", "E"][:n_models]],
        max_concurrent_consultants=cap,
        parallel_timeout=30,
    )
    mgr = ModelManager(config=cfg)
    state = {"active": 0, "peak": 0}

    async def fake_call_model(model, ctx, q):
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        await asyncio.sleep(0.02)  # hold the slot so overlap is observable
        state["active"] -= 1
        return ConsultantResult("ok", True)

    monkeypatch.setattr(mgr, "call_model", fake_call_model)
    asyncio.run(mgr.call_models_parallel(cfg.models, "c", "q"))
    return state["peak"]


def test_scribe_serializes_at_cap_one(monkeypatch):
    assert _peak_concurrency(monkeypatch, n_models=4, cap=1) == 1


def test_scribe_allows_up_to_cap(monkeypatch):
    # 4 models, cap 3 -> at most 3 run at once.
    assert _peak_concurrency(monkeypatch, n_models=4, cap=3) == 3
