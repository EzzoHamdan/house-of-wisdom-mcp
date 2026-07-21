import asyncio
import json
import random
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from openai import AsyncOpenAI
from .logger import AICouncilLogger
from .config import AICouncilConfig, ModelConfig, Provider, load_config


# Transient failures worth a retry: rate limits (429) and gateway/server errors
# (5xx). Connection/timeout errors carry no status_code and are matched by type.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_RETRYABLE_EXC_NAMES = {
    "APIConnectionError",
    "APITimeoutError",
    "APIConnectionTimeoutError",
    "InternalServerError",
}


class ConfigValidationError(Exception):
    """Raised when configuration validation fails."""
    pass


@dataclass
class ConsultantResult:
    """Outcome of a single consultant call.

    `ok` is the authoritative success flag. Callers MUST read `ok` and never
    infer success by prefix-matching `text` — a legitimate answer can start
    with a word like "Error", and a failure (e.g. empty output) need not.
    """
    text: str
    ok: bool


def _extract_text(msg) -> str:
    """Pull final answer text from a chat completion message.

    Ollama's thinking/cloud models (glm-5.2:cloud, kimi, minimax) often put
    the answer in a non-standard `reasoning` field and leave `content` empty.
    Fall back to `reasoning` / `thinking` / `reasoning_content` before giving
    up. Works on both raw OpenAI SDK message objects and dict-like messages.
    """
    content = (getattr(msg, "content", None) or "").strip()
    if content:
        return content
    try:
        dumped = msg.model_dump(exclude_none=True)
    except Exception:
        dumped = msg if isinstance(msg, dict) else {}
    for key in ("reasoning", "thinking", "reasoning_content"):
        val = dumped.get(key) if isinstance(dumped, dict) else None
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def build_consultant_system_prompt(
    max_iterations: int,
    scope_hint: Optional[str] = None,
    mode_guidance: Optional[str] = None,
) -> str:
    """Assemble the consultant system prompt for the agentic tool loop.

    The caller's `scope_hint` (if any) becomes a STRICT cage: "stay within this
    scope, do not read outside it". `mode_guidance` (the per-mode suffix, e.g.
    SCHOLAR's "scope is a starting point, not a cage") is appended OUTSIDE that
    cage. Keeping them separate is the fix for the SCHOLAR self-contradiction:
    folding the mode suffix into scope_hint made it get relabeled "SCOPE
    (strict)" and immediately negated by "Do NOT read outside it".
    """
    scope_block = ""
    if scope_hint and scope_hint.strip():
        scope_block = (
            "\n\nSCOPE (strict):\n"
            f"{scope_hint.strip()}\n"
            "Stay within this scope. Do NOT read, list, or glob paths "
            "outside it unless the scope explicitly allows it.\n"
        )

    mode_block = ""
    if mode_guidance and mode_guidance.strip():
        mode_block = f"\n{mode_guidance.strip()}\n"

    return (
        "You are an independent consultant in an AI council investigating a "
        "question about a codebase that is available to you via tools.\n\n"
        "Your job: investigate the codebase, then produce your OWN complete, "
        "self-contained analysis of the question. There is no synthesizer — "
        "your output IS the final product the orchestrator will read.\n\n"
        f"TOOL BUDGET (strict): You have AT MOST {max_iterations} tool calls "
        f"total. Use them sparingly. Each read_file / list_dir / glob_search "
        f"costs one call. think() also costs one call. If you exhaust the "
        f"budget, you will be forced to answer from whatever you have so far. "
        f"Plan before you read: decide which files matter, read ONLY those, "
        f"then answer. Do NOT read files speculatively.\n\n"
        "TOOL RULES:\n"
        "- Use read_file / list_dir / glob_search to ground your analysis in "
        "the ACTUAL code and docs. Do not make claims you cannot verify.\n"
        "- Use think() to structure your reasoning before the final answer.\n"
        "- When done investigating, emit your final analysis as plain text "
        "with NO tool_calls.\n"
        f"{scope_block}"
        f"{mode_block}\n"
        "Structure your final answer: (1) what you verified in the code, "
        "(2) your findings, (3) your recommendation or verdict, (4) any "
        "caveats or things you could not verify."
    )


class ModelManager:
    """Manages model configurations and API calls."""
    
    def __init__(self, config: Optional[AICouncilConfig] = None, logger: Optional[AICouncilLogger] = None):
        self.logger = logger or AICouncilLogger()
        self.config = config or load_config()
        self._apply_log_level()
        self._validate_api_keys()
    
    def _apply_log_level(self) -> None:
        """Apply log level from configuration to the logger."""
        try:
            log_level = self.config.get_log_level()
            self.logger.set_level(log_level)
        except Exception as e:
            self.logger.warning(f"Failed to apply log level: {e}")
            import logging
            self.logger.set_level(logging.INFO)
    
    def _validate_api_keys(self) -> None:
        """Validate that required API keys are available."""
        if self.config.openai_api_key:
            self.logger.debug("OpenAI API key found")
        else:
            self.logger.warning("No OpenAI API key found")
        
        if self.config.openrouter_api_key:
            self.logger.debug("OpenRouter API key found")
        else:
            self.logger.warning("No OpenRouter API key found")
    
    def _get_client_for_model(self, model_config: ModelConfig) -> AsyncOpenAI:
        """Create an appropriate client for the given model configuration."""
        # Determine which API key to use (priority: model-specific -> provider-specific)
        api_key = ""
        
        if model_config.api_key:
            # prefer to use model-specific API key if provided
            api_key = model_config.api_key
        elif model_config.provider == Provider.CUSTOM:
            # A custom endpoint has no provider-level key to fall back on, so a
            # missing per-model key is fatal. (The `if model_config.api_key`
            # above already handled the present-key case; reaching here means
            # it's absent.) Startup validation normally catches this first.
            raise ValueError(f"API key required for model {model_config.name} using custom endpoint.")
        elif model_config.provider == Provider.OPENAI:
            api_key = self.config.openai_api_key
            if not api_key:
                raise ValueError(f"OpenAI API key required for model {model_config.name}")
        elif model_config.provider == Provider.OPENROUTER:
            api_key = self.config.openrouter_api_key
            if not api_key:
                raise ValueError(f"OpenRouter API key required for model {model_config.name}")
        else:
            raise ValueError(f"Unknown provider: {model_config.provider}")
        
        # Determine base URL
        if model_config.provider == Provider.CUSTOM:
            base_url = model_config.base_url
            if not base_url:
                # Never let a custom model fall back to AsyncOpenAI's default
                # (api.openai.com) — that would ship prompts and the wrong
                # bearer token to OpenAI. Config validation catches this too.
                raise ValueError(
                    f"Custom endpoint model {model_config.name} requires a base_url"
                )
        elif model_config.provider == Provider.OPENROUTER:
            base_url = "https://openrouter.ai/api/v1"
        else:
            # OpenAI uses default base URL (None)
            base_url = None
        

        return AsyncOpenAI(api_key=api_key, base_url=base_url)

    def get_enabled_models(self, limit: bool = True) -> List[ModelConfig]:
        """Enabled models. ``limit=True`` caps to ``max_models`` (default
        fan-out); ``limit=False`` returns the full enabled list for resolving an
        explicit ``models`` subset. See ``Config.get_enabled_models``."""
        return self.config.get_enabled_models(limit=limit)

    @staticmethod
    def _unsupported_param(error: Exception) -> Optional[str]:
        """Return the parameter name an OpenAI-style 400 says it can't accept.

        Newer OpenAI models reject params the older API accepted, two ways:
        `unsupported_parameter` (e.g. `max_tokens` — the API wants
        `max_completion_tokens`) and `unsupported_value` (e.g. reasoning models
        allow only the default `temperature`). Returns the offending param name,
        or None if the error is something else.
        """
        import re
        body = getattr(error, "body", None)
        if isinstance(body, dict):
            err = body.get("error") or {}
            if err.get("code") in ("unsupported_parameter", "unsupported_value") and err.get("param"):
                return err["param"]
        m = re.search(r"'([A-Za-z_]+)'\s+(?:is not supported|does not support)", str(error))
        return m.group(1) if m else None

    # Transient-retry tuning. Attempts beyond the first, exponential base delay,
    # per-attempt jitter, and a hard cap so a large Retry-After can't stall a
    # consultant past its batch timeout. Class attributes so tests can lower them.
    RETRY_MAX_ATTEMPTS = 2
    RETRY_BASE_DELAY = 0.5
    RETRY_JITTER = 0.5
    RETRY_MAX_DELAY = 8.0

    @staticmethod
    def _retry_after_seconds(error: Exception) -> Optional[float]:
        """Parse a numeric ``Retry-After`` header (seconds) if the error has one."""
        response = getattr(error, "response", None)
        headers = getattr(response, "headers", None)
        if not headers:
            return None
        raw = headers.get("retry-after") or headers.get("Retry-After")
        try:
            return float(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    @classmethod
    def _transient_retry_delay(cls, error: Exception, attempt: int) -> Optional[float]:
        """Seconds to wait before retrying a transient error, or None if not retryable.

        ``attempt`` is 0-based (0 = the delay before the first retry). Honors a
        server ``Retry-After`` when present, else exponential backoff + jitter,
        capped at ``RETRY_MAX_DELAY``.
        """
        status = getattr(error, "status_code", None)
        retryable = status in _RETRYABLE_STATUS or type(error).__name__ in _RETRYABLE_EXC_NAMES
        if not retryable:
            return None
        retry_after = cls._retry_after_seconds(error)
        if retry_after is not None:
            return min(retry_after, cls.RETRY_MAX_DELAY)
        backoff = cls.RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, cls.RETRY_JITTER)
        return min(backoff, cls.RETRY_MAX_DELAY)

    async def _create_completion(self, client, model_config: ModelConfig, **kwargs):
        """chat.completions.create with provider-aware params, strip-and-retry
        for unsupported parameters, and bounded backoff on transient failures.

        - OpenAI's newer models renamed the output cap: `max_tokens` becomes
          `max_completion_tokens`. Custom (OpenAI-compatible / Ollama) and
          OpenRouter endpoints keep `max_tokens`, so only OpenAI is rewritten.
        - If the model rejects a parameter (e.g. reasoning models allow only the
          default `temperature`), drop that param and retry immediately. Up to
          three strips before the error propagates.
        - On a transient failure (429 / 5xx / connection / timeout), wait with
          exponential backoff (honoring ``Retry-After``) and retry, up to
          ``RETRY_MAX_ATTEMPTS`` times. Without this, a single rate-limit under
          concurrency permanently drops an otherwise-healthy consultant. The
          batch/consultant timeouts still bound total time, so a retry loop
          can't outlive its window.
        """
        if "max_tokens" in kwargs and model_config.provider == Provider.OPENAI:
            kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
        strips = 0
        retries = 0
        while True:
            try:
                return await client.chat.completions.create(model=model_config.model_id, **kwargs)
            except Exception as error:
                param = self._unsupported_param(error)
                if param and param in kwargs and strips < 3:
                    strips += 1
                    self.logger.warning(
                        f"{model_config.name} rejected '{param}'; retrying without it"
                    )
                    kwargs.pop(param, None)
                    continue
                if retries < self.RETRY_MAX_ATTEMPTS:
                    delay = self._transient_retry_delay(error, retries)
                    if delay is not None:
                        retries += 1
                        self.logger.warning(
                            f"{model_config.name} transient error "
                            f"({type(error).__name__}); retry {retries}/"
                            f"{self.RETRY_MAX_ATTEMPTS} in {delay:.2f}s"
                        )
                        await asyncio.sleep(delay)
                        continue
                raise

    async def call_model(
        self,
        model_config: ModelConfig,
        context: str,
        question: str,
    ) -> ConsultantResult:
        """Make an API call to a specific model.

        Returns a ConsultantResult; `ok` is False on any API error or when the
        model produces no usable content even after a retry nudge.
        """
        start_time = time.time()
        self.logger.debug(f"Calling {model_config.name}...", {
            "model": model_config.name,
            "code_name": model_config.code_name
        })

        try:
            # Input validation
            if not question or not question.strip():
                raise ValueError("Question cannot be empty")

            prompt = f"Context: {context}\n\nQuestion: {question}\n\nPlease provide a detailed, well-reasoned answer."

            # Choose the appropriate client
            client = self._get_client_for_model(model_config)

            # Make the API call with better error handling
            try:
                response = await self._create_completion(
                    client, model_config,
                    messages=[
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.7,
                    max_tokens=8000,
                )

                content = _extract_text(response.choices[0].message)
                if not content:
                    # Ollama thinking models sometimes emit empty content on
                    # the first call and need a nudge to emit the final answer.
                    self.logger.debug(
                        f"Empty content from {model_config.name}; nudging once"
                    )
                    retry = await self._create_completion(
                        client, model_config,
                        messages=[
                            {"role": "user", "content": prompt},
                            {"role": "assistant", "content": ""},
                            {"role": "user", "content": (
                                "Your previous response was empty. Emit your "
                                "final answer now as plain text."
                            )},
                        ],
                        temperature=0.7,
                        max_tokens=8000,
                    )
                    content = _extract_text(retry.choices[0].message)
                    if not content:
                        raise ValueError("Empty response received from model")

            except Exception as api_error:
                # More specific API error handling
                error_msg = str(api_error)
                if "rate_limit" in error_msg.lower():
                    raise ValueError(f"Rate limit exceeded for {model_config.name}")
                elif "auth" in error_msg.lower():
                    raise ValueError(f"Authentication failed for {model_config.name}")
                else:
                    raise ValueError(f"API error for {model_config.name}: {error_msg}")
            
            duration = time.time() - start_time
            
            self.logger.debug(f"Received response from {model_config.name} in {duration:.2f}s", {
                "model": model_config.name,
                "duration": duration,
                "response_length": len(content),
                "response_preview": content[:200] + "..." if len(content) > 200 else content
            })
            
            return ConsultantResult(text=content, ok=True)

        except Exception as e:
            duration = time.time() - start_time
            error_msg = f"Error from {model_config.name}: {str(e)}"
            self.logger.error(f"Error calling {model_config.name}", {
                "model": model_config.name,
                "error": str(e),
                "duration": duration
            })
            return ConsultantResult(text=error_msg, ok=False)

    async def call_model_with_tools(
        self,
        model_config: ModelConfig,
        system_prompt: str,
        user_prompt: str,
        tool_schemas: List[Dict[str, Any]],
        tool_dispatcher,
        max_iterations: int = 8,
        timeout: Optional[int] = None,
    ) -> ConsultantResult:
        """Run a tool-calling loop against a single model.

        The model may emit either final text content (loop ends, returned) or
        one or more `tool_calls` (we execute each via `tool_dispatcher.call`,
        append the results as `tool` role messages, and continue). The loop is
        hard-capped at `max_iterations` rounds of tool calls.

        Args:
            model_config: Synthesizer model to call.
            system_prompt: Strict system prompt governing tool use.
            user_prompt: The synthesis prompt (anonymous responses + question).
            tool_schemas: OpenAI-style tool schemas to advertise to the model.
            tool_dispatcher: object with `.call(name, arguments) -> str`.
            max_iterations: hard cap on tool-call rounds.
            timeout: optional total timeout in seconds.

        Returns:
            A ConsultantResult. `ok` is False on timeout, on any exception, and
            when the model never produces usable text (even after nudges).
        """
        start_time = time.time()
        client = self._get_client_for_model(model_config)
        effective_timeout = timeout or self.config.parallel_timeout

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        # An empty schema list (e.g. from allowed_tools: []) must be sent as an
        # OMITTED tools param, not `tools: []` — strict endpoints reject an empty
        # array and every consultant call fails. None makes the SDK drop the key.
        tools_arg = tool_schemas or None

        iterations = 0
        try:
            async def _loop() -> ConsultantResult:
                nonlocal iterations
                while True:
                    response = await self._create_completion(
                        client, model_config,
                        messages=messages,
                        tools=tools_arg,
                        temperature=0.4,
                        max_tokens=16000,
                    )
                    choice = response.choices[0]
                    msg = choice.message
                    tool_calls = getattr(msg, "tool_calls", None)

                    if not tool_calls:
                        text = _extract_text(msg)
                        if text:
                            return ConsultantResult(text=text, ok=True)
                        # Empty content AND no tool calls: nudge once, then give up.
                        messages.append({
                            "role": "assistant",
                            "content": "",
                        })
                        messages.append({
                            "role": "user",
                            "content": (
                                "Your previous response was empty. Emit your final "
                                "analysis now as plain text. Do not call any tools."
                            ),
                        })
                        retry = await self._create_completion(
                            client, model_config,
                            messages=messages,
                            temperature=0.4,
                            max_tokens=16000,
                        )
                        text = _extract_text(retry.choices[0].message)
                        if text:
                            return ConsultantResult(text=text, ok=True)
                        return ConsultantResult(
                            text="Consultant returned empty content after retry.",
                            ok=False,
                        )

                    iterations += 1
                    if iterations > max_iterations:
                        self.logger.warning(
                            f"Consultant hit max_tool_iterations={max_iterations}; "
                            "forcing final analysis from current context."
                        )
                        force_prompt = (
                            "You have reached the tool-call limit. Stop calling tools "
                            "now and produce your final analysis using only the "
                            "information gathered so far. Emit it as plain text."
                        )
                        messages.append(msg.model_dump(exclude_none=True))
                        # The assistant message still carries unanswered
                        # tool_calls. The OpenAI tool-calling contract requires a
                        # tool response for EACH tool_call_id; strict endpoints
                        # 400 ("must be followed by tool messages…") without
                        # them, which broke the entire budget-exhaustion recovery
                        # path on non-Ollama providers. Answer each pending call
                        # with a stub so the forced-final turn is a valid history.
                        for tc in tool_calls:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": "[tool budget exhausted — call not executed]",
                            })
                        messages.append({"role": "user", "content": force_prompt})
                        final = await self._create_completion(
                            client, model_config,
                            messages=messages,
                            temperature=0.4,
                            max_tokens=16000,
                        )
                        text = _extract_text(final.choices[0].message)
                        if text:
                            return ConsultantResult(text=text, ok=True)
                        # One more nudge without tools advertised
                        messages.append({"role": "assistant", "content": ""})
                        messages.append({
                            "role": "user",
                            "content": "Emit your final analysis now as plain text.",
                        })
                        final2 = await self._create_completion(
                            client, model_config,
                            messages=messages,
                            temperature=0.4,
                            max_tokens=16000,
                            tools=None,
                        )
                        text = _extract_text(final2.choices[0].message)
                        if text:
                            return ConsultantResult(text=text, ok=True)
                        return ConsultantResult(
                            text="Consultant returned empty content after forcing final.",
                            ok=False,
                        )

                    # Append the assistant message carrying the tool_calls
                    messages.append(msg.model_dump(exclude_none=True))

                    # Execute each tool call and append a tool role message
                    for tc in tool_calls:
                        name = tc.function.name
                        try:
                            args = json.loads(tc.function.arguments or "{}")
                        except json.JSONDecodeError:
                            args = {}
                        self.logger.debug(
                            f"Synthesizer tool call #{iterations}: {name} args={args}"
                        )
                        result = await asyncio.to_thread(
                            tool_dispatcher.call, name, args
                        )
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": str(result),
                        })

            return await asyncio.wait_for(_loop(), timeout=effective_timeout)
        except asyncio.TimeoutError:
            self.logger.error(
                f"Synthesizer tool loop timed out after {effective_timeout}s "
                f"({iterations} iterations)"
            )
            return ConsultantResult(
                text="Synthesis timed out during agentic tool loop.", ok=False
            )
        except Exception as e:
            duration = time.time() - start_time
            self.logger.error(
                f"Synthesizer tool loop failed after {duration:.2f}s: {e}"
            )
            return ConsultantResult(text=f"Error during agentic synthesis: {e}", ok=False)
    
    async def _gather_consultants(
        self,
        coros: List[Any],
        models: List[ModelConfig],
        timeout: int,
    ) -> List[ConsultantResult]:
        """Run consultant coroutines concurrently, preserving completed work.

        Uses ``asyncio.wait`` with a timeout rather than
        ``wait_for(gather(...))``: results that already finished are kept, and
        only consultants still running at the deadline are cancelled and
        reported as timed out. ``wait_for`` cancels the entire batch, which
        turned one slow consultant into a total (ALL_MODELS_FAILED) failure.

        Returns one ConsultantResult per model, in the original order.
        """
        tasks = [asyncio.ensure_future(c) for c in coros]
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            # Await the cancellations so the tasks don't leak.
            await asyncio.gather(*pending, return_exceptions=True)
            self.logger.error(
                f"{len(pending)} consultant(s) did not finish within {timeout}s; "
                "keeping the results that completed."
            )

        results: List[ConsultantResult] = []
        for i, task in enumerate(tasks):
            if task in done:
                exc = task.exception()
                if exc is not None:
                    results.append(ConsultantResult(
                        text=f"Error for model {models[i].name}: {exc}", ok=False))
                else:
                    results.append(task.result())
            else:
                results.append(ConsultantResult(
                    text=f"Timeout: {models[i].name} did not finish within {timeout}s",
                    ok=False))
        return results

    async def call_models_parallel(
        self,
        models: List[ModelConfig],
        context: str,
        question: str
    ) -> List[ConsultantResult]:
        """Call multiple models in parallel, keeping whatever completed."""
        if not models:
            raise ValueError("No models provided for parallel calls")

        coros = [self.call_model(model, context, question) for model in models]
        return await self._gather_consultants(coros, models, self.config.parallel_timeout)

    async def call_models_parallel_agentic(
        self,
        models: List[ModelConfig],
        context: str,
        question: str,
        tool_schemas: List[Dict[str, Any]],
        tool_registries: List[Any],
        max_iterations: int = 8,
        scope_hint: Optional[str] = None,
        mode_guidance: Optional[str] = None,
    ) -> List[ConsultantResult]:
        """Run a tool-calling loop for every consultant, concurrency-capped.

        Each consultant runs its own read-only investigation of the workspace
        then emits its final analysis. Concurrency is capped by
        `config.max_concurrent_consultants` via an asyncio.Semaphore —
        consultants beyond the cap queue and run as slots free up. This keeps
        us within Ollama Cloud plan limits (Pro = 3, Max = 10, etc.).

        Args:
            scope_hint: optional natural-language scope restriction from the
                CALLER (e.g. "Only read these files: A, B. Do not wander to
                other files."). It is wrapped in a strict "stay within scope"
                cage. When None, no scope is enforced beyond the sandbox.
            mode_guidance: optional per-mode prompt suffix (e.g. the SCHOLAR
                "scope is a starting point, not a cage" text). Appended OUTSIDE
                the strict scope cage so it does not get relabeled as a strict
                restriction and contradict itself.

        Returns:
            One final-analysis string per model, same order as `models`.
        """
        if not models:
            raise ValueError("No models provided for parallel agentic calls")
        if len(tool_registries) != len(models):
            raise ValueError("tool_registries must match models length")

        timeout = self.config.parallel_timeout
        max_concurrent = self.config.max_concurrent_consultants
        semaphore = asyncio.Semaphore(max_concurrent)
        self.logger.info(
            f"Agentic batch: {len(models)} consultants, "
            f"max_concurrent={max_concurrent}, max_iter={max_iterations}"
        )

        consultant_system_prompt = build_consultant_system_prompt(
            max_iterations, scope_hint, mode_guidance
        )
        user_prompt = (
            f"Context:\n{context}\n\n"
            f"Question:\n{question}\n\n"
            "Investigate the codebase as needed (within your tool budget and "
            "scope), then give your complete independent analysis."
        )

        async def _run_one(model, registry):
            async with semaphore:
                self.logger.debug(
                    f"Starting consultant {model.name} "
                    f"(semaphore slot acquired)"
                )
                return await self.call_model_with_tools(
                    model,
                    system_prompt=consultant_system_prompt,
                    user_prompt=user_prompt,
                    tool_schemas=tool_schemas,
                    tool_dispatcher=registry,
                    max_iterations=max_iterations,
                )

        coros = [
            _run_one(model, registry)
            for model, registry in zip(models, tool_registries)
        ]
        # Keep whatever finished: a single slow consultant no longer cancels
        # the batch and collapses a partial success into ALL_MODELS_FAILED.
        return await self._gather_consultants(coros, models, timeout) 