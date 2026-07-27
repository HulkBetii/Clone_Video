import json
from pathlib import Path

from yt_pro_max.artifacts import parse_vtt, render_artifacts, to_srt
from yt_pro_max.models import TranscriptSegment, TranscriptSource, VideoMetadata


def test_parse_vtt_normalizes_duplicate_and_overlapping_cues(tmp_path: Path):
    caption_path = tmp_path / "captions.vtt"
    caption_path.write_text(
        """WEBVTT

00:00.000 --> 00:01.000
Hello &amp; welcome

00:00.900 --> 00:01.500
Hello &amp; welcome

00:01.500 --> 00:02.000
<c.color>Next line</c>
""",
        encoding="utf-8",
    )

    segments = parse_vtt(caption_path)

    assert len(segments) == 2
    assert segments[0].text == "Hello & welcome"
    assert segments[0].end_ms == 1500
    assert segments[1].text == "Next line"


def test_parse_vtt_collapses_youtube_rolling_captions(tmp_path: Path):
    caption_path = tmp_path / "rolling.vtt"
    caption_path.write_text(
        """WEBVTT
Kind: captions

00:00:00.160 --> 00:00:02.610

Alpha<00:00:00.500><c> phrase</c>

00:00:02.610 --> 00:00:02.620
Alpha phrase

00:00:02.620 --> 00:00:05.430
Alpha phrase
Beta<00:00:03.000><c> phrase</c>

00:00:05.430 --> 00:00:05.440
Beta phrase

00:00:05.440 --> 00:00:08.669
Beta phrase
Gamma<00:00:06.000><c> phrase</c>
""",
        encoding="utf-8",
    )

    segments = parse_vtt(caption_path)

    assert [(segment.start_ms, segment.end_ms, segment.text) for segment in segments] == [
        (160, 2620, "Alpha phrase"),
        (2620, 5440, "Beta phrase"),
        (5440, 8669, "Gamma phrase"),
    ]


def test_parse_vtt_keeps_legitimate_repeated_text_after_a_gap(tmp_path: Path):
    caption_path = tmp_path / "repeated.vtt"
    caption_path.write_text(
        """WEBVTT

00:00.000 --> 00:01.000
Again

00:03.000 --> 00:04.000
Again
""",
        encoding="utf-8",
    )

    segments = parse_vtt(caption_path)

    assert len(segments) == 2
    assert segments[0].text == segments[1].text == "Again"


def test_render_artifacts_writes_utf8_srt_txt_and_json(tmp_path: Path):
    video = VideoMetadata(
        id="dQw4w9WgXcQ",
        title="Demo",
        channel="Channel",
        duration_seconds=2,
        webpage_url="https://youtube.com/watch?v=dQw4w9WgXcQ",
    )
    segments = [
        TranscriptSegment(index=99, start_ms=0, end_ms=1234, text="Xin chào"),
    ]

    files = render_artifacts(
        output_dir=tmp_path / "out",
        job_id="job-1",
        source=TranscriptSource.MANUAL_CAPTION,
        language="vi",
        language_confidence=None,
        video=video,
        segments=segments,
        warnings=[],
    )

    assert (
        files["srt"].read_text(encoding="utf-8") == "1\n00:00:00,000 --> 00:00:01,234\nXin chào\n"
    )
    assert files["txt"].read_text(encoding="utf-8") == "Xin chào\n"
    artifact = json.loads(files["json"].read_text(encoding="utf-8"))
    assert artifact["schema_version"] == 1
    assert artifact["segments"][0]["index"] == 1


def test_srt_supports_timestamps_longer_than_one_hour():
    segment = TranscriptSegment(index=1, start_ms=3_600_001, end_ms=3_601_002, text="Long")
    assert "01:00:00,001 --> 01:00:01,002" in to_srt([segment])
