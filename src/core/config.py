"""AgentHub configuration via pydantic-settings.

All settings can be overridden by AGENTHUB_* environment variables.
Nested keys use double-underscore: AGENTHUB_LLM__API_KEY=sk-xxx
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Nested config models
# ---------------------------------------------------------------------------

class LLMConfig(BaseModel):
    api_key: str = ""
    model: str = "claude-sonnet-4-6"
    base_url: str = ""
    max_tokens: int = 4096
    temperature: float = 0.7


class AgentEntry(BaseModel):
    enabled: bool = True
    cli_path: str = "claude"
    max_concurrency: int = 3


class AgentsConfig(BaseModel):
    claude: AgentEntry = Field(default_factory=AgentEntry)
    codex: AgentEntry = Field(
        default_factory=lambda: AgentEntry(enabled=False, cli_path="codex", max_concurrency=2)
    )


class OrchestratorConfig(BaseModel):
    default_retries: int = 3
    default_timeout: float = 60.0
    fallback_agent: str = "claude"
    max_subtasks: int = 10


class StorageConfig(BaseModel):
    type: Literal["sqlite", "json", "postgresql"] = "sqlite"
    path: str = "data/agenthub.db"
    chat_history_limit: int = 200


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:8501", "http://localhost:8080"]
    )
    log_level: Literal["debug", "info", "warning", "error"] = "info"


class DiffToolConfig(BaseModel):
    max_file_size: int = 1_048_576


class PreviewToolConfig(BaseModel):
    sandbox_port_range: tuple[int, int] = (9000, 9100)


class DeployToolConfig(BaseModel):
    docker_compose_path: str = "docker/docker-compose.yml"


class ToolsConfig(BaseModel):
    diff: DiffToolConfig = Field(default_factory=DiffToolConfig)
    preview: PreviewToolConfig = Field(default_factory=PreviewToolConfig)
    deploy: DeployToolConfig = Field(default_factory=DeployToolConfig)


# ---------------------------------------------------------------------------
# Top-level settings
# ---------------------------------------------------------------------------

class AgentHubSettings(BaseSettings):
    """Root settings, loaded from YAML + env overrides.

    Environment variables use the AGENTHUB_ prefix.
    Nested fields are separated by __ (double underscore).

    Examples:
        AGENTHUB_LLM__API_KEY=sk-xxx
        AGENTHUB_SERVER__PORT=9000
        AGENTHUB_ORCHESTRATOR__DEFAULT_RETRIES=5
    """

    model_config = SettingsConfigDict(
        env_prefix="AGENTHUB_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm: LLMConfig = Field(default_factory=LLMConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)

    # ------------------------------------------------------------------
    # Factory: load from YAML then overlay env
    # ------------------------------------------------------------------

    @classmethod
    def from_yaml(cls, yaml_path: str | Path | None = None) -> "AgentHubSettings":
        """Load settings from a YAML file first, then override from env.

        Args:
            yaml_path: Path to settings.yaml. Defaults to ``config/settings.yaml``
                       relative to the project root (two levels up from this file).

        Returns:
            AgentHubSettings with YAML values as defaults, env overrides applied.
        """
        if yaml_path is None:
            # Default: config/settings.yaml relative to project root
            yaml_path = Path(__file__).resolve().parent.parent.parent / "config" / "settings.yaml"
        else:
            yaml_path = Path(yaml_path)

        init_kwargs: dict = {}
        if yaml_path.exists():
            with open(yaml_path, "r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
            init_kwargs = cls._flatten_yaml(raw)

        # Create instance — pydantic-settings will overlay AGENTHUB_* env vars
        return cls(**init_kwargs)

    @staticmethod
    def _flatten_yaml(raw: dict, prefix: str = "") -> dict:
        """Flatten a nested YAML dict into pydantic nested-model kwargs."""
        result: dict = {}
        for key, value in raw.items():
            full_key = f"{prefix}__{key}" if prefix else key
            if isinstance(value, dict) and not any(
                isinstance(value, t) for t in (list, tuple)
            ):
                result.update(AgentHubSettings._flatten_yaml(value, full_key))
            else:
                result[full_key] = value
        return result


# ---------------------------------------------------------------------------
# Singleton helper
# ---------------------------------------------------------------------------

_settings: AgentHubSettings | None = None


def get_settings(yaml_path: str | Path | None = None) -> AgentHubSettings:
    """Return the cached global settings instance, creating it if needed."""
    global _settings
    if _settings is None:
        _settings = AgentHubSettings.from_yaml(yaml_path)
    return _settings


def reload_settings(yaml_path: str | Path | None = None) -> AgentHubSettings:
    """Force-reload settings (useful in tests)."""
    global _settings
    _settings = AgentHubSettings.from_yaml(yaml_path)
    return _settings
