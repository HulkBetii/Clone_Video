from __future__ import annotations

import shutil
import uuid
from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path
from typing import Any, NoReturn

import yt_dlp
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from yt_pro_max.config import Settings, get_settings
from yt_pro_max.errors import PipelineError
from yt_pro_max.models import (
    ArtifactLinks,
    CreateRewriteJobRequest,
    CreateTranscriptJobRequest,
    CreateWorkspaceRequest,
    DeleteWorkspacesRequest,
    DeleteWorkspacesResponse,
    ErrorInfo,
    GptRuntimeResponse,
    GptRuntimeStatus,
    JobResponse,
    JobStatus,
    OpenGptRuntimeRequest,
    RewriteArtifactLinks,
    RewriteJobResponse,
    RewriteValidationSummary,
    VideoMetadata,
    WorkspaceListResponse,
    WorkspaceResponse,
    WorkspaceStatus,
)
from yt_pro_max.pipeline import TranscriptPipeline
from yt_pro_max.repository import JobRepository, StoredJob
from yt_pro_max.rewrite_repository import RewriteJobRepository, StoredRewriteJob
from yt_pro_max.rewrite_worker import RewriteWorker
from yt_pro_max.url_utils import canonicalize_youtube_url
from yt_pro_max.worker import JobWorker
from yt_pro_max.workspace import WorkspaceCoordinator, WorkspaceService, WorkspaceSnapshot


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
    app_workspace_service = WorkspaceService(
        app_repository,
        app_rewrite_repository,
        app_settings,
    )
    app_workspace_coordinator = (
        WorkspaceCoordinator(
            app_repository,
            app_rewrite_repository,
            app_rewrite_worker,
            app_settings,
            service=app_workspace_service,
        )
        if app_rewrite_worker is not None
        else None
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal app_rewrite_pipeline, app_rewrite_worker, app_workspace_coordinator
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
        if app_rewrite_worker is None:
            raise RuntimeError("rewrite worker was not initialized")
        if app_workspace_coordinator is None:
            app_workspace_coordinator = WorkspaceCoordinator(
                app_repository,
                app_rewrite_repository,
                app_rewrite_worker,
                app_settings,
                service=app_workspace_service,
            )
            app.state.workspace_coordinator = app_workspace_coordinator
        await app_worker.start()
        await app_rewrite_worker.start()
        await app_workspace_coordinator.start()
        try:
            yield
        finally:
            try:
                await app_workspace_coordinator.stop()
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
    app.state.workspace_service = app_workspace_service
    app.state.workspace_coordinator = app_workspace_coordinator
    app.state.gpt_runtime_authenticated = None
    app.state.gpt_runtime_error = None

    async def submit_transcript(
        request: CreateTranscriptJobRequest,
        *,
        auto_rewrite: bool,
    ) -> tuple[StoredJob, bool]:
        try:
            canonical = canonicalize_youtube_url(request.url)
        except PipelineError as error:
            raise HTTPException(
                status_code=422,
                detail=error.info.model_dump(mode="json"),
            ) from error

        cache_key = _cache_key(
            canonical.video_id,
            request.language,
            app_settings.pipeline_version,
            app_settings.whisper_model,
            app_settings.reconciliation_model,
        )
        if not request.force_refresh:
            cached_job = app_repository.find_completed(cache_key)
            if cached_job and _artifacts_exist(cached_job):
                if auto_rewrite:
                    cached_job = app_repository.request_auto_rewrite(cached_job.id)
                return cached_job, True

        job = app_repository.create_job(
            job_id=str(uuid.uuid4()),
            cache_key=cache_key,
            request_url=canonical.url,
            requested_language=request.language,
            force_refresh=request.force_refresh,
            auto_rewrite_requested=auto_rewrite,
        )
        await app_worker.enqueue(job.id)
        return job, False

    @app.post("/api/v1/transcript-jobs", response_model=JobResponse, name="create_job")
    async def create_job(request: CreateTranscriptJobRequest, http_request: Request):
        job, cached = await submit_transcript(request, auto_rewrite=False)
        response = _job_response(job, http_request, cached_override=cached)
        return JSONResponse(
            status_code=200 if cached else 202,
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
        "/api/v1/workspaces",
        response_model=WorkspaceResponse,
        name="create_workspace",
    )
    async def create_workspace(request: CreateWorkspaceRequest, http_request: Request):
        job, transcript_cached = await submit_transcript(
            request,
            auto_rewrite=request.auto_rewrite,
        )
        coordinator: WorkspaceCoordinator = http_request.app.state.workspace_coordinator
        if request.auto_rewrite and job.status == JobStatus.COMPLETED:
            await coordinator.request_auto_rewrite(
                job.id,
                force_refresh=request.force_refresh,
            )
        snapshot = app_workspace_service.get_workspace(job.id)
        if snapshot is None:
            raise _rewrite_http_error(404, "WORKSPACE_NOT_FOUND", "Workspace not found.")
        response = _workspace_response(
            snapshot,
            http_request,
            transcript_cached=transcript_cached,
        )
        completed_cache_hit = transcript_cached and snapshot.status == WorkspaceStatus.COMPLETED
        return JSONResponse(
            status_code=200 if completed_cache_hit else 202,
            headers={
                "Location": str(
                    http_request.url_for("get_workspace", transcript_job_id=job.id)
                )
            },
            content=response.model_dump(mode="json", by_alias=True),
        )

    @app.get(
        "/api/v1/workspaces",
        response_model=WorkspaceListResponse,
        name="list_workspaces",
    )
    async def list_workspaces(
        http_request: Request,
        status: WorkspaceStatus | None = None,
        q: str | None = Query(default=None, max_length=200),
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ):
        snapshots, total = app_workspace_service.list_workspaces(
            status=status,
            query=q,
            limit=limit,
            offset=offset,
        )
        return WorkspaceListResponse(
            items=[_workspace_response(item, http_request) for item in snapshots],
            total=total,
            limit=limit,
            offset=offset,
        )

    @app.post(
        "/api/v1/workspaces/bulk-delete",
        response_model=DeleteWorkspacesResponse,
        name="delete_workspaces",
    )
    async def delete_workspaces(request: DeleteWorkspacesRequest, http_request: Request):
        service: WorkspaceService = http_request.app.state.workspace_service
        try:
            deleted_ids = service.delete_workspaces(request.transcript_job_ids)
        except PipelineError as error:
            status_code = 404 if error.info.code == "WORKSPACE_NOT_FOUND" else 409
            raise HTTPException(
                status_code=status_code,
                detail=error.info.model_dump(mode="json"),
            ) from error
        return DeleteWorkspacesResponse(deleted_ids=deleted_ids)

    @app.get(
        "/api/v1/workspaces/{transcript_job_id}",
        response_model=WorkspaceResponse,
        name="get_workspace",
    )
    async def get_workspace(transcript_job_id: str, http_request: Request):
        snapshot = app_workspace_service.get_workspace(transcript_job_id)
        if snapshot is None:
            raise _rewrite_http_error(404, "WORKSPACE_NOT_FOUND", "Workspace not found.")
        return _workspace_response(snapshot, http_request)

    @app.post(
        "/api/v1/workspaces/{transcript_job_id}/resume",
        response_model=WorkspaceResponse,
        name="resume_workspace",
    )
    async def resume_workspace(transcript_job_id: str, http_request: Request):
        coordinator: WorkspaceCoordinator = http_request.app.state.workspace_coordinator
        try:
            await coordinator.resume(transcript_job_id)
        except PipelineError as error:
            status_code = 404 if error.info.code == "REWRITE_JOB_NOT_FOUND" else 409
            raise HTTPException(
                status_code=status_code,
                detail=error.info.model_dump(mode="json"),
            ) from error
        snapshot = app_workspace_service.get_workspace(transcript_job_id)
        if snapshot is None:
            raise _rewrite_http_error(404, "WORKSPACE_NOT_FOUND", "Workspace not found.")
        return JSONResponse(
            status_code=202,
            content=_workspace_response(snapshot, http_request).model_dump(
                mode="json",
                by_alias=True,
            ),
        )

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

    @app.get(
        "/api/v1/gpt-runtime",
        response_model=GptRuntimeResponse,
        name="get_gpt_runtime",
    )
    async def get_gpt_runtime(http_request: Request):
        return _gpt_runtime_response(http_request)

    @app.post(
        "/api/v1/gpt-runtime/open",
        response_model=GptRuntimeResponse,
        name="open_gpt_runtime",
    )
    async def open_gpt_runtime(request: OpenGptRuntimeRequest, http_request: Request):
        _ensure_gpt_idle(http_request)
        conversation_url = None
        if request.rewrite_job_id:
            rewrite_job = app_rewrite_repository.get_job(request.rewrite_job_id)
            if rewrite_job is None:
                raise _rewrite_http_error(
                    404,
                    "REWRITE_JOB_NOT_FOUND",
                    "Rewrite job not found.",
                )
            conversation_url = rewrite_job.conversation_url
        open_browser = getattr(http_request.app.state.rewrite_pipeline, "open_browser", None)
        if not callable(open_browser):
            raise _rewrite_http_error(
                503,
                "GPT_BROWSER_UNAVAILABLE",
                "The ChatGPT browser runtime is unavailable.",
            )
        try:
            await open_browser(conversation_url)
        except PipelineError as error:
            http_request.app.state.gpt_runtime_error = error.info.model_dump(mode="json")
            raise _gpt_runtime_http_error(error) from error
        http_request.app.state.gpt_runtime_authenticated = None
        http_request.app.state.gpt_runtime_error = None
        return _gpt_runtime_response(http_request)

    @app.post(
        "/api/v1/gpt-runtime/check",
        response_model=GptRuntimeResponse,
        name="check_gpt_runtime",
    )
    async def check_gpt_runtime(http_request: Request):
        _ensure_gpt_idle(http_request)
        check_login = getattr(http_request.app.state.rewrite_pipeline, "check_login", None)
        if not callable(check_login):
            raise _rewrite_http_error(
                503,
                "GPT_BROWSER_UNAVAILABLE",
                "The ChatGPT browser runtime is unavailable.",
            )
        try:
            http_request.app.state.gpt_runtime_authenticated = bool(await check_login())
            http_request.app.state.gpt_runtime_error = None
        except PipelineError as error:
            error_data = error.info.model_dump(mode="json")
            http_request.app.state.gpt_runtime_error = error_data
            if error.info.code == "GPT_LOGIN_REQUIRED":
                http_request.app.state.gpt_runtime_authenticated = False
                return _gpt_runtime_response(http_request)
            raise _gpt_runtime_http_error(error) from error
        return _gpt_runtime_response(http_request)

    @app.post(
        "/api/v1/gpt-runtime/close",
        response_model=GptRuntimeResponse,
        name="close_gpt_runtime",
    )
    async def close_gpt_runtime(http_request: Request):
        _ensure_gpt_idle(http_request)
        close_browser = getattr(http_request.app.state.rewrite_pipeline, "close_browser", None)
        if not callable(close_browser):
            raise _rewrite_http_error(
                503,
                "GPT_BROWSER_UNAVAILABLE",
                "The ChatGPT browser runtime is unavailable.",
            )
        await close_browser()
        http_request.app.state.gpt_runtime_authenticated = None
        http_request.app.state.gpt_runtime_error = None
        return _gpt_runtime_response(http_request)

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

    static_dir = Path(__file__).resolve().parent / "static"

    @app.get("/{path:path}", include_in_schema=False, name="serve_spa")
    async def serve_spa(path: str):
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found.")
        static_root = static_dir.resolve()
        requested_path = (static_root / path).resolve()
        if requested_path.is_relative_to(static_root) and requested_path.is_file():
            return FileResponse(requested_path)
        index_path = static_root / "index.html"
        if index_path.is_file():
            return FileResponse(index_path, media_type="text/html")
        return HTMLResponse(
            "<h1>YT Pro Max</h1><p>Frontend assets are not built.</p>",
            status_code=503,
        )

    return app


def _cache_key(
    video_id: str,
    language: str | None,
    pipeline_version: str,
    whisper_model: str,
    reconciliation_model: str = "large-v3",
) -> str:
    return (
        f"v{pipeline_version}:{video_id}:{language or '__auto__'}:"
        f"{whisper_model}:{reconciliation_model}"
    )


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
        request_url=job.request_url,
        auto_rewrite_requested=job.auto_rewrite_requested,
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
        validation=RewriteValidationSummary(**job.validation) if job.validation else None,
        artifacts=artifacts,
        warnings=job.warnings,
        error=ErrorInfo(**job.error) if job.error else None,
        cached=cached_override or job.cached,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _workspace_response(
    snapshot: WorkspaceSnapshot,
    request: Request,
    *,
    transcript_cached: bool = False,
) -> WorkspaceResponse:
    return WorkspaceResponse(
        id=snapshot.id,
        status=snapshot.status,
        phase=snapshot.phase,
        progress=snapshot.progress,
        auto_rewrite=(
            snapshot.transcript.auto_rewrite_requested or snapshot.rewrite is not None
        ),
        request_url=snapshot.transcript.request_url,
        transcript=_job_response(
            snapshot.transcript,
            request,
            cached_override=transcript_cached,
        ),
        rewrite=(
            _rewrite_job_response(
                snapshot.rewrite,
                snapshot.transcript,
                request,
                cached_override=snapshot.rewrite_cache_hit,
            )
            if snapshot.rewrite
            else None
        ),
        action_required=(
            ErrorInfo(**snapshot.action_required) if snapshot.action_required else None
        ),
        created_at=snapshot.created_at,
        updated_at=snapshot.updated_at,
    )


def _gpt_runtime_response(request: Request) -> GptRuntimeResponse:
    settings: Settings = request.app.state.settings
    worker = request.app.state.rewrite_worker
    pipeline = request.app.state.rewrite_pipeline
    health = getattr(pipeline, "health", None)
    checks = health() if callable(health) else {}
    profile_exists = bool(checks.get("profile_exists", settings.gpt_profile_dir.is_dir()))
    browser_running = bool(checks.get("browser_running", False))
    active_job_id = getattr(worker, "active_job_id", None)
    queue_depth = int(getattr(worker, "queue_depth", 0))
    error_data = request.app.state.gpt_runtime_error
    authenticated = request.app.state.gpt_runtime_authenticated

    if active_job_id:
        status = GptRuntimeStatus.BUSY
    elif not profile_exists:
        status = GptRuntimeStatus.UNAVAILABLE
    elif error_data:
        code = str(error_data.get("code") or "")
        if code == "GPT_PROFILE_LOCKED":
            status = GptRuntimeStatus.PROFILE_LOCKED
        elif code == "GPT_LOGIN_REQUIRED":
            status = GptRuntimeStatus.LOGIN_REQUIRED
        elif code in {"GPT_BROWSER_UNAVAILABLE", "GPT_PROFILE_MISSING"}:
            status = GptRuntimeStatus.UNAVAILABLE
        else:
            status = GptRuntimeStatus.ERROR
    elif authenticated is True:
        status = GptRuntimeStatus.READY
    elif authenticated is False:
        status = GptRuntimeStatus.LOGIN_REQUIRED
    else:
        status = GptRuntimeStatus.NOT_CHECKED

    return GptRuntimeResponse(
        status=status,
        profile_id=settings.gpt_profile_id,
        profile_exists=profile_exists,
        browser_running=browser_running,
        authenticated=authenticated,
        active_job_id=active_job_id,
        queue_depth=queue_depth,
        error=ErrorInfo(**error_data) if error_data else None,
    )


def _ensure_gpt_idle(request: Request) -> None:
    worker = request.app.state.rewrite_worker
    if getattr(worker, "active_job_id", None):
        raise _rewrite_http_error(
            409,
            "GPT_BUSY",
            "ChatGPT is currently processing a rewrite job.",
            retryable=True,
        )


def _gpt_runtime_http_error(error: PipelineError) -> HTTPException:
    status_code = 409 if error.info.code in {"GPT_LOGIN_REQUIRED", "GPT_PROFILE_LOCKED"} else 503
    return HTTPException(
        status_code=status_code,
        detail=error.info.model_dump(mode="json"),
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
