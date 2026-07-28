import sqlite3

import pytest

from yt_pro_max.models import JobStatus
from yt_pro_max.repository import JobRepository


def _create_job(
    repository: JobRepository,
    job_id: str,
    *,
    auto_rewrite: bool = False,
):
    return repository.create_job(
        job_id=job_id,
        cache_key=f"cache:{job_id}",
        request_url=f"https://www.youtube.com/watch?v={job_id}",
        requested_language="vi",
        force_refresh=False,
        auto_rewrite_requested=auto_rewrite,
    )


def test_initialize_migrates_existing_jobs_table_without_data_loss(settings):
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute(
            """
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY,
                cache_key TEXT NOT NULL,
                request_url TEXT NOT NULL,
                requested_language TEXT,
                force_refresh INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                stage TEXT,
                progress INTEGER NOT NULL DEFAULT 0,
                source TEXT,
                actual_language TEXT,
                language_confidence REAL,
                video_json TEXT,
                artifacts_json TEXT,
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
            INSERT INTO jobs (
                id, cache_key, request_url, status, created_at, updated_at
            ) VALUES ('legacy', 'legacy-cache', 'https://youtu.be/legacy', 'completed', '1', '1')
            """
        )

    repository = JobRepository(settings.database_path)
    repository.initialize()

    legacy = repository.get_job("legacy")
    assert legacy is not None
    assert legacy.status == JobStatus.COMPLETED
    assert not legacy.auto_rewrite_requested


def test_repository_lists_searches_and_marks_auto_rewrite_monotonically(settings):
    repository = JobRepository(settings.database_path)
    repository.initialize()
    first = _create_job(repository, "first")
    second = _create_job(repository, "second")
    repository.update_job(
        first.id,
        status=JobStatus.COMPLETED,
        video_json={"id": "first", "title": "A searchable title"},
    )
    repository.update_job(second.id, status=JobStatus.FAILED)

    requested = repository.request_auto_rewrite(first.id)
    requested_again = repository.request_auto_rewrite(first.id)

    assert requested.auto_rewrite_requested
    assert requested_again.updated_at == requested.updated_at
    assert repository.list_auto_rewrite_candidates() == [requested]
    assert repository.list_jobs(query="searchable") == [requested]
    assert repository.list_jobs(statuses=(JobStatus.FAILED,)) == [
        repository.get_job(second.id)
    ]
    assert repository.count_jobs() == 2
    assert repository.count_jobs(statuses=(JobStatus.COMPLETED,)) == 1
    assert len(repository.list_jobs(limit=1, offset=1)) == 1

    with pytest.raises(ValueError, match="limit"):
        repository.list_jobs(limit=0)
    with pytest.raises(ValueError, match="offset"):
        repository.list_jobs(offset=1)
