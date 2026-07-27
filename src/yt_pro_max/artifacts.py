from __future__ import annotations

import html
import json
import re
from collections.abc import Iterable
from pathlib import Path

import webvtt

from yt_pro_max.errors import PipelineError
from yt_pro_max.models import (
    TranscriptArtifact,
    TranscriptSegment,
    TranscriptSource,
    VideoMetadata,
)

TAG_PATTERN = re.compile(r"<[^>]+>")
WHITESPACE_PATTERN = re.compile(r"\s+")


def parse_vtt(path: Path) -> list[TranscriptSegment]:
    try:
        captions = webvtt.read(str(path))
    except Exception as error:
        raise PipelineError(
            "CAPTION_PARSE_FAILED",
            "The downloaded caption file could not be parsed.",
            details={"format": "vtt"},
        ) from error

    segments: list[TranscriptSegment] = []
    for caption in captions:
        text = _clean_text(caption.text)
        if not text:
            continue
        start_ms = _timestamp_to_ms(caption.start)
        end_ms = _timestamp_to_ms(caption.end)
        if end_ms <= start_ms:
            continue
        if segments and text == segments[-1].text and start_ms <= segments[-1].end_ms + 1000:
            segments[-1].end_ms = max(segments[-1].end_ms, end_ms)
            continue
        if segments and start_ms < segments[-1].end_ms:
            start_ms = segments[-1].end_ms
        if end_ms <= start_ms:
            continue
        segments.append(
            TranscriptSegment(
                index=len(segments) + 1,
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
            )
        )
    if not segments:
        raise PipelineError(
            "NO_CAPTION_TEXT", "The selected caption track contains no usable text."
        )
    return segments


def render_artifacts(
    *,
    output_dir: Path,
    job_id: str,
    source: TranscriptSource,
    language: str,
    language_confidence: float | None,
    video: VideoMetadata,
    segments: Iterable[TranscriptSegment],
    warnings: list[str],
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    normalized_segments = [
        segment.model_copy(update={"index": index}) for index, segment in enumerate(segments, 1)
    ]
    artifact = TranscriptArtifact(
        job_id=job_id,
        source=source,
        language=language,
        language_confidence=language_confidence,
        video=video,
        segments=normalized_segments,
        warnings=warnings,
    )
    files = {
        "srt": output_dir / f"{video.id}.{language}.srt",
        "txt": output_dir / f"{video.id}.{language}.txt",
        "json": output_dir / f"{video.id}.{language}.json",
    }
    _atomic_write(files["srt"], to_srt(normalized_segments))
    _atomic_write(files["txt"], to_text(normalized_segments))
    _atomic_write(
        files["json"],
        json.dumps(artifact.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
    )
    return files


def to_srt(segments: Iterable[TranscriptSegment]) -> str:
    blocks = []
    for index, segment in enumerate(segments, 1):
        start = _format_timestamp(segment.start_ms)
        end = _format_timestamp(segment.end_ms)
        blocks.append(f"{index}\n{start} --> {end}\n{segment.text}\n")
    return "\n".join(blocks)


def to_text(segments: Iterable[TranscriptSegment]) -> str:
    return " ".join(segment.text for segment in segments).strip() + "\n"


def _clean_text(value: str) -> str:
    value = html.unescape(value)
    value = TAG_PATTERN.sub("", value)
    return WHITESPACE_PATTERN.sub(" ", value).strip()


def _timestamp_to_ms(value: str) -> int:
    parts = value.replace(",", ".").split(":")
    if len(parts) == 2:
        hours = 0
        minutes, seconds = parts
    elif len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        raise ValueError(f"invalid WebVTT timestamp: {value}")
    whole_seconds, milliseconds = seconds.split(".", 1)
    return (
        int(hours) * 3_600_000
        + int(minutes) * 60_000
        + int(whole_seconds) * 1_000
        + int(milliseconds.ljust(3, "0")[:3])
    )


def _format_timestamp(milliseconds: int) -> str:
    milliseconds = max(0, int(milliseconds))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _atomic_write(path: Path, content: str) -> None:
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(content, encoding="utf-8", newline="\n")
    temporary_path.replace(path)
