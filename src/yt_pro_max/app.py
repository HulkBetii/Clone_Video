from __future__ import annotations

import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from yt_pro_max.config import Settings, get_settings
from yt_pro_max.errors import PipelineError
from yt_pro_max.models import (
    ArtifactLinks,
    CreateTranscriptJobRequest,
    ErrorInfo,
    JobResponse,
    JobStatus,
    VideoMetadata,
)
from yt_pro_max.pipeline import TranscriptPipeline
from yt_pro_max.repository import JobRepository, StoredJob
from yt_pro_max.url_utils import canonicalize_youtube_url
from yt_pro_max.worker import JobWorker


def create_app(
    settings: Settings | None = None,
    *,
    repository: JobRepository | None = None,
    pipeline: TranscriptPipeline | None = None,
) -> FastAPI:
    app_settings = settings or get_settings()
    app_settings.ensure_directories()
    app_repository = repository or JobRepository(app_settings.database_path)
    app_pipeline = pipeline or TranscriptPipeline(app_settings)
    app_worker = JobWorker(app_repository, app_pipeline)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app_repository.initialize()
        await app_worker.start()
        yield
        await app_worker.stop()

    app = FastAPI(title=app_settings.app_name, version="0.1.0", lifespan=lifespan)
    app.state.settings = app_settings
    app.state.repository = app_repository
    app.state.pipeline = app_pipeline
    app.state.worker = app_worker

    @app.post("/api/v1/transcript-jobs", response_model=JobResponse, name="create_job")
    async def create_job(request: CreateTranscriptJobRequest, http_request: Request):
        try:
            canonical = canonicalize_youtube_url(request.url)
        except PipelineError as error:
            raise HTTPException(
                status_code=422, detail=error.info.model_dump(mode="json")
            ) from error

        cache_key = _cache_key(
            canonical.video_id,
            request.language,
            app_settings.pipeline_version,
            app_settings.whisper_model,
        )
        if not request.force_refresh:
            cached_job = app_repository.find_completed(cache_key)
            if cached_job and _artifacts_exist(cached_job):
                response = _job_response(cached_job, http_request, cached_override=True)
                return JSONResponse(
                    status_code=200,
                    content=response.model_dump(mode="json", by_alias=True),
                )

        job = app_repository.create_job(
            job_id=str(uuid.uuid4()),
            cache_key=cache_key,
            request_url=canonical.url,
            requested_language=request.language,
            force_refresh=request.force_refresh,
        )
        await app_worker.enqueue(job.id)
        response = _job_response(job, http_request)
        return JSONResponse(
            status_code=202,
            headers={"Location": str(http_request.url_for("get_job", job_id=job.id))},
            content=response.model_dump(mode="json", by_alias=True),
        )

    @app.get("/api/v1/transcript-jobs/{job_id}", response_model=JobResponse, name="get_job")
    async def get_job(job_id: str, http_request: Request):
        job = app_repository.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        return _job_response(job, http_request)

    @app.get("/api/v1/transcript-jobs/{job_id}/artifacts/{artifact_format}", name="get_artifact")
    async def get_artifact(job_id: str, artifact_format: str):
        if artifact_format not in {"srt", "txt", "json"}:
            raise HTTPException(status_code=404, detail="Unsupported artifact format.")
        job = app_repository.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        if job.status != JobStatus.COMPLETED:
            raise HTTPException(
                status_code=409, detail="Artifacts are available only after completion."
            )
        if not job.artifact_paths or artifact_format not in job.artifact_paths:
            raise HTTPException(status_code=404, detail="Artifact not found.")
        path = _safe_artifact_path(Path(job.artifact_paths[artifact_format]), app_settings.jobs_dir)
        if not path.exists():
            raise HTTPException(status_code=404, detail="Artifact not found.")
        media_types = {
            "srt": "application/x-subrip",
            "txt": "text/plain",
            "json": "application/json",
        }
        return FileResponse(path, media_type=media_types[artifact_format], filename=path.name)

    @app.get("/api/v1/health", name="health")
    async def health():
        transcriber = app_pipeline.transcriber
        checks: dict[str, Any] = {
            "sqlite": app_repository.is_healthy(),
            "yt_dlp": getattr(yt_dlp.version, "__version__", "unknown"),
            "ffmpeg": shutil.which("ffmpeg") is not None,
            "ffprobe": shutil.which("ffprobe") is not None,
            "whisper": transcriber.health(),
        }
        whisper_checks = checks["whisper"]
        healthy = (
            checks["sqlite"]
            and checks["ffmpeg"]
            and checks["ffprobe"]
            and whisper_checks.get("cuda_runtime_available", True)
        )
        return {"status": "ok" if healthy else "degraded", "checks": checks}

    return app


def _cache_key(
    video_id: str, language: str | None, pipeline_version: str, whisper_model: str
) -> str:
    return f"v{pipeline_version}:{video_id}:{language or '__auto__'}:{whisper_model}"


def _artifacts_exist(job: StoredJob) -> bool:
    return bool(job.artifact_paths) and all(
        Path(path).is_file() for path in job.artifact_paths.values()
    )


def _job_response(job: StoredJob, request: Request, cached_override: bool = False) -> JobResponse:
    artifacts = None
    if job.status == JobStatus.COMPLETED and job.artifact_paths:
        artifacts = ArtifactLinks(
            srt=str(request.url_for("get_artifact", job_id=job.id, artifact_format="srt")),
            txt=str(request.url_for("get_artifact", job_id=job.id, artifact_format="txt")),
            json=str(request.url_for("get_artifact", job_id=job.id, artifact_format="json")),
        )
    return JobResponse(
        id=job.id,
        status=job.status,
        stage=job.stage,
        progress=job.progress,
        source=job.source,
        requested_language=job.requested_language,
        language=job.actual_language,
        language_confidence=job.language_confidence,
        video=VideoMetadata(**job.video) if job.video else None,
        artifacts=artifacts,
        warnings=job.warnings,
        error=ErrorInfo(**job.error) if job.error else None,
        cached=cached_override or job.cached,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _safe_artifact_path(path: Path, jobs_dir: Path) -> Path:
    resolved_path = path.resolve()
    resolved_root = jobs_dir.resolve()
    if not resolved_path.is_relative_to(resolved_root):
        raise HTTPException(status_code=404, detail="Artifact not found.")
    return resolved_path


app = create_app()
