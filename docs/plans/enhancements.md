# PLAN — Possible enhancements

> Status: draft
> Created: 2026-07-21 · Last updated: 2026-07-21
> Owns: non-defect improvements (robustness, ergonomics, test coverage, performance) for `ai_council/` at commit `c9be2e0` (v0.6.1) · Does not own: correctness/security bugs → [bugs-and-issues.md](bugs-and-issues.md)
> Done when: each item is either adopted (with a linked change) or explicitly declined with a reason.

## How to read it

These are *changes worth considering*, not defects — the server works today. Each is scored by
value and effort so the owner can triage. High-value/low-effort items are at the top of the table.
Anchors point at the code an enhancement would touch.

| ID | Value | Effort | Enhancement | Anchor |
| --- | --- | --- | --- | --- |
| [E1](#e1) | High | Low | ✅ **done** — retry with backoff on rate-limit / transient API errors | `models.py::_create_completion`, `_transient_retry_delay` |
| [E2](#e2) | High | Med | ✅ **done** — real test coverage for the tool loop, mode resolution, and sandbox | `ai_council/tests/` |
| [E3](#e3) | Med | Low | ✅ **done** — apply a concurrency cap in SCRIBE mode too | `models.py::call_models_parallel` |
| [E4](#e4) | Med | Low | ✅ **done** — cache one `AsyncOpenAI` client per endpoint instead of rebuilding per call | `models.py::_get_client_for_model` |
| [E5](#e5) | Med | Low | Split per-consultant vs whole-batch timeout | `models.py:302`, `_gather_consultants` |
| [E6](#e6) | Med | Med | Make `anonymous_perspectives` actually redact `model_name` in the payload | `main.py::Perspective`, `synthesis.py:188-195` |
| [E7](#e7) | Low | Low | Count individual tool calls, not rounds, against the budget (or restate) | `models.py:355-356` |
| [E8](#e8) | Low | Low | ✅ **done** — drop the unused `models_run` return value | `synthesis.py::collect_perspectives`, `main.py` |
| [E9](#e9) | Low | Low | Inject the logger instead of a process-wide singleton | `logger.py::AICouncilLogger` |
| [E10](#e10) | Low | Low | Optional JSON log format for machine-readable MCP debugging | `logger.py` |
| [E11](#e11) | Low | Low | Reject duplicate model `name` values at startup | `config.py::model_post_init` |

---

## E1 — retry with backoff on transient failures {#e1} ✅ done

**Was.** `call_model` mapped `rate_limit` / `auth` substrings to friendlier errors but never
retried — one 429 or dropped connection failed that consultant for the whole call.
`_create_completion` retried only to *strip unsupported params*, not for transient network/HTTP
errors.

**Done.** `_create_completion` now backs off and retries on transient classes: HTTP 429 / 500 /
502 / 503 / 504 and connection/timeout exceptions (matched by `status_code` or exception type name
— `models.py::_transient_retry_delay`). It honors a numeric `Retry-After` header when present, else
uses exponential backoff (`RETRY_BASE_DELAY * 2^attempt`) plus jitter, clamped to `RETRY_MAX_DELAY`
(8 s). Retries are capped at `RETRY_MAX_ATTEMPTS` (2 extra attempts). The unsupported-param
strip-and-retry is unchanged and takes precedence. The batch/consultant timeouts still bound total
wall-clock, so a retry loop can't outlive its window. Tunables are class attributes so tests lower
them.

**Verified by.** `tests/test_retry.py` — 429/connection retried then succeed, 400 raised
immediately with no sleep, attempts exhaust to propagation, `Retry-After` honored and clamped.

---

## E2 — real test coverage for the loop, modes, and sandbox {#e2} ✅ done

**Was.** 62 tests covered config parsing, tool-name plumbing, and path resolution only. No test
exercised the tool-calling loop, mode-resolution precedence, prompt assembly, the glob sandbox
boundary, or `.env` edge cases.

**Done.** Coverage now runs the trickiest logic against a stubbed OpenAI client (no network):

| Area | Test file |
| --- | --- |
| Tool-loop message contract (forced-final answers pending `tool_calls`; empty `tools` omitted) | `tests/test_tool_loop.py` |
| Transient-retry backoff | `tests/test_retry.py` |
| Mode precedence (`mode` > `agentic` > config default), unknown-mode fallback, scholar budget, empty-content nudge | `tests/test_mode_resolution.py` |
| SCRIBE/TRANSLATOR/SCHOLAR prompt assembly (scope cage vs mode guidance) | `tests/test_prompt_assembly.py` |
| Glob sandbox boundary, truncation marker, cap non-override | `tests/test_tools.py` |
| `.env` inline-comment parsing | `tests/test_config.py` |

Suite is **89 passing** (was 62). Remaining gap: no test hits a *live* model endpoint — by design;
all model I/O is stubbed.

**Why it mattered.** The tool loop and mode resolution were the least-tested and highest-risk code;
several [bugs](bugs-and-issues.md) lived exactly there.

---

## E3 — concurrency cap in SCRIBE mode {#e3} ✅ done

**Was.** `call_models_parallel` fired every model at once with no semaphore;
`max_concurrent_consultants` was honored only on the agentic path.

**Done.** SCRIBE now wraps each `call_model` in the same
`asyncio.Semaphore(max_concurrent_consultants)` the agentic path uses; models beyond the cap queue
and run as slots free up. The `_gather_consultants` timeout handling is unchanged, so a straggler
still can't collapse the batch.

**Verified by.** `tests/test_client_and_concurrency.py` — peak concurrency is 1 at cap 1 and 3 at
cap 3 with a 4-model roster.

**Note.** The cap now bounds SCRIBE latency too; the default (3) matches Ollama Pro. Raising it
trades latency for provider-limit safety — the README's SCRIBE "sharp edge" no longer applies.

---

## E4 — cache the client per endpoint {#e4} ✅ done

**Was.** `_get_client_for_model` built a fresh `AsyncOpenAI` on every call and every tool-loop
iteration, discarding connection pooling.

**Done.** `ModelManager` now memoizes one client per `(base_url, api_key)` in `self._client_cache`
and reuses it. Models sharing an endpoint+key share a client; distinct endpoints get distinct
clients.

**Verified by.** `tests/test_client_and_concurrency.py` — same model reuses its client, same
endpoint is shared across models, distinct endpoints stay separate.

---

## E5 — separate per-consultant and batch timeouts {#e5}

**Today.** `parallel_timeout` is applied twice with the same value: once per consultant inside
`call_model_with_tools` (`models.py:302`, via `asyncio.wait_for`) and once to the whole batch in
`_gather_consultants`. Queued consultants (behind the semaphore) burn their wait inside the batch
window, so a SCHOLAR run with more models than `max_concurrent_consultants` needs a generous value
— documented, but a single knob conflates two concerns.

**Change.** Introduce an optional `consultant_timeout` distinct from the batch `parallel_timeout`
(default: consultant = batch, preserving today's behavior).

**Why it matters.** Lets an operator bound a single slow consultant tightly while still allowing a
long total batch — the current single value forces a trade-off.

---

## E6 — make anonymity actually redact {#e6}

**Today.** With `anonymous_perspectives: true`, only `label` switches to the code name; the payload
still carries both `model_name` and `code_name` for every perspective
(`synthesis.py:188-195`, `main.py::Perspective`). The README flags this: "this hides nothing from
the orchestrator."

**Change.** When anonymous, omit or null `model_name` in the returned `Perspective` (keep an
internal mapping server-side if needed for logs).

**Why it matters.** The feature's stated purpose is bias reduction; leaving the real name in the
payload defeats it for any orchestrator that reads the field. Either redact it or drop the feature
and document that anonymity isn't offered.

**Caveat.** Some callers may rely on `model_name` always being present — this is a payload-shape
change; gate it behind the existing flag so default behavior is unchanged.

---

## E7 — budget counts rounds, not tool calls {#e7}

**Today.** The budget increments once per assistant turn containing tool calls
(`models.py:355-356`), so a model that requests four files in one turn spends one unit while the
system prompt tells it each call costs one unit (`models.py:547-552`). Documented sharp edge.

**Change.** Either count `len(tool_calls)` per round for true per-call accounting, or soften the
prompt wording to "each *round* of tool calls costs one unit" so the prompt matches reality.

**Why it matters.** A batching model can read far more of the workspace than the budget number
implies. Low urgency (read-only sandbox), but the prompt currently misstates the mechanic.

---

## E8 — drop the unused `models_run` return value {#e8} ✅ done

**Was.** `collect_perspectives` returned `(perspectives, models)`; the caller unpacked
`perspectives, models_run` but used the local `models` for all counts. `models_run` was always
identical to the input.

**Done.** `collect_perspectives` now returns just `perspectives` (return type `List[Dict]`); the
`main.py` call site and the two test call sites were updated, and the unused `Tuple` import
removed. Pure simplification — no behavior change.

---

## E9 — inject the logger instead of a global singleton {#e9}

**Today.** `AICouncilLogger` is a process-wide singleton (`logger.py:9-34`) whose `__init__` guard
means the first construction wins and later config (log level, handlers) mutates shared state.

**Change.** Allow an injected logger / factory; keep the singleton as the default for the CLI entry
point.

**Why it matters.** Makes tests (E2) isolatable and would let two configs coexist in one process
(e.g. embedding the server). Low effort; unblocks cleaner tests.

---

## E10 — optional JSON log format {#e10}

**Today.** Logs are human-formatted text on stderr (`logger.py:27-31`); structured `data` is
pretty-printed JSON appended after the message.

**Change.** Add a `log_format: text | json` option emitting one JSON object per line when `json`.

**Why it matters.** MCP clients collect stderr; line-delimited JSON is far easier to filter and
correlate when debugging a multi-consultant run. Nice-to-have, not urgent.

---

## E11 — reject duplicate model names at startup {#e11}

**Today.** `model_post_init` (`config.py`) validates unique `code_name` values but not `name`
values. Two enabled models can share a `name`; the per-call `models` argument matches by `name`
(`main.py::_process_ai_council`), so a duplicate makes the subset selection and the returned
`label` ambiguous — both entries fire under one requested name, or neither is individually
addressable.

**Change.** Add a uniqueness check on `name` alongside the existing `code_name` check, failing
startup with a clear message.

**Why it matters.** Low frequency (you'd have to name two models the same), and it's a
configuration mistake rather than a code defect — hence an enhancement, not a bug. Filed here so
it isn't lost. Kept out of the fix batch because tightening validation could reject a config that
"works" today for someone relying on first-match behavior.

---

## Triage summary

```text
done:                                E1 retry · E2 tests · E3 scribe cap · E4 client cache · E8 dead return
deliberate changes (payload/config): E5 timeouts · E6 anonymity · E7 budget wording
polish:                              E9 logger injection · E10 json logs · E11 dup-name check
```

Suite is **94 passing**. None of the remaining items are blockers; the ordering reflects
value-to-effort, not necessity. E5/E6/E7 are the "decide the behavior first" set — worth a
conversation before implementing.
