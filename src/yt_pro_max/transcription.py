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
WINDOW_EXTRACTION_TIMEOUT_SECONDS = 180


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
        self._models: dict[str, object] = {}
        self._model_runtimes: dict[str, tuple[str, str]] = {}
        self._warnings: list[str] = []
        self._gpu_failed_models: set[str] = set()
        self._cuda_dll_handle = _configure_cuda_dll_directory(settings.cuda_dll_dir)

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        model_name: str | None = None,
        detect_language: bool = True,
        progress_callback: Callable[[int], None] | None = None,
    ) -> TranscriptionResult:
        resolved_model_name = model_name or self.settings.whisper_model
        language_sample = (
            _load_language_sample(audio_path) if language and detect_language else None
        )
        model = self._load_model(resolved_model_name)
        model_device, _compute_type = self._model_runtimes[resolved_model_name]
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
            if model_device == "cuda":
                warning = "GPU_TRANSCRIPTION_FAILED_CPU_FALLBACK"
                if warning not in self._warnings:
                    self._warnings.append(warning)
                LOGGER.warning("CUDA transcription failed; retrying on CPU: %s", error)
                self._models.pop(resolved_model_name, None)
                self._model_runtimes.pop(resolved_model_name, None)
                self._gpu_failed_models.add(resolved_model_name)
                if resolved_model_name == self.settings.whisper_model:
                    self._model = None
                    self._device = None
                    self._compute_type = None
                return self.transcribe(
                    audio_path,
                    language=language,
                    model_name=resolved_model_name,
                    detect_language=detect_language,
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

        result_language = (
            str(detected_language or getattr(info, "language", "") or "").strip().lower()
        )
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

    def transcribe_window(
        self,
        audio_path: Path,
        *,
        start_ms: int,
        end_ms: int,
        language: str,
        model_name: str,
        output_dir: Path,
    ) -> TranscriptionResult:
        if end_ms <= start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        output_dir.mkdir(parents=True, exist_ok=True)
        window_path = output_dir / f"reconciliation-{start_ms}-{end_ms}.wav"
        _extract_audio_window(audio_path, window_path, start_ms=start_ms, end_ms=end_ms)
        result = self.transcribe(
            window_path,
            language=language,
            model_name=model_name,
            detect_language=False,
        )
        return TranscriptionResult(
            language=result.language,
            language_confidence=result.language_confidence,
            segments=[_offset_segment(segment, start_ms) for segment in result.segments],
            warnings=result.warnings,
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

    def _load_model(self, model_name: str | None = None):
        resolved_model_name = model_name or self.settings.whisper_model
        if resolved_model_name in self._models:
            return self._models[resolved_model_name]
        try:
            from faster_whisper import WhisperModel
        except Exception as error:
            LOGGER.exception("Unable to import faster-whisper")
            raise PipelineError(
                "MODEL_LOAD_FAILED", "The local Whisper model could not be loaded."
            ) from error

        if resolved_model_name not in self._gpu_failed_models and self.settings.whisper_device in {
            "auto",
            "cuda",
        }:
            try:
                model = WhisperModel(
                    resolved_model_name,
                    device="cuda",
                    compute_type=self.settings.gpu_compute_type,
                )
                self._store_model(
                    resolved_model_name,
                    model,
                    device="cuda",
                    compute_type=self.settings.gpu_compute_type,
                )
                return model
            except Exception as error:
                warning = "GPU_RUNTIME_UNAVAILABLE_CPU_FALLBACK"
                if warning not in self._warnings:
                    self._warnings.append(warning)
                LOGGER.warning("CUDA Whisper runtime unavailable; using CPU fallback: %s", error)

        try:
            model = WhisperModel(
                resolved_model_name,
                device="cpu",
                compute_type=self.settings.cpu_compute_type,
            )
            self._store_model(
                resolved_model_name,
                model,
                device="cpu",
                compute_type=self.settings.cpu_compute_type,
            )
            return model
        except Exception as error:
            LOGGER.exception("Unable to load Whisper model")
            raise PipelineError(
                "MODEL_LOAD_FAILED", "The local Whisper model could not be loaded."
            ) from error

    def _store_model(self, model_name: str, model, *, device: str, compute_type: str) -> None:
        self._models[model_name] = model
        self._model_runtimes[model_name] = (device, compute_type)
        if model_name == self.settings.whisper_model:
            self._model = model
            self._device = device
            self._compute_type = compute_type


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


def _extract_audio_window(
    audio_path: Path,
    output_path: Path,
    *,
    start_ms: int,
    end_ms: int,
) -> None:
    duration_ms = end_ms - start_ms
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(audio_path),
        "-ss",
        f"{start_ms / 1000:.3f}",
        "-t",
        f"{duration_ms / 1000:.3f}",
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(WHISPER_SAMPLE_RATE),
        "-c:a",
        "pcm_s16le",
        str(output_path),
    ]
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            timeout=WINDOW_EXTRACTION_TIMEOUT_SECONDS,
        )
    except Exception as error:
        LOGGER.exception(
            "Unable to extract reconciliation audio window start_ms=%s end_ms=%s",
            start_ms,
            end_ms,
        )
        raise PipelineError(
            "TRANSCRIPTION_FAILED",
            "The suspicious audio span could not be prepared for verification.",
            details={"stage": "reconciliation_audio", "start_ms": start_ms, "end_ms": end_ms},
        ) from error


def _offset_segment(segment: TranscriptSegment, offset_ms: int) -> TranscriptSegment:
    words = None
    if segment.words:
        words = [
            word.model_copy(
                update={
                    "start_ms": word.start_ms + offset_ms,
                    "end_ms": word.end_ms + offset_ms,
                }
            )
            for word in segment.words
        ]
    return segment.model_copy(
        update={
            "start_ms": segment.start_ms + offset_ms,
            "end_ms": segment.end_ms + offset_ms,
            "words": words,
        }
    )


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
