from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from yt_pro_max.models import JobStage, JobStatus, TranscriptSource

UPDATABLE_COLUMNS = {
    "status",
    "stage",
    "progress",
    "source",
    "actual_language",
    "language_confidence",
    "video_json",
    "artifacts_json",
    "warnings_json",
    "error_json",
    "cached",
}


@dataclass(frozen=True)
class StoredJob:
    id: str
    cache_key: str
    request_url: str
    requested_language: str | None
    force_refresh: bool
    status: JobStatus
    stage: JobStage | None
    progress: int
    source: TranscriptSource | None
    actual_language: str | None
    language_confidence: float | None
    video: dict[str, Any] | None
    artifact_paths: dict[str, str] | None
    warnings: list[str]
    error: dict[str, Any] | None
    cached: bool
    created_at: str
    updated_at: str
    auto_rewrite_requested: bool = False


class JobRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    cache_key TEXT NOT NULL,
                    request_url TEXT NOT NULL,
                    requested_language TEXT,
                    force_refresh INTEGER NOT NULL DEFAULT 0,
                    auto_rewrite_requested INTEGER NOT NULL DEFAULT 0,
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
                "CREATE INDEX IF NOT EXISTS jobs_cache_idx ON jobs(cache_key, status, updated_at)"
            )
            self._add_column_if_missing(
                connection,
                "auto_rewrite_requested",
                "INTEGER NOT NULL DEFAULT 0",
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS jobs_workspace_idx
                ON jobs(auto_rewrite_requested, status, updated_at)
                """
            )

    def create_job(
        self,
        *,
        job_id: str,
        cache_key: str,
        request_url: str,
        requested_language: str | None,
        force_refresh: bool,
        auto_rewrite_requested: bool = False,
    ) -> StoredJob:
        timestamp = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, cache_key, request_url, requested_language, force_refresh,
                    auto_rewrite_requested, status, progress, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    job_id,
                    cache_key,
                    request_url,
                    requested_language,
                    int(force_refresh),
                    int(auto_rewrite_requested),
                    JobStatus.QUEUED.value,
                    timestamp,
                    timestamp,
                ),
            )
        job = self.get_job(job_id)
        if job is None:
            raise RuntimeError("created job could not be loaded")
        return job

    def get_job(self, job_id: str) -> StoredJob | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None

    def find_completed(self, cache_key: str) -> StoredJob | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE cache_key = ? AND status = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (cache_key, JobStatus.COMPLETED.value),
            ).fetchone()
        return _row_to_job(row) if row else None

    def list_unfinished(self) -> list[StoredJob]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE status IN (?, ?) ORDER BY created_at",
                (JobStatus.QUEUED.value, JobStatus.RUNNING.value),
            ).fetchall()
        return [_row_to_job(row) for row in rows]

    def request_auto_rewrite(self, job_id: str) -> StoredJob:
        current = self.get_job(job_id)
        if current is None:
            raise KeyError(job_id)
        if current.auto_rewrite_requested:
            return current
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET auto_rewrite_requested = 1, updated_at = ?
                WHERE id = ?
                """,
                (_now(), job_id),
            )
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def list_auto_rewrite_candidates(self) -> list[StoredJob]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE auto_rewrite_requested = 1 AND status = ?
                ORDER BY updated_at, id
                """,
                (JobStatus.COMPLETED.value,),
            ).fetchall()
        return [_row_to_job(row) for row in rows]

    def list_jobs(
        self,
        *,
        query: str | None = None,
        statuses: tuple[JobStatus, ...] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[StoredJob]:
        sql, values = _job_list_query(query=query, statuses=statuses)
        sql += " ORDER BY created_at DESC, id DESC"
        if limit is not None:
            if limit <= 0:
                raise ValueError("limit must be positive")
            if offset < 0:
                raise ValueError("offset must not be negative")
            sql += " LIMIT ? OFFSET ?"
            values.extend((limit, offset))
        elif offset:
            raise ValueError("offset requires a limit")
        with self._connect() as connection:
            rows = connection.execute(sql, values).fetchall()
        return [_row_to_job(row) for row in rows]

    def count_jobs(
        self,
        *,
        query: str | None = None,
        statuses: tuple[JobStatus, ...] | None = None,
    ) -> int:
        sql, values = _job_list_query(query=query, statuses=statuses, count=True)
        with self._connect() as connection:
            row = connection.execute(sql, values).fetchone()
        return int(row[0])

    def update_job(self, job_id: str, **changes: Any) -> StoredJob:
        unknown_columns = changes.keys() - UPDATABLE_COLUMNS
        if unknown_columns:
            raise ValueError(f"unsupported job columns: {sorted(unknown_columns)}")
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
                f"UPDATE jobs SET {assignments} WHERE id = ?",  # noqa: S608
                values,
            )
            if cursor.rowcount == 0:
                raise KeyError(job_id)
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def is_healthy(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _add_column_if_missing(
        connection: sqlite3.Connection,
        column: str,
        definition: str,
    ) -> None:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE jobs ADD COLUMN {column} {definition}")  # noqa: S608


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _serialize_value(column: str, value: Any) -> Any:
    if column in {"video_json", "artifacts_json", "warnings_json", "error_json"}:
        return json.dumps(value, ensure_ascii=False) if value is not None else None
    if column in {"status", "stage", "source"} and value is not None:
        return value.value if hasattr(value, "value") else value
    if column == "cached":
        return int(value)
    return value


def _job_list_query(
    *,
    query: str | None,
    statuses: tuple[JobStatus, ...] | None,
    count: bool = False,
) -> tuple[str, list[Any]]:
    sql = "SELECT COUNT(*) FROM jobs" if count else "SELECT * FROM jobs"
    clauses: list[str] = []
    values: list[Any] = []
    normalized_query = query.strip() if query else ""
    if normalized_query:
        clauses.append(
            "(request_url LIKE ? OR requested_language LIKE ? OR video_json LIKE ?)"
        )
        pattern = f"%{normalized_query}%"
        values.extend((pattern, pattern, pattern))
    if statuses:
        placeholders = ", ".join("?" for _ in statuses)
        clauses.append(f"status IN ({placeholders})")
        values.extend(status.value for status in statuses)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    return sql, values


def _row_to_job(row: sqlite3.Row) -> StoredJob:
    return StoredJob(
        id=row["id"],
        cache_key=row["cache_key"],
        request_url=row["request_url"],
        requested_language=row["requested_language"],
        force_refresh=bool(row["force_refresh"]),
        auto_rewrite_requested=bool(row["auto_rewrite_requested"]),
        status=JobStatus(row["status"]),
        stage=JobStage(row["stage"]) if row["stage"] else None,
        progress=row["progress"],
        source=TranscriptSource(row["source"]) if row["source"] else None,
        actual_language=row["actual_language"],
        language_confidence=row["language_confidence"],
        video=json.loads(row["video_json"]) if row["video_json"] else None,
        artifact_paths=json.loads(row["artifacts_json"]) if row["artifacts_json"] else None,
        warnings=json.loads(row["warnings_json"]),
        error=json.loads(row["error_json"]) if row["error_json"] else None,
        cached=bool(row["cached"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
