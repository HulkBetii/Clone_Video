from yt_pro_max.models import JobStatus, RewriteStage
from yt_pro_max.rewrite_repository import RewriteJobRepository


def test_rewrite_repository_persists_progress_and_cache(settings):
    repository = RewriteJobRepository(settings.database_path)
    repository.initialize()
    created = repository.create_job(
        job_id="rewrite-1",
        transcript_job_id="source-1",
        source_hash="source-hash",
        cache_key="cache-key",
        force_refresh=False,
        source_language="vi",
    )

    assert created.status == JobStatus.QUEUED
    assert repository.find_active("cache-key") == created

    updated = repository.update_job(
        created.id,
        status=JobStatus.RUNNING,
        stage=RewriteStage.REWRITING,
        progress=52,
        sections_completed=2,
        sections_total=4,
        checkpoint_json={"next_section": 3},
        work_files_json={"draft": "draft.txt"},
        conversation_url="https://chatgpt.com/c/example",
    )
    assert updated.checkpoint == {"next_section": 3}
    assert updated.work_files == {"draft": "draft.txt"}
    assert repository.list_unfinished() == [updated]

    completed = repository.update_job(
        created.id,
        status=JobStatus.COMPLETED,
        stage=RewriteStage.RENDERING,
        progress=100,
        source_length=1_000,
        output_length=1_100,
        sections_completed=4,
        sections_total=4,
        title="SEO title",
        artifact_path="rewrite.txt",
        warnings_json=["warning"],
    )
    assert repository.find_active("cache-key") is None
    assert repository.find_completed("cache-key") == completed
    assert repository.list_unfinished() == []
