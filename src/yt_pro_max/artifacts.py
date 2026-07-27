from __future__ import annotations

import html
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from yt_pro_max.errors import PipelineError
from yt_pro_max.models import (
    TranscriptArtifact,
    TranscriptSegment,
    TranscriptSource,
    VideoMetadata,
)

TAG_PATTERN = re.compile(r"<[^>]+>")
WHITESPACE_PATTERN = re.compile(r"\s+")
TIMING_PATTERN = re.compile(
    r"^(?P<start>(?:\d{2}:)?\d{2}:\d{2}[.,]\d{3})\s+-->\s+"
    r"(?P<end>(?:\d{2}:)?\d{2}:\d{2}[.,]\d{3})(?:\s|$)"
)
INLINE_TIMESTAMP_PATTERN = re.compile(r"<(?:\d{2}:)?\d{2}:\d{2}[.,]\d{3}>")
ROLLING_CUE_GAP_MS = 1000
MIN_TEXT_OVERLAP = 4


@dataclass(frozen=True)
class _VttCue:
    start_ms: int
    end_ms: int
    lines: tuple[str, ...]
    has_inline_timestamps: bool


def parse_vtt(path: Path) -> list[TranscriptSegment]:
    try:
        cues = _read_vtt_cues(path)
    except Exception as error:
        raise PipelineError(
            "CAPTION_PARSE_FAILED",
            "The downloaded caption file could not be parsed.",
            details={"format": "vtt"},
        ) from error

    segments = (
        _collapse_rolling_cues(cues) if _contains_rolling_captions(cues) else _normalize_cues(cues)
    )
    if not segments:
        raise PipelineError(
            "NO_CAPTION_TEXT", "The selected caption track contains no usable text."
        )
    return [
        segment.model_copy(update={"index": index}) for index, segment in enumerate(segments, 1)
    ]


def _read_vtt_cues(path: Path) -> list[_VttCue]:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    cues = []
    line_index = 0
    while line_index < len(lines):
        timing_match = TIMING_PATTERN.match(lines[line_index].strip())
        if not timing_match:
            line_index += 1
            continue

        start_ms = _timestamp_to_ms(timing_match.group("start"))
        end_ms = _timestamp_to_ms(timing_match.group("end"))
        line_index += 1
        while line_index < len(lines) and not lines[line_index].strip():
            line_index += 1
        if line_index < len(lines) and TIMING_PATTERN.match(lines[line_index].strip()):
            continue
        raw_text_lines = []
        while line_index < len(lines) and lines[line_index].strip():
            raw_text_lines.append(lines[line_index])
            line_index += 1

        cleaned_lines = tuple(
            cleaned for raw_line in raw_text_lines if (cleaned := _clean_text(raw_line))
        )
        if cleaned_lines and end_ms > start_ms:
            cues.append(
                _VttCue(
                    start_ms=start_ms,
                    end_ms=end_ms,
                    lines=cleaned_lines,
                    has_inline_timestamps=any(
                        INLINE_TIMESTAMP_PATTERN.search(raw_line) for raw_line in raw_text_lines
                    ),
                )
            )
    return cues


def _contains_rolling_captions(cues: list[_VttCue]) -> bool:
    if any(cue.has_inline_timestamps for cue in cues):
        return True
    for previous, current in zip(cues, cues[1:], strict=False):
        close_together = current.start_ms <= previous.end_ms + ROLLING_CUE_GAP_MS
        has_multiple_lines = len(previous.lines) > 1 or len(current.lines) > 1
        if close_together and has_multiple_lines and set(previous.lines) & set(current.lines):
            return True
    return False


def _normalize_cues(cues: list[_VttCue]) -> list[TranscriptSegment]:
    segments: list[TranscriptSegment] = []
    for cue in cues:
        text = " ".join(cue.lines)
        if (
            segments
            and text == segments[-1].text
            and cue.start_ms <= segments[-1].end_ms + ROLLING_CUE_GAP_MS
        ):
            segments[-1].end_ms = max(segments[-1].end_ms, cue.end_ms)
            continue
        start_ms = max(cue.start_ms, segments[-1].end_ms) if segments else cue.start_ms
        if cue.end_ms > start_ms:
            segments.append(
                TranscriptSegment(
                    index=0,
                    start_ms=start_ms,
                    end_ms=cue.end_ms,
                    text=text,
                )
            )
    return segments


def _collapse_rolling_cues(cues: list[_VttCue]) -> list[TranscriptSegment]:
    segments: list[TranscriptSegment] = []
    previous_lines: tuple[str, ...] = ()
    previous_end_ms: int | None = None

    for cue in cues:
        if previous_end_ms is None or cue.start_ms > previous_end_ms + ROLLING_CUE_GAP_MS:
            previous_lines = ()

        new_lines = []
        for line in cue.lines:
            previous_line = _find_related_line(line, previous_lines)
            if previous_line is None:
                new_lines.append(line)
                continue
            _merge_extended_line(segments, previous_line, line)

        if new_lines:
            _append_segment(segments, cue, " ".join(new_lines))
        elif not _extend_latest_carried_segment(segments, cue):
            _append_segment(segments, cue, " ".join(cue.lines))

        previous_lines = cue.lines
        previous_end_ms = cue.end_ms
    return segments


def _find_related_line(current: str, previous_lines: tuple[str, ...]) -> str | None:
    for previous in previous_lines:
        if current == previous or current.startswith(previous) or previous.startswith(current):
            return previous
        overlap = _suffix_prefix_overlap(previous, current)
        if overlap >= MIN_TEXT_OVERLAP:
            return previous
    return None


def _merge_extended_line(
    segments: list[TranscriptSegment],
    previous_line: str,
    current_line: str,
) -> None:
    if not segments or current_line == previous_line:
        return
    for segment in reversed(segments):
        if segment.text != previous_line:
            continue
        if current_line.startswith(previous_line):
            segment.text = current_line
        elif previous_line.startswith(current_line):
            return
        else:
            overlap = _suffix_prefix_overlap(previous_line, current_line)
            if overlap >= MIN_TEXT_OVERLAP:
                segment.text = previous_line + current_line[overlap:]
        return


def _extend_latest_carried_segment(
    segments: list[TranscriptSegment],
    cue: _VttCue,
) -> bool:
    for segment in reversed(segments):
        if any(_texts_related(segment.text, line) for line in cue.lines):
            segment.end_ms = max(segment.end_ms, cue.end_ms)
            return True
    return False


def _append_segment(
    segments: list[TranscriptSegment],
    cue: _VttCue,
    text: str,
) -> None:
    start_ms = max(cue.start_ms, segments[-1].end_ms) if segments else cue.start_ms
    if cue.end_ms <= start_ms:
        return
    segments.append(
        TranscriptSegment(
            index=0,
            start_ms=start_ms,
            end_ms=cue.end_ms,
            text=text,
        )
    )


def _texts_related(first: str, second: str) -> bool:
    return (
        first == second
        or first.startswith(second)
        or second.startswith(first)
        or _suffix_prefix_overlap(first, second) >= MIN_TEXT_OVERLAP
    )


def _suffix_prefix_overlap(first: str, second: str) -> int:
    max_length = min(len(first), len(second))
    for length in range(max_length, MIN_TEXT_OVERLAP - 1, -1):
        if first[-length:] == second[:length]:
            return length
    return 0


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
