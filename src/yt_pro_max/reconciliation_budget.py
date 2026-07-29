from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WindowCandidate:
    start_ms: int
    end_ms: int
    span_ids: tuple[int, ...]
    priority_tier: int
    priority_score: float


def select_windows(
    candidates: list[WindowCandidate],
    *,
    max_windows: int,
    max_total_ms: int,
) -> tuple[list[WindowCandidate], set[int]]:
    """Select the most suspicious windows, then return them in timeline order."""
    selected = []
    skipped_span_ids = set()
    total_duration_ms = 0

    prioritized = sorted(
        candidates,
        key=lambda candidate: (
            candidate.priority_tier,
            candidate.priority_score,
            candidate.start_ms,
            candidate.end_ms,
            candidate.span_ids,
        ),
    )
    for candidate in prioritized:
        duration_ms = candidate.end_ms - candidate.start_ms
        if (
            len(selected) >= max_windows
            or total_duration_ms + duration_ms > max_total_ms
        ):
            skipped_span_ids.update(candidate.span_ids)
            continue
        selected.append(candidate)
        total_duration_ms += duration_ms

    selected.sort(
        key=lambda candidate: (
            candidate.start_ms,
            candidate.end_ms,
            candidate.span_ids,
        )
    )
    return selected, skipped_span_ids
