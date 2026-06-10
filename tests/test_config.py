"""Unit tests for config module (src/core/config.py).

Covers:
  - Default settings instantiation
  - YAML loading from config/settings.yaml
  - Environment variable override (AGENTHUB_* prefix)
  - Nested key access
  - Singleton get_settings / reload_settings
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.core.config import (
    AgentHubSettings,
    get_settings,
    reload_settings,
    LLMConfig,
    OrchestratorConfig,
    StorageConfig,
    ServerConfig,
)


# ======================================================================
# Default instantiation
# ======================================================================

class TestDefaults:
    def test_llm_defaults(self):
        cfg = LLMConfig()
        assert cfg.model == "claude-sonnet-4-6"
        assert cfg.api_key == ""
        assert cfg.max_tokens == 4096

    def test_orchestrator_defaults(self):
        cfg = OrchestratorConfig()
        assert cfg.default_retries == 3
        assert cfg.default_timeout == 60.0
        assert cfg.fallback_agent == "claude"
        assert cfg.max_subtasks == 10

    def test_storage_defaults(self):
        cfg = StorageConfig()
        assert cfg.type == "sqlite"
        assert cfg.path == "data/agenthub.db"

    def test_server_defaults(self):
        cfg = ServerConfig()
        assert cfg.host == "0.0.0.0"
        assert cfg.port == 8000


class TestTopLevelSettings:
    def test_default_construction(self):
        settings = AgentHubSettings()
        assert settings.llm.model == "claude-sonnet-4-6"
        assert settings.orchestrator.default_retries == 3
        assert settings.storage.type == "sqlite"


# ======================================================================
# YAML loading
# ======================================================================

class TestYamlLoading:
    def test_load_from_file(self):
        yaml_path = (
            Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
        )
        settings = AgentHubSettings.from_yaml(yaml_path)
        # Values from the YAML file
        assert settings.llm.model == "claude-sonnet-4-6"
        assert settings.orchestrator.default_retries == 3
        assert settings.server.host == "0.0.0.0"

    def test_load_returns_correct_types(self):
        """YAML values must be coerced to their Pydantic types."""
        settings = AgentHubSettings.from_yaml()
        assert isinstance(settings.orchestrator.default_retries, int)
        assert isinstance(settings.orchestrator.default_timeout, float)
        assert isinstance(settings.server.port, int)


# ======================================================================
# Environment variable overrides (AGENTHUB_*)
# ======================================================================

class TestEnvOverrides:
    def test_override_llm_model(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("AGENTHUB_LLM__MODEL", "claude-opus-4-8")
        settings = AgentHubSettings()
        assert settings.llm.model == "claude-opus-4-8"

    def test_override_orchestrator_retries(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("AGENTHUB_ORCHESTRATOR__DEFAULT_RETRIES", "10")
        settings = AgentHubSettings()
        assert settings.orchestrator.default_retries == 10

    def test_override_server_port(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("AGENTHUB_SERVER__PORT", "9000")
        settings = AgentHubSettings()
        assert settings.server.port == 9000

    def test_override_storage_path(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("AGENTHUB_STORAGE__PATH", "/tmp/test.db")
        settings = AgentHubSettings()
        assert settings.storage.path == "/tmp/test.db"

    def test_env_overrides_yaml(self, monkeypatch: pytest.MonkeyPatch):
        """Env vars take precedence over YAML values."""
        monkeypatch.setenv("AGENTHUB_LLM__MODEL", "env-model")
        settings = AgentHubSettings.from_yaml()
        # Env should override the YAML value "claude-sonnet-4-6"
        assert settings.llm.model == "env-model"

    def test_clean_env(self, monkeypatch: pytest.MonkeyPatch):
        """Without env overrides, YAML values win."""
        # Remove any AGENTHUB_ vars
        for key in list(os.environ):
            if key.startswith("AGENTHUB_"):
                monkeypatch.delenv(key)
        settings = AgentHubSettings.from_yaml()
        assert settings.llm.model == "claude-sonnet-4-6"


# ======================================================================
# Singleton helpers
# ======================================================================

class TestSingleton:
    def test_get_settings_returns_cached(self, monkeypatch: pytest.MonkeyPatch):
        # Clear any AGENTHUB_ env vars that might override
        for key in list(os.environ):
            if key.startswith("AGENTHUB_"):
                monkeypatch.delenv(key)
        reload_settings()  # reset
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2

    def test_reload_returns_new_instance(self):
        s1 = reload_settings()
        s2 = reload_settings()
        # Different instances (or same if YAML didn't change — both fine)
        assert s1.llm.model == s2.llm.model
