from __future__ import annotations
import os
from dataclasses import dataclass, field
from pathlib import Path
import yaml
from platformdirs import user_config_dir

CONFIG_PATH = Path(user_config_dir('fluxen')) / 'config.yaml'

@dataclass
class Model:
    name: str
    model: str
    provider: str
    api_key_env: str | None = None
    api_base: str | None = None
    api_version: str | None = None
    context_tokens: int = 32768

    def options(self) -> dict:
        data: dict = {'model': self.model, 'timeout': 90, 'num_retries': 2}
        if self.api_key_env:
            key = os.getenv(self.api_key_env)
            if not key and self.provider != 'ollama':
                raise ValueError(f'Missing {self.api_key_env}; set it in your environment')
            if key: data['api_key'] = key
        if self.api_base: data['api_base'] = self.api_base
        if self.api_version: data['api_version'] = self.api_version
        return data

DEFAULT_MODELS = [
    Model('gpt', 'openai/gpt-4.1', 'openai', 'OPENAI_API_KEY'),
    Model('claude', 'anthropic/claude-sonnet-4-5-20250929', 'anthropic', 'ANTHROPIC_API_KEY'),
    Model('azure', 'azure/my-deployment', 'azure', 'AZURE_API_KEY', api_version='2024-10-21'),
    Model('gemini', 'gemini/gemini-2.5-pro', 'gemini', 'GEMINI_API_KEY'),
    Model('openrouter', 'openrouter/anthropic/claude-sonnet-4', 'openrouter', 'OPENROUTER_API_KEY'),
    Model('ollama', 'ollama_chat/qwen2.5-coder:7b', 'ollama', api_base='http://localhost:11434'),
    Model('custom', 'openai/my-model', 'compatible', 'CUSTOM_API_KEY', api_base='http://localhost:8000/v1'),
    Model('foundry', 'azure_ai/my-model', 'foundry', 'AZURE_AI_API_KEY'),
]

@dataclass
class Settings:
    models: list[Model] = field(default_factory=lambda: list(DEFAULT_MODELS))
    default_model: str = 'gpt'
    permission: str = 'ask'  # ask | read-only | auto-edit
    max_steps: int = 12
    max_output_chars: int = 12000

    def model(self, name: str | None = None) -> Model:
        desired = name or self.default_model
        for m in self.models:
            if m.name == desired: return m
        raise ValueError(f'Unknown model {desired}. Available: {", ".join(m.name for m in self.models)}')

def load(path: Path = CONFIG_PATH) -> Settings:
    if not path.exists(): return Settings()
    raw = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    models = [Model(**m) for m in raw.get('models', [vars(m) for m in DEFAULT_MODELS])]
    permission = raw.get('permission', 'ask')
    if permission not in ('ask', 'read-only', 'auto-edit'): raise ValueError('Invalid permission mode')
    return Settings(models=models, default_model=raw.get('default_model', 'gpt'),
                    permission=permission, max_steps=max(1, min(40, int(raw.get('max_steps',12)))),
                    max_output_chars=max(1000,int(raw.get('max_output_chars',12000))))

def init_config(path: Path = CONFIG_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        text = '# Environment variable names only; do not store credentials here.\n'
        text += yaml.safe_dump({'default_model':'gpt', 'permission':'ask', 'max_steps':12,
                                'models':[vars(m) for m in DEFAULT_MODELS]}, sort_keys=False)
        path.write_text(text,encoding='utf-8')
        try: path.chmod(0o600)
        except OSError: pass
    return path
