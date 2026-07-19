"""Tests for the consultant success/error contract (bugs 7, 8, 9).

Status is carried by ConsultantResult.ok, not inferred by prefix-matching the
analysis text, and a batch timeout keeps whatever already completed.
"""

from __future__ import annotations

import asyncio

from ai_council.config import AICouncilConfig, ModelConfig, Provider
from ai_council.logger import AICouncilLogger
from ai_council.models import ConsultantResult, ModelManager
from ai_council.synthesis import ResponseSynthesizer


def _config():
    return AICouncilConfig(models=[
        ModelConfig(name="A", provider=Provider.CUSTOM, model_id="a", base_url="http://x", api_key="k"),
        ModelConfig(name="B", provider=Provider.CUSTOM, model_id="b", base_url="http://x", api_key="k"),
    ])


class _FakeModelManager:
    """Stands in for ModelManager: real config, canned parallel results."""

    def __init__(self, config, results):
        self.config = config
        self._results = results

    async def call_models_parallel(self, models, context, question):
        return self._results


# --- Bugs 8 & 9: status comes from `ok`, not from the text's prefix ----------

def test_status_derived_from_ok_flag_not_text_prefix():
    config = _config()
    models = config.get_enabled_models()
    results = [
        # A genuine answer that happens to start with "Error" -> still ok.
        ConsultantResult(text="Error handling is the topic; here is the analysis...", ok=True),
        # An empty-content failure -> must be reported as error (bug 8).
        ConsultantResult(text="Consultant returned empty content after retry.", ok=False),
    ]
    synth = ResponseSynthesizer(_FakeModelManager(config, results))

    perspectives, _ = asyncio.run(
        synth.collect_perspectives("ctx", "question", models, mode="scribe")
    )

    assert perspectives[0]["status"] == "ok"
    assert perspectives[1]["status"] == "error"
    # The text is passed through verbatim in both cases.
    assert perspectives[0]["analysis"].startswith("Error handling")


# --- Bug 7: a batch timeout preserves the consultants that finished ----------

def _bare_manager():
    """A ModelManager without running __init__ (skips API-key validation)."""
    mm = ModelManager.__new__(ModelManager)
    mm.logger = AICouncilLogger()
    return mm


def test_gather_keeps_completed_when_one_times_out():
    mm = _bare_manager()
    models = [
        ModelConfig(name="fast", model_id="f"),
        ModelConfig(name="slow", model_id="s"),
    ]

    async def fast():
        return ConsultantResult(text="done", ok=True)

    async def slow():
        await asyncio.sleep(30)  # will be cancelled by the 1s deadline
        return ConsultantResult(text="late", ok=True)

    results = asyncio.run(mm._gather_consultants([fast(), slow()], models, timeout=1))

    assert results[0].ok is True and results[0].text == "done"
    assert results[1].ok is False and "did not finish" in results[1].text


def test_gather_marks_raised_exception_as_error():
    mm = _bare_manager()
    models = [ModelConfig(name="boom", model_id="b")]

    async def boom():
        raise RuntimeError("kaboom")

    results = asyncio.run(mm._gather_consultants([boom()], models, timeout=5))

    assert results[0].ok is False
    assert "boom" in results[0].text and "kaboom" in results[0].text
