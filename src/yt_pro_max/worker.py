from __future__ import annotations

import asyncio
import logging

from yt_pro_max.errors import PipelineError
from yt_pro_max.models import JobStage, JobStatus
from yt_pro_max.pipeline import TranscriptPipeline
from yt_pro_max.repository import JobRepository

LOGGER = logging.getLogger(__name__)


class JobWorker:
    def __init__(self, repository: JobRepository, pipeline: TranscriptPipeline) -> None:
        self.repository = repository
        self.pipeline = pipeline
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._queued_ids: set[str] = set()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        for job in self.repository.list_unfinished():
            self.repository.update_job(
                job.id,
                status=JobStatus.QUEUED,
                stage=None,
                progress=0,
                error_json=None,
            )
            await self.enqueue(job.id)
        self._task = asyncio.create_task(self._run(), name="transcript-job-worker")

    async def stop(self) -> None:
        if self._task is None:
            return
        await self._queue.put(None)
        await self._task
        self._task = None

    async def enqueue(self, job_id: str) -> None:
        if job_id in self._queued_ids:
            return
        self._queued_ids.add(job_id)
        await self._queue.put(job_id)

    async def _run(self) -> None:
        while True:
            job_id = await self._queue.get()
            if job_id is None:
                return
            self._queued_ids.discard(job_id)
            await self._process(job_id)

    async def _process(self, job_id: str) -> None:
        job = self.repository.get_job(job_id)
        if job is None or job.status == JobStatus.COMPLETED:
            return
        try:
            self.repository.update_job(
                job_id,
                status=JobStatus.RUNNING,
                stage=JobStage.INSPECTING,
                progress=0,
                error_json=None,
            )
            result = await asyncio.to_thread(
                self.pipeline.process,
                job_id=job.id,
                request_url=job.request_url,
                requested_language=job.requested_language,
                update=lambda stage, progress: self._update_progress(job_id, stage, progress),
            )
            self.repository.update_job(
                job_id,
                status=JobStatus.COMPLETED,
                stage=JobStage.RENDERING,
                progress=100,
                source=result.source,
                actual_language=result.language,
                language_confidence=result.language_confidence,
                video_json=result.video.model_dump(mode="json"),
                artifacts_json={key: str(path) for key, path in result.artifact_paths.items()},
                warnings_json=result.warnings,
                error_json=None,
            )
        except PipelineError as error:
            self.repository.update_job(
                job_id,
                status=JobStatus.FAILED,
                stage=None,
                progress=0,
                error_json=error.info.model_dump(mode="json"),
            )
        except Exception:
            LOGGER.exception("Unexpected transcript job failure job_id=%s", job_id)
            self.repository.update_job(
                job_id,
                status=JobStatus.FAILED,
                stage=None,
                progress=0,
                error_json={
                    "code": "INTERNAL_ERROR",
                    "message": "An unexpected internal error occurred.",
                    "retryable": False,
                    "details": {},
                },
            )

    def _update_progress(self, job_id: str, stage: JobStage, progress: int) -> None:
        self.repository.update_job(
            job_id,
            status=JobStatus.RUNNING,
            stage=stage,
            progress=max(0, min(100, progress)),
        )
