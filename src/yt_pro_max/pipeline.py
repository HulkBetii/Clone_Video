from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from yt_pro_max.artifacts import parse_vtt, render_artifacts
from yt_pro_max.config import Settings
from yt_pro_max.errors import PipelineError
from yt_pro_max.models import JobStage, TranscriptSource, VideoMetadata
from yt_pro_max.transcription import WhisperTranscriber
from yt_pro_max.url_utils import canonicalize_youtube_url
from yt_pro_max.youtube import CaptionTrack, YouTubeClient

LOGGER = logging.getLogger(__name__)
JAPANESE_LANGUAGE = "ja"
JAPANESE_AUDIO_FIRST_WARNING = "JAPANESE_AUTO_CAPTION_REPLACED_BY_WHISPER"


@dataclass(frozen=True)
class PipelineOutput:
    source: TranscriptSource
    language: str
    language_confidence: float | None
    video: VideoMetadata
    warnings: list[str]
    artifact_paths: dict[str, Path]


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
            audio_first_language = _audio_first_language(track)
            if track and audio_first_language is None:
                update(JobStage.FETCHING_CAPTION, 10)
                caption_path = self.youtube.download_caption(inspection, track, temporary_dir)
                segments = parse_vtt(caption_path)
                source = track.source
                language = track.language
                language_confidence = None
            else:
                update(JobStage.DOWNLOADING_AUDIO, 10)
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
                        "Whisper did not return the expected Japanese transcript language.",
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
                    warnings.append(JAPANESE_AUDIO_FIRST_WARNING)

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
            )
        finally:
            if temporary_dir.exists():
                shutil.rmtree(temporary_dir)


def _audio_first_language(track: CaptionTrack | None) -> str | None:
    if (
        track is not None
        and track.source == TranscriptSource.AUTOMATIC_CAPTION
        and _base_language(track.language) == JAPANESE_LANGUAGE
    ):
        return JAPANESE_LANGUAGE
    return None


def _base_language(language: str) -> str:
    return language.strip().lower().split("-", 1)[0]
