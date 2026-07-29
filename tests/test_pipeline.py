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
    def __init__(self, *, caption_track, caption_text="Caption text"):
        self.caption_track = caption_track
        self.caption_text = caption_text
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
            f"WEBVTT\n\n00:00.000 --> 00:01.000\n{self.caption_text}\n",
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


class FailingCaptionYouTubeClient(FakeYouTubeClient):
    def download_caption(self, inspection, track, output_dir):
        self.caption_downloaded = True
        raise PipelineError("CAPTION_DOWNLOAD_FAILED", "Caption failed.")


class JapaneseReconcilingTranscriber(FakeTranscriber):
    def transcribe(self, audio_path, *, language=UNSET_LANGUAGE, progress_callback):
        self.called = True
        if language is not UNSET_LANGUAGE:
            self.language_hints.append(language)
        return TranscriptionResult(
            language="ja",
            language_confidence=0.99,
            segments=[
                TranscriptSegment(
                    index=1,
                    start_ms=0,
                    end_ms=1000,
                    text="これは太目が覚める",
                    words=[
                        WordTimestamp(
                            start_ms=0,
                            end_ms=250,
                            text="これは太",
                            probability=0.97,
                        ),
                        WordTimestamp(
                            start_ms=250,
                            end_ms=500,
                            text="目",
                            probability=0.4,
                        ),
                        WordTimestamp(
                            start_ms=500,
                            end_ms=650,
                            text="が",
                            probability=0.97,
                        ),
                        WordTimestamp(
                            start_ms=650,
                            end_ms=1000,
                            text="覚める",
                            probability=0.97,
                        ),
                    ],
                )
            ],
            warnings=[],
        )

    def transcribe_window(
        self,
        audio_path,
        *,
        start_ms,
        end_ms,
        language,
        model_name,
        output_dir,
    ):
        return TranscriptionResult(
            language="ja",
            language_confidence=0.99,
            segments=[
                TranscriptSegment(
                    index=1,
                    start_ms=0,
                    end_ms=1000,
                    text="これは太めが覚める",
                    words=[
                        WordTimestamp(
                            start_ms=0,
                            end_ms=250,
                            text="これは太",
                            probability=0.98,
                        ),
                        WordTimestamp(
                            start_ms=250,
                            end_ms=500,
                            text="め",
                            probability=0.98,
                        ),
                        WordTimestamp(
                            start_ms=500,
                            end_ms=650,
                            text="が",
                            probability=0.98,
                        ),
                        WordTimestamp(
                            start_ms=650,
                            end_ms=1000,
                            text="覚める",
                            probability=0.98,
                        ),
                    ],
                )
            ],
            warnings=[],
        )


class AmbiguousJapaneseReconcilingTranscriber(JapaneseReconcilingTranscriber):
    def transcribe_window(
        self,
        audio_path,
        *,
        start_ms,
        end_ms,
        language,
        model_name,
        output_dir,
    ):
        return TranscriptionResult(
            language="ja",
            language_confidence=0.99,
            segments=[
                TranscriptSegment(
                    index=1,
                    start_ms=0,
                    end_ms=1000,
                    text="これは太ふとが覚める",
                    words=[
                        WordTimestamp(
                            start_ms=0,
                            end_ms=250,
                            text="これは太",
                            probability=0.98,
                        ),
                        WordTimestamp(
                            start_ms=250,
                            end_ms=500,
                            text="ふと",
                            probability=0.98,
                        ),
                        WordTimestamp(
                            start_ms=500,
                            end_ms=650,
                            text="が",
                            probability=0.98,
                        ),
                        WordTimestamp(
                            start_ms=650,
                            end_ms=1000,
                            text="覚める",
                            probability=0.98,
                        ),
                    ],
                )
            ],
            warnings=[],
        )


class FailingReconciliationTranscriber(JapaneseReconcilingTranscriber):
    def transcribe_window(self, *args, **kwargs):
        raise PipelineError("MODEL_LOAD_FAILED", "Secondary model failed.")


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
        ),
        caption_text="Whisper",
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
    assert youtube.caption_downloaded
    assert transcriber.language_hints == ["ja"]
    assert "JAPANESE_AUTO_CAPTION_REPLACED_BY_WHISPER" in result.warnings
    stages = [stage for stage, _progress in updates]
    assert JobStage.DOWNLOADING_AUDIO in stages
    assert JobStage.LOADING_MODEL in stages
    assert JobStage.TRANSCRIBING in stages
    assert JobStage.FETCHING_CAPTION in stages
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


def test_pipeline_applies_conservative_japanese_reconciliation(settings):
    youtube = FakeYouTubeClient(
        caption_track=CaptionTrack(
            language="ja",
            provider_language="ja",
            source=TranscriptSource.AUTOMATIC_CAPTION,
        ),
        caption_text="これは太めが覚める",
    )
    transcriber = JapaneseReconcilingTranscriber()

    result = TranscriptPipeline(settings, youtube=youtube, transcriber=transcriber).process(
        job_id="audio-first-reconciled",
        request_url="https://youtu.be/dQw4w9WgXcQ",
        requested_language=None,
        update=lambda *_args: None,
    )

    assert result.artifact_paths["txt"].read_text(encoding="utf-8").endswith(
        "これは太めが覚める\n"
    )
    assert result.reconciliation is not None
    assert result.reconciliation.corrected_segments == 1
    assert result.reconciliation.corrected_words == 1
    artifact = json.loads(result.artifact_paths["json"].read_text(encoding="utf-8"))
    assert artifact["schema_version"] == 3
    assert artifact["reconciliation"]["secondary_model"] == settings.reconciliation_model
    assert artifact["reconciliation"]["alignment_version"] == "monotonic_char_word_v1"
    assert artifact["reconciliation"]["selected_spans"] == 1
    assert artifact["reconciliation"]["processed_spans"] == 1
    assert artifact["reconciliation"]["corrected_words"] == 1
    item = artifact["reconciliation"]["items"][0]
    assert item["word_start"] == 1
    assert item["word_end"] == 2
    assert item["priority_tier"] == 1
    assert item["decision_reason"] == "caption_secondary_consensus"
    assert item["corrected_words"] == 1


def test_pipeline_keeps_primary_when_caption_and_secondary_disagree_locally(settings):
    youtube = FakeYouTubeClient(
        caption_track=CaptionTrack(
            language="ja",
            provider_language="ja",
            source=TranscriptSource.AUTOMATIC_CAPTION,
        ),
        caption_text="これは太めが覚める",
    )

    result = TranscriptPipeline(
        settings,
        youtube=youtube,
        transcriber=AmbiguousJapaneseReconcilingTranscriber(),
    ).process(
        job_id="audio-first-ambiguous",
        request_url="https://youtu.be/dQw4w9WgXcQ",
        requested_language=None,
        update=lambda *_args: None,
    )

    assert result.artifact_paths["txt"].read_text(encoding="utf-8").endswith(
        "これは太目が覚める\n"
    )
    assert result.reconciliation is not None
    assert result.reconciliation.corrected_words == 0
    assert result.reconciliation.unresolved_segments == 1
    item = result.reconciliation.items[0]
    assert item.primary_text == "目"
    assert item.caption_text == "め"
    assert item.secondary_text == "ふと"
    assert item.decision_reason == "consensus_missing"


def test_pipeline_keeps_primary_when_japanese_caption_reference_fails(settings):
    youtube = FailingCaptionYouTubeClient(
        caption_track=CaptionTrack(
            language="ja",
            provider_language="ja",
            source=TranscriptSource.AUTOMATIC_CAPTION,
        )
    )

    result = TranscriptPipeline(
        settings,
        youtube=youtube,
        transcriber=FakeTranscriber(language="ja"),
    ).process(
        job_id="audio-first-reference-failed",
        request_url="https://youtu.be/dQw4w9WgXcQ",
        requested_language=None,
        update=lambda *_args: None,
    )

    assert result.source == TranscriptSource.WHISPER
    assert "JAPANESE_RECONCILIATION_UNAVAILABLE" in result.warnings
    assert result.reconciliation is None


def test_pipeline_keeps_primary_when_secondary_reconciliation_fails(settings):
    youtube = FakeYouTubeClient(
        caption_track=CaptionTrack(
            language="ja",
            provider_language="ja",
            source=TranscriptSource.AUTOMATIC_CAPTION,
        ),
        caption_text="これは太めが覚める",
    )

    result = TranscriptPipeline(
        settings,
        youtube=youtube,
        transcriber=FailingReconciliationTranscriber(),
    ).process(
        job_id="audio-first-secondary-failed",
        request_url="https://youtu.be/dQw4w9WgXcQ",
        requested_language=None,
        update=lambda *_args: None,
    )

    assert result.artifact_paths["txt"].read_text(encoding="utf-8").endswith(
        "これは太目が覚める\n"
    )
    assert "JAPANESE_RECONCILIATION_UNAVAILABLE" in result.warnings
    assert "JAPANESE_RECONCILIATION_UNRESOLVED" in result.warnings
    assert result.reconciliation.unresolved_segments == 1


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
    assert youtube.caption_downloaded


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
    assert youtube.caption_downloaded
