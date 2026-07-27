from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from contextlib import suppress
from typing import Any, Protocol

from yt_pro_max.errors import PipelineError
from yt_pro_max.models import JobStatus, RewriteStage
from yt_pro_max.repository import JobRepository, StoredJob
from yt_pro_max.rewrite_repository import RewriteJobRepository, StoredRewriteJob

LOGGER = logging.getLogger(__name__)


class RewritePipeline(Protocol):
    async def process(
        self,
        job: StoredRewriteJob,
        source_job: StoredJob,
        update: Callable[..., Any],
    ) -> Any: ...


class RewriteWorker:
    def __init__(
        self,
        repository: RewriteJobRepository,
        transcript_repository: JobRepository,
        pipeline: RewritePipeline,
    ) -> None:
        self.repository = repository
        self.transcript_repository = transcript_repository
        self.pipeline = pipeline
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._queued_ids: set[str] = set()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        for job in self.repository.list_unfinished():
            self.repository.update_job(
                job.id,
                status=JobStatus.QUEUED,
                error_json=None,
            )
            await self.enqueue(job.id)
        self._task = asyncio.create_task(self._run(), name="rewrite-job-worker")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        self._queued_ids.clear()
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._queue = asyncio.Queue()
        close = getattr(self.pipeline, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result

    async def enqueue(self, job_id: str) -> None:
        if job_id in self._queued_ids:
            return
        self._queued_ids.add(job_id)
        await self._queue.put(job_id)

    async def _run(self) -> None:
        while True:
            job_id = await self._queue.get()
            self._queued_ids.discard(job_id)
            try:
                await self._process(job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Rewrite worker job loop failure job_id=%s", job_id)
                self._record_internal_failure(job_id)

    async def _process(self, job_id: str) -> None:
        job = self.repository.get_job(job_id)
        if job is None or job.status == JobStatus.COMPLETED:
            return
        source_job = self.transcript_repository.get_job(job.transcript_job_id)
        if source_job is None or source_job.status != JobStatus.COMPLETED:
            self._fail(
                job_id,
                PipelineError(
                    "SOURCE_NOT_COMPLETED",
                    "The source transcript job is not completed.",
                ),
            )
            return

        try:
            self.repository.update_job(
                job_id,
                status=JobStatus.RUNNING,
                stage=job.stage or RewriteStage.PREPARING_SOURCE,
                error_json=None,
            )
            result = await self.pipeline.process(
                job,
                source_job,
                lambda stage, progress, completed_sections=0, total_sections=0, **details: (
                    self._update_progress(
                        job_id,
                        stage,
                        progress,
                        completed_sections,
                        total_sections,
                        **details,
                    )
                ),
            )
            changes: dict[str, Any] = {
                "status": JobStatus.COMPLETED,
                "stage": RewriteStage.RENDERING,
                "progress": 100,
                "source_language": source_job.actual_language,
                "source_length": result.source_length,
                "output_length": result.output_length,
                "sections_completed": result.sections_completed,
                "sections_total": result.sections_total,
                "title": result.title,
                "artifact_path": str(result.artifact_path),
                "warnings_json": result.warnings or [],
                "error_json": None,
            }
            optional_fields = {
                "conversation_url": getattr(result, "conversation_url", None),
                "checkpoint_json": getattr(result, "checkpoint", None),
                "work_files_json": getattr(result, "work_files", None),
            }
            changes.update(
                {key: value for key, value in optional_fields.items() if value is not None}
            )
            self.repository.update_job(job_id, **changes)
        except PipelineError as error:
            self._fail(job_id, error)
        except Exception:
            LOGGER.exception("Unexpected rewrite job failure job_id=%s", job_id)
            self._record_internal_failure(job_id)

    def _update_progress(
        self,
        job_id: str,
        stage: RewriteStage | str,
        progress: int,
        completed_sections: int,
        total_sections: int,
        *,
        checkpoint: dict[str, Any] | None = None,
        conversation_url: str | None = None,
        work_files: dict[str, str] | None = None,
    ) -> None:
        changes: dict[str, Any] = {
            "status": JobStatus.RUNNING,
            "stage": stage,
            "progress": max(0, min(100, progress)),
            "sections_completed": max(0, completed_sections),
            "sections_total": max(0, total_sections),
        }
        if checkpoint is not None:
            changes["checkpoint_json"] = checkpoint
        if conversation_url is not None:
            changes["conversation_url"] = conversation_url
        if work_files is not None:
            changes["work_files_json"] = work_files
        self.repository.update_job(job_id, **changes)

    def _fail(self, job_id: str, error: PipelineError) -> None:
        self.repository.update_job(
            job_id,
            status=JobStatus.FAILED,
            error_json=error.info.model_dump(mode="json"),
        )

    def _record_internal_failure(self, job_id: str) -> None:
        try:
            self.repository.update_job(
                job_id,
                status=JobStatus.FAILED,
                error_json={
                    "code": "INTERNAL_ERROR",
                    "message": "An unexpected internal error occurred.",
                    "retryable": False,
                    "details": {},
                },
            )
        except Exception:
            LOGGER.exception("Could not persist rewrite job failure job_id=%s", job_id)
