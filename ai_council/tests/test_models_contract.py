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


# --- Provider-aware completion params (max_tokens / max_completion_tokens) ----

class _UnsupportedParam(Exception):
    """Mimics OpenAI's 400 for a parameter a model rejects."""

    def __init__(self, param):
        self.body = {"error": {
            "code": "unsupported_parameter",
            "param": param,
            "message": f"Unsupported parameter: '{param}' is not supported with this model.",
        }}
        super().__init__(self.body["error"]["message"])


class _FakeCompletions:
    def __init__(self, reject_once=None):
        self.calls = []
        self._reject_once = reject_once

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._reject_once and self._reject_once in kwargs:
            bad, self._reject_once = self._reject_once, None
            raise _UnsupportedParam(bad)
        return "RESPONSE"


class _FakeClient:
    def __init__(self, reject_once=None):
        self.chat = type("Chat", (), {"completions": _FakeCompletions(reject_once)})()


def test_unsupported_param_from_error_body():
    assert ModelManager._unsupported_param(_UnsupportedParam("max_tokens")) == "max_tokens"


def test_unsupported_param_from_message_text():
    err = Exception("Error code: 400 - 'temperature' is not supported with this model.")
    assert ModelManager._unsupported_param(err) == "temperature"


def test_unsupported_value_temperature_detected():
    # OpenAI reasoning models reject a non-default temperature with code
    # 'unsupported_value' and a "does not support" message.
    err = Exception(
        "Error code: 400 - Unsupported value: 'temperature' does not support 0.7 "
        "with this model. Only the default (1) value is supported."
    )
    assert ModelManager._unsupported_param(err) == "temperature"


def test_unsupported_param_none_for_unrelated_error():
    assert ModelManager._unsupported_param(Exception("connection reset")) is None


def test_openai_gets_max_completion_tokens():
    mm = _bare_manager()
    client = _FakeClient()
    model = ModelConfig(name="G", model_id="gpt-x", provider=Provider.OPENAI)
    asyncio.run(mm._create_completion(client, model, messages=[], max_tokens=8000))
    sent = client.chat.completions.calls[0]
    assert sent.get("max_completion_tokens") == 8000
    assert "max_tokens" not in sent


def test_custom_keeps_max_tokens():
    mm = _bare_manager()
    client = _FakeClient()
    model = ModelConfig(name="C", model_id="glm", provider=Provider.CUSTOM)
    asyncio.run(mm._create_completion(client, model, messages=[], max_tokens=8000))
    sent = client.chat.completions.calls[0]
    assert sent.get("max_tokens") == 8000
    assert "max_completion_tokens" not in sent


def test_rejected_param_is_stripped_and_retried():
    mm = _bare_manager()
    client = _FakeClient(reject_once="temperature")
    model = ModelConfig(name="G", model_id="gpt-x", provider=Provider.OPENAI)
    result = asyncio.run(
        mm._create_completion(client, model, messages=[], temperature=0.4, max_tokens=8000)
    )
    assert result == "RESPONSE"
    calls = client.chat.completions.calls
    assert len(calls) == 2                      # first rejected, second succeeded
    assert "temperature" not in calls[1]        # offending param dropped on retry
    assert calls[1].get("max_completion_tokens") == 8000  # token cap preserved
