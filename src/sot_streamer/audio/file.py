"""Deterministic file audio source with exact 16 kHz mono normalization."""

from __future__ import annotations

from pathlib import Path

from .buffer import ArrayAudioBuffer


def load_audio_file(path: Path, target_sample_rate: int = 16_000) -> ArrayAudioBuffer:
    import numpy as np
    import soundfile as sf
    import soxr

    if not path.is_file():
        raise FileNotFoundError(path)
    audio, sample_rate = sf.read(path, dtype="float32", always_2d=True)
    mono = np.mean(audio, axis=1, dtype=np.float32)
    if sample_rate != target_sample_rate:
        mono = soxr.resample(mono, sample_rate, target_sample_rate, quality="HQ")
    return ArrayAudioBuffer(mono, target_sample_rate)
