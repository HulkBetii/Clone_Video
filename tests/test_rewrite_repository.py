import sqlite3

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
        validation_json={
            "passed": True,
            "style_score": 91,
            "coverage_score": 93,
            "language_match": True,
            "tts_ready": True,
            "unsupported_claims": [],
            "missing_points": [],
            "length_ratio": 1.1,
        },
    )
    assert updated.checkpoint == {"next_section": 3}
    assert updated.work_files == {"draft": "draft.txt"}
    assert updated.validation == {
        "passed": True,
        "style_score": 91,
        "coverage_score": 93,
        "language_match": True,
        "tts_ready": True,
        "unsupported_claims": [],
        "missing_points": [],
        "length_ratio": 1.1,
    }
    assert repository.find_latest_for_source("source-1") == updated
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


def test_rewrite_repository_adds_validation_column_without_losing_jobs(settings):
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute(
            """
            CREATE TABLE rewrite_jobs (
                id TEXT PRIMARY KEY,
                transcript_job_id TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                cache_key TEXT NOT NULL,
                force_refresh INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                stage TEXT,
                progress INTEGER NOT NULL DEFAULT 0,
                source_language TEXT,
                source_length INTEGER,
                output_length INTEGER,
                sections_completed INTEGER NOT NULL DEFAULT 0,
                sections_total INTEGER NOT NULL DEFAULT 0,
                title TEXT,
                artifact_path TEXT,
                conversation_url TEXT,
                checkpoint_json TEXT,
                work_files_json TEXT,
                warnings_json TEXT NOT NULL DEFAULT '[]',
                error_json TEXT,
                cached INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO rewrite_jobs (
                id, transcript_job_id, source_hash, cache_key, status, created_at, updated_at
            ) VALUES ('legacy', 'source', 'hash', 'cache', 'failed', '2026-01-01', '2026-01-01')
            """
        )

    repository = RewriteJobRepository(settings.database_path)
    repository.initialize()

    legacy = repository.get_job("legacy")
    assert legacy is not None
    assert legacy.id == "legacy"
    assert legacy.validation is None
