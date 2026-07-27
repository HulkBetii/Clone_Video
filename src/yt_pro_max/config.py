from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="YT_PRO_MAX_",
        extra="ignore",
    )

    app_name: str = "YT Pro Max"
    data_dir: Path = Path("data")
    max_duration_seconds: int = 6 * 60 * 60
    youtube_retries: int = 3
    pipeline_version: str = "1"
    whisper_model: str = "turbo"
    whisper_device: str = "auto"
    gpu_compute_type: str = "float16"
    cpu_compute_type: str = "int8"
    rewrite_pipeline_version: str = "1"
    rewrite_prompt_version: str = "1"
    rewrite_chunk_max_chars: int = 25_000
    rewrite_target_ratio: float = 1.10
    rewrite_min_ratio: float = 1.0
    rewrite_max_ratio: float = 1.30
    rewrite_repair_attempts: int = 2
    rewrite_validation_score: int = 80
    gpt_profile_id: str = "PROFILE_GPT_1"
    gpt_profile_dir: Path = Path(r"D:\VibeCoding\auto_YT\data\chrome_user_data\PROFILE_GPT_1")
    gpt_reply_timeout_seconds: int = 300
    gpt_attachment_timeout_seconds: int = 60
    gpt_retries: int = 3

    @property
    def database_path(self) -> Path:
        return self.data_dir / "jobs.sqlite3"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def temp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def rewrite_jobs_dir(self) -> Path:
        return self.data_dir / "rewrite_jobs"

    @property
    def rewrite_temp_dir(self) -> Path:
        return self.temp_dir / "rewrite"

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.rewrite_jobs_dir.mkdir(parents=True, exist_ok=True)
        self.rewrite_temp_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
