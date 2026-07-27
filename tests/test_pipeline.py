from yt_pro_max.models import (
    TranscriptSegment,
    TranscriptSource,
    VideoMetadata,
)
from yt_pro_max.pipeline import TranscriptPipeline
from yt_pro_max.transcription import TranscriptionResult
from yt_pro_max.youtube import CaptionTrack, VideoInspection


class FakeYouTubeClient:
    def __init__(self, *, caption_track):
        self.caption_track = caption_track
        self.audio_downloaded = False

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
    def __init__(self):
        self.called = False

    def transcribe(self, audio_path, *, progress_callback):
        self.called = True
        progress_callback(50)
        return TranscriptionResult(
            language="vi",
            language_confidence=0.98,
            segments=[TranscriptSegment(index=1, start_ms=0, end_ms=1000, text="Whisper text")],
            warnings=[],
        )


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
    updates = []

    result = pipeline.process(
        job_id="caption-job",
        request_url="https://youtu.be/dQw4w9WgXcQ",
        requested_language=None,
        update=lambda stage, progress: updates.append((stage, progress)),
    )

    assert result.source == TranscriptSource.MANUAL_CAPTION
    assert result.language == "en"
    assert not youtube.audio_downloaded
    assert not transcriber.called
    assert result.artifact_paths["txt"].read_text(encoding="utf-8") == "Caption text\n"
    assert not (settings.temp_dir / "caption-job").exists()


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
    assert result.artifact_paths["txt"].read_text(encoding="utf-8") == "Whisper text\n"
