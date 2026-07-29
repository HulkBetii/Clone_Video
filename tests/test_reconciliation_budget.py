from yt_pro_max.reconciliation_budget import WindowCandidate, select_windows


def _candidate(
    span_id: int,
    start_ms: int,
    *,
    tier: int,
    score: float,
    duration_ms: int = 1_000,
) -> WindowCandidate:
    return WindowCandidate(
        start_ms=start_ms,
        end_ms=start_ms + duration_ms,
        span_ids=(span_id,),
        priority_tier=tier,
        priority_score=score,
    )


def test_select_windows_prioritizes_tier_before_timeline():
    candidates = [
        _candidate(1, 1_000, tier=3, score=0.1),
        _candidate(2, 2_000, tier=2, score=0.2),
        _candidate(3, 3_000, tier=1, score=0.9),
    ]

    selected, skipped = select_windows(
        candidates,
        max_windows=1,
        max_total_ms=10_000,
    )

    assert [candidate.span_ids for candidate in selected] == [(3,)]
    assert skipped == {1, 2}


def test_select_windows_uses_score_then_timestamp_within_tier():
    candidates = [
        _candidate(1, 1_000, tier=2, score=0.4),
        _candidate(2, 3_000, tier=2, score=0.2),
        _candidate(3, 2_000, tier=2, score=0.2),
    ]

    selected, skipped = select_windows(
        candidates,
        max_windows=2,
        max_total_ms=10_000,
    )

    assert [candidate.span_ids for candidate in selected] == [(3,), (2,)]
    assert skipped == {1}


def test_select_windows_returns_selected_candidates_in_timeline_order():
    candidates = [
        _candidate(1, 5_000, tier=1, score=0.1),
        _candidate(2, 1_000, tier=2, score=0.1),
    ]

    selected, skipped = select_windows(
        candidates,
        max_windows=2,
        max_total_ms=10_000,
    )

    assert [candidate.span_ids for candidate in selected] == [(2,), (1,)]
    assert skipped == set()


def test_select_windows_enforces_sixty_window_and_ten_minute_budget():
    candidates = [
        _candidate(
            span_id,
            span_id * 20_000,
            tier=1,
            score=0.1,
            duration_ms=10_000,
        )
        for span_id in range(61)
    ]

    selected, skipped = select_windows(
        candidates,
        max_windows=60,
        max_total_ms=600_000,
    )

    assert len(selected) == 60
    assert sum(candidate.end_ms - candidate.start_ms for candidate in selected) == 600_000
    assert skipped == {60}


def test_select_windows_skips_oversized_candidate_and_uses_remaining_budget():
    candidates = [
        _candidate(1, 0, tier=1, score=0.1, duration_ms=7_000),
        _candidate(2, 10_000, tier=2, score=0.1, duration_ms=4_000),
        _candidate(3, 20_000, tier=3, score=0.1, duration_ms=3_000),
    ]

    selected, skipped = select_windows(
        candidates,
        max_windows=3,
        max_total_ms=10_000,
    )

    assert [candidate.span_ids for candidate in selected] == [(1,), (3,)]
    assert skipped == {2}
