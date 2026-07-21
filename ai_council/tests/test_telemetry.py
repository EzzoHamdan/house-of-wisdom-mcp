"""Per-consultant telemetry and progress reporting (v0.7.0).

Three capabilities are covered here, all of which exist to make a consult
auditable rather than an opaque charge:

A. `files_read` — which evidence a perspective actually rests on. A confident
   analysis from a consultant that opened nothing is not the same artifact as
   one that read the file in question, and only the server can tell them apart.
B. tokens / duration / cost — a call spends several API calls across several
   providers; the caller should not be billed blind.
C. progress notifications — a batch that runs silently for tens of seconds
   reads as a hang.

The model client is faked throughout; nothing here touches a network.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_council.config import AICouncilConfig, ModelConfig, Provider
from ai_council.models import ConsultantResult, ConsultantTelemetry, ModelManager
from ai_council.tools import TOOL_SCHEMAS, ToolRegistry

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"


def _cfg(**kw):
    return AICouncilConfig(models=[
        ModelConfig(name="A", provider=Provider.CUSTOM, model_id="a",
                    base_url="http://x/v1", api_key="k", **kw),
        ModelConfig(name="B", provider=Provider.CUSTOM, model_id="b",
                    base_url="http://x/v1", api_key="k", **kw),
    ])


# --- fakes -------------------------------------------------------------------

def _tool_call(idx, name, args):
    return SimpleNamespace(id=f"call_{idx}",
                           function=SimpleNamespace(name=name, arguments=args))


class _Msg:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls

    def model_dump(self, exclude_none=False):
        d = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name,
                              "arguments": tc.function.arguments}}
                for tc in self.tool_calls
            ]
        return d


def _response(msg, tokens_in=0, tokens_out=0, with_usage=True):
    usage = SimpleNamespace(prompt_tokens=tokens_in, completion_tokens=tokens_out)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)],
                           usage=usage if with_usage else None)


# --- A: what the consultant actually read ------------------------------------

def test_registry_records_files_read_not_misses():
    # Goes through `call()` — the dispatch point the tool loop actually uses,
    # and the only place per-tool counts are recorded.
    reg = ToolRegistry(workspace_root=str(FIXTURE))
    reg.call("read_file", {"path": "src/math.py"})
    reg.call("read_file", {"path": "src/math.py"})        # repeat -> recorded once
    reg.call("read_file", {"path": "does/not/exist.py"})  # miss -> not evidence
    reg.call("list_dir", {"path": "src"})

    activity = reg.activity
    assert activity["files_read"] == ["src/math.py"]
    assert activity["paths_listed"] == ["src"]
    # Counts measure effort, so the miss and the repeat both count.
    assert activity["tool_calls"] == {"read_file": 3, "list_dir": 1}


def test_sandbox_violation_is_not_recorded_as_a_read():
    reg = ToolRegistry(workspace_root=str(FIXTURE))
    out = reg.call("read_file", {"path": "../../../etc/passwd"})
    assert out.startswith("Error")
    assert reg.activity["files_read"] == []


def test_tool_loop_reports_the_files_it_opened(monkeypatch):
    """The end-to-end path: a consultant reads a file, telemetry proves it."""
    mgr = ModelManager(config=_cfg())
    reg = ToolRegistry(workspace_root=str(FIXTURE))
    turns = [
        _response(_Msg(tool_calls=[_tool_call(1, "read_file",
                                              '{"path":"src/math.py"}')]),
                  tokens_in=100, tokens_out=20),
        _response(_Msg(content="math.py defines add()."), tokens_in=300, tokens_out=50),
    ]

    async def fake_create(client, model_config, messages, **kwargs):
        return turns.pop(0)

    monkeypatch.setattr(mgr, "_create_completion", fake_create)
    result = asyncio.run(mgr.call_model_with_tools(
        mgr.config.models[0], "sys", "user", TOOL_SCHEMAS, reg, max_iterations=4,
    ))

    assert result.ok
    tel = result.telemetry
    assert tel.files_read == ["src/math.py"]
    assert tel.tool_calls == {"read_file": 1}
    assert tel.tool_rounds_used == 1
    assert tel.tool_rounds_budget == 4


def test_ungrounded_consultant_reports_no_files(monkeypatch):
    """A consultant that answers without reading anything must say so."""
    mgr = ModelManager(config=_cfg())
    reg = ToolRegistry(workspace_root=str(FIXTURE))

    async def fake_create(client, model_config, messages, **kwargs):
        return _response(_Msg(content="Confident answer, zero evidence."))

    monkeypatch.setattr(mgr, "_create_completion", fake_create)
    result = asyncio.run(mgr.call_model_with_tools(
        mgr.config.models[0], "sys", "user", TOOL_SCHEMAS, reg, max_iterations=4,
    ))

    assert result.ok
    assert result.telemetry.files_read == []
    assert result.telemetry.tool_rounds_used == 0


# --- B: cost and latency accounting ------------------------------------------

def test_tokens_accumulate_across_every_completion(monkeypatch):
    """Retries and forced-final nudges are billed too — all must be counted."""
    mgr = ModelManager(config=_cfg())
    reg = ToolRegistry(workspace_root=str(FIXTURE))
    turns = [
        _response(_Msg(tool_calls=[_tool_call(1, "think", '{"thought":"x"}')]),
                  tokens_in=100, tokens_out=10),
        _response(_Msg(tool_calls=[_tool_call(2, "think", '{"thought":"y"}')]),
                  tokens_in=200, tokens_out=20),
        _response(_Msg(content="final"), tokens_in=400, tokens_out=30),
    ]

    async def fake_create(client, model_config, messages, **kwargs):
        return turns.pop(0)

    monkeypatch.setattr(mgr, "_create_completion", fake_create)
    result = asyncio.run(mgr.call_model_with_tools(
        mgr.config.models[0], "sys", "user", TOOL_SCHEMAS, reg, max_iterations=4,
    ))

    assert result.telemetry.api_calls == 3
    assert result.telemetry.tokens_in == 700
    assert result.telemetry.tokens_out == 60
    assert result.telemetry.duration_s >= 0.0


def test_cost_is_none_without_pricing_and_computed_with_it():
    unpriced = ModelConfig(name="local", model_id="l", provider=Provider.CUSTOM,
                           base_url="http://x/v1", api_key="k")
    tel = ConsultantTelemetry(tokens_in=1_000_000, tokens_out=500_000)
    tel.price(unpriced)
    # A local model has no dollar cost; 0.0 would read as "measured as free".
    assert tel.cost_usd is None

    priced = ModelConfig(name="cloud", model_id="c", provider=Provider.CUSTOM,
                         base_url="http://x/v1", api_key="k",
                         input_cost_per_1m=3.0, output_cost_per_1m=15.0)
    tel2 = ConsultantTelemetry(tokens_in=1_000_000, tokens_out=500_000)
    tel2.price(priced)
    assert tel2.cost_usd == pytest.approx(3.0 + 7.5)


def test_missing_usage_block_degrades_quietly():
    tel = ConsultantTelemetry()
    tel.add_usage(_response(_Msg(content="x"), with_usage=False))
    assert tel.api_calls == 1
    assert tel.tokens_in == 0 and tel.tokens_out == 0


def test_failed_consultant_still_reports_what_it_burned(monkeypatch):
    """A consultant that errors already cost money; the tally must show it."""
    mgr = ModelManager(config=_cfg())

    async def boom(client, model_config, messages, **kwargs):
        raise RuntimeError("upstream exploded")

    monkeypatch.setattr(mgr, "_create_completion", boom)
    result = asyncio.run(mgr.call_model(mgr.config.models[0], "", "q?"))

    assert result.ok is False
    assert result.telemetry.duration_s >= 0.0


# --- C: progress notifications -----------------------------------------------

def test_progress_fires_once_per_consultant_in_completion_order():
    mgr = ModelManager(config=_cfg())
    seen = []

    async def cb(completed, total, name):
        seen.append((completed, total, name))

    async def quick(delay, text):
        await asyncio.sleep(delay)
        return ConsultantResult(text=text, ok=True)

    # B is deliberately faster than A, so completion order != roster order.
    coros = [quick(0.05, "a"), quick(0.01, "b")]
    results = asyncio.run(mgr._gather_consultants(
        coros, mgr.config.models, timeout=5, progress_cb=cb))

    assert [c for c, _, _ in seen] == [1, 2]          # monotonic
    assert [n for _, _, n in seen] == ["B", "A"]      # completion order
    assert all(t == 2 for _, t, _ in seen)
    # Results still come back in ROSTER order regardless of who finished first.
    assert [r.text for r in results] == ["a", "b"]


def test_timed_out_consultant_does_not_report_progress():
    mgr = ModelManager(config=_cfg())
    seen = []

    async def cb(completed, total, name):
        seen.append(name)

    async def fast():
        return ConsultantResult(text="done", ok=True)

    async def never():
        await asyncio.sleep(30)
        return ConsultantResult(text="late", ok=True)

    results = asyncio.run(mgr._gather_consultants(
        [fast(), never()], mgr.config.models, timeout=1, progress_cb=cb))

    assert seen == ["A"]  # only the one that actually finished
    assert results[0].ok is True and results[1].ok is False


def test_broken_progress_callback_cannot_fail_the_consultant():
    """A client that mishandles notifications must not lose us the answer."""
    mgr = ModelManager(config=_cfg())

    async def bad_cb(completed, total, name):
        raise RuntimeError("client went away")

    async def fine():
        return ConsultantResult(text="analysis", ok=True)

    results = asyncio.run(mgr._gather_consultants(
        [fine()], [mgr.config.models[0]], timeout=5, progress_cb=bad_cb))

    assert results[0].ok is True and results[0].text == "analysis"


def test_no_callback_is_the_default_and_changes_nothing():
    mgr = ModelManager(config=_cfg())

    async def fine():
        return ConsultantResult(text="analysis", ok=True)

    results = asyncio.run(mgr._gather_consultants(
        [fine()], [mgr.config.models[0]], timeout=5))
    assert results[0].text == "analysis"


# --- C, end to end: progress over a real MCP session -------------------------

def test_progress_notifications_reach_a_real_mcp_client():
    """Drive the server through an in-memory MCP session, as a client would.

    This is the only test that exercises the whole chain — client progressToken
    -> request context -> _make_progress_cb -> _gather_consultants -> back over
    the wire — so it is what proves the feature actually works rather than
    merely type-checks.
    """
    from mcp.shared.memory import create_connected_server_and_client_session

    from ai_council.config import SynthesizerToolsConfig
    from ai_council.main import AICouncilServer

    cfg = AICouncilConfig(
        models=[
            ModelConfig(name="A", provider=Provider.CUSTOM, model_id="a",
                        base_url="http://x/v1", api_key="k"),
            ModelConfig(name="B", provider=Provider.CUSTOM, model_id="b",
                        base_url="http://x/v1", api_key="k"),
        ],
        synthesizer_tools=SynthesizerToolsConfig(enabled=False),
    )
    srv = AICouncilServer(config=cfg)

    async def fake_create(client, model_config, messages, **kwargs):
        await asyncio.sleep(0.01)
        return _response(_Msg(content=f"{model_config.name} analysis"),
                         tokens_in=10, tokens_out=5)

    srv.model_manager._create_completion = fake_create

    seen = []

    async def on_progress(progress, total, message):
        seen.append((progress, total, message))

    async def go():
        async with create_connected_server_and_client_session(srv.server) as client:
            return await client.call_tool(
                "consult",
                {"question": "Is this sound?", "mode": "scribe"},
                progress_callback=on_progress,
            )

    result = asyncio.run(go())

    assert result.isError is not True
    # One notification per consultant, monotonic, carrying the model's name.
    assert [p for p, _, _ in seen] == [1.0, 2.0]
    assert all(t == 2.0 for _, t, _ in seen)
    assert {"A", "B"} == {m.split()[0] for _, _, m in seen if m}


def test_no_progress_token_means_no_reporter():
    """A client that did not ask for progress gets none, and nothing breaks."""
    from ai_council.main import AICouncilServer

    srv = AICouncilServer(config=_cfg())
    # No active request context at all -> no reporter, no exception.
    assert srv._make_progress_cb() is None
