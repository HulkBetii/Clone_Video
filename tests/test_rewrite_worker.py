import asyncio
from types import SimpleNamespace

import pytest

from yt_pro_max.models import JobStatus
from yt_pro_max.repository import JobRepository
from yt_pro_max.rewrite_repository import RewriteJobRepository
from yt_pro_max.rewrite_worker import RewriteWorker


class BlockingPipeline:
    def __init__(self):
        self.started = asyncio.Event()
        self.cancelled = False
        self.closed = False

    async def process(self, job, source_job, update):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def close(self):
        self.closed = True


class CompletingPipeline:
    def __init__(self, settings):
        self.settings = settings
        self.calls = 0

    async def process(self, job, source_job, update):
        self.calls += 1
        output_dir = self.settings.rewrite_jobs_dir / job.id
        output_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = output_dir / "rewrite.txt"
        artifact_path.write_text("Title: Rewritten\n\nBody.\n", encoding="utf-8")
        return SimpleNamespace(
            artifact_path=artifact_path,
            title="Rewritten",
            source_length=4,
            output_length=5,
            sections_completed=1,
            sections_total=1,
            warnings=[],
            conversation_url=None,
            checkpoint={"completed": True},
            work_files={},
        )


class FailFirstGetRepository:
    def __init__(self, repository, failing_job_id):
        self.repository = repository
        self.failing_job_id = failing_job_id
        self.failed = False

    def list_unfinished(self):
        return self.repository.list_unfinished()

    def get_job(self, job_id):
        if job_id == self.failing_job_id and not self.failed:
            self.failed = True
            raise RuntimeError("temporary repository failure")
        return self.repository.get_job(job_id)

    def update_job(self, job_id, **changes):
        return self.repository.update_job(job_id, **changes)


def _create_source(repository, job_id="source"):
    job = repository.create_job(
        job_id=job_id,
        cache_key=f"cache:{job_id}",
        request_url="https://youtu.be/dQw4w9WgXcQ",
        requested_language=None,
        force_refresh=False,
    )
    return repository.update_job(
        job.id,
        status=JobStatus.COMPLETED,
        progress=100,
        actual_language="vi",
    )


def _create_rewrite(repository, job_id, source_job_id):
    return repository.create_job(
        job_id=job_id,
        transcript_job_id=source_job_id,
        source_hash=f"hash:{job_id}",
        cache_key=f"cache:{job_id}",
        force_refresh=False,
        source_language="vi",
    )


async def _wait_for_status(repository, job_id, status):
    for _ in range(100):
        job = repository.get_job(job_id)
        if job is not None and job.status == status:
            return job
        await asyncio.sleep(0.01)
    raise AssertionError(f"rewrite job did not reach {status}")


@pytest.mark.asyncio
async def test_stop_cancels_active_job_and_restart_recovers_it(settings):
    transcript_repository = JobRepository(settings.database_path)
    transcript_repository.initialize()
    source = _create_source(transcript_repository)
    rewrite_repository = RewriteJobRepository(settings.database_path)
    rewrite_repository.initialize()
    rewrite_job = _create_rewrite(rewrite_repository, "rewrite-active", source.id)

    blocking_pipeline = BlockingPipeline()
    worker = RewriteWorker(rewrite_repository, transcript_repository, blocking_pipeline)
    await worker.start()
    await asyncio.wait_for(blocking_pipeline.started.wait(), timeout=1)
    await asyncio.wait_for(worker.stop(), timeout=1)

    interrupted = rewrite_repository.get_job(rewrite_job.id)
    assert interrupted is not None
    assert interrupted.status == JobStatus.RUNNING
    assert blocking_pipeline.cancelled is True
    assert blocking_pipeline.closed is True

    completing_pipeline = CompletingPipeline(settings)
    restarted_worker = RewriteWorker(
        rewrite_repository,
        transcript_repository,
        completing_pipeline,
    )
    await restarted_worker.start()
    completed = await _wait_for_status(
        rewrite_repository,
        rewrite_job.id,
        JobStatus.COMPLETED,
    )
    await restarted_worker.stop()

    assert completed.progress == 100
    assert completing_pipeline.calls == 1


@pytest.mark.asyncio
async def test_worker_continues_after_repository_failure(settings):
    transcript_repository = JobRepository(settings.database_path)
    transcript_repository.initialize()
    source = _create_source(transcript_repository)
    rewrite_repository = RewriteJobRepository(settings.database_path)
    rewrite_repository.initialize()
    first = _create_rewrite(rewrite_repository, "rewrite-first", source.id)
    await asyncio.sleep(0.001)
    second = _create_rewrite(rewrite_repository, "rewrite-second", source.id)
    flaky_repository = FailFirstGetRepository(rewrite_repository, first.id)
    pipeline = CompletingPipeline(settings)
    worker = RewriteWorker(flaky_repository, transcript_repository, pipeline)

    await worker.start()
    failed = await _wait_for_status(rewrite_repository, first.id, JobStatus.FAILED)
    completed = await _wait_for_status(rewrite_repository, second.id, JobStatus.COMPLETED)
    await worker.stop()

    assert failed.error["code"] == "INTERNAL_ERROR"
    assert completed.progress == 100
    assert pipeline.calls == 1
