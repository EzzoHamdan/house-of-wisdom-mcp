"""Transient-failure retry with backoff in _create_completion (E1)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

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
