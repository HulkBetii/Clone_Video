import pytest

from yt_pro_max.errors import PipelineError
from yt_pro_max.models import TranscriptSource, VideoMetadata
from yt_pro_max.url_utils import CanonicalVideoUrl
from yt_pro_max.youtube import VideoInspection, YouTubeClient


def _inspection(**kwargs):
    defaults = {
        "manual_languages": (),
        "automatic_languages": (),
        "original_language": None,
    }
    defaults.update(kwargs)
    return VideoInspection(
        canonical_url=CanonicalVideoUrl(
            video_id="dQw4w9WgXcQ",
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        metadata=VideoMetadata(
            id="dQw4w9WgXcQ",
            title="Demo",
            duration_seconds=10,
            webpage_url="https://youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        live_status=None,
        raw_info={},
        **defaults,
    )


def test_caption_selection_prefers_manual_and_exact_language(settings):
    client = YouTubeClient(settings)
    track = client.select_caption_track(
        _inspection(manual_languages=("en",), automatic_languages=("en", "vi")),
        "en-US",
    )
    assert track is not None
    assert track.source == TranscriptSource.MANUAL_CAPTION
    assert track.language == "en"
    assert track.provider_language == "en"


def test_caption_selection_uses_original_marker_when_metadata_missing(settings):
    client = YouTubeClient(settings)
    track = client.select_caption_track(
        _inspection(automatic_languages=("en", "vi-orig")),
        None,
    )
    assert track is not None
    assert track.language == "vi"
    assert track.provider_language == "vi-orig"


def test_caption_selection_rejects_missing_requested_language(settings):
    client = YouTubeClient(settings)
    with pytest.raises(PipelineError) as error:
        client.select_caption_track(_inspection(manual_languages=("en",)), "vi")
    assert error.value.info.code == "LANGUAGE_NOT_AVAILABLE"
    assert error.value.info.details["available_languages"] == ["en"]


def test_caption_selection_rejects_ambiguous_original_language(settings):
    client = YouTubeClient(settings)
    with pytest.raises(PipelineError) as error:
        client.select_caption_track(_inspection(manual_languages=("en", "vi")), None)
    assert error.value.info.code == "LANGUAGE_AMBIGUOUS"


@pytest.mark.parametrize(
    ("info", "expected_code"),
    [
        ({"availability": "private", "duration": 10}, "VIDEO_PRIVATE"),
        ({"availability": "subscriber_only", "duration": 10}, "MEMBERS_ONLY"),
        ({"availability": "unavailable", "duration": 10}, "VIDEO_UNAVAILABLE"),
        ({"availability": "public", "duration": 10, "live_status": "is_live"}, "LIVE_NOT_FINISHED"),
        ({"availability": "public", "duration": 21_601}, "DURATION_LIMIT_EXCEEDED"),
    ],
)
def test_inspection_classifies_unusable_videos(settings, monkeypatch, info, expected_code):
    client = YouTubeClient(settings)
    complete_info = {
        "id": "dQw4w9WgXcQ",
        "title": "Demo",
        "webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        **info,
    }
    monkeypatch.setattr(client, "_extract_info", lambda *args, **kwargs: complete_info)

    with pytest.raises(PipelineError) as error:
        client.inspect(
            CanonicalVideoUrl(
                video_id="dQw4w9WgXcQ",
                url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            )
        )

    assert error.value.info.code == expected_code


def test_inspection_excludes_automatically_translated_caption_tracks(settings, monkeypatch):
    client = YouTubeClient(settings)
    info = {
        "id": "dQw4w9WgXcQ",
        "title": "Demo",
        "duration": 10,
        "language": "en",
        "availability": "public",
        "webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "subtitles": {},
        "automatic_captions": {
            "en": [{"url": "https://example.test/caption?lang=en"}],
            "vi": [{"url": "https://example.test/caption?lang=en&tlang=vi"}],
        },
    }
    monkeypatch.setattr(client, "_extract_info", lambda *args, **kwargs: info)

    inspection = client.inspect(
        CanonicalVideoUrl(
            video_id="dQw4w9WgXcQ",
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )
    )

    assert inspection.automatic_languages == ("en",)
