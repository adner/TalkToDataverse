"""Typed, non-secret runtime configuration loaded from ``config/sot.toml``."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DecisionPoolConfig(BaseModel):
    """One Decision-1 deployment; each has its own requests-per-minute quota."""

    model_config = ConfigDict(extra="forbid")

    endpoint: str
    key_file: str
    deployment: str = "Decision-1"


class IntentConfig(BaseModel):
    """L1: the per-word Decision-1 loop."""

    model_config = ConfigDict(extra="forbid")

    decision_pools: list[DecisionPoolConfig] = Field(default_factory=list)
    decision_rpm: float = Field(default=54.0, gt=0, le=10_000)
    complete_threshold: float = Field(default=0.7, ge=0, le=1)
    write_pause_ms: int = Field(default=1_000, ge=0, le=10_000)
    schema_threshold: float = Field(default=0.5, ge=0, le=1)
    ui_threshold: float = Field(default=0.7, ge=0, le=1)
    reserve_ms: int = Field(default=300, ge=0, le=5_000)
    settle_ms: int = Field(default=150, ge=0, le=2_000)


class RecordAppConfig(BaseModel):
    """The one model-driven app that "open that record" opens records in."""

    model_config = ConfigDict(extra="forbid")

    name: str
    app_id: str
    tables: list[str] = Field(min_length=1)


class AgentConfig(BaseModel):
    """L2: one gpt-6-luna flight per cut segment, with the typed Contoso Bikes record tools."""

    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65_535)
    model: str = "gpt-6-luna"
    reasoning_effort: Literal["none", "low", "medium", "high"] | None = "low"
    # An OpenAI-compatible Responses endpoint and the git-ignored file holding its key.
    endpoint: str = ""
    key_file: str = ""
    # The Dataverse environment and its Entra tenant (Azure CLI tokens are requested for it).
    dataverse_url: str = ""
    tenant_id: str = ""
    environment_name: str = "Contoso Bikes"
    schema_path: str = "data/contoso/schema.json"
    exports_dir: str = "exports"
    question_timeout_seconds: int = Field(default=300, ge=5, le=3_600)
    max_tool_calls: int = Field(default=16, ge=1, le=64)
    graceful_shutdown_timeout_seconds: int = Field(default=3, ge=0, le=30)
    thread_context_timeout_seconds: int = Field(default=120, ge=1, le=900)
    record_app: RecordAppConfig | None = None


class PresentationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ]
    )


class DiagnosticsConfig(BaseModel):
    """Local, observational logging controls; never an execution or audit authority."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    console: bool = False
    file: bool = False
    file_path: str = "logs/sot-diagnostics.jsonl"
    max_file_bytes: int = Field(default=10_000_000, ge=100_000, le=1_000_000_000)
    backup_count: int = Field(default=5, ge=0, le=100)
    memory_events: int = Field(default=4_000, ge=100, le=100_000)
    include_transcripts: bool = True
    include_intent_payloads: bool = True
    include_tool_payloads: bool = True
    include_responses: bool = True


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    l1: IntentConfig = Field(default_factory=IntentConfig)
    l2: AgentConfig = Field(default_factory=AgentConfig)
    l3: PresentationConfig = Field(default_factory=PresentationConfig)
    diagnostics: DiagnosticsConfig = Field(default_factory=DiagnosticsConfig)


def load_config(path: Path | None = None) -> AppConfig:
    """Load ``config/sot.toml`` (or ``SOT_CONFIG``); defaults when the file is absent."""

    config_path = path or Path(os.getenv("SOT_CONFIG", "config/sot.toml"))
    if not config_path.exists():
        return AppConfig()
    with config_path.open("rb") as config_file:
        return AppConfig.model_validate(tomllib.load(config_file))
