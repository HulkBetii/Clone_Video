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

    @property
    def database_path(self) -> Path:
        return self.data_dir / "jobs.sqlite3"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def temp_dir(self) -> Path:
        return self.data_dir / "tmp"

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
