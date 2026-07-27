import pytest

from yt_pro_max.errors import PipelineError
from yt_pro_max.url_utils import canonicalize_youtube_url


@pytest.mark.parametrize(
    ("value", "video_id"),
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?t=10", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ],
)
def test_canonicalize_supported_urls(value, video_id):
    result = canonicalize_youtube_url(value)
    assert result.video_id == video_id
    assert result.url == f"https://www.youtube.com/watch?v={video_id}"


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/playlist?list=PL1234567890",
        "not-a-url",
        "https://www.youtube.com/watch?v=short",
    ],
)
def test_canonicalize_rejects_unsupported_urls(value):
    with pytest.raises(PipelineError) as error:
        canonicalize_youtube_url(value)
    assert error.value.info.code in {"INVALID_YOUTUBE_URL", "PLAYLIST_NOT_SUPPORTED"}
