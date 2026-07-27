from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

LANGUAGE_CODE_PATTERN = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class JobStage(StrEnum):
    INSPECTING = "inspecting"
    FETCHING_CAPTION = "fetching_caption"
    DOWNLOADING_AUDIO = "downloading_audio"
    LOADING_MODEL = "loading_model"
    TRANSCRIBING = "transcribing"
    RENDERING = "rendering"


class TranscriptSource(StrEnum):
    MANUAL_CAPTION = "manual_caption"
    AUTOMATIC_CAPTION = "automatic_caption"
    WHISPER = "whisper"


class CreateTranscriptJobRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    language: str | None = None
    force_refresh: bool = False

    @field_validator("url")
    @classmethod
    def strip_url(cls, value: str) -> str:
        return value.strip()

    @field_validator("language")
    @classmethod
    def normalize_language(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        if not LANGUAGE_CODE_PATTERN.fullmatch(normalized):
            raise ValueError("language must be a BCP-47-like language code")
        parts = normalized.split("-")
        return "-".join([parts[0].lower(), *parts[1:]])


class ErrorInfo(BaseModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class VideoMetadata(BaseModel):
    id: str
    title: str
    channel: str | None = None
    duration_seconds: int | None = None
    webpage_url: str


class ArtifactLinks(BaseModel):
    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    srt: str
    txt: str
    json_url: str = Field(alias="json")


class JobResponse(BaseModel):
    id: str
    status: JobStatus
    stage: JobStage | None = None
    progress: int = Field(ge=0, le=100)
    source: TranscriptSource | None = None
    requested_language: str | None = None
    language: str | None = None
    language_confidence: float | None = None
    video: VideoMetadata | None = None
    artifacts: ArtifactLinks | None = None
    warnings: list[str] = Field(default_factory=list)
    error: ErrorInfo | None = None
    cached: bool = False
    created_at: str
    updated_at: str


class WordTimestamp(BaseModel):
    start_ms: int
    end_ms: int
    text: str
    probability: float | None = None


class TranscriptSegment(BaseModel):
    index: int
    start_ms: int
    end_ms: int
    text: str
    words: list[WordTimestamp] | None = None


class TranscriptArtifact(BaseModel):
    schema_version: int = 1
    job_id: str
    source: TranscriptSource
    language: str
    language_confidence: float | None = None
    video: VideoMetadata
    segments: list[TranscriptSegment]
    warnings: list[str] = Field(default_factory=list)
