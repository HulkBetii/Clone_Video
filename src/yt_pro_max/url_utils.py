from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from yt_pro_max.errors import PipelineError

VIDEO_ID_LENGTH = 11
ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
    "youtu.be",
}


@dataclass(frozen=True)
class CanonicalVideoUrl:
    video_id: str
    url: str


def canonicalize_youtube_url(value: str) -> CanonicalVideoUrl:
    parsed = urlparse(value.strip())
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or hostname not in ALLOWED_HOSTS:
        raise PipelineError(
            "INVALID_YOUTUBE_URL",
            "URL must point to a supported YouTube video.",
            details={"allowed_hosts": sorted(ALLOWED_HOSTS)},
        )

    video_id = _extract_video_id(parsed)
    if video_id is None:
        if _is_playlist_only(parsed):
            raise PipelineError(
                "PLAYLIST_NOT_SUPPORTED",
                "Only one video URL is supported; playlist-only URLs are not accepted.",
            )
        raise PipelineError(
            "INVALID_YOUTUBE_URL",
            "URL does not contain a valid YouTube video ID.",
        )
    return CanonicalVideoUrl(video_id=video_id, url=f"https://www.youtube.com/watch?v={video_id}")


def _extract_video_id(parsed) -> str | None:
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/")[0]
    else:
        query_video_id = parse_qs(parsed.query).get("v", [None])[0]
        path_parts = [part for part in parsed.path.split("/") if part]
        candidate = query_video_id or _path_video_id(path_parts)
    if candidate and _is_video_id(candidate):
        return candidate
    return None


def _path_video_id(path_parts: list[str]) -> str | None:
    if len(path_parts) >= 2 and path_parts[0].lower() in {"shorts", "embed", "live", "v"}:
        return path_parts[1]
    return None


def _is_video_id(value: str) -> bool:
    return len(value) == VIDEO_ID_LENGTH and all(char.isalnum() or char in "-_" for char in value)


def _is_playlist_only(parsed) -> bool:
    return "list" in parse_qs(parsed.query) and "v" not in parse_qs(parsed.query)
