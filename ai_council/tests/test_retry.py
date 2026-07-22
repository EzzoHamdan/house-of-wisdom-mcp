"""Transient-failure retry with backoff in _create_completion (E1)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from ai_council import models as models_mod
from ai_council.config import AICouncilConfig, ModelConfig, Provider
from ai_council.models import ModelManager


# --- fake errors -------------------------------------------------------------
class _StatusError(Exception):
    """Mimics an openai APIStatusError carrying an HTTP status_code."""

    def __init__(self, status_code, retry_after=None):
        super().__init__(f"status {status_code}")
        self.status_code = status_code
        if retry_after is not None:
            self.response = SimpleNamespace(headers={"retry-after": str(retry_after)})


class APIConnectionError(Exception):
    """Named to match the retryable-by-type check (no status_code)."""


# --- fakes -------------------------------------------------------------------
class _ScriptedCompletions:
    def __init__(self, seq):
        self.seq = seq
        self.calls = 0

    async def create(self, **kwargs):
        item = self.seq[self.calls]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(choices=[SimpleNamespace(message=item)])


def _client(seq):
    return SimpleNamespace(chat=SimpleNamespace(completions=_ScriptedCompletions(seq)))


def _manager():
    cfg = AICouncilConfig(models=[
        ModelConfig(name="A", model_id="a", provider=Provider.CUSTOM,
                    base_url="http://x/v1", api_key="k"),
        ModelConfig(name="B", model_id="b", provider=Provider.CUSTOM,
                    base_url="http://x/v1", api_key="k"),
    ])
    return cfg, ModelManager(config=cfg)


def _no_sleep(monkeypatch):
    """Replace asyncio.sleep in models.py with a recorder so tests don't wait."""
    delays = []

    async def fake_sleep(d):
        delays.append(d)

    monkeypatch.setattr(models_mod.asyncio, "sleep", fake_sleep)
    return delays


OK = SimpleNamespace(content="done")


def test_retryable_429_then_success(monkeypatch):
    delays = _no_sleep(monkeypatch)
    cfg, mgr = _manager()
    client = _client([_StatusError(429), OK])
    resp = asyncio.run(mgr._create_completion(client, cfg.models[0], messages=[]))
    assert resp.choices[0].message is OK
    assert len(delays) == 1  # retried once


def test_connection_error_retried(monkeypatch):
    delays = _no_sleep(monkeypatch)
    cfg, mgr = _manager()
    client = _client([APIConnectionError("boom"), OK])
    resp = asyncio.run(mgr._create_completion(client, cfg.models[0], messages=[]))
    assert resp.choices[0].message is OK
    assert len(delays) == 1


def test_non_retryable_400_raises_immediately(monkeypatch):
    delays = _no_sleep(monkeypatch)
    cfg, mgr = _manager()
    client = _client([_StatusError(400), OK])
    try:
        asyncio.run(mgr._create_completion(client, cfg.models[0], messages=[]))
        assert False, "expected the 400 to propagate"
    except _StatusError as e:
        assert e.status_code == 400
    assert delays == []  # never slept


def test_exhausts_max_attempts(monkeypatch):
    delays = _no_sleep(monkeypatch)
    cfg, mgr = _manager()
    # Always 429: initial call + RETRY_MAX_ATTEMPTS retries, then propagate.
    client = _client([_StatusError(429)] * 10)
    try:
        asyncio.run(mgr._create_completion(client, cfg.models[0], messages=[]))
        assert False, "expected the 429 to propagate after retries"
    except _StatusError:
        pass
    assert len(delays) == ModelManager.RETRY_MAX_ATTEMPTS


def test_retry_after_header_honored():
    # Below the cap -> used verbatim.
    d = ModelManager._transient_retry_delay(_StatusError(429, retry_after=2), attempt=0)
    assert d == 2.0
    # Above the cap -> clamped.
    big = ModelManager._transient_retry_delay(
        _StatusError(429, retry_after=999), attempt=0
    )
    assert big == ModelManager.RETRY_MAX_DELAY


def test_non_retryable_delay_is_none():
    assert ModelManager._transient_retry_delay(_StatusError(400), attempt=0) is None
    assert ModelManager._transient_retry_delay(ValueError("x"), attempt=0) is None


# --- OpenAI reasoning models × function tools (v0.9.1) ------------------------
# Observed live with gpt-5.6-terra: /v1/chat/completions rejects tool-bearing
# requests from reasoning models unless reasoning_effort='none'. The offending
# parameter is implicit (never sent), so strip-and-retry can't fix it.


class _ReasoningToolsError(Exception):
    def __init__(self):
        super().__init__(
            "Error code: 400 - Function tools with reasoning_effort are not "
            "supported for gpt-x in /v1/chat/completions. To use function "
            "tools, use /v1/responses or set reasoning_effort to 'none'."
        )
        self.body = {"error": {"param": "reasoning_effort", "message": str(self)}}


class _KwargsRecorder:
    def __init__(self, seq):
        self.seq = seq
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.seq[len(self.calls) - 1]
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(choices=[SimpleNamespace(message=item)])


def _tools_kwargs():
    return {
        "messages": [{"role": "user", "content": "q"}],
        "tools": [{"type": "function", "function": {"name": "read_file"}}],
    }


def test_reasoning_tools_400_retried_with_effort_none(monkeypatch):
    cfg, mgr = _manager()
    delays = _no_sleep(monkeypatch)
    rec = _KwargsRecorder([_ReasoningToolsError(), SimpleNamespace(content="ok")])
    client = SimpleNamespace(chat=SimpleNamespace(completions=rec))
    result = asyncio.run(mgr._create_completion(client, cfg.models[0], **_tools_kwargs()))
    assert result.choices[0].message.content == "ok"
    assert len(rec.calls) == 2
    assert "reasoning_effort" not in rec.calls[0]
    assert rec.calls[1]["reasoning_effort"] == "none"
    assert delays == []  # immediate adaptation, no backoff sleep
    assert cfg.models[0].name in mgr._reasoning_off_models


def test_reasoning_off_is_remembered_across_calls(monkeypatch):
    """A tool LOOP must pay the discovery 400 once, not once per round."""
    cfg, mgr = _manager()
    _no_sleep(monkeypatch)
    first = _KwargsRecorder([_ReasoningToolsError(), SimpleNamespace(content="ok")])
    client = SimpleNamespace(chat=SimpleNamespace(completions=first))
    asyncio.run(mgr._create_completion(client, cfg.models[0], **_tools_kwargs()))

    second = _KwargsRecorder([SimpleNamespace(content="ok")])
    client2 = SimpleNamespace(chat=SimpleNamespace(completions=second))
    asyncio.run(mgr._create_completion(client2, cfg.models[0], **_tools_kwargs()))
    assert second.calls[0]["reasoning_effort"] == "none"  # preemptive, no 400


def test_no_adaptation_without_tools(monkeypatch):
    """The same 400 on a tool-less call is not ours to fix — it propagates."""
    cfg, mgr = _manager()
    _no_sleep(monkeypatch)
    rec = _KwargsRecorder([_ReasoningToolsError()])
    client = SimpleNamespace(chat=SimpleNamespace(completions=rec))
    with pytest.raises(_ReasoningToolsError):
        asyncio.run(mgr._create_completion(
            client, cfg.models[0], messages=[{"role": "user", "content": "q"}],
        ))
    assert len(rec.calls) == 1


def test_adaptation_fires_at_most_once(monkeypatch):
    """If the model still rejects after reasoning_effort='none', propagate —
    no infinite adapt loop."""
    cfg, mgr = _manager()
    _no_sleep(monkeypatch)
    rec = _KwargsRecorder([_ReasoningToolsError(), _ReasoningToolsError()])
    client = SimpleNamespace(chat=SimpleNamespace(completions=rec))
    with pytest.raises(_ReasoningToolsError):
        asyncio.run(mgr._create_completion(client, cfg.models[0], **_tools_kwargs()))
    assert len(rec.calls) == 2
