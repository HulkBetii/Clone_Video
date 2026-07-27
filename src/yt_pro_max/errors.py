from __future__ import annotations

from typing import Any

from yt_pro_max.models import ErrorInfo


class PipelineError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.info = ErrorInfo(
            code=code,
            message=message,
            retryable=retryable,
            details=details or {},
        )
