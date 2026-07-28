import asyncio
from pathlib import Path

import pytest

from yt_pro_max.models import JobStatus, WorkspacePhase, WorkspaceStatus
from yt_pro_max.repository import JobRepository, StoredJob
from yt_pro_max.rewrite_repository import RewriteJobRepository
from yt_pro_max.workspace import (
    WorkspaceCoordinator,
    WorkspaceService,
    load_rewrite_source,
    rewrite_cache_key,
)


class RecordingQueue:
    def __init__(self) -> None:
        self.job_ids: list[str] = []
        self.enqueued = asyncio.Event()

    async def enqueue(self, job_id: str) -> None:
        self.job_ids.append(job_id)
        self.enqueued.set()


def _completed_transcript(
    repository: JobRepository,
    jobs_dir: Path,
    job_id: str,
    *,
    body: str = "Source body",
    auto_rewrite: bool = False,
    force_refresh: bool = False,
) -> StoredJob:
    job = repository.create_job(
        job_id=job_id,
        cache_key=f"source:{job_id}",
        request_url=f"https://www.youtube.com/watch?v={job_id}",
        requested_language=None,
        force_refresh=force_refresh,
        auto_rewrite_requested=auto_rewrite,
    )
    artifact_path = jobs_dir / job_id / "source.txt"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(f"Title: Source title\n\n{body}\n", encoding="utf-8")
    return repository.update_job(
        job.id,
        status=JobStatus.COMPLETED,
        progress=100,
        actual_language="en",
        artifacts_json={"txt": str(artifact_path)},
        video_json={
            "id": job_id,
            "title": f"Video {job_id}",
            "webpage_url": job.request_url,
        },
    )


def _repositories(settings):
    transcript_repository = JobRepository(settings.database_path)
    rewrite_repository = RewriteJobRepository(settings.database_path)
    transcript_repository.initialize()
    rewrite_repository.initialize()
    return transcript_repository, rewrite_repository


@pytest.mark.asyncio
async def test_coordinator_recovers_completed_auto_rewrite_and_enqueues_once(settings):
    transcript_repository, rewrite_repository = _repositories(settings)
    source = _completed_transcript(
        transcript_repository,
        settings.jobs_dir,
        "source-1",
        auto_rewrite=True,
    )
    queue = RecordingQueue()
    coordinator = WorkspaceCoordinator(
        transcript_repository,
        rewrite_repository,
        queue,
        settings,
    )

    await coordinator.start()
    await coordinator.stop()
    await coordinator.reconcile_once()

    rewrite = rewrite_repository.find_latest_for_source(source.id)
    assert rewrite is not None
    assert rewrite.status == JobStatus.QUEUED
    assert queue.job_ids == [rewrite.id]
    snapshot = coordinator.service.get_workspace(source.id)
    assert snapshot is not None
    assert snapshot.status == WorkspaceStatus.QUEUED
    assert snapshot.phase == WorkspacePhase.REWRITE
    assert snapshot.progress == 50


@pytest.mark.asyncio
async def test_coordinator_chains_rewrite_after_transcript_completes(settings):
    transcript_repository, rewrite_repository = _repositories(settings)
    source = transcript_repository.create_job(
        job_id="running-source",
        cache_key="source:running",
        request_url="https://www.youtube.com/watch?v=running",
        requested_language=None,
        force_refresh=False,
    )
    queue = RecordingQueue()
    coordinator = WorkspaceCoordinator(
        transcript_repository,
        rewrite_repository,
        queue,
        settings,
        poll_interval_seconds=0.01,
    )

    await coordinator.start()
    try:
        await coordinator.request_auto_rewrite(source.id)
        artifact_path = settings.jobs_dir / source.id / "source.txt"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("Title: Source\n\nCompleted body\n", encoding="utf-8")
        transcript_repository.update_job(
            source.id,
            status=JobStatus.COMPLETED,
            progress=100,
            actual_language="en",
            artifacts_json={"txt": str(artifact_path)},
        )
        await asyncio.wait_for(queue.enqueued.wait(), timeout=1)
    finally:
        await coordinator.stop()

    rewrite = rewrite_repository.find_latest_for_source(source.id)
    assert rewrite is not None
    assert queue.job_ids == [rewrite.id]


@pytest.mark.asyncio
async def test_coordinator_reuses_completed_rewrite_cache_for_matching_source(settings):
    transcript_repository, rewrite_repository = _repositories(settings)
    original = _completed_transcript(
        transcript_repository,
        settings.jobs_dir,
        "original",
        body="Identical source content",
    )
    source = load_rewrite_source(original, settings.jobs_dir)
    cached = rewrite_repository.create_job(
        job_id="cached-rewrite",
        transcript_job_id=original.id,
        source_hash=source.content_hash,
        cache_key=rewrite_cache_key(source.content_hash, settings),
        force_refresh=False,
        source_language="en",
    )
    artifact_path = settings.rewrite_jobs_dir / cached.id / "rewrite.txt"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text("Title: Rewritten\n\nRewritten body\n", encoding="utf-8")
    cached = rewrite_repository.update_job(
        cached.id,
        status=JobStatus.COMPLETED,
        progress=100,
        artifact_path=str(artifact_path),
    )
    duplicate = _completed_transcript(
        transcript_repository,
        settings.jobs_dir,
        "duplicate",
        body="Identical source content",
        auto_rewrite=True,
    )
    queue = RecordingQueue()
    coordinator = WorkspaceCoordinator(
        transcript_repository,
        rewrite_repository,
        queue,
        settings,
    )

    resolved = await coordinator.ensure_rewrite(duplicate.id)

    assert resolved == cached
    assert queue.job_ids == []
    snapshot = coordinator.service.get_workspace(duplicate.id)
    assert snapshot is not None
    assert snapshot.rewrite == cached
    assert snapshot.rewrite_cache_hit
    assert snapshot.status == WorkspaceStatus.COMPLETED


@pytest.mark.asyncio
async def test_waiting_workspace_resumes_same_rewrite_checkpoint(settings):
    transcript_repository, rewrite_repository = _repositories(settings)
    source = _completed_transcript(
        transcript_repository,
        settings.jobs_dir,
        "waiting",
        auto_rewrite=True,
    )
    queue = RecordingQueue()
    coordinator = WorkspaceCoordinator(
        transcript_repository,
        rewrite_repository,
        queue,
        settings,
    )
    rewrite = await coordinator.ensure_rewrite(source.id)
    assert rewrite is not None
    failed = rewrite_repository.update_job(
        rewrite.id,
        status=JobStatus.FAILED,
        progress=60,
        checkpoint_json={"next_stage": "validating"},
        error_json={
            "code": "GPT_LOGIN_REQUIRED",
            "message": "Sign in to ChatGPT.",
            "retryable": False,
            "details": {},
        },
    )
    snapshot = coordinator.service.get_workspace(source.id)
    assert snapshot is not None
    assert snapshot.status == WorkspaceStatus.WAITING_FOR_USER
    assert snapshot.action_required == failed.error
    queue.job_ids.clear()

    resumed = await coordinator.resume(source.id)

    assert resumed.id == failed.id
    assert resumed.status == JobStatus.QUEUED
    assert resumed.checkpoint == {"next_stage": "validating"}
    assert queue.job_ids == [failed.id]


@pytest.mark.asyncio
async def test_coordinator_records_invalid_source_without_starting_browser(settings):
    transcript_repository, rewrite_repository = _repositories(settings)
    source = _completed_transcript(
        transcript_repository,
        settings.jobs_dir,
        "empty",
        body="",
        auto_rewrite=True,
    )
    queue = RecordingQueue()
    coordinator = WorkspaceCoordinator(
        transcript_repository,
        rewrite_repository,
        queue,
        settings,
    )

    failed = await coordinator.ensure_rewrite(source.id)
    await coordinator.reconcile_once()

    assert failed is not None
    assert failed.status == JobStatus.FAILED
    assert failed.error is not None
    assert failed.error["code"] == "SOURCE_EMPTY"
    assert queue.job_ids == []
    assert rewrite_repository.find_latest_for_source(source.id) == failed


def test_workspace_service_filters_aggregate_status_and_search(settings):
    transcript_repository, rewrite_repository = _repositories(settings)
    completed = _completed_transcript(
        transcript_repository,
        settings.jobs_dir,
        "completed",
    )
    failed = transcript_repository.create_job(
        job_id="failed",
        cache_key="source:failed",
        request_url="https://www.youtube.com/watch?v=failed",
        requested_language=None,
        force_refresh=False,
    )
    transcript_repository.update_job(
        failed.id,
        status=JobStatus.FAILED,
        video_json={"id": "failed", "title": "Search me"},
    )
    service = WorkspaceService(
        transcript_repository,
        rewrite_repository,
        settings,
    )

    completed_items, completed_total = service.list_workspaces(
        status=WorkspaceStatus.COMPLETED
    )
    searched_items, searched_total = service.list_workspaces(query="Search me")

    assert [item.id for item in completed_items] == [completed.id]
    assert completed_total == 1
    assert [item.id for item in searched_items] == [failed.id]
    assert searched_total == 1


def test_workspace_includes_direct_rewrite_created_before_auto_workflows(settings):
    transcript_repository, rewrite_repository = _repositories(settings)
    source = _completed_transcript(
        transcript_repository,
        settings.jobs_dir,
        "legacy-source",
        auto_rewrite=False,
    )
    rewrite = rewrite_repository.create_job(
        job_id="legacy-rewrite",
        transcript_job_id=source.id,
        source_hash="legacy-hash",
        cache_key="legacy-cache",
        force_refresh=False,
        source_language="en",
    )
    rewrite_repository.update_job(
        rewrite.id,
        status=JobStatus.RUNNING,
        progress=40,
    )
    service = WorkspaceService(
        transcript_repository,
        rewrite_repository,
        settings,
    )

    snapshot = service.get_workspace(source.id)

    assert snapshot is not None
    assert snapshot.status == WorkspaceStatus.RUNNING
    assert snapshot.phase == WorkspacePhase.REWRITE
    assert snapshot.progress == 70
