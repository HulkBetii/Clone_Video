from __future__ import annotations

import shutil
import uuid
from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path
from typing import Any, NoReturn

import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from yt_pro_max.config import Settings, get_settings
from yt_pro_max.errors import PipelineError
from yt_pro_max.models import (
    ArtifactLinks,
    CreateRewriteJobRequest,
    CreateTranscriptJobRequest,
    ErrorInfo,
    JobResponse,
    JobStatus,
    RewriteArtifactLinks,
    RewriteJobResponse,
    VideoMetadata,
)
from yt_pro_max.pipeline import TranscriptPipeline
from yt_pro_max.repository import JobRepository, StoredJob
from yt_pro_max.rewrite_repository import RewriteJobRepository, StoredRewriteJob
from yt_pro_max.rewrite_worker import RewriteWorker
from yt_pro_max.url_utils import canonicalize_youtube_url
from yt_pro_max.worker import JobWorker


def create_app(
    settings: Settings | None = None,
    *,
    repository: JobRepository | None = None,
    pipeline: TranscriptPipeline | None = None,
    rewrite_repository: RewriteJobRepository | None = None,
    rewrite_pipeline: Any | None = None,
) -> FastAPI:
    app_settings = settings or get_settings()
    app_settings.ensure_directories()
    app_repository = repository or JobRepository(app_settings.database_path)
    app_pipeline = pipeline or TranscriptPipeline(app_settings)
    app_worker = JobWorker(app_repository, app_pipeline)
    app_rewrite_repository = rewrite_repository or RewriteJobRepository(app_settings.database_path)
    app_rewrite_pipeline = rewrite_pipeline
    app_rewrite_worker = (
        RewriteWorker(
            app_rewrite_repository,
            app_repository,
            app_rewrite_pipeline,
        )
        if app_rewrite_pipeline is not None
        else None
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal app_rewrite_pipeline, app_rewrite_worker
        app_repository.initialize()
        app_rewrite_repository.initialize()
        if app_rewrite_pipeline is None:
            from yt_pro_max.rewrite_pipeline import RewritePipeline

            app_rewrite_pipeline = RewritePipeline(app_settings)
            app_rewrite_worker = RewriteWorker(
                app_rewrite_repository,
                app_repository,
                app_rewrite_pipeline,
            )
            app.state.rewrite_pipeline = app_rewrite_pipeline
            app.state.rewrite_worker = app_rewrite_worker
        await app_worker.start()
        if app_rewrite_worker is None:
            raise RuntimeError("rewrite worker was not initialized")
        await app_rewrite_worker.start()
        try:
            yield
        finally:
            try:
                await app_rewrite_worker.stop()
            finally:
                await app_worker.stop()

    app = FastAPI(title=app_settings.app_name, version="0.1.0", lifespan=lifespan)
    app.state.settings = app_settings
    app.state.repository = app_repository
    app.state.pipeline = app_pipeline
    app.state.worker = app_worker
    app.state.rewrite_repository = app_rewrite_repository
    app.state.rewrite_pipeline = app_rewrite_pipeline
    app.state.rewrite_worker = app_rewrite_worker

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

    @app.post(
        "/api/v1/rewrite-jobs",
        response_model=RewriteJobResponse,
        name="create_rewrite_job",
    )
    async def create_rewrite_job(request: CreateRewriteJobRequest, http_request: Request):
        source_job = app_repository.get_job(request.transcript_job_id)
        if source_job is None:
            raise _rewrite_http_error(
                404,
                "SOURCE_NOT_FOUND",
                "The source transcript job was not found.",
            )
        source_path = _rewrite_source_path(source_job, app_settings.jobs_dir)
        try:
            source_hash = sha256(source_path.read_bytes()).hexdigest()
        except OSError:
            _raise_empty_source()
        cache_key = _rewrite_cache_key(source_hash, app_settings)

        if not request.force_refresh:
            cached_job = app_rewrite_repository.find_completed(cache_key)
            if cached_job and _rewrite_artifact_exists(cached_job, app_settings.rewrite_jobs_dir):
                response = _rewrite_job_response(
                    cached_job,
                    app_repository.get_job(cached_job.transcript_job_id),
                    http_request,
                    cached_override=True,
                )
                return JSONResponse(
                    status_code=200,
                    content=response.model_dump(mode="json"),
                )
            active_job = app_rewrite_repository.find_active(cache_key)
            if active_job:
                response = _rewrite_job_response(
                    active_job,
                    app_repository.get_job(active_job.transcript_job_id),
                    http_request,
                )
                return JSONResponse(
                    status_code=202,
                    headers={
                        "Location": str(
                            http_request.url_for("get_rewrite_job", job_id=active_job.id)
                        )
                    },
                    content=response.model_dump(mode="json"),
                )

        job = app_rewrite_repository.create_job(
            job_id=str(uuid.uuid4()),
            transcript_job_id=source_job.id,
            source_hash=source_hash,
            cache_key=cache_key,
            force_refresh=request.force_refresh,
            source_language=source_job.actual_language,
        )
        await http_request.app.state.rewrite_worker.enqueue(job.id)
        response = _rewrite_job_response(job, source_job, http_request)
        return JSONResponse(
            status_code=202,
            headers={"Location": str(http_request.url_for("get_rewrite_job", job_id=job.id))},
            content=response.model_dump(mode="json"),
        )

    @app.get(
        "/api/v1/rewrite-jobs/{job_id}",
        response_model=RewriteJobResponse,
        name="get_rewrite_job",
    )
    async def get_rewrite_job(job_id: str, http_request: Request):
        job = app_rewrite_repository.get_job(job_id)
        if job is None:
            raise _rewrite_http_error(404, "REWRITE_JOB_NOT_FOUND", "Rewrite job not found.")
        source_job = app_repository.get_job(job.transcript_job_id)
        return _rewrite_job_response(job, source_job, http_request)

    @app.get(
        "/api/v1/rewrite-jobs/{job_id}/artifacts/txt",
        name="get_rewrite_artifact",
    )
    async def get_rewrite_artifact(job_id: str):
        job = app_rewrite_repository.get_job(job_id)
        if job is None:
            raise _rewrite_http_error(404, "REWRITE_JOB_NOT_FOUND", "Rewrite job not found.")
        if job.status != JobStatus.COMPLETED:
            raise _rewrite_http_error(
                409,
                "ARTIFACT_NOT_READY",
                "Artifact is available only after completion.",
            )
        if not job.artifact_path:
            raise _rewrite_http_error(404, "ARTIFACT_NOT_FOUND", "Artifact not found.")
        path = _safe_rewrite_artifact_path(Path(job.artifact_path), app_settings.rewrite_jobs_dir)
        if not path.is_file():
            raise _rewrite_http_error(404, "ARTIFACT_NOT_FOUND", "Artifact not found.")
        return FileResponse(path, media_type="text/plain", filename=path.name)

    @app.get("/api/v1/health", name="health")
    async def health():
        transcriber = app_pipeline.transcriber
        rewrite_health = getattr(app_rewrite_pipeline, "health", None)
        rewrite_checks = (
            rewrite_health()
            if callable(rewrite_health)
            else {
                "profile_id": app_settings.gpt_profile_id,
                "profile_exists": app_settings.gpt_profile_dir.is_dir(),
                "browser_running": False,
                "worker_concurrency": 1,
            }
        )
        checks: dict[str, Any] = {
            "sqlite": app_repository.is_healthy(),
            "yt_dlp": getattr(yt_dlp.version, "__version__", "unknown"),
            "ffmpeg": shutil.which("ffmpeg") is not None,
            "ffprobe": shutil.which("ffprobe") is not None,
            "whisper": transcriber.health(),
            "gpt_rewrite": rewrite_checks,
        }
        whisper_checks = checks["whisper"]
        healthy = (
            checks["sqlite"]
            and checks["ffmpeg"]
            and checks["ffprobe"]
            and whisper_checks.get("cuda_runtime_available", True)
            and rewrite_checks.get("profile_exists", False)
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


def _rewrite_job_response(
    job: StoredRewriteJob,
    source_job: StoredJob | None,
    request: Request,
    cached_override: bool = False,
) -> RewriteJobResponse:
    artifacts = None
    if job.status == JobStatus.COMPLETED and job.artifact_path:
        artifacts = RewriteArtifactLinks(
            txt=str(request.url_for("get_rewrite_artifact", job_id=job.id))
        )
    return RewriteJobResponse(
        id=job.id,
        transcript_job_id=job.transcript_job_id,
        status=job.status,
        stage=job.stage,
        progress=job.progress,
        video=VideoMetadata(**source_job.video) if source_job and source_job.video else None,
        language=job.source_language,
        source_length=job.source_length,
        output_length=job.output_length,
        sections_completed=job.sections_completed,
        sections_total=job.sections_total,
        title=job.title,
        artifacts=artifacts,
        warnings=job.warnings,
        error=ErrorInfo(**job.error) if job.error else None,
        cached=cached_override or job.cached,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _rewrite_source_path(job: StoredJob, jobs_dir: Path) -> Path:
    if job.status != JobStatus.COMPLETED:
        raise _rewrite_http_error(
            409,
            "SOURCE_NOT_COMPLETED",
            "The source transcript job is not completed.",
        )
    if not job.artifact_paths or not job.artifact_paths.get("txt"):
        _raise_empty_source()
    path = _safe_rewrite_artifact_path(Path(job.artifact_paths["txt"]), jobs_dir)
    if not path.is_file():
        _raise_empty_source()
    try:
        content = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        _raise_empty_source()
    lines = content.splitlines()
    if lines and lines[0].strip().lower().startswith("title:"):
        lines = lines[1:]
    if not "\n".join(lines).strip():
        _raise_empty_source()
    return path


def _raise_empty_source() -> NoReturn:
    raise _rewrite_http_error(
        422,
        "SOURCE_EMPTY",
        "The source transcript contains no usable text.",
    )


def _rewrite_http_error(
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=ErrorInfo(
            code=code,
            message=message,
            retryable=retryable,
            details=details or {},
        ).model_dump(mode="json"),
    )


def _rewrite_cache_key(source_hash: str, settings: Settings) -> str:
    length_policy = (
        f"{settings.rewrite_target_ratio:g}:"
        f"{settings.rewrite_min_ratio:g}:"
        f"{settings.rewrite_max_ratio:g}"
    )
    return (
        f"v{settings.rewrite_pipeline_version}:"
        f"p{settings.rewrite_prompt_version}:"
        f"{settings.gpt_profile_id}:"
        f"{length_policy}:"
        f"{source_hash}"
    )


def _rewrite_artifact_exists(job: StoredRewriteJob, rewrite_jobs_dir: Path) -> bool:
    if not job.artifact_path:
        return False
    path = Path(job.artifact_path).resolve()
    return path.is_relative_to(rewrite_jobs_dir.resolve()) and path.is_file()


def _safe_artifact_path(path: Path, jobs_dir: Path) -> Path:
    resolved_path = path.resolve()
    resolved_root = jobs_dir.resolve()
    if not resolved_path.is_relative_to(resolved_root):
        raise HTTPException(status_code=404, detail="Artifact not found.")
    return resolved_path


def _safe_rewrite_artifact_path(path: Path, rewrite_jobs_dir: Path) -> Path:
    resolved_path = path.resolve()
    resolved_root = rewrite_jobs_dir.resolve()
    if not resolved_path.is_relative_to(resolved_root):
        raise _rewrite_http_error(404, "ARTIFACT_NOT_FOUND", "Artifact not found.")
    return resolved_path


app = create_app()
