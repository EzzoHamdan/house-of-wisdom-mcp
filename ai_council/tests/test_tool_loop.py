"""Tests for the agentic tool loop's message-contract correctness.

Uses a fake OpenAI-style client that ENFORCES the tool-calling ordering rule
(every assistant message with tool_calls must be followed by one tool message
per tool_call_id) so we catch 400-class contract violations without a network.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ai_council.config import AICouncilConfig, ModelConfig, Provider
from ai_council.models import ModelManager
from ai_council.tools import ToolRegistry, TOOL_SCHEMAS


class StrictContractError(Exception):
    """Mimics an OpenAI 400 for a malformed tool-calling history."""


def _validate_message_order(messages):
    """Raise if any assistant tool_calls are not answered by tool messages."""
    for i, m in enumerate(messages):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            answered = set()
            for later in messages[i + 1 :]:
                if later.get("role") == "tool":
                    answered.add(later.get("tool_call_id"))
                elif later.get("role") in ("assistant", "user"):
                    break
            for tc in m["tool_calls"]:
                tc_id = tc["id"] if isinstance(tc, dict) else tc.id
                if tc_id not in answered:
                    raise StrictContractError(
                        "assistant message with tool_calls must be followed by "
                        "tool messages responding to each tool_call_id"
                    )


def _tool_call(idx, name="think", args='{"thought":"x"}'):
    return SimpleNamespace(
        id=f"call_{idx}",
        function=SimpleNamespace(name=name, arguments=args),
    )


class _Msg:
    """A fake chat message exposing the bits the loop touches, incl. model_dump."""

    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls

    def model_dump(self, exclude_none=False):
        d = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [
                {"id": tc.id,
                 "type": "function",
                 "function": {"name": tc.function.name,
                              "arguments": tc.function.arguments}}
                for tc in self.tool_calls
            ]
        if exclude_none:
            d = {k: v for k, v in d.items() if v is not None}
        return d


class _FakeCompletions:
    def __init__(self, script):
        self._script = script
        self._i = 0

    async def create(self, **kwargs):
        _validate_message_order(kwargs["messages"])  # strict endpoint check
        msg = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


class _FakeClient:
    def __init__(self, script):
        self.chat = SimpleNamespace(completions=_FakeCompletions(script))


def _manager(monkeypatch, script):
    cfg = AICouncilConfig(
        parallel_timeout=30,
        models=[
            ModelConfig(name="A", model_id="a", provider=Provider.CUSTOM,
                        base_url="http://x/v1", api_key="k", enabled=True),
            ModelConfig(name="B", model_id="b", provider=Provider.CUSTOM,
                        base_url="http://x/v1", api_key="k", enabled=True),
        ],
    )
    mgr = ModelManager(config=cfg)
    monkeypatch.setattr(mgr, "_get_client_for_model", lambda mc: _FakeClient(script))
    return mgr, cfg


def test_forced_final_answers_pending_tool_calls(monkeypatch, tmp_path):
    """B1: budget exhaustion must not send unanswered tool_calls to the API.

    The model always asks for a tool (never answers), so the loop hits the
    max_iterations forced-final path. The fake client enforces the contract, so
    the old code (appending the tool_calls-bearing assistant msg then a user
    msg) raises StrictContractError; the fix appends tool stubs first.
    """
    asking = _Msg(content="", tool_calls=[_tool_call(0)])
    answering = _Msg(content="final analysis", tool_calls=None)
    # Enough "asking" turns to blow a budget of 1, then an answer if asked again.
    script = [asking, asking, asking, answering]
    mgr, cfg = _manager(monkeypatch, script)
    reg = ToolRegistry(workspace_root=str(tmp_path), allowed_tools=None)

    result = asyncio.run(mgr.call_model_with_tools(
        cfg.models[0],
        system_prompt="sys",
        user_prompt="usr",
        tool_schemas=TOOL_SCHEMAS,
        tool_dispatcher=reg,
        max_iterations=1,
    ))
    assert result.ok
    assert "final analysis" in result.text


def test_empty_schemas_send_no_tools_param(monkeypatch, tmp_path):
    """B5: allowed_tools: [] must omit the tools param, not send tools=[]."""
    seen = {}

    class _RecordingCompletions(_FakeCompletions):
        async def create(self, **kwargs):
            seen["tools"] = kwargs.get("tools", "OMITTED")
            return await super().create(**kwargs)

    answering = _Msg(content="done", tool_calls=None)
    mgr, cfg = _manager(monkeypatch, [answering])
    client = _FakeClient([answering])
    client.chat.completions = _RecordingCompletions([answering])
    monkeypatch.setattr(mgr, "_get_client_for_model", lambda mc: client)

    reg = ToolRegistry(workspace_root=str(tmp_path), allowed_tools=[])
    result = asyncio.run(mgr.call_model_with_tools(
        cfg.models[0],
        system_prompt="sys",
        user_prompt="usr",
        tool_schemas=[],  # empty allowlist
        tool_dispatcher=reg,
        max_iterations=8,
    ))
    assert result.ok
    assert seen["tools"] is None  # None => SDK omits the key, not tools=[]
