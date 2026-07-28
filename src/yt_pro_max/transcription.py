from __future__ import annotations

import ctypes
import logging
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from yt_pro_max.config import Settings
from yt_pro_max.errors import PipelineError
from yt_pro_max.models import TranscriptSegment, WordTimestamp

LOGGER = logging.getLogger(__name__)
LANGUAGE_DETECTION_SAMPLE_SECONDS = 90
LANGUAGE_DETECTION_SEGMENTS = 3
LANGUAGE_DETECTION_TIMEOUT_SECONDS = 120
WHISPER_SAMPLE_RATE = 16_000


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
        self._cuda_dll_handle = _configure_cuda_dll_directory(settings.cuda_dll_dir)

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        progress_callback: Callable[[int], None] | None = None,
    ) -> TranscriptionResult:
        language_sample = _load_language_sample(audio_path) if language else None
        model = self._load_model()
        detected_language: str | None = None
        detected_confidence: float | None = None
        try:
            if language_sample is not None:
                detected_language, detected_confidence, _ = model.detect_language(
                    audio=language_sample,
                    vad_filter=True,
                    language_detection_segments=LANGUAGE_DETECTION_SEGMENTS,
                )
                if _base_language(detected_language) != _base_language(language):
                    raise PipelineError(
                        "TRANSCRIPTION_LANGUAGE_MISMATCH",
                        "Whisper detected a different spoken language than expected.",
                        details={
                            "expected_language": language,
                            "detected_language": detected_language,
                            "language_confidence": detected_confidence,
                        },
                    )
            options = {
                "vad_filter": True,
                "word_timestamps": True,
            }
            if language:
                options["language"] = language
            raw_segments, info = model.transcribe(
                str(audio_path),
                **options,
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
                return self.transcribe(
                    audio_path,
                    language=language,
                    progress_callback=progress_callback,
                )
            LOGGER.exception("Whisper transcription failed")
            raise PipelineError(
                "TRANSCRIPTION_FAILED", "Local Whisper transcription failed."
            ) from error

        if not segments:
            raise PipelineError(
                "NO_SPEECH_DETECTED", "No speech was detected in the downloaded audio."
            )

        result_language = str(
            detected_language or getattr(info, "language", "") or ""
        ).strip().lower()
        if not result_language:
            raise PipelineError(
                "TRANSCRIPTION_FAILED", "Whisper did not identify a spoken language."
            )
        confidence = detected_confidence
        if confidence is None:
            confidence = _safe_float(getattr(info, "language_probability", None))
        warnings = list(self._warnings)
        if confidence is not None and confidence < 0.5:
            warnings.append("LOW_LANGUAGE_CONFIDENCE")
        return TranscriptionResult(
            language=result_language,
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
            result["cuda_dll_dir_configured"] = self.settings.cuda_dll_dir is not None
            if runtime_error:
                result["runtime_error"] = runtime_error
        except Exception as error:
            result["cuda_device_count"] = 0
            result["runtime_error"] = type(error).__name__
        return result

    def _load_model(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except Exception as error:
            LOGGER.exception("Unable to import faster-whisper")
            raise PipelineError(
                "MODEL_LOAD_FAILED", "The local Whisper model could not be loaded."
            ) from error

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


def _base_language(language: str) -> str:
    return language.strip().lower().split("-", 1)[0]


def _load_language_sample(audio_path: Path):
    try:
        import numpy as np

        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(audio_path),
            "-t",
            str(LANGUAGE_DETECTION_SAMPLE_SECONDS),
            "-ac",
            "1",
            "-ar",
            str(WHISPER_SAMPLE_RATE),
            "-f",
            "s16le",
            "pipe:1",
        ]
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            timeout=LANGUAGE_DETECTION_TIMEOUT_SECONDS,
        )
        if not completed.stdout:
            raise ValueError("language detection sample is empty")
        return np.frombuffer(completed.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    except PipelineError:
        raise
    except Exception as error:
        LOGGER.exception("Unable to prepare audio for Whisper language detection")
        raise PipelineError(
            "TRANSCRIPTION_FAILED",
            "Local Whisper language detection could not read the downloaded audio.",
            details={"stage": "language_detection"},
        ) from error


def _cuda_runtime_status() -> tuple[bool, str | None]:
    if os.name != "nt":
        return True, None
    for library in ("cublas64_12.dll", "cudnn64_9.dll"):
        try:
            ctypes.WinDLL(library)
        except OSError:
            return False, f"{library}_NOT_FOUND"
    return True, None


def _configure_cuda_dll_directory(cuda_dll_dir: Path | None):
    if os.name != "nt" or cuda_dll_dir is None:
        return None
    resolved_dir = cuda_dll_dir.expanduser().resolve()
    if not resolved_dir.is_dir():
        LOGGER.warning("Configured CUDA DLL directory does not exist: %s", resolved_dir)
        return None
    current_path = os.environ.get("PATH", "")
    path_entries = current_path.split(os.pathsep) if current_path else []
    if not any(entry.casefold() == str(resolved_dir).casefold() for entry in path_entries):
        os.environ["PATH"] = os.pathsep.join([str(resolved_dir), *path_entries])
    try:
        return os.add_dll_directory(str(resolved_dir))
    except OSError as error:
        LOGGER.warning("Unable to register CUDA DLL directory: %s", error)
        return None
