import json

import pytest

from yt_pro_max.errors import PipelineError
from yt_pro_max.models import (
    JobStage,
    TranscriptSegment,
    TranscriptSource,
    VideoMetadata,
    WordTimestamp,
)
from yt_pro_max.pipeline import TranscriptPipeline
from yt_pro_max.transcription import TranscriptionResult
from yt_pro_max.youtube import CaptionTrack, VideoInspection

UNSET_LANGUAGE = object()


class FakeYouTubeClient:
    def __init__(self, *, caption_track):
        self.caption_track = caption_track
        self.audio_downloaded = False
        self.caption_downloaded = False

    def inspect(self, canonical_url):
        return VideoInspection(
            canonical_url=canonical_url,
            metadata=VideoMetadata(
                id=canonical_url.video_id,
                title="Demo",
                channel="Channel",
                duration_seconds=10,
                webpage_url=canonical_url.url,
            ),
            original_language="en",
            manual_languages=("en",) if self.caption_track else (),
            automatic_languages=(),
            live_status=None,
            raw_info={},
        )

    def select_caption_track(self, inspection, requested_language):
        return self.caption_track

    def download_caption(self, inspection, track, output_dir):
        self.caption_downloaded = True
        path = output_dir / "caption.vtt"
        path.write_text(
            "WEBVTT\n\n00:00.000 --> 00:01.000\nCaption text\n",
            encoding="utf-8",
        )
        return path

    def download_audio(self, inspection, output_dir):
        self.audio_downloaded = True
        path = output_dir / "audio.webm"
        path.write_bytes(b"audio")
        return path


class FakeTranscriber:
    def __init__(self, *, language="vi"):
        self.called = False
        self.language = language
        self.language_hints = []

    def transcribe(self, audio_path, *, language=UNSET_LANGUAGE, progress_callback):
        self.called = True
        if language is not UNSET_LANGUAGE:
            self.language_hints.append(language)
        progress_callback(50)
        return TranscriptionResult(
            language=self.language,
            language_confidence=0.98,
            segments=[
                TranscriptSegment(
                    index=1,
                    start_ms=0,
                    end_ms=1000,
                    text="Whisper text",
                    words=[
                        WordTimestamp(
                            start_ms=0,
                            end_ms=500,
                            text="Whisper",
                            probability=0.97,
                        )
                    ],
                )
            ],
            warnings=[],
        )


class FailingTranscriber:
    def __init__(self, code):
        self.code = code

    def transcribe(self, audio_path, *, language=None, progress_callback):
        raise PipelineError(self.code, "Whisper failed.")


def test_pipeline_uses_caption_without_downloading_audio(settings):
    youtube = FakeYouTubeClient(
        caption_track=CaptionTrack(
            language="en",
            provider_language="en",
            source=TranscriptSource.MANUAL_CAPTION,
        )
    )
    transcriber = FakeTranscriber()
    pipeline = TranscriptPipeline(settings, youtube=youtube, transcriber=transcriber)

    result = pipeline.process(
        job_id="caption-job",
        request_url="https://youtu.be/dQw4w9WgXcQ",
        requested_language=None,
        update=lambda *_args: None,
    )

    assert result.source == TranscriptSource.MANUAL_CAPTION
    assert result.language == "en"
    assert not youtube.audio_downloaded
    assert not transcriber.called
    assert (
        result.artifact_paths["txt"].read_text(encoding="utf-8") == "Title: Demo\n\nCaption text\n"
    )
    assert not (settings.temp_dir / "caption-job").exists()


def test_pipeline_keeps_manual_japanese_caption(settings):
    youtube = FakeYouTubeClient(
        caption_track=CaptionTrack(
            language="ja",
            provider_language="ja",
            source=TranscriptSource.MANUAL_CAPTION,
        )
    )
    transcriber = FakeTranscriber(language="ja")

    result = TranscriptPipeline(settings, youtube=youtube, transcriber=transcriber).process(
        job_id="manual-ja-job",
        request_url="https://youtu.be/dQw4w9WgXcQ",
        requested_language=None,
        update=lambda *_args: None,
    )

    assert result.source == TranscriptSource.MANUAL_CAPTION
    assert not youtube.audio_downloaded
    assert not transcriber.called


@pytest.mark.parametrize("caption_language", ["ja", "ja-JP", "ja-orig"])
def test_pipeline_uses_whisper_for_japanese_automatic_caption(settings, caption_language):
    youtube = FakeYouTubeClient(
        caption_track=CaptionTrack(
            language=caption_language,
            provider_language=caption_language,
            source=TranscriptSource.AUTOMATIC_CAPTION,
        )
    )
    transcriber = FakeTranscriber(language="ja")
    updates = []

    result = TranscriptPipeline(settings, youtube=youtube, transcriber=transcriber).process(
        job_id=f"audio-first-{caption_language}",
        request_url="https://youtu.be/dQw4w9WgXcQ",
        requested_language=None,
        update=lambda stage, progress: updates.append((stage, progress)),
    )

    assert result.source == TranscriptSource.WHISPER
    assert result.language == "ja"
    assert youtube.audio_downloaded
    assert not youtube.caption_downloaded
    assert transcriber.language_hints == ["ja"]
    assert "JAPANESE_AUTO_CAPTION_REPLACED_BY_WHISPER" in result.warnings
    stages = [stage for stage, _progress in updates]
    assert JobStage.DOWNLOADING_AUDIO in stages
    assert JobStage.LOADING_MODEL in stages
    assert JobStage.TRANSCRIBING in stages
    assert JobStage.FETCHING_CAPTION not in stages
    artifact = json.loads(result.artifact_paths["json"].read_text(encoding="utf-8"))
    assert artifact["source"] == "whisper"
    assert artifact["language_confidence"] == 0.98
    assert artifact["segments"][0]["words"][0]["text"] == "Whisper"
    assert artifact["warnings"] == ["JAPANESE_AUTO_CAPTION_REPLACED_BY_WHISPER"]


def test_pipeline_keeps_non_japanese_automatic_caption(settings):
    youtube = FakeYouTubeClient(
        caption_track=CaptionTrack(
            language="en",
            provider_language="en",
            source=TranscriptSource.AUTOMATIC_CAPTION,
        )
    )
    transcriber = FakeTranscriber()

    result = TranscriptPipeline(settings, youtube=youtube, transcriber=transcriber).process(
        job_id="automatic-en-job",
        request_url="https://youtu.be/dQw4w9WgXcQ",
        requested_language=None,
        update=lambda *_args: None,
    )

    assert result.source == TranscriptSource.AUTOMATIC_CAPTION
    assert youtube.caption_downloaded
    assert not youtube.audio_downloaded
    assert not transcriber.called


def test_pipeline_falls_back_to_whisper_when_captions_are_missing(settings):
    youtube = FakeYouTubeClient(caption_track=None)
    transcriber = FakeTranscriber()
    pipeline = TranscriptPipeline(settings, youtube=youtube, transcriber=transcriber)

    result = pipeline.process(
        job_id="whisper-job",
        request_url="https://youtu.be/dQw4w9WgXcQ",
        requested_language=None,
        update=lambda stage, progress: None,
    )

    assert result.source == TranscriptSource.WHISPER
    assert result.language == "vi"
    assert youtube.audio_downloaded
    assert transcriber.called
    assert transcriber.language_hints == []
    assert (
        result.artifact_paths["txt"].read_text(encoding="utf-8") == "Title: Demo\n\nWhisper text\n"
    )


def test_pipeline_fails_japanese_audio_first_on_language_mismatch(settings):
    youtube = FakeYouTubeClient(
        caption_track=CaptionTrack(
            language="ja",
            provider_language="ja",
            source=TranscriptSource.AUTOMATIC_CAPTION,
        )
    )
    transcriber = FakeTranscriber(language="en")

    with pytest.raises(PipelineError) as error:
        TranscriptPipeline(settings, youtube=youtube, transcriber=transcriber).process(
            job_id="audio-first-mismatch",
            request_url="https://youtu.be/dQw4w9WgXcQ",
            requested_language=None,
            update=lambda *_args: None,
        )

    assert error.value.info.code == "TRANSCRIPTION_LANGUAGE_MISMATCH"
    assert youtube.audio_downloaded
    assert not youtube.caption_downloaded


@pytest.mark.parametrize(
    "error_code",
    ["MODEL_LOAD_FAILED", "TRANSCRIPTION_FAILED", "NO_SPEECH_DETECTED"],
)
def test_pipeline_never_falls_back_to_japanese_auto_caption_on_whisper_failure(
    settings, error_code
):
    youtube = FakeYouTubeClient(
        caption_track=CaptionTrack(
            language="ja",
            provider_language="ja",
            source=TranscriptSource.AUTOMATIC_CAPTION,
        )
    )

    with pytest.raises(PipelineError) as error:
        TranscriptPipeline(
            settings,
            youtube=youtube,
            transcriber=FailingTranscriber(error_code),
        ).process(
            job_id=f"audio-first-{error_code}",
            request_url="https://youtu.be/dQw4w9WgXcQ",
            requested_language=None,
            update=lambda *_args: None,
        )

    assert error.value.info.code == error_code
    assert youtube.audio_downloaded
    assert not youtube.caption_downloaded
