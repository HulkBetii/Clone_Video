from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from yt_pro_max.models import JobStatus, RewriteStage

UPDATABLE_COLUMNS = {
    "status",
    "stage",
    "progress",
    "source_language",
    "source_length",
    "output_length",
    "sections_completed",
    "sections_total",
    "title",
    "artifact_path",
    "conversation_url",
    "checkpoint_json",
    "work_files_json",
    "warnings_json",
    "validation_json",
    "error_json",
    "cached",
}


@dataclass(frozen=True)
class StoredRewriteJob:
    id: str
    transcript_job_id: str
    source_hash: str
    cache_key: str
    force_refresh: bool
    status: JobStatus
    stage: RewriteStage | None
    progress: int
    source_language: str | None
    source_length: int | None
    output_length: int | None
    sections_completed: int
    sections_total: int
    title: str | None
    artifact_path: str | None
    conversation_url: str | None
    checkpoint: dict[str, Any] | None
    work_files: dict[str, str] | None
    warnings: list[str]
    error: dict[str, Any] | None
    cached: bool
    created_at: str
    updated_at: str
    validation: dict[str, Any] | None = None


class RewriteJobRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS rewrite_jobs (
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
                    validation_json TEXT,
                    error_json TEXT,
                    cached INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(transcript_job_id) REFERENCES jobs(id)
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(rewrite_jobs)").fetchall()
            }
            if "validation_json" not in columns:
                connection.execute("ALTER TABLE rewrite_jobs ADD COLUMN validation_json TEXT")
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS rewrite_jobs_cache_idx
                ON rewrite_jobs(cache_key, status, updated_at)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS rewrite_jobs_source_idx
                ON rewrite_jobs(transcript_job_id, created_at)
                """
            )

    def create_job(
        self,
        *,
        job_id: str,
        transcript_job_id: str,
        source_hash: str,
        cache_key: str,
        force_refresh: bool,
        source_language: str | None,
    ) -> StoredRewriteJob:
        timestamp = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO rewrite_jobs (
                    id, transcript_job_id, source_hash, cache_key, force_refresh,
                    status, progress, source_language, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    job_id,
                    transcript_job_id,
                    source_hash,
                    cache_key,
                    int(force_refresh),
                    JobStatus.QUEUED.value,
                    source_language,
                    timestamp,
                    timestamp,
                ),
            )
        job = self.get_job(job_id)
        if job is None:
            raise RuntimeError("created rewrite job could not be loaded")
        return job

    def get_job(self, job_id: str) -> StoredRewriteJob | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM rewrite_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return _row_to_job(row) if row else None

    def find_completed(self, cache_key: str) -> StoredRewriteJob | None:
        return self._find_latest(cache_key, (JobStatus.COMPLETED,))

    def find_active(self, cache_key: str) -> StoredRewriteJob | None:
        return self._find_latest(cache_key, (JobStatus.QUEUED, JobStatus.RUNNING))

    def find_latest_for_source(self, transcript_job_id: str) -> StoredRewriteJob | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM rewrite_jobs
                WHERE transcript_job_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (transcript_job_id,),
            ).fetchone()
        return _row_to_job(row) if row else None

    def list_unfinished(self) -> list[StoredRewriteJob]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM rewrite_jobs
                WHERE status IN (?, ?)
                ORDER BY created_at
                """,
                (JobStatus.QUEUED.value, JobStatus.RUNNING.value),
            ).fetchall()
        return [_row_to_job(row) for row in rows]

    def update_job(self, job_id: str, **changes: Any) -> StoredRewriteJob:
        unknown_columns = changes.keys() - UPDATABLE_COLUMNS
        if unknown_columns:
            raise ValueError(f"unsupported rewrite job columns: {sorted(unknown_columns)}")
        if not changes:
            job = self.get_job(job_id)
            if job is None:
                raise KeyError(job_id)
            return job

        serialized = {key: _serialize_value(key, value) for key, value in changes.items()}
        serialized["updated_at"] = _now()
        assignments = ", ".join(f"{column} = ?" for column in serialized)
        values = [*serialized.values(), job_id]
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE rewrite_jobs SET {assignments} WHERE id = ?",  # noqa: S608
                values,
            )
            if cursor.rowcount == 0:
                raise KeyError(job_id)
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def _find_latest(
        self, cache_key: str, statuses: tuple[JobStatus, ...]
    ) -> StoredRewriteJob | None:
        placeholders = ", ".join("?" for _ in statuses)
        values = [cache_key, *(status.value for status in statuses)]
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT * FROM rewrite_jobs
                WHERE cache_key = ? AND status IN ({placeholders})
                ORDER BY updated_at DESC
                LIMIT 1
                """,  # noqa: S608
                values,
            ).fetchone()
        return _row_to_job(row) if row else None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _serialize_value(column: str, value: Any) -> Any:
    if column in {
        "checkpoint_json",
        "work_files_json",
        "warnings_json",
        "validation_json",
        "error_json",
    }:
        return json.dumps(value, ensure_ascii=False) if value is not None else None
    if column in {"status", "stage"} and value is not None:
        return value.value if hasattr(value, "value") else value
    if column == "cached":
        return int(value)
    return value


def _row_to_job(row: sqlite3.Row) -> StoredRewriteJob:
    return StoredRewriteJob(
        id=row["id"],
        transcript_job_id=row["transcript_job_id"],
        source_hash=row["source_hash"],
        cache_key=row["cache_key"],
        force_refresh=bool(row["force_refresh"]),
        status=JobStatus(row["status"]),
        stage=RewriteStage(row["stage"]) if row["stage"] else None,
        progress=row["progress"],
        source_language=row["source_language"],
        source_length=row["source_length"],
        output_length=row["output_length"],
        sections_completed=row["sections_completed"],
        sections_total=row["sections_total"],
        title=row["title"],
        artifact_path=row["artifact_path"],
        conversation_url=row["conversation_url"],
        checkpoint=json.loads(row["checkpoint_json"]) if row["checkpoint_json"] else None,
        work_files=json.loads(row["work_files_json"]) if row["work_files_json"] else None,
        warnings=json.loads(row["warnings_json"]),
        error=json.loads(row["error_json"]) if row["error_json"] else None,
        cached=bool(row["cached"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        validation=json.loads(row["validation_json"]) if row["validation_json"] else None,
    )
