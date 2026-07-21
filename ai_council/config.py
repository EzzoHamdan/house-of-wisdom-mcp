"""
Pydantic-based configuration system for AI Council.

Follows Pydantic v2 best practices with proper BaseSettings usage.
"""

import logging
import os
import re
from enum import Enum
from pathlib import Path
from typing import List, Optional

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Matches ${ENV_VAR} placeholders in config string fields. The name must be a
# valid shell-style identifier (letters, digits, underscore; not starting with
# a digit) so a literal key that merely contains a stray "$" is left untouched.
_ENV_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _resolve_env_placeholders(value: Optional[str], field_name: str) -> Optional[str]:
    """Expand ``${ENV_VAR}`` placeholders in a config string from the environment.

    Lets secrets stay out of the YAML entirely — e.g. ``api_key: "${DEEPSEEK_API_KEY}"``
    resolves at load time, so the file an agent reads or edits holds no real key.

    A referenced-but-unset variable raises ValueError naming it, so a typo fails
    loudly at startup instead of sending an empty key to a provider. Values with
    no ``${...}`` (including None and real keys) pass through unchanged.
    """
    if not isinstance(value, str) or "${" not in value:
        return value

    missing: List[str] = []

    def _sub(match: "re.Match[str]") -> str:
        var = match.group(1)
        env_val = os.environ.get(var)
        if env_val is None:
            missing.append(var)
            return match.group(0)
        return env_val

    resolved = _ENV_PLACEHOLDER.sub(_sub, value)
    if missing:
        joined = ", ".join(f"${{{v}}}" for v in missing)
        raise ValueError(
            f"{field_name} references environment variable(s) {joined} that are "
            "not set. Export them (or pass the key via the AI_COUNCIL_-prefixed "
            "env var / a .env file) before starting the server."
        )
    return resolved


class Provider(str, Enum):
    """Supported AI providers."""
    OPENAI = "openai"
    OPENROUTER = "openrouter"
    CUSTOM = "custom"


class LogLevel(str, Enum):
    """Supported log levels."""
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class ModelConfig(BaseModel):
    """Configuration for a single AI model."""
    name: str = Field(..., min_length=1, description="Human-readable name of the model")
    model_id: str = Field(..., min_length=1, description="Provider-specific model identifier")
    provider: Provider = Field(default=Provider.OPENROUTER, description="AI provider")
    base_url: Optional[str] = Field(default=None, description="Custom OpenAI-compatible API base URL (overrides provider default)")
    api_key: Optional[str] = Field(default=None, description="API key for this specific model (overrides global keys)")
    code_name: Optional[str] = Field(default=None, description="Anonymous code name for bias reduction (auto-assigned if not provided)")
    enabled: bool = Field(default=True, description="Whether this model is enabled")

    @field_validator("api_key", "base_url")
    @classmethod
    def _expand_env(cls, value, info):
        """Resolve ${ENV_VAR} placeholders so per-model keys can stay out of the YAML."""
        return _resolve_env_placeholders(value, info.field_name)


# Default code names for anonymous model identification
DEFAULT_CODE_NAMES = [
    "Alpha", "Beta", "Gamma", "Delta", "Epsilon", 
    "Zeta", "Eta", "Theta", "Iota", "Kappa"
]


class SynthesizerToolsConfig(BaseModel):
    """Configuration for the agentic consultant tool loop (v0.4.0+).

    When enabled, every consultant model gets its own read-only tool-calling
    loop so it can ground its answer in the actual codebase. There is no
    synthesizer — each consultant returns its complete, independent analysis
    and the orchestrator (the MCP client) weighs them itself.

    Retains the name SynthesizerToolsConfig for backward-compat with v0.3.0
    configs; the field semantics have shifted to apply to ALL consultants.
    """
    enabled: bool = Field(default=False, description="Enable the agentic tool loop for all consultants")
    workspace_root: Optional[str] = Field(
        default=None,
        description="Sandbox root the consultants can read. If None, defaults to the process cwd at call time."
    )
    max_tool_iterations: int = Field(
        default=8,
        ge=1,
        le=128,
        description="Hard cap on tool-call rounds per consultant before forcing a final answer"
    )
    allowed_tools: List[str] = Field(
        default_factory=lambda: ["read_file", "list_dir", "glob_search", "think"],
        description="Subset of tools each consultant may call"
    )

    # Scholar mode: liberated budget for free inquiry. The translator budget
    # (max_tool_iterations) still applies to translator-mode calls; this only
    # overrides it when the caller picks scholar mode.
    scholar_max_tool_iterations: int = Field(
        default=64,
        ge=1,
        le=256,
        description="Tool-call cap for SCHOLAR mode. Liberated budget for free inquiry."
    )


class AICouncilConfig(BaseSettings):
    """Main configuration class for AI Council using BaseSettings for environment support."""
    
    model_config = SettingsConfigDict(
        env_prefix="AI_COUNCIL_",
        case_sensitive=False,
        extra="ignore"
    )

    # API Keys. No alias: an explicit alias makes pydantic-settings skip the
    # env_prefix, which silently read a bare ambient OPENAI_API_KEY instead of
    # the documented AI_COUNCIL_OPENAI_API_KEY. Without an alias the env_prefix
    # applies, so these are read from AI_COUNCIL_OPENAI_API_KEY /
    # AI_COUNCIL_OPENROUTER_API_KEY (and still from YAML / CLI by field name).
    openai_api_key: Optional[str] = Field(default=None, description="OpenAI API key")
    openrouter_api_key: Optional[str] = Field(default=None, description="OpenRouter API key")

    @field_validator("openai_api_key", "openrouter_api_key")
    @classmethod
    def _expand_env_keys(cls, value, info):
        """Resolve ${ENV_VAR} placeholders in provider-level keys set via YAML."""
        return _resolve_env_placeholders(value, info.field_name)

    # Settings with validation
    max_models: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum number of models to consult simultaneously"
    )
    parallel_timeout: int = Field(
        default=60,
        ge=5,
        le=600,
        description="Timeout for parallel API calls in seconds"
    )
    log_level: LogLevel = Field(
        default=LogLevel.INFO,
        description="Logging level"
    )

    # Models configuration
    models: List[ModelConfig] = Field(
        default_factory=list,
        description="List of AI models to use"
    )

    # Agentic consultant configuration (v0.4.0+: all consultants get tools; no synthesizer)
    synthesizer_tools: SynthesizerToolsConfig = Field(
        default_factory=SynthesizerToolsConfig,
        description="Configuration for the consultants' read-only tool loop"
    )

    # v0.4.0: when true, returned perspectives are labeled Alpha/Beta/Gamma
    # instead of real model names. Default false = labeled by model name.
    anonymous_perspectives: bool = Field(
        default=False,
        description="If true, label returned perspectives with code names (Alpha/Beta/Gamma) instead of real model names"
    )

    # v0.4.1: caps how many consultants run their tool loops concurrently.
    # Set to 1 for fully sequential (low-concurrency plans / weak hardware).
    # Set to 3 for Ollama Pro (3 concurrent cloud models). Set to 10 for Max.
    # Consultants beyond this limit queue and run as slots free up.
    max_concurrent_consultants: int = Field(
        default=3,
        ge=1,
        le=32,
        description="Max consultants running tool loops concurrently (asyncio.Semaphore)"
    )

    def model_post_init(self, __context) -> None:
        """Post-initialization validation and setup."""
        # Set default models if none provided
        if not self.models:
            self.models = self._get_default_models()
        
        # Validate model count limit
        if len(self.models) > 10:
            raise ValueError(f"Cannot configure more than 10 models (found {len(self.models)})")
        
        # Auto-assign code names if not provided
        self._assign_code_names()
        
        # Validate unique code names
        code_names = [model.code_name for model in self.models if model.code_name]
        if len(code_names) != len(set(code_names)):
            raise ValueError("Duplicate code names found in model configuration")
        
        # Ensure at least two enabled models
        enabled_models = [model for model in self.models if model.enabled]
        if len(enabled_models) < 2:
            raise ValueError("At least two models must be enabled")
        
        self._validate_api_key_requirements(enabled_models)

    def _assign_code_names(self) -> None:
        """Auto-assign code names to models that don't have them.

        Names already claimed by an explicit ``code_name`` are removed from the
        pool, then the remaining names are handed out to unassigned models in
        order. The pool is walked by its own iterator — indexing it by each
        model's absolute position overran the shrunken list (IndexError at 10
        models with any explicit name) and skipped names in the normal case.
        """
        taken = {model.code_name for model in self.models if model.code_name}
        pool = iter(name for name in DEFAULT_CODE_NAMES if name not in taken)

        for model in self.models:
            if not model.code_name:
                try:
                    model.code_name = next(pool)
                except StopIteration:
                    raise ValueError(
                        "Ran out of default code names; set code_name explicitly "
                        f"on each model (only {len(DEFAULT_CODE_NAMES)} defaults exist)."
                    )

    def _get_default_models(self) -> List[ModelConfig]:
        """Get default model configuration for uvx usage."""
        return [
            ModelConfig(
                name="claude-sonnet-4",
                provider=Provider.OPENROUTER,
                model_id="anthropic/claude-sonnet-4",
                enabled=True
            ),
            ModelConfig(
                name="gemini-2.5-pro",
                provider=Provider.OPENROUTER,
                model_id="google/gemini-2.5-pro",
                enabled=True
            ),
            ModelConfig(
                name="deepseek-chat-v3",
                provider=Provider.OPENROUTER,
                model_id="deepseek/deepseek-chat-v3-0324",
                enabled=True
            )
        ]

    def get_enabled_models(self, limit: bool = True) -> List[ModelConfig]:
        """Enabled models.

        ``limit=True`` (default) truncates to ``max_models`` — the DEFAULT
        fan-out budget, i.e. how many models fire when the caller names none.
        ``limit=False`` returns the full enabled list and is used to resolve an
        explicit ``models`` subset: an explicitly named, enabled model must be
        reachable regardless of file order or ``max_models``. Truncating before
        the explicit filter would make an enabled model past position N silently
        unrequestable.
        """
        enabled = [model for model in self.models if model.enabled]
        return enabled[:self.max_models] if limit else enabled

    def get_log_level(self) -> int:
        """Get logging level as integer constant."""
        return getattr(logging, self.log_level.value)

    def _validate_api_key_requirements(self, enabled_models: List["ModelConfig"]) -> None:
        """Validate endpoint structure and API-key availability for enabled models."""

        for model in enabled_models:
            # Structural requirement: a custom endpoint must say WHERE to connect.
            # This is checked independently of the api_key — every custom model
            # has a key by definition, so folding it behind an api_key check made
            # it unreachable and let a base_url-less custom model fall through to
            # AsyncOpenAI's default (api.openai.com).
            if model.provider == Provider.CUSTOM and not model.base_url:
                raise ValueError(
                    f"Custom endpoint '{model.name}' requires a base_url"
                )

            # Key-presence checks: a per-model api_key satisfies any provider.
            if model.api_key:
                continue
            if model.provider == Provider.CUSTOM:
                raise ValueError(
                    f"Custom endpoint '{model.name}' requires an api_key"
                )
            elif model.provider == Provider.OPENAI and not self.openai_api_key:
                raise ValueError("OpenAI API key is required if using OpenAI models")
            elif model.provider == Provider.OPENROUTER and not self.openrouter_api_key:
                raise ValueError("OpenRouter API key is required if using OpenRouter models")

def _load_dotenv_files(config_file: Optional[str]) -> None:
    """Load ``KEY=VALUE`` pairs from .env files into ``os.environ``.

    Looked up in two places so an MCP server launched from an unpredictable
    working directory still finds keys placed beside its config:

      1. a ``.env`` next to the resolved config file
      2. a ``.env`` in the current working directory

    Real environment variables always win — a value already present in
    ``os.environ`` (e.g. from the MCP client's ``env`` block) is never
    overwritten. A malformed .env is skipped rather than crashing startup;
    keys can still come from the environment or the YAML.
    """
    candidates: List[Path] = []
    if config_file:
        candidates.append(Path(config_file).resolve().parent / ".env")
    candidates.append(Path.cwd() / ".env")

    seen: set = set()
    for path in candidates:
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        try:
            lines = path.read_text().splitlines()
        except Exception:
            continue
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if value[:1] in ("\"", "'"):
                # Quoted value: keep what's between the opening quote and its
                # matching close; ignore anything after (e.g. a trailing
                # comment). A '#' inside the quotes stays literal.
                quote = value[0]
                close = value.find(quote, 1)
                value = value[1:close] if close != -1 else value[1:]
            else:
                # Unquoted value: an inline " #" starts a comment, matching how
                # common dotenv parsers behave. Without this, `KEY=v # note`
                # loads the value as "v # note" and is sent to the provider.
                hash_idx = value.find(" #")
                if hash_idx != -1:
                    value = value[:hash_idx].rstrip()
            if key and key not in os.environ:
                os.environ[key] = value


def load_config(
    config_file: Optional[str] = None,
    **overrides
) -> AICouncilConfig:
    """
    Load configuration from file and environment with overrides.

    Args:
        config_file: Optional path to YAML config file
        **overrides: Direct field overrides

    Returns:
        AICouncilConfig instance
    """
    # Find config file if not specified
    if config_file is None:
        default_path = Path.home() / ".config" / "ai-council" / "config.yaml"
        if default_path.exists():
            config_file = str(default_path)

    # Populate the environment from .env files BEFORE constructing the settings,
    # so AI_COUNCIL_-prefixed keys and any ${ENV_VAR} placeholders can resolve.
    _load_dotenv_files(config_file)

    # Load from YAML file if it exists
    yaml_data = {}
    if config_file and Path(config_file).exists():
        try:
            with open(config_file, 'r') as f:
                yaml_data = yaml.safe_load(f) or {}
        except Exception as e:
            raise ValueError(f"Failed to load config file {config_file}: {e}")
    
    # Merge YAML data with overrides (overrides take precedence)
    config_data = {**yaml_data, **overrides}

    return AICouncilConfig(**config_data)