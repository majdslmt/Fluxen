"""YAML model profiles and environment-based credentials."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml
from platformdirs import user_config_dir

CONFIG_PATH = Path(user_config_dir("fluxen")) / "config.yaml"
PERMISSIONS = ("ask", "read-only", "auto-edit")
FALLBACK_CONTEXT_TOKENS = 32768


class ConfigError(ValueError):
    """The configuration file is missing required values or has invalid ones."""


@dataclass
class Model:
    name: str
    model: str
    provider: str
    api_key_env: str | None = None
    api_base: str | None = None
    api_version: str | None = None
    # Leave unset to use LiteLLM's published limit for the model.
    context_tokens: int | None = None
    timeout: int = 90

    def options(self) -> dict:
        data: dict = {"model": self.model, "timeout": self.timeout, "num_retries": 2}
        if self.api_key_env:
            key = os.getenv(self.api_key_env)
            if not key and self.provider != "ollama":
                raise ValueError(f"Missing {self.api_key_env}; set it in your environment")
            if key:
                data["api_key"] = key
        if self.api_base:
            data["api_base"] = self.api_base
        if self.api_version:
            data["api_version"] = self.api_version
        return data

    def context_window(self) -> int:
        """Input token limit: the configured value, else LiteLLM's model info, else a safe default."""
        if self.context_tokens:
            return self.context_tokens
        try:
            from litellm import get_model_info

            limit = get_model_info(self.model).get("max_input_tokens")
        except Exception:
            limit = None
        return int(limit) if limit else FALLBACK_CONTEXT_TOKENS

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self) if getattr(self, f.name) is not None}


MODEL_KEYS = {f.name for f in fields(Model)}

DEFAULT_MODELS = [
    Model("gpt", "openai/gpt-4.1", "openai", "OPENAI_API_KEY"),
    Model("claude", "anthropic/claude-sonnet-5-5", "anthropic", "ANTHROPIC_API_KEY"),
    Model("azure", "azure/my-deployment", "azure", "AZURE_API_KEY", api_version="2024-10-21"),
    Model("gemini", "gemini/gemini-2.5-pro", "gemini", "GEMINI_API_KEY"),
    Model("openrouter", "openrouter/anthropic/claude-sonnet-4", "openrouter", "OPENROUTER_API_KEY"),
    Model("ollama", "ollama_chat/qwen2.5-coder:7b", "ollama", api_base="http://localhost:11434"),
    Model("custom", "openai/my-model", "compatible", "CUSTOM_API_KEY", api_base="http://localhost:8000/v1"),
    Model("foundry", "azure_ai/my-model", "foundry", "AZURE_AI_API_KEY"),
]


@dataclass
class Settings:
    models: list[Model] = field(default_factory=lambda: list(DEFAULT_MODELS))
    default_model: str = "gpt"
    permission: str = "ask"  # ask | read-only | auto-edit
    max_steps: int = 12
    max_output_chars: int = 12000
    shell_timeout: int = 120

    def model(self, name: str | None = None) -> Model:
        desired = name or self.default_model
        for m in self.models:
            if m.name == desired:
                return m
        raise ValueError(f"Unknown model {desired}. Available: {', '.join(m.name for m in self.models)}")


def _model(raw: object, index: int) -> Model:
    if not isinstance(raw, dict):
        raise ConfigError(f"models[{index}] must be a mapping")
    unknown = set(raw) - MODEL_KEYS
    if unknown:
        raise ConfigError(
            f"models[{index}] has unknown key(s): {', '.join(sorted(map(str, unknown)))}. "
            f"Allowed: {', '.join(sorted(MODEL_KEYS))}"
        )
    missing = [key for key in ("name", "model", "provider") if not raw.get(key)]
    if missing:
        raise ConfigError(f"models[{index}] is missing: {', '.join(missing)}")
    try:
        return Model(**raw)
    except TypeError as exc:
        raise ConfigError(f"models[{index}]: {exc}") from None


def _bounded(raw: dict, key: str, default: int, low: int, high: int | None = None) -> int:
    try:
        value = int(raw.get(key, default))
    except (TypeError, ValueError):
        raise ConfigError(f"{key} must be an integer") from None
    value = max(low, value)
    return min(high, value) if high is not None else value


def load(path: Path = CONFIG_PATH) -> Settings:
    if not path.exists():
        return Settings()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    try:
        entries = raw.get("models")
        if entries is None:
            models = list(DEFAULT_MODELS)
        elif isinstance(entries, list) and entries:
            models = [_model(entry, i) for i, entry in enumerate(entries)]
        else:
            raise ConfigError("models must be a non-empty list")
        names = [m.name for m in models]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ConfigError(f"duplicate model name(s): {', '.join(duplicates)}")
        permission = raw.get("permission", "ask")
        if permission not in PERMISSIONS:
            raise ConfigError(f"permission must be one of: {', '.join(PERMISSIONS)}")
        default_model = str(raw.get("default_model", names[0] if "gpt" not in names else "gpt"))
        if default_model not in names:
            raise ConfigError(f"default_model '{default_model}' is not defined. Available: {', '.join(names)}")
        return Settings(
            models=models,
            default_model=default_model,
            permission=permission,
            max_steps=_bounded(raw, "max_steps", 12, 1, 40),
            max_output_chars=_bounded(raw, "max_output_chars", 12000, 1000),
            shell_timeout=_bounded(raw, "shell_timeout", 120, 1, 3600),
        )
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from None


def init_config(path: Path = CONFIG_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        text = "# Environment variable names only; do not store credentials here.\n"
        text += yaml.safe_dump(
            {
                "default_model": "gpt",
                "permission": "ask",
                "max_steps": 12,
                "shell_timeout": 120,
                "models": [m.to_dict() for m in DEFAULT_MODELS],
            },
            sort_keys=False,
        )
        path.write_text(text, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return path
