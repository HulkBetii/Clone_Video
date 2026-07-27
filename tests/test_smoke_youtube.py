import os

import pytest

from yt_pro_max.url_utils import canonicalize_youtube_url
from yt_pro_max.youtube import YouTubeClient


@pytest.mark.skipif(
    not os.getenv("YT_PRO_MAX_SMOKE_URL"),
    reason="Set YT_PRO_MAX_SMOKE_URL to run the live YouTube smoke test.",
)
def test_live_youtube_metadata_smoke(settings):
    url = os.environ["YT_PRO_MAX_SMOKE_URL"]
    inspection = YouTubeClient(settings).inspect(canonicalize_youtube_url(url))
    assert inspection.metadata.id
    assert inspection.metadata.title
    assert inspection.metadata.duration_seconds
