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


class RewriteStage(StrEnum):
    PREPARING_SOURCE = "preparing_source"
    ANALYZING_STYLE = "analyzing_style"
    PLANNING = "planning"
    UPLOADING = "uploading"
    REWRITING = "rewriting"
    EDITING = "editing"
    VALIDATING = "validating"
    RENDERING = "rendering"


class WorkspaceStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_USER = "waiting_for_user"
    COMPLETED = "completed"
    FAILED = "failed"


class WorkspacePhase(StrEnum):
    TRANSCRIPT = "transcript"
    REWRITE = "rewrite"
    COMPLETED = "completed"


class GptRuntimeStatus(StrEnum):
    NOT_CHECKED = "not_checked"
    READY = "ready"
    LOGIN_REQUIRED = "login_required"
    PROFILE_LOCKED = "profile_locked"
    BUSY = "busy"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


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


class CreateRewriteJobRequest(BaseModel):
    transcript_job_id: str = Field(min_length=1, max_length=128)
    force_refresh: bool = False

    @field_validator("transcript_job_id")
    @classmethod
    def strip_transcript_job_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("transcript_job_id must not be empty")
        return normalized


class CreateWorkspaceRequest(CreateTranscriptJobRequest):
    auto_rewrite: bool = True


class DeleteWorkspacesRequest(BaseModel):
    transcript_job_ids: list[str] = Field(min_length=1, max_length=100)

    @field_validator("transcript_job_ids")
    @classmethod
    def normalize_transcript_job_ids(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not normalized:
            raise ValueError("transcript_job_ids must not be empty")
        return normalized


class DeleteWorkspacesResponse(BaseModel):
    deleted_ids: list[str]


class OpenGptRuntimeRequest(BaseModel):
    rewrite_job_id: str | None = Field(default=None, max_length=128)

    @field_validator("rewrite_job_id")
    @classmethod
    def normalize_rewrite_job_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


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
    request_url: str
    auto_rewrite_requested: bool = False
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


class RewriteArtifactLinks(BaseModel):
    txt: str


class RewriteValidationSummary(BaseModel):
    passed: bool
    style_score: float = Field(ge=0, le=100)
    coverage_score: float = Field(ge=0, le=100)
    language_match: bool
    tts_ready: bool
    unsupported_claims: list[str] = Field(default_factory=list)
    missing_points: list[str] = Field(default_factory=list)
    length_ratio: float = Field(ge=0)


class RewriteJobResponse(BaseModel):
    id: str
    transcript_job_id: str
    status: JobStatus
    stage: RewriteStage | None = None
    progress: int = Field(ge=0, le=100)
    video: VideoMetadata | None = None
    language: str | None = None
    source_length: int | None = Field(default=None, ge=0)
    output_length: int | None = Field(default=None, ge=0)
    sections_completed: int = Field(default=0, ge=0)
    sections_total: int = Field(default=0, ge=0)
    title: str | None = None
    validation: RewriteValidationSummary | None = None
    artifacts: RewriteArtifactLinks | None = None
    warnings: list[str] = Field(default_factory=list)
    error: ErrorInfo | None = None
    cached: bool = False
    created_at: str
    updated_at: str


class WorkspaceResponse(BaseModel):
    id: str
    status: WorkspaceStatus
    phase: WorkspacePhase
    progress: int = Field(ge=0, le=100)
    auto_rewrite: bool
    request_url: str
    transcript: JobResponse
    rewrite: RewriteJobResponse | None = None
    action_required: ErrorInfo | None = None
    created_at: str
    updated_at: str


class WorkspaceListResponse(BaseModel):
    items: list[WorkspaceResponse]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class GptRuntimeResponse(BaseModel):
    status: GptRuntimeStatus
    profile_id: str
    profile_exists: bool
    browser_running: bool
    authenticated: bool | None = None
    active_job_id: str | None = None
    queue_depth: int = Field(default=0, ge=0)
    error: ErrorInfo | None = None


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


class TranscriptReconciliationItem(BaseModel):
    segment_index: int
    start_ms: int
    end_ms: int
    primary_text: str
    caption_text: str | None = None
    secondary_text: str | None = None
    final_text: str
    decision: str
    triggers: list[str] = Field(default_factory=list)
    word_start: int | None = None
    word_end: int | None = None
    priority_tier: int | None = None
    alignment_coverage: float | None = None
    temporal_overlap: float | None = None
    primary_probability: float | None = None
    secondary_mean_probability: float | None = None
    secondary_min_probability: float | None = None
    decision_reason: str | None = None
    corrected_words: int = 0


class TranscriptReconciliation(BaseModel):
    strategy: str = "conservative_consensus"
    alignment_version: str = "monotonic_char_word_v3"
    reference_source: str = "youtube_automatic_caption"
    secondary_model: str
    alignment_coverage: float | None = None
    compared_segments: int = 0
    suspicious_segments: int = 0
    selected_spans: int = 0
    processed_spans: int = 0
    selected_windows: int = 0
    selected_duration_ms: int = 0
    secondary_windows: int = 0
    secondary_duration_ms: int = 0
    corrected_segments: int = 0
    corrected_words: int = 0
    unresolved_segments: int = 0
    skipped_segments: int = 0
    items: list[TranscriptReconciliationItem] = Field(default_factory=list)


class TranscriptArtifact(BaseModel):
    schema_version: int = 3
    job_id: str
    source: TranscriptSource
    language: str
    language_confidence: float | None = None
    video: VideoMetadata
    segments: list[TranscriptSegment]
    warnings: list[str] = Field(default_factory=list)
    reconciliation: TranscriptReconciliation | None = None
