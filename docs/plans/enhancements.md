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
| [E9](#e9) | Low | Low | ✅ **done** — logger is injectable, not a forced singleton | `logger.py::AICouncilLogger` |
| [E10](#e10) | Low | Low | ✅ **done** — optional JSON log format for machine-readable MCP debugging | `logger.py`, `config.py`, `main.py` |
| [E11](#e11) | Low | Low | ✅ **done** — reject duplicate model `name` values at startup | `config.py::model_post_init` |

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

## E9 — inject the logger instead of a global singleton {#e9} ✅ done

**Was.** `AICouncilLogger` was a process-wide singleton (`__new__` + `_initialized` guard); the
first construction won and no second independent instance could exist.

**Done.** The singleton machinery is gone. Each `AICouncilLogger()` is a distinct object, but they
route through the same *named* stdlib logger and a single handler, so no duplicate handlers or
double startup lines. Level and format stay process-wide (set by first construction or the last
`set_level` / `set_format`).

**Verified by.** `tests/test_logger.py` — two instances are distinct, the named logger keeps
exactly one handler.

---

## E10 — optional JSON log format {#e10} ✅ done

**Was.** Logs were human text only; structured `data` was pretty-printed and appended after the
message.

**Done.** Added a `log_format: text | json` config key (and `--log-format` CLI flag). In `json`
mode each record is one JSON object per line — `ts`, `level`, `logger`, `message`, and `data`
(omitted when absent) — via a `_JsonFormatter`. `data` now rides the record as an attribute, so
both formatters render it cleanly; text output is unchanged. Applied after config load in
`ModelManager._apply_log_format`.

**Verified by.** `tests/test_logger.py` (formatter selection, valid JSON line, data omitted when
None) and `tests/test_config.py` (`log_format` parsing/default).

---

## E11 — reject duplicate model names at startup {#e11} ✅ done

**Was.** `model_post_init` validated unique `code_name` values but not `name` values, so two models
could share a `name` and make the per-call `models` selection (and the returned label) ambiguous.

**Done.** A `name`-uniqueness check now runs alongside the `code_name` check, raising
`Duplicate model names found in model configuration` at startup.

**Verified by.** `tests/test_config.py::test_duplicate_model_names_rejected`.

---

## Triage summary

```text
done:      E1 retry · E2 tests · E3 scribe cap · E4 client cache · E8 dead return
           E9 logger injection · E10 json logs · E11 dup-name check
remaining: E5 timeouts · E6 anonymity · E7 budget wording  (decide behavior first)
```

Suite is **103 passing**. Only E5/E6/E7 remain — the "decide the behavior first" set, each a
semantics choice worth confirming before implementing.
