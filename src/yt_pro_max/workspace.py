from __future__ import annotations

import asyncio
import logging
import shutil
import sqlite3
import uuid
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from yt_pro_max.config import Settings
from yt_pro_max.errors import PipelineError
from yt_pro_max.models import JobStatus, WorkspacePhase, WorkspaceStatus
from yt_pro_max.repository import JobRepository, StoredJob
from yt_pro_max.rewrite_repository import RewriteJobRepository, StoredRewriteJob

LOGGER = logging.getLogger(__name__)
WORKSPACE_POLL_INTERVAL_SECONDS = 0.5
USER_ACTION_REQUIRED_CODES = {
    "GPT_LOGIN_REQUIRED",
    "GPT_PROFILE_LOCKED",
}


class RewriteQueue(Protocol):
    async def enqueue(self, job_id: str) -> None: ...


@dataclass(frozen=True)
class RewriteSource:
    path: Path
    content_hash: str


@dataclass(frozen=True)
class WorkspaceSnapshot:
    transcript: StoredJob
    rewrite: StoredRewriteJob | None
    status: WorkspaceStatus
    phase: WorkspacePhase
    progress: int
    action_required: dict[str, object] | None
    rewrite_cache_hit: bool

    @property
    def id(self) -> str:
        return self.transcript.id

    @property
    def created_at(self) -> str:
        return self.transcript.created_at

    @property
    def updated_at(self) -> str:
        if self.rewrite is None:
            return self.transcript.updated_at
        return max(self.transcript.updated_at, self.rewrite.updated_at)


class WorkspaceService:
    def __init__(
        self,
        transcript_repository: JobRepository,
        rewrite_repository: RewriteJobRepository,
        settings: Settings,
    ) -> None:
        self.transcript_repository = transcript_repository
        self.rewrite_repository = rewrite_repository
        self.settings = settings

    def get_workspace(self, transcript_job_id: str) -> WorkspaceSnapshot | None:
        transcript = self.transcript_repository.get_job(transcript_job_id)
        if transcript is None:
            return None
        return self._snapshot(transcript)

    def _snapshot(self, transcript: StoredJob) -> WorkspaceSnapshot:
        rewrite, cache_hit = self._resolve_rewrite(transcript)
        return _workspace_snapshot(transcript, rewrite, rewrite_cache_hit=cache_hit)

    def list_workspaces(
        self,
        *,
        status: WorkspaceStatus | str | None = None,
        query: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[WorkspaceSnapshot], int]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if offset < 0:
            raise ValueError("offset must not be negative")
        status_filter = WorkspaceStatus(status) if status else None
        transcripts = self.transcript_repository.list_jobs(query=query)
        snapshots: list[WorkspaceSnapshot] = []
        for transcript in transcripts:
            snapshot = self._snapshot(transcript)
            if status_filter is None or snapshot.status == status_filter:
                snapshots.append(snapshot)
        total = len(snapshots)
        return snapshots[offset : offset + limit], total

    def latest_direct_rewrite(self, transcript_job_id: str) -> StoredRewriteJob | None:
        finder = getattr(self.rewrite_repository, "find_latest_for_source", None)
        if finder is None:
            raise RuntimeError("RewriteJobRepository.find_latest_for_source is required")
        return finder(transcript_job_id)

    def delete_workspaces(self, transcript_job_ids: list[str]) -> list[str]:
        normalized_ids = list(dict.fromkeys(transcript_job_ids))
        placeholders = ", ".join("?" for _ in normalized_ids)
        database_path = self.transcript_repository.database_path
        rewrite_ids: list[str] = []
        with sqlite3.connect(database_path) as connection:
            rows = connection.execute(
                f"SELECT id, status FROM jobs WHERE id IN ({placeholders})",  # noqa: S608
                normalized_ids,
            ).fetchall()
            found_ids = {row[0] for row in rows}
            missing_ids = [job_id for job_id in normalized_ids if job_id not in found_ids]
            if missing_ids:
                raise PipelineError(
                    "WORKSPACE_NOT_FOUND",
                    "One or more selected workspaces no longer exist.",
                    details={"workspace_ids": missing_ids},
                )

            blocked_workspace_ids = [
                row[0]
                for row in rows
                if row[1] in {JobStatus.QUEUED.value, JobStatus.RUNNING.value}
            ]
            rewrite_rows = connection.execute(
                f"""
                SELECT id, transcript_job_id, status
                FROM rewrite_jobs
                WHERE transcript_job_id IN ({placeholders})
                """,  # noqa: S608
                normalized_ids,
            ).fetchall()
            blocked_rewrite_ids = [
                row[0]
                for row in rewrite_rows
                if row[2] in {JobStatus.QUEUED.value, JobStatus.RUNNING.value}
            ]
            if blocked_workspace_ids or blocked_rewrite_ids:
                raise PipelineError(
                    "WORKSPACE_BUSY",
                    "Running workspaces cannot be deleted.",
                    details={
                        "workspace_ids": blocked_workspace_ids,
                        "rewrite_job_ids": blocked_rewrite_ids,
                    },
                )

            rewrite_ids = [row[0] for row in rewrite_rows]
            connection.executemany(
                "DELETE FROM rewrite_jobs WHERE transcript_job_id = ?",
                ((job_id,) for job_id in normalized_ids),
            )
            connection.executemany(
                "DELETE FROM jobs WHERE id = ?",
                ((job_id,) for job_id in normalized_ids),
            )

        for job_id in normalized_ids:
            _remove_job_directory(self.settings.jobs_dir, job_id)
            _remove_job_directory(self.settings.temp_dir, job_id)
        for rewrite_id in rewrite_ids:
            _remove_job_directory(self.settings.rewrite_jobs_dir, rewrite_id)
            _remove_job_directory(self.settings.rewrite_temp_dir, rewrite_id)
        return normalized_ids

    def _resolve_rewrite(self, transcript: StoredJob) -> tuple[StoredRewriteJob | None, bool]:
        direct = self.latest_direct_rewrite(transcript.id)
        if direct is not None:
            return direct, False
        if (
            not transcript.auto_rewrite_requested
            or transcript.force_refresh
            or transcript.status != JobStatus.COMPLETED
        ):
            return None, False
        try:
            source = load_rewrite_source(transcript, self.settings.jobs_dir)
        except PipelineError:
            return None, False
        cache_key = rewrite_cache_key(source.content_hash, self.settings)
        cached = self.rewrite_repository.find_completed(cache_key)
        if cached and rewrite_artifact_exists(cached, self.settings.rewrite_jobs_dir):
            return cached, cached.transcript_job_id != transcript.id
        active = self.rewrite_repository.find_active(cache_key)
        return active, bool(active and active.transcript_job_id != transcript.id)


class WorkspaceCoordinator:
    def __init__(
        self,
        transcript_repository: JobRepository,
        rewrite_repository: RewriteJobRepository,
        rewrite_queue: RewriteQueue,
        settings: Settings,
        *,
        service: WorkspaceService | None = None,
        poll_interval_seconds: float = WORKSPACE_POLL_INTERVAL_SECONDS,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self.transcript_repository = transcript_repository
        self.rewrite_repository = rewrite_repository
        self.rewrite_queue = rewrite_queue
        self.settings = settings
        self.service = service or WorkspaceService(
            transcript_repository,
            rewrite_repository,
            settings,
        )
        self.poll_interval_seconds = poll_interval_seconds
        self._force_refresh_ids: set[str] = set()
        self._ensure_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        await self.reconcile_once()
        self._task = asyncio.create_task(self._run(), name="workspace-coordinator")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def request_auto_rewrite(
        self,
        transcript_job_id: str,
        *,
        force_refresh: bool = False,
    ) -> StoredRewriteJob | None:
        transcript = self.transcript_repository.request_auto_rewrite(transcript_job_id)
        if force_refresh:
            self._force_refresh_ids.add(transcript_job_id)
        if transcript.status != JobStatus.COMPLETED:
            return None
        return await self.ensure_rewrite(transcript_job_id)

    async def ensure_rewrite(self, transcript_job_id: str) -> StoredRewriteJob | None:
        async with self._ensure_lock:
            transcript = self.transcript_repository.get_job(transcript_job_id)
            if (
                transcript is None
                or not transcript.auto_rewrite_requested
                or transcript.status != JobStatus.COMPLETED
            ):
                return None
            return await self._ensure_rewrite_locked(transcript)

    async def resume(self, transcript_job_id: str) -> StoredRewriteJob:
        async with self._ensure_lock:
            rewrite = self.service.latest_direct_rewrite(transcript_job_id)
            if rewrite is None:
                raise PipelineError(
                    "REWRITE_JOB_NOT_FOUND",
                    "No rewrite job exists for this workspace.",
                )
            if rewrite.status != JobStatus.FAILED or not is_recoverable_rewrite_error(
                rewrite.error
            ):
                raise PipelineError(
                    "REWRITE_NOT_RESUMABLE",
                    "The rewrite job is not waiting for a recoverable user action.",
                )
            rewrite = self.rewrite_repository.update_job(
                rewrite.id,
                status=JobStatus.QUEUED,
                error_json=None,
            )
            await self.rewrite_queue.enqueue(rewrite.id)
            return rewrite

    async def reconcile_once(self) -> None:
        for transcript in self.transcript_repository.list_auto_rewrite_candidates():
            try:
                await self.ensure_rewrite(transcript.id)
            except Exception:
                LOGGER.exception(
                    "Could not reconcile automatic rewrite transcript_job_id=%s",
                    transcript.id,
                )

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval_seconds)
            await self.reconcile_once()

    async def _ensure_rewrite_locked(self, transcript: StoredJob) -> StoredRewriteJob | None:
        explicit_force_refresh = transcript.id in self._force_refresh_ids
        direct = self.service.latest_direct_rewrite(transcript.id)
        if (
            direct is not None
            and not (explicit_force_refresh and direct.status == JobStatus.COMPLETED)
            and (
                direct.status != JobStatus.COMPLETED
                or rewrite_artifact_exists(direct, self.settings.rewrite_jobs_dir)
            )
        ):
            self._force_refresh_ids.discard(transcript.id)
            return direct

        force_refresh = explicit_force_refresh or transcript.force_refresh
        try:
            source = load_rewrite_source(transcript, self.settings.jobs_dir)
        except PipelineError as error:
            return self._record_source_failure(transcript, error)
        cache_key = rewrite_cache_key(source.content_hash, self.settings)

        if not force_refresh:
            cached = self.rewrite_repository.find_completed(cache_key)
            if cached and rewrite_artifact_exists(cached, self.settings.rewrite_jobs_dir):
                return cached
            active = self.rewrite_repository.find_active(cache_key)
            if active is not None:
                return active

        rewrite = self.rewrite_repository.create_job(
            job_id=str(uuid.uuid4()),
            transcript_job_id=transcript.id,
            source_hash=source.content_hash,
            cache_key=cache_key,
            force_refresh=force_refresh,
            source_language=transcript.actual_language,
        )
        self._force_refresh_ids.discard(transcript.id)
        await self.rewrite_queue.enqueue(rewrite.id)
        return rewrite

    def _record_source_failure(
        self,
        transcript: StoredJob,
        error: PipelineError,
    ) -> StoredRewriteJob:
        source_hash = sha256(f"invalid:{transcript.id}".encode()).hexdigest()
        rewrite = self.rewrite_repository.create_job(
            job_id=str(uuid.uuid4()),
            transcript_job_id=transcript.id,
            source_hash=source_hash,
            cache_key=rewrite_cache_key(source_hash, self.settings),
            force_refresh=False,
            source_language=transcript.actual_language,
        )
        return self.rewrite_repository.update_job(
            rewrite.id,
            status=JobStatus.FAILED,
            error_json=error.info.model_dump(mode="json"),
        )


def load_rewrite_source(transcript: StoredJob, jobs_dir: Path) -> RewriteSource:
    if transcript.status != JobStatus.COMPLETED:
        raise PipelineError(
            "SOURCE_NOT_COMPLETED",
            "The source transcript job is not completed.",
        )
    if not transcript.artifact_paths or not transcript.artifact_paths.get("txt"):
        raise PipelineError("SOURCE_EMPTY", "The source transcript contains no usable text.")
    path = Path(transcript.artifact_paths["txt"]).resolve()
    if not path.is_relative_to(jobs_dir.resolve()) or not path.is_file():
        raise PipelineError("SOURCE_EMPTY", "The source transcript contains no usable text.")
    try:
        content = path.read_text(encoding="utf-8-sig")
        content_bytes = path.read_bytes()
    except (OSError, UnicodeError) as error:
        raise PipelineError(
            "SOURCE_EMPTY",
            "The source transcript contains no usable text.",
        ) from error
    lines = content.splitlines()
    if lines and lines[0].strip().lower().startswith("title:"):
        lines = lines[1:]
    if not "\n".join(lines).strip():
        raise PipelineError("SOURCE_EMPTY", "The source transcript contains no usable text.")
    return RewriteSource(path=path, content_hash=sha256(content_bytes).hexdigest())


def rewrite_cache_key(source_hash: str, settings: Settings) -> str:
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


def rewrite_artifact_exists(job: StoredRewriteJob, rewrite_jobs_dir: Path) -> bool:
    if not job.artifact_path:
        return False
    path = Path(job.artifact_path).resolve()
    return path.is_relative_to(rewrite_jobs_dir.resolve()) and path.is_file()


def _remove_job_directory(root: Path, job_id: str) -> None:
    root_path = root.resolve()
    target = (root_path / job_id).resolve()
    if not target.is_relative_to(root_path) or target == root_path:
        return
    try:
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    except OSError:
        LOGGER.warning("Could not remove workspace files at %s", target, exc_info=True)


def is_recoverable_rewrite_error(error: dict[str, object] | None) -> bool:
    if not error:
        return False
    code = str(error.get("code") or "")
    return code in USER_ACTION_REQUIRED_CODES or (
        code.startswith("GPT_") and error.get("retryable") is True
    )


def _workspace_snapshot(
    transcript: StoredJob,
    rewrite: StoredRewriteJob | None,
    *,
    rewrite_cache_hit: bool,
) -> WorkspaceSnapshot:
    if transcript.status == JobStatus.FAILED:
        return WorkspaceSnapshot(
            transcript=transcript,
            rewrite=rewrite,
            status=WorkspaceStatus.FAILED,
            phase=WorkspacePhase.TRANSCRIPT,
            progress=transcript.progress,
            action_required=None,
            rewrite_cache_hit=rewrite_cache_hit,
        )
    if transcript.status in {JobStatus.QUEUED, JobStatus.RUNNING}:
        return WorkspaceSnapshot(
            transcript=transcript,
            rewrite=rewrite,
            status=WorkspaceStatus(transcript.status.value),
            phase=WorkspacePhase.TRANSCRIPT,
            progress=(
                transcript.progress // 2
                if transcript.auto_rewrite_requested
                else transcript.progress
            ),
            action_required=None,
            rewrite_cache_hit=rewrite_cache_hit,
        )
    if not transcript.auto_rewrite_requested and rewrite is None:
        return WorkspaceSnapshot(
            transcript=transcript,
            rewrite=rewrite,
            status=WorkspaceStatus.COMPLETED,
            phase=WorkspacePhase.COMPLETED,
            progress=100,
            action_required=None,
            rewrite_cache_hit=rewrite_cache_hit,
        )
    if rewrite is None:
        return WorkspaceSnapshot(
            transcript=transcript,
            rewrite=None,
            status=WorkspaceStatus.QUEUED,
            phase=WorkspacePhase.REWRITE,
            progress=50,
            action_required=None,
            rewrite_cache_hit=False,
        )
    if rewrite.status == JobStatus.FAILED:
        recoverable = is_recoverable_rewrite_error(rewrite.error)
        return WorkspaceSnapshot(
            transcript=transcript,
            rewrite=rewrite,
            status=(
                WorkspaceStatus.WAITING_FOR_USER if recoverable else WorkspaceStatus.FAILED
            ),
            phase=WorkspacePhase.REWRITE,
            progress=50 + rewrite.progress // 2,
            action_required=rewrite.error if recoverable else None,
            rewrite_cache_hit=rewrite_cache_hit,
        )
    if rewrite.status == JobStatus.COMPLETED:
        return WorkspaceSnapshot(
            transcript=transcript,
            rewrite=rewrite,
            status=WorkspaceStatus.COMPLETED,
            phase=WorkspacePhase.COMPLETED,
            progress=100,
            action_required=None,
            rewrite_cache_hit=rewrite_cache_hit,
        )
    return WorkspaceSnapshot(
        transcript=transcript,
        rewrite=rewrite,
        status=WorkspaceStatus(rewrite.status.value),
        phase=WorkspacePhase.REWRITE,
        progress=50 + rewrite.progress // 2,
        action_required=None,
        rewrite_cache_hit=rewrite_cache_hit,
    )
