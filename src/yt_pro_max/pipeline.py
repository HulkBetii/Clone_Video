from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from yt_pro_max.artifacts import parse_vtt, render_artifacts
from yt_pro_max.config import Settings
from yt_pro_max.errors import PipelineError
from yt_pro_max.models import (
    JobStage,
    TranscriptReconciliation,
    TranscriptSegment,
    TranscriptSource,
    VideoMetadata,
)
from yt_pro_max.reconciliation import apply_reconciliation, build_reconciliation_plan
from yt_pro_max.transcription import WhisperTranscriber
from yt_pro_max.url_utils import canonicalize_youtube_url
from yt_pro_max.youtube import CaptionTrack, VideoInspection, YouTubeClient

LOGGER = logging.getLogger(__name__)
JAPANESE_LANGUAGE = "ja"
KOREAN_LANGUAGE = "ko"
JAPANESE_AUDIO_FIRST_WARNING = "JAPANESE_AUTO_CAPTION_REPLACED_BY_WHISPER"
JAPANESE_RECONCILIATION_UNAVAILABLE = "JAPANESE_RECONCILIATION_UNAVAILABLE"
KOREAN_AUDIO_FIRST_WARNING = "KOREAN_AUTO_CAPTION_REPLACED_BY_WHISPER"
KOREAN_RECONCILIATION_UNAVAILABLE = "KOREAN_RECONCILIATION_UNAVAILABLE"
AUDIO_FIRST_LANGUAGES = frozenset({JAPANESE_LANGUAGE, KOREAN_LANGUAGE})
AUDIO_FIRST_WARNINGS = {
    JAPANESE_LANGUAGE: JAPANESE_AUDIO_FIRST_WARNING,
    KOREAN_LANGUAGE: KOREAN_AUDIO_FIRST_WARNING,
}
RECONCILIATION_UNAVAILABLE_WARNINGS = {
    JAPANESE_LANGUAGE: JAPANESE_RECONCILIATION_UNAVAILABLE,
    KOREAN_LANGUAGE: KOREAN_RECONCILIATION_UNAVAILABLE,
}


@dataclass(frozen=True)
class PipelineOutput:
    source: TranscriptSource
    language: str
    language_confidence: float | None
    video: VideoMetadata
    warnings: list[str]
    artifact_paths: dict[str, Path]
    reconciliation: TranscriptReconciliation | None = None


class TranscriptPipeline:
    def __init__(
        self,
        settings: Settings,
        youtube: YouTubeClient | None = None,
        transcriber: WhisperTranscriber | None = None,
    ) -> None:
        self.settings = settings
        self.youtube = youtube or YouTubeClient(settings)
        self.transcriber = transcriber or WhisperTranscriber(settings)

    def process(
        self,
        *,
        job_id: str,
        request_url: str,
        requested_language: str | None,
        update: Callable[[JobStage, int], None],
    ) -> PipelineOutput:
        self.settings.ensure_directories()
        temporary_dir = self.settings.temp_dir / job_id
        output_dir = self.settings.jobs_dir / job_id
        artifact_staging_dir = temporary_dir / "artifacts"
        temporary_dir.mkdir(parents=True, exist_ok=True)
        try:
            update(JobStage.INSPECTING, 1)
            canonical_url = canonicalize_youtube_url(request_url)
            inspection = self.youtube.inspect(canonical_url)
            track = self.youtube.select_caption_track(inspection, requested_language)

            warnings: list[str] = []
            reconciliation: TranscriptReconciliation | None = None
            audio_first_language = _audio_first_language(track)
            reference_caption_segments: list[TranscriptSegment] | None = None
            if audio_first_language and track is not None:
                update(JobStage.FETCHING_CAPTION, 10)
                reference_caption_segments = _load_reference_caption(
                    self.youtube,
                    inspection,
                    track,
                    temporary_dir,
                    audio_first_language,
                    warnings,
                )
            if track and audio_first_language is None:
                update(JobStage.FETCHING_CAPTION, 10)
                caption_path = self.youtube.download_caption(inspection, track, temporary_dir)
                segments = parse_vtt(caption_path)
                source = track.source
                language = track.language
                language_confidence = None
            else:
                update(JobStage.DOWNLOADING_AUDIO, 15 if audio_first_language else 10)
                audio_path = self.youtube.download_audio(inspection, temporary_dir)
                update(JobStage.LOADING_MODEL, 45)
                transcribe_options = {
                    "progress_callback": lambda progress: update(
                        JobStage.TRANSCRIBING,
                        45 + min(45, progress // 2),
                    )
                }
                if audio_first_language:
                    transcribe_options["language"] = audio_first_language
                result = self.transcriber.transcribe(audio_path, **transcribe_options)
                if audio_first_language and _base_language(result.language) != audio_first_language:
                    raise PipelineError(
                        "TRANSCRIPTION_LANGUAGE_MISMATCH",
                        "Whisper did not return the expected transcript language.",
                        details={
                            "expected_language": audio_first_language,
                            "actual_language": result.language,
                        },
                    )
                segments = result.segments
                source = TranscriptSource.WHISPER
                language = result.language
                language_confidence = result.language_confidence
                warnings.extend(result.warnings)
                if audio_first_language:
                    unavailable_warning = RECONCILIATION_UNAVAILABLE_WARNINGS[
                        audio_first_language
                    ]
                    warnings.append(AUDIO_FIRST_WARNINGS[audio_first_language])
                    if reference_caption_segments:
                        plan = build_reconciliation_plan(
                            result.segments,
                            reference_caption_segments,
                            self.settings,
                            language=audio_first_language,
                        )
                        secondary_by_window: dict[int, list[TranscriptSegment]] = {}
                        transcribe_window = getattr(self.transcriber, "transcribe_window", None)
                        if plan.windows and transcribe_window is None:
                            warnings.append(unavailable_warning)
                        elif transcribe_window is not None:
                            reconciliation_dir = temporary_dir / "reconciliation"
                            window_count = len(plan.windows)
                            for window_position, window in enumerate(plan.windows, start=1):
                                try:
                                    secondary = transcribe_window(
                                        audio_path,
                                        start_ms=window.start_ms,
                                        end_ms=window.end_ms,
                                        language=audio_first_language,
                                        model_name=self.settings.reconciliation_model,
                                        output_dir=reconciliation_dir,
                                    )
                                    if _base_language(secondary.language) != audio_first_language:
                                        raise PipelineError(
                                            "TRANSCRIPTION_LANGUAGE_MISMATCH",
                                            "Secondary Whisper verification returned a "
                                            "different language.",
                                            details={
                                                "expected_language": audio_first_language,
                                                "actual_language": secondary.language,
                                            },
                                    )
                                    secondary_by_window[window.index] = secondary.segments
                                    warnings.extend(secondary.warnings)
                                    update(
                                        JobStage.TRANSCRIBING,
                                        90 + min(4, (window_position * 5) // window_count),
                                    )
                                except PipelineError as error:
                                    LOGGER.warning(
                                        "Transcript reconciliation window failed "
                                        "language=%s index=%s code=%s",
                                        audio_first_language,
                                        window.index,
                                        error.info.code,
                                    )
                                    warnings.append(unavailable_warning)
                                    if error.info.code in {
                                        "MODEL_LOAD_FAILED",
                                        "TRANSCRIPTION_LANGUAGE_MISMATCH",
                                    }:
                                        break
                                except Exception as error:
                                    LOGGER.warning(
                                        "Unexpected transcript reconciliation failure "
                                        "language=%s index=%s: %s",
                                        audio_first_language,
                                        window.index,
                                        error,
                                    )
                                    warnings.append(unavailable_warning)
                        outcome = apply_reconciliation(
                            result.segments,
                            plan,
                            secondary_by_window,
                            self.settings,
                            language=audio_first_language,
                        )
                        segments = outcome.segments
                        reconciliation = outcome.reconciliation
                        warnings.extend(outcome.warnings)

            warnings = list(dict.fromkeys(warnings))
            update(JobStage.RENDERING, 95)
            staged_artifact_paths = render_artifacts(
                output_dir=artifact_staging_dir,
                job_id=job_id,
                source=source,
                language=language,
                language_confidence=language_confidence,
                video=inspection.metadata,
                segments=segments,
                warnings=warnings,
                reconciliation=reconciliation,
            )
            if output_dir.exists():
                shutil.rmtree(output_dir)
            artifact_staging_dir.replace(output_dir)
            artifact_paths = {
                file_type: output_dir / path.name
                for file_type, path in staged_artifact_paths.items()
            }
            update(JobStage.RENDERING, 100)
            return PipelineOutput(
                source=source,
                language=language,
                language_confidence=language_confidence,
                video=inspection.metadata,
                warnings=warnings,
                artifact_paths=artifact_paths,
                reconciliation=reconciliation,
            )
        finally:
            if temporary_dir.exists():
                shutil.rmtree(temporary_dir)


def _audio_first_language(track: CaptionTrack | None) -> str | None:
    if track is not None and track.source == TranscriptSource.AUTOMATIC_CAPTION:
        language = _base_language(track.language)
        if language in AUDIO_FIRST_LANGUAGES:
            return language
    return None


def _base_language(language: str) -> str:
    return language.strip().lower().split("-", 1)[0]


def _load_reference_caption(
    youtube: YouTubeClient,
    inspection: VideoInspection,
    track: CaptionTrack,
    temporary_dir: Path,
    language: str,
    warnings: list[str],
) -> list[TranscriptSegment] | None:
    try:
        caption_path = youtube.download_caption(inspection, track, temporary_dir)
        return parse_vtt(caption_path)
    except PipelineError as error:
        LOGGER.warning(
            "Transcript caption reference unavailable language=%s code=%s",
            language,
            error.info.code,
        )
        warnings.append(RECONCILIATION_UNAVAILABLE_WARNINGS[language])
        return None
