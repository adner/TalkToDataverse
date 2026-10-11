"""Validated configuration for local streaming transcription."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .audio.vad import VadSettings

SUPPORTED_LATENCY_TIERS = (80, 320, 560, 1120)
LOOKAHEAD_BY_TIER = {80: 0, 320: 3, 560: 6, 1120: 13}


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str = "nvidia/nemotron-3.5-asr-streaming-0.6b"
    locale: str = "en-US"
    device: str = "cuda:0"
    precision: Literal["bf16", "fp16", "fp32"] = "bf16"
    latency_tier_ms: Literal[80, 320, 560, 1120]


class SecondPassConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    mode: Literal["full_context", "offline_model"] = "full_context"
    model_id: str = ""
    timeout_ms: int = Field(default=1500, ge=1, le=60_000)

    @model_validator(mode="after")
    def require_offline_model(self) -> SecondPassConfig:
        if self.mode == "offline_model" and not self.model_id:
            raise ValueError("second_pass.model_id is required for offline_model mode")
        return self


class UtteranceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preroll_ms: int = Field(default=800, ge=0, le=5_000)
    tail_flush_ms: int = Field(default=300, ge=0, le=5_000)
    min_press_ms: int = Field(default=150, ge=0, le=5_000)


class AudioConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_match: str = ""
    target_sr: Literal[16000] = 16000
    resampler: Literal["soxr"] = "soxr"
    block_ms: int = Field(default=10, ge=1, le=100)


class VadConfig(BaseModel):
    """Speech pause detection on the live microphone (not used for file input)."""

    model_config = ConfigDict(extra="forbid")

    pause_ms: int = Field(default=400, ge=100, le=5_000)
    margin_db: float = Field(default=10.0, ge=3.0, le=40.0)
    min_threshold_db: float = Field(default=-55.0, ge=-90.0, le=-10.0)
    onset_ms: int = Field(default=60, ge=0, le=1_000)

    def settings(self) -> VadSettings:
        return VadSettings(
            pause_ms=self.pause_ms,
            margin_db=self.margin_db,
            min_threshold_db=self.min_threshold_db,
            onset_ms=self.onset_ms,
        )


class PersistenceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    dir: Path = Path("./_capture")


class TelemetryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    metrics: bool = True


class StreamerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: ModelConfig
    second_pass: SecondPassConfig = Field(default_factory=SecondPassConfig)
    utterance: UtteranceConfig = Field(default_factory=UtteranceConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    vad: VadConfig = Field(default_factory=VadConfig)
    persistence: PersistenceConfig = Field(default_factory=PersistenceConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)

    @property
    def lookahead_tokens(self) -> int:
        return LOOKAHEAD_BY_TIER[self.model.latency_tier_ms]


def load_streamer_config(path: Path) -> StreamerConfig:
    with path.open("rb") as config_file:
        return StreamerConfig.model_validate(tomllib.load(config_file))
