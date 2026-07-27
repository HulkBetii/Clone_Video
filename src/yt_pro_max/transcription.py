from __future__ import annotations

import ctypes
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from yt_pro_max.config import Settings
from yt_pro_max.errors import PipelineError
from yt_pro_max.models import TranscriptSegment, WordTimestamp

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class TranscriptionResult:
    language: str
    language_confidence: float | None
    segments: list[TranscriptSegment]
    warnings: list[str]


class WhisperTranscriber:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._model = None
        self._device: str | None = None
        self._compute_type: str | None = None
        self._warnings: list[str] = []
        self._gpu_failed = False

    def transcribe(
        self,
        audio_path: Path,
        *,
        progress_callback: Callable[[int], None] | None = None,
    ) -> TranscriptionResult:
        model = self._load_model()
        try:
            raw_segments, info = model.transcribe(
                str(audio_path),
                vad_filter=True,
                word_timestamps=True,
            )
            segments = []
            for raw_segment in raw_segments:
                text = str(getattr(raw_segment, "text", "")).strip()
                start_ms = max(0, int(round(float(getattr(raw_segment, "start", 0)) * 1000)))
                end_ms = max(start_ms + 1, int(round(float(getattr(raw_segment, "end", 0)) * 1000)))
                if not text:
                    continue
                words = _to_words(getattr(raw_segment, "words", None))
                segments.append(
                    TranscriptSegment(
                        index=len(segments) + 1,
                        start_ms=start_ms,
                        end_ms=end_ms,
                        text=text,
                        words=words,
                    )
                )
                if progress_callback:
                    progress_callback(min(99, len(segments)))
        except PipelineError:
            raise
        except Exception as error:
            if self._device == "cuda":
                warning = "GPU_TRANSCRIPTION_FAILED_CPU_FALLBACK"
                if warning not in self._warnings:
                    self._warnings.append(warning)
                LOGGER.warning("CUDA transcription failed; retrying on CPU: %s", error)
                self._model = None
                self._device = None
                self._compute_type = None
                self._gpu_failed = True
                return self.transcribe(audio_path, progress_callback=progress_callback)
            LOGGER.exception("Whisper transcription failed")
            raise PipelineError(
                "TRANSCRIPTION_FAILED", "Local Whisper transcription failed."
            ) from error

        if not segments:
            raise PipelineError(
                "NO_SPEECH_DETECTED", "No speech was detected in the downloaded audio."
            )

        language = str(getattr(info, "language", "") or "").strip().lower()
        if not language:
            raise PipelineError(
                "TRANSCRIPTION_FAILED", "Whisper did not identify a spoken language."
            )
        confidence = _safe_float(getattr(info, "language_probability", None))
        warnings = list(self._warnings)
        if confidence is not None and confidence < 0.5:
            warnings.append("LOW_LANGUAGE_CONFIDENCE")
        return TranscriptionResult(
            language=language,
            language_confidence=confidence,
            segments=segments,
            warnings=warnings,
        )

    def health(self) -> dict[str, object]:
        result: dict[str, object] = {
            "model": self.settings.whisper_model,
            "loaded": self._model is not None,
            "device": self._device or "not_loaded",
            "compute_type": self._compute_type or "not_loaded",
            "warnings": list(self._warnings),
        }
        try:
            import ctranslate2

            result["cuda_device_count"] = ctranslate2.get_cuda_device_count()
            result["cuda_runtime_available"], runtime_error = _cuda_runtime_status()
            if runtime_error:
                result["runtime_error"] = runtime_error
        except Exception as error:
            result["cuda_device_count"] = 0
            result["runtime_error"] = type(error).__name__
        return result

    def _load_model(self):
        if self._model is not None:
            return self._model
        from faster_whisper import WhisperModel

        if not self._gpu_failed and self.settings.whisper_device in {"auto", "cuda"}:
            try:
                self._model = WhisperModel(
                    self.settings.whisper_model,
                    device="cuda",
                    compute_type=self.settings.gpu_compute_type,
                )
                self._device = "cuda"
                self._compute_type = self.settings.gpu_compute_type
                return self._model
            except Exception as error:
                self._warnings.append("GPU_RUNTIME_UNAVAILABLE_CPU_FALLBACK")
                LOGGER.warning("CUDA Whisper runtime unavailable; using CPU fallback: %s", error)

        try:
            self._model = WhisperModel(
                self.settings.whisper_model,
                device="cpu",
                compute_type=self.settings.cpu_compute_type,
            )
            self._device = "cpu"
            self._compute_type = self.settings.cpu_compute_type
            return self._model
        except Exception as error:
            LOGGER.exception("Unable to load Whisper model")
            raise PipelineError(
                "MODEL_LOAD_FAILED", "The local Whisper model could not be loaded."
            ) from error


def _to_words(raw_words) -> list[WordTimestamp] | None:
    if not raw_words:
        return None
    words = []
    for raw_word in raw_words:
        text = str(getattr(raw_word, "word", "")).strip()
        if not text:
            continue
        words.append(
            WordTimestamp(
                start_ms=max(0, int(round(float(getattr(raw_word, "start", 0)) * 1000))),
                end_ms=max(1, int(round(float(getattr(raw_word, "end", 0)) * 1000))),
                text=text,
                probability=_safe_float(getattr(raw_word, "probability", None)),
            )
        )
    return words or None


def _safe_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _cuda_runtime_status() -> tuple[bool, str | None]:
    if os.name != "nt":
        return True, None
    for library in ("cublas64_12.dll", "cudnn64_9.dll"):
        try:
            ctypes.WinDLL(library)
        except OSError:
            return False, f"{library}_NOT_FOUND"
    return True, None
