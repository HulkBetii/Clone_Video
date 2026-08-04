from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yt_dlp import DownloadError, YoutubeDL

from yt_pro_max.config import Settings
from yt_pro_max.errors import PipelineError
from yt_pro_max.models import TranscriptSource, VideoMetadata
from yt_pro_max.url_utils import CanonicalVideoUrl

LOGGER = logging.getLogger(__name__)
TRANSIENT_ERROR_PATTERNS = (
    "429",
    "too many requests",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "connection reset",
    "network is unreachable",
    "http error 5",
)


@dataclass(frozen=True)
class CaptionTrack:
    language: str
    provider_language: str
    source: TranscriptSource


@dataclass(frozen=True)
class VideoInspection:
    canonical_url: CanonicalVideoUrl
    metadata: VideoMetadata
    original_language: str | None
    manual_languages: tuple[str, ...]
    automatic_languages: tuple[str, ...]
    live_status: str | None
    raw_info: dict[str, Any]


class YouTubeClient:
    def __init__(
        self, settings: Settings, progress_callback: Callable[[int], None] | None = None
    ) -> None:
        self.settings = settings
        self.progress_callback = progress_callback

    def inspect(self, canonical_url: CanonicalVideoUrl) -> VideoInspection:
        info = self._run_ytdlp(
            lambda: self._extract_info(canonical_url.url, download=False),
            operation="inspect video",
        )
        self._validate_access(info)
        duration = _safe_int(info.get("duration"))
        if duration is not None and duration > self.settings.max_duration_seconds:
            raise PipelineError(
                "DURATION_LIMIT_EXCEEDED",
                f"Video duration exceeds the {self.settings.max_duration_seconds}-second limit.",
                details={
                    "duration_seconds": duration,
                    "max_duration_seconds": self.settings.max_duration_seconds,
                },
            )
        live_status = info.get("live_status")
        if live_status in {"is_live", "is_upcoming"}:
            raise PipelineError(
                "LIVE_NOT_FINISHED",
                "Active and upcoming livestreams are not supported.",
                details={"live_status": live_status},
            )
        if duration is None and live_status not in {"post_live", "was_live"}:
            raise PipelineError(
                "VIDEO_UNAVAILABLE", "YouTube did not provide a usable video duration."
            )

        metadata = VideoMetadata(
            id=canonical_url.video_id,
            title=str(info.get("title") or canonical_url.video_id),
            channel=info.get("channel") or info.get("uploader"),
            duration_seconds=duration,
            webpage_url=str(info.get("webpage_url") or canonical_url.url),
        )
        return VideoInspection(
            canonical_url=canonical_url,
            metadata=metadata,
            original_language=_normalize_language(
                info.get("language") or info.get("original_language")
            ),
            manual_languages=tuple(_available_languages(info.get("subtitles") or {})),
            automatic_languages=tuple(_available_languages(info.get("automatic_captions") or {})),
            live_status=live_status,
            raw_info=info,
        )

    def select_caption_track(
        self,
        inspection: VideoInspection,
        requested_language: str | None,
    ) -> CaptionTrack | None:
        if requested_language:
            for source, languages in (
                (TranscriptSource.MANUAL_CAPTION, inspection.manual_languages),
                (TranscriptSource.AUTOMATIC_CAPTION, inspection.automatic_languages),
            ):
                match = _match_language(
                    requested_language,
                    languages,
                    prefer_original=(
                        source == TranscriptSource.AUTOMATIC_CAPTION
                        and _base_language(requested_language) == "ko"
                    ),
                )
                if match:
                    return CaptionTrack(
                        language=_normalize_language(match) or match,
                        provider_language=match,
                        source=source,
                    )
            available = sorted(
                {
                    _normalize_language(value) or value
                    for value in inspection.manual_languages + inspection.automatic_languages
                }
            )
            raise PipelineError(
                "LANGUAGE_NOT_AVAILABLE",
                f"No caption track matches requested language '{requested_language}'.",
                details={"available_languages": available},
            )

        original_language = inspection.original_language
        candidates = [
            (TranscriptSource.MANUAL_CAPTION, inspection.manual_languages),
            (TranscriptSource.AUTOMATIC_CAPTION, inspection.automatic_languages),
        ]
        if original_language:
            for source, languages in candidates:
                match = _match_language(
                    original_language,
                    languages,
                    prefer_original=(
                        source == TranscriptSource.AUTOMATIC_CAPTION
                        and _base_language(original_language) == "ko"
                    ),
                )
                if match:
                    return CaptionTrack(
                        language=_normalize_language(match) or match,
                        provider_language=match,
                        source=source,
                    )

        for source, languages in candidates:
            original_track = next(
                (language for language in languages if language.lower().endswith("-orig")),
                None,
            )
            if original_track:
                return CaptionTrack(
                    language=_normalize_language(original_track) or original_track,
                    provider_language=original_track,
                    source=source,
                )

        non_empty = [(source, languages) for source, languages in candidates if languages]
        distinct_languages = {
            _normalize_language(language) or language
            for _, languages in non_empty
            for language in languages
        }
        if len(distinct_languages) == 1:
            source, languages = non_empty[0]
            provider_language = languages[0]
            return CaptionTrack(
                language=_normalize_language(provider_language) or provider_language,
                provider_language=provider_language,
                source=source,
            )
        if distinct_languages:
            raise PipelineError(
                "LANGUAGE_AMBIGUOUS",
                "Multiple caption languages are available and the original language is unknown.",
                details={"available_languages": sorted(distinct_languages)},
            )
        return None

    def download_caption(
        self,
        inspection: VideoInspection,
        track: CaptionTrack,
        output_dir: Path,
    ) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        options = self._base_options(output_dir)
        options.update(
            {
                "skip_download": True,
                "writesubtitles": track.source == TranscriptSource.MANUAL_CAPTION,
                "writeautomaticsub": track.source == TranscriptSource.AUTOMATIC_CAPTION,
                "subtitleslangs": [track.provider_language],
                "subtitlesformat": "vtt",
            }
        )
        self._run_ytdlp(
            lambda: self._extract_info(
                inspection.canonical_url.url, download=True, options=options
            ),
            operation="download captions",
        )
        candidates = sorted(output_dir.rglob("*.vtt"))
        if not candidates:
            raise PipelineError(
                "CAPTION_DOWNLOAD_FAILED",
                "YouTube did not return the selected caption track.",
                retryable=True,
                details={"language": track.language, "source": track.source.value},
            )
        preferred = [path for path in candidates if f".{track.provider_language}." in path.name]
        return preferred[0] if preferred else candidates[0]

    def download_audio(self, inspection: VideoInspection, output_dir: Path) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        options = self._base_options(output_dir)
        options.update(
            {
                "format": "bestaudio/best",
                "outtmpl": {"default": str(output_dir / "%(id)s.%(ext)s")},
                "progress_hooks": [self._download_progress],
            }
        )
        self._run_ytdlp(
            lambda: self._extract_info(
                inspection.canonical_url.url, download=True, options=options
            ),
            operation="download audio",
        )
        candidates = [
            path
            for path in output_dir.iterdir()
            if path.is_file() and path.suffix.lower() not in {".part", ".ytdl", ".vtt"}
        ]
        if not candidates:
            raise PipelineError(
                "AUDIO_DOWNLOAD_FAILED",
                "YouTube did not return an audio file.",
                retryable=True,
            )
        return max(candidates, key=lambda path: path.stat().st_size)

    def _extract_info(
        self, url: str, *, download: bool, options: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        with YoutubeDL(options or self._base_options(self.settings.temp_dir)) as ydl:
            return ydl.extract_info(url, download=download)

    def _base_options(self, output_dir: Path) -> dict[str, Any]:
        return {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "retries": 1,
            "fragment_retries": 1,
            "js_runtimes": {"node": {}},
            "outtmpl": {"default": str(output_dir / "%(id)s.%(ext)s")},
        }

    def _run_ytdlp(
        self,
        action: Callable[[], dict[str, Any]],
        *,
        operation: str,
    ) -> dict[str, Any]:
        last_error: DownloadError | None = None
        for attempt in range(self.settings.youtube_retries):
            try:
                return action()
            except DownloadError as error:
                last_error = error
                if (
                    not _is_retryable_error(str(error))
                    or attempt + 1 >= self.settings.youtube_retries
                ):
                    raise _map_download_error(str(error)) from error
                LOGGER.warning(
                    "yt-dlp operation failed; retrying operation=%s attempt=%s",
                    operation,
                    attempt + 1,
                )
                time.sleep(2**attempt)
        raise PipelineError("YOUTUBE_EXTRACTION_FAILED", f"Unable to {operation}.") from last_error

    @staticmethod
    def _validate_access(info: dict[str, Any]) -> None:
        availability = str(info.get("availability") or "").lower()
        message = " ".join(
            str(info.get(key) or "") for key in ("title", "availability", "live_status")
        ).lower()
        if availability in {"private", "needs_auth"} or "private video" in message:
            raise PipelineError("VIDEO_PRIVATE", "The video is private or requires account access.")
        if availability in {"subscriber_only", "premium_only", "needs_subscription"}:
            raise PipelineError("MEMBERS_ONLY", "The video requires a membership or subscription.")
        if availability in {"unavailable", "deleted"}:
            raise PipelineError("VIDEO_UNAVAILABLE", "The video is unavailable or deleted.")
        if "age-restricted" in message or "sign in to confirm your age" in message:
            raise PipelineError("AGE_RESTRICTED", "The video requires age verification.")
        if "not available in your country" in message or "geo" in availability:
            raise PipelineError("GEO_RESTRICTED", "The video is not available in this region.")

    def _download_progress(self, status: dict[str, Any]) -> None:
        if not self.progress_callback or status.get("status") != "downloading":
            return
        total = status.get("total_bytes") or status.get("total_bytes_estimate")
        downloaded = status.get("downloaded_bytes")
        if total and downloaded:
            self.progress_callback(min(99, int(downloaded * 100 / total)))


def _available_languages(captions: dict[str, Any]) -> list[str]:
    available = []
    for language, formats in captions.items():
        if language == "live_chat" or not _normalize_language(language):
            continue
        if formats and all(_is_translated_caption_format(item) for item in formats):
            continue
        available.append(language)
    return available


def _is_translated_caption_format(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return "tlang=" in str(value.get("url") or "")


def _normalize_language(value: Any) -> str | None:
    if not value or not isinstance(value, str):
        return None
    normalized = value.replace("_", "-")
    if normalized.lower().endswith("-orig"):
        normalized = normalized[:-5]
    parts = normalized.split("-")
    if not parts or not parts[0]:
        return None
    return "-".join([parts[0].lower(), *parts[1:]])


def _match_language(
    requested: str,
    available: tuple[str, ...],
    *,
    prefer_original: bool = False,
) -> str | None:
    requested_normalized = _normalize_language(requested)
    if not requested_normalized:
        return None
    requested_base = requested_normalized.split("-")[0]
    if prefer_original:
        original = next(
            (
                value
                for value in available
                if value.lower().endswith("-orig")
                and _base_language(value) == requested_base.lower()
            ),
            None,
        )
        if original:
            return original
    exact = next(
        (value for value in available if value.lower() == requested_normalized.lower()), None
    )
    if exact:
        return exact
    return next(
        (value for value in available if value.split("-")[0].lower() == requested_base.lower()),
        None,
    )


def _base_language(value: str) -> str:
    normalized = _normalize_language(value)
    return normalized.split("-", 1)[0] if normalized else ""


def _safe_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _is_retryable_error(message: str) -> bool:
    lowered = message.lower()
    return any(pattern in lowered for pattern in TRANSIENT_ERROR_PATTERNS)


def _map_download_error(message: str) -> PipelineError:
    lowered = message.lower()
    if "private video" in lowered:
        return PipelineError("VIDEO_PRIVATE", "The video is private or requires account access.")
    if "members-only" in lowered or "members only" in lowered:
        return PipelineError("MEMBERS_ONLY", "The video requires a membership or subscription.")
    if "sign in to confirm your age" in lowered or "age-restricted" in lowered:
        return PipelineError("AGE_RESTRICTED", "The video requires age verification.")
    if "not available in your country" in lowered:
        return PipelineError("GEO_RESTRICTED", "The video is not available in this region.")
    if "live event will begin" in lowered or "is live" in lowered:
        return PipelineError(
            "LIVE_NOT_FINISHED", "Active and upcoming livestreams are not supported."
        )
    if _is_retryable_error(message):
        return PipelineError(
            "YOUTUBE_RATE_LIMITED", "YouTube temporarily rejected the request.", retryable=True
        )
    LOGGER.error("yt-dlp extraction failed: %s", message)
    return PipelineError("YOUTUBE_EXTRACTION_FAILED", "YouTube extraction failed.", retryable=False)
