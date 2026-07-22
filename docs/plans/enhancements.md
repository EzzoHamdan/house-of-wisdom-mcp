# PLAN — Possible enhancements

> Status: draft
> Created: 2026-07-21 · Last updated: 2026-07-22
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
| [E5](#e5) | Med | Low | ⏸️ **deferred** — split per-consultant vs whole-batch timeout | `models.py`, `_gather_consultants` |
| [E6](#e6) | Med | Med | ✅ **done (option C)** — removed `anonymous_perspectives` as incoherent | `config.py`, `synthesis.py` |
| [E7](#e7) | Low | Low | ✅ **done (option B)** — tool-budget prompt now states round-accounting honestly | `models.py::build_consultant_system_prompt` |
| [E8](#e8) | Low | Low | ✅ **done** — drop the unused `models_run` return value | `synthesis.py::collect_perspectives`, `main.py` |
| [E9](#e9) | Low | Low | ✅ **done** — logger is injectable, not a forced singleton | `logger.py::AICouncilLogger` |
| [E10](#e10) | Low | Low | ✅ **done** — optional JSON log format for machine-readable MCP debugging | `logger.py`, `config.py`, `main.py` |
| [E11](#e11) | Low | Low | ✅ **done** — reject duplicate model `name` values at startup | `config.py::model_post_init` |
| [E12](#e12) | High | Med | ✅ **done** — report which files each consultant actually read | `tools.py::ToolRegistry`, `models.py::ConsultantTelemetry` |
| [E13](#e13) | High | Low | ✅ **done** — token, latency and cost accounting per perspective | `models.py::ConsultantTelemetry`, `config.py::ModelConfig` |
| [E14](#e14) | Med | Med | ✅ **done** — MCP progress notification per consultant | `main.py::_make_progress_cb`, `models.py::_gather_consultants` |

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

## E5 — separate per-consultant and batch timeouts {#e5} ⏸️ deferred

**Status.** Deferred by owner decision — lowest value of the batch, only worth it given a concrete
slow-straggler need. Left as-is for now.

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

## E6 — anonymity was incoherent; removed {#e6} ✅ done (option C)

**Was.** `anonymous_perspectives: true` only switched the `label` to the code name; the payload
still carried the real `model_name`, so an AI orchestrator (which reads every field) saw straight
through it — the "bias reduction" was theater.

**Decision.** Resolved by *consulting the House of Wisdom server itself* — a 4-family panel (GLM,
Kimi, DeepSeek-Pro, GPT) split 2 remove / 2 make-real, but was **unanimous** that (a) the current
state was theater, (b) model identity is legitimate *signal* for an AI reader, and (c) real
anonymity needs heavy machinery (per-request random aliases, style/ordering leak-scrubbing) not
worth it here. Chose **removal** — the honest, proportionate outcome.

**Done.** Removed the `anonymous_perspectives` field and all label-switching logic; `label` is now
always `model_name`. `code_name` stays as a short handle, always present in the payload. An old
config still setting the key is silently ignored (`extra="ignore"`). If a true blinded-evaluation
mode is ever wanted, it should be built deliberately (rename to `blinded`, randomize aliases,
strip leaks) — not as a flag that half-hides.

**Verified by.** `tests/test_config.py::test_anonymous_perspectives_field_removed`.

---

## E7 — budget wording made honest {#e7} ✅ done (option B)

**Was.** The budget counts *rounds* (turns with tool calls), but the prompt told the model "each
call costs one call" — so a batching model was misled about how much it could read.

**Done.** The prompt now says it plainly: "AT MOST N **rounds**; a round is one turn, batched calls
cost one round." No accounting change (Option B) — just honesty, so the prompt matches the
mechanic. Frugality guidance ("read only what matters, don't read speculatively") still bounds
reads. The now-resolved README "Tool budget" sharp-edge row was removed.

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

## E12 — report which files each consultant actually read {#e12} ✅ done

**Was.** A perspective was a wall of prose and nothing else. Two consultants disagreeing looked
like a 50/50 split even when one had read the relevant file and the other had answered from the
prompt alone. The server knew the difference — every tool call passed through `ToolRegistry` — and
threw it away.

**Done.** `ToolRegistry` now records its own activity: `files_read` (successful reads only, in
first-touch order, deduplicated), `paths_listed`, and `call_counts` per tool. Misses and sandbox
rejections are counted as *effort* in `call_counts` but never recorded as *evidence* in
`files_read`. One registry is built per consultant, so the record is already per-perspective. The
tool loop absorbs the snapshot via `ConsultantTelemetry.absorb_activity` and it ships in the
payload.

**Why this is not a synthesizer.** It merges, ranks and votes on nothing. It reports what happened.
The orchestrator still does all the weighing — it just no longer has to take each analysis on faith.

**Verified by.** `tests/test_telemetry.py` — reads recorded once, misses and sandbox violations
excluded, an end-to-end loop proving `files_read` after a real `read_file`, and the inverse case
where a consultant answers without opening anything.

---

## E13 — token, latency and cost accounting {#e13} ✅ done

**Was.** `consensus` reported queried/succeeded/failed and nothing else. Every call spent several
API calls across several providers and the caller was billed blind.

**Done.** `ConsultantTelemetry` accumulates `tokens_in`, `tokens_out` and `api_calls` from the
provider's own `usage` block at **every** completion — including the retry nudges and forced-final
turns, which are billed but were previously invisible. `duration_s` is stamped at a single exit
point (`_finish`) so timeouts and errors report what they burned too. Optional
`input_cost_per_1m` / `output_cost_per_1m` on `ModelConfig` produce `cost_usd`; with neither set it
stays `None`, because `0.0` would claim an unpriced cloud model was measured as free.
`consensus` gained `wall_clock_s` (elapsed, not the sum of parallel durations) and batch totals.

**Verified by.** `tests/test_telemetry.py` — accumulation across three completions, pricing math,
`None` without rates, quiet degradation when a provider omits `usage`, and a failed consultant
still reporting its spend.

---

## E14 — progress notification per consultant {#e14} ✅ done

**Was.** A `scholar` run went silent for tens of seconds. To a user that reads as a hang; to a
calling agent it is a reason not to call again.

**Done.** `_gather_consultants` takes an optional `progress_cb` and awaits it as each consultant
finishes. `main.py::_make_progress_cb` binds it to the live MCP request, but only when the client
opted in with a `progressToken` — otherwise it returns `None` and nothing is emitted. The reporter
fires inside the consultant's own task, so a consultant cancelled at the deadline never reports
completion, and a callback that raises is logged and swallowed rather than losing an answer that
already succeeded.

**Verified by.** `tests/test_telemetry.py` — monotonic counts in completion order (not roster
order) while results stay in roster order, no report from a timed-out consultant, a raising
callback that cannot fail the consultant, and an **end-to-end test over a real in-memory MCP client
session** that proves notifications actually cross the wire.

---

## Triage summary

```text
done:     E1 retry · E2 tests · E3 scribe cap · E4 client cache · E6 remove anonymity
          E7 honest budget wording · E8 dead return · E9 logger injection
          E10 json logs · E11 dup-name check
          E12 files-read evidence · E13 cost/latency accounting · E14 progress notifications
deferred: E5 split timeout  (owner decision — revisit on a concrete need)
```

Suite is **134 passing** (127 when E12–E14 landed; v0.7.1's workspace-root validation and
config-path fixes added seven). Every enhancement is resolved except E5, which is intentionally
deferred. E1–E11 released as **v0.6.3**; E12–E14 land in **v0.7.0** alongside the discoverability
work.

> **Note on what changed between them.** E1–E11 made the server more correct. E12–E14 make it more
> *usable by an agent* — the same theme as the v0.7.0 interface work: the code was mature well
> before the interface was.
