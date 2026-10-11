"""Transformers 5.15 adapter for NVIDIA Nemotron 3.5 streaming RNNT."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from queue import Queue
from typing import Any

from ..audio.buffer import ArrayAudioBuffer, AudioBuffer
from .engine import EngineFragment


class TransformersNemotronEngine:
    runtime_name = "transformers"

    def __init__(
        self,
        model_id: str = "nvidia/nemotron-3.5-asr-streaming-0.6b",
        device: str = "cuda:0",
        precision: str = "bf16",
        *,
        local_files_only: bool = False,
    ) -> None:
        self.model_id = model_id
        self.requested_device = device
        self.precision_name = precision
        self.local_files_only = local_files_only
        self.device_name = device
        self.load_ms = 0.0
        self._processor: Any | None = None
        self._model: Any | None = None
        self._torch: Any | None = None
        self._runtime_lock = threading.Lock()

    def load(self, lookahead: int, language: str, *, warmup: bool = True) -> None:
        if self._model is not None:
            return
        started = time.perf_counter()
        try:
            import torch
            from transformers import AutoModelForRNNT, AutoProcessor
        except ImportError as exc:
            raise RuntimeError(
                "L0 dependencies are not installed. Run `uv sync --extra l0`."
            ) from exc
        if not torch.cuda.is_available():
            raise RuntimeError("Nemotron L0 requires a CUDA-capable PyTorch runtime.")
        if not self.requested_device.startswith("cuda"):
            raise ValueError("The first L0 implementation supports CUDA devices only.")

        dtype_by_name = {
            "bf16": torch.bfloat16,
            "fp16": torch.float16,
            "fp32": torch.float32,
        }
        processor = AutoProcessor.from_pretrained(
            self.model_id, local_files_only=self.local_files_only
        )
        model = AutoModelForRNNT.from_pretrained(
            self.model_id,
            device_map="auto",
            dtype=dtype_by_name[self.precision_name],
            local_files_only=self.local_files_only,
        ).eval()
        processor.set_num_lookahead_tokens(lookahead)
        self._torch = torch
        self._processor = processor
        self._model = model
        self.device_name = torch.cuda.get_device_name(model.device)
        if warmup:
            import numpy as np

            minimum_samples = max(processor.num_samples_first_audio_chunk, 16_000)
            silence = ArrayAudioBuffer(np.zeros(minimum_samples, dtype=np.float32))
            list(self._stream_iter(silence, lookahead, language, threading.Event()))
        self.load_ms = (time.perf_counter() - started) * 1_000

    def stream(
        self,
        audio: AudioBuffer,
        lookahead: int,
        language: str,
        cancel: threading.Event | None = None,
    ) -> Iterator[EngineFragment]:
        yield from self._stream_iter(audio, lookahead, language, cancel or threading.Event())

    def transcribe(
        self,
        audio: AudioBuffer,
        lookahead: int,
        language: str,
        cancel: threading.Event | None = None,
    ) -> str:
        text = ""
        for fragment in self._stream_iter(audio, lookahead, language, cancel or threading.Event()):
            text = fragment.text if fragment.is_snapshot else text + fragment.text
        return text.strip()

    def close(self) -> None:
        model, torch = self._model, self._torch
        self._model = None
        self._processor = None
        if model is not None:
            del model
        if torch is not None and torch.cuda.is_available():
            torch.cuda.empty_cache()

    def _stream_iter(
        self,
        audio: AudioBuffer,
        lookahead: int,
        language: str,
        cancel: threading.Event,
    ) -> Any:
        if self._model is None or self._processor is None or self._torch is None:
            raise RuntimeError("Load the ASR engine before transcription.")
        with self._runtime_lock:
            processor = self._processor
            model = self._model
            torch = self._torch
            processor.set_num_lookahead_tokens(lookahead)
            dtype = next(model.parameters()).dtype
            cursor_lock = threading.Lock()
            cursor_ms = 0.0

            first_audio = audio.window(0, processor.num_samples_first_audio_chunk)
            if first_audio is None or cancel.is_set():
                return
            first = processor(
                first_audio,
                sampling_rate=audio.sample_rate,
                is_streaming=True,
                is_first_audio_chunk=True,
                language=language,
                return_tensors="pt",
            ).to(model.device, dtype=dtype)

            def input_features() -> Any:
                nonlocal cursor_ms
                yield first.input_features[:, : processor.num_mel_frames_first_audio_chunk, :]
                with cursor_lock:
                    cursor_ms = min(
                        audio.duration_ms,
                        processor.num_samples_first_audio_chunk * 1_000 / audio.sample_rate,
                    )
                mel_frame_index = processor.num_mel_frames_first_audio_chunk
                hop_length = processor.feature_extractor.hop_length
                n_fft = processor.feature_extractor.n_fft
                start = mel_frame_index * hop_length - n_fft // 2
                while not cancel.is_set():
                    end = start + processor.num_samples_per_audio_chunk
                    chunk_audio = audio.window(start, end)
                    if chunk_audio is None:
                        break
                    chunk = processor(
                        chunk_audio,
                        sampling_rate=audio.sample_rate,
                        is_streaming=True,
                        is_first_audio_chunk=False,
                        language=language,
                        return_tensors="pt",
                    ).to(model.device, dtype=dtype)
                    with cursor_lock:
                        cursor_ms = min(audio.duration_ms, end * 1_000 / audio.sample_rate)
                    yield chunk.input_features
                    mel_frame_index += processor.num_mel_frames_per_audio_chunk
                    start = mel_frame_index * hop_length - n_fft // 2

            streamer = _SnapshotTextIteratorStreamer(processor.tokenizer, skip_special_tokens=True)
            error: list[BaseException] = []

            def generate() -> None:
                try:
                    inputs = dict(first)
                    inputs["input_features"] = input_features()
                    with torch.inference_mode():
                        model.generate(**inputs, streamer=streamer, max_new_tokens=10_000)
                except BaseException as exc:
                    error.append(exc)
                    streamer.end()

            worker = threading.Thread(target=generate, name="sot-l0-generate", daemon=True)
            worker.start()
            for text in streamer:
                if cancel.is_set():
                    break
                if text:
                    with cursor_lock:
                        emitted_at = cursor_ms
                    yield EngineFragment(text=text, audio_ms=emitted_at, is_snapshot=True)
            worker.join(timeout=5)
            if worker.is_alive():
                raise RuntimeError("ASR generation did not stop after input completion.")
            if error and not cancel.is_set():
                raise RuntimeError("Nemotron streaming generation failed.") from error[0]


class _SnapshotTextIteratorStreamer:
    """Yield the full decoded hypothesis on every token, including the current word.

    Transformers' TextIteratorStreamer deliberately waits for whitespace before exposing text.
    That makes a live transcript stay one word behind. Cumulative snapshots let L0 publish the
    revisable tail immediately while completed, whitespace-delimited words remain stable.
    """

    _STOP = object()

    def __init__(self, tokenizer: Any, **decode_kwargs: Any) -> None:
        self.tokenizer = tokenizer
        self.decode_kwargs = decode_kwargs
        self.token_cache: list[int] = []
        self.text_queue: Queue[Any] = Queue()
        self._last_text = ""

    def put(self, value: Any) -> None:
        if len(value.shape) > 1 and value.shape[0] > 1:
            raise ValueError("Snapshot streamer supports batch size 1")
        if len(value.shape) > 1:
            value = value[0]
        self.token_cache.extend(value.tolist())
        text = str(self.tokenizer.decode(self.token_cache, **self.decode_kwargs))
        if text != self._last_text:
            self._last_text = text
            self.text_queue.put(text)

    def end(self) -> None:
        self.text_queue.put(self._STOP)

    def __iter__(self) -> _SnapshotTextIteratorStreamer:
        return self

    def __next__(self) -> str:
        value = self.text_queue.get()
        if value is self._STOP:
            raise StopIteration
        return str(value)
