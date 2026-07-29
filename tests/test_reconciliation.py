from __future__ import annotations

from yt_pro_max.config import Settings
from yt_pro_max.models import TranscriptSegment, WordTimestamp
from yt_pro_max.reconciliation import (
    CAPTION_DISAGREEMENT_TRIGGER,
    DECISION_CORRECTED,
    DECISION_UNRESOLVED,
    LIMIT_WARNING,
    LOW_CONFIDENCE_TRIGGER,
    RECONCILED_WARNING,
    UNRESOLVED_WARNING,
    align_character_timelines,
    align_segment_to_captions,
    apply_reconciliation,
    build_character_timeline,
    build_reconciliation_plan,
    japanese_text_similarity,
    normalize_japanese_text,
)


def _words_segment(
    index: int,
    start_ms: int,
    end_ms: int,
    words: list[str],
    *,
    probabilities: list[float] | None = None,
) -> TranscriptSegment:
    duration = end_ms - start_ms
    probabilities = probabilities or [0.98] * len(words)
    word_timestamps = []
    for word_index, (text, probability) in enumerate(zip(words, probabilities, strict=True)):
        word_start = start_ms + round(duration * word_index / len(words))
        word_end = start_ms + round(duration * (word_index + 1) / len(words))
        word_timestamps.append(
            WordTimestamp(
                start_ms=word_start,
                end_ms=max(word_start + 1, word_end),
                text=text,
                probability=probability,
            )
        )
    return TranscriptSegment(
        index=index,
        start_ms=start_ms,
        end_ms=end_ms,
        text="".join(words),
        words=word_timestamps,
    )


def _timed_segment(
    index: int,
    start_ms: int,
    end_ms: int,
    words: list[tuple[str, int, int, float]],
) -> TranscriptSegment:
    word_timestamps = [
        WordTimestamp(
            text=text,
            start_ms=word_start,
            end_ms=word_end,
            probability=probability,
        )
        for text, word_start, word_end, probability in words
    ]
    return TranscriptSegment(
        index=index,
        start_ms=start_ms,
        end_ms=end_ms,
        text="".join(word.text for word in word_timestamps),
        words=word_timestamps,
    )


def _window_index_for_span(plan, span_id: int) -> int:
    return next(window.index for window in plan.windows if span_id in window.span_ids)


def test_normalize_japanese_text_handles_noise_width_and_script():
    assert normalize_japanese_text("［音楽］カタカナ、ＡＢＣ♪") == "かたかなabc"
    assert japanese_text_similarity("カタカナ。", "かたかな") == 1.0


def test_character_alignment_is_monotonic_for_split_cues():
    alignment = align_segment_to_captions(
        _words_segment(1, 1_000, 3_000, list("甲乙丙丁戊")),
        [
            _words_segment(1, 1_100, 2_000, list("甲乙")),
            _words_segment(2, 2_000, 2_900, list("丙丁戊")),
        ],
        Settings(),
    )

    assert alignment.coverage >= 0.8
    assert list(alignment.pairs) == sorted(alignment.pairs)
    assert alignment.caption_text == "甲乙丙丁戊"


def test_character_alignment_prefers_temporally_closest_repeated_phrase():
    primary = _words_segment(1, 10_000, 11_000, list("甲乙丙丁"))
    captions = [
        _words_segment(1, 7_000, 8_000, list("甲乙丙丁")),
        _words_segment(2, 10_000, 11_000, list("甲乙丙丁")),
    ]

    alignment = align_segment_to_captions(primary, captions, Settings())

    assert alignment.pairs
    assert min(caption_index for _, caption_index in alignment.pairs) >= 4
    assert alignment.temporal_overlap == 1.0


def test_character_alignment_merges_anchors_from_overlapping_time_windows():
    primary_words = [chr(0x4E00 + index) for index in range(100)]
    caption_words = primary_words.copy()
    caption_words[25:27] = [chr(0x5000), chr(0x5001)]

    alignment = align_character_timelines(
        build_character_timeline([_words_segment(1, 0, 100_000, primary_words)]),
        build_character_timeline([_words_segment(1, 0, 100_000, caption_words)]),
        window_ms=60_000,
        overlap_ms=5_000,
        min_anchor_chars=4,
    )

    assert len(alignment.pairs) == 98
    assert list(alignment.pairs) == sorted(alignment.pairs)


def test_global_alignment_uses_anchor_across_segment_boundary(tmp_path):
    settings = Settings(data_dir=tmp_path)
    primary = [
        _words_segment(1, 0, 4_000, list("甲乙丙丁")),
        _words_segment(2, 4_000, 8_000, list("戊己庚辛")),
    ]
    captions = [_words_segment(1, 0, 8_000, list("甲乙丙子戊己庚辛"))]

    plan = build_reconciliation_plan(primary, captions, settings)

    target = next(span for span in plan.spans if span.primary_text == "丁")
    assert target.segment_index == 1
    assert target.caption_text == "子"
    assert target.alignment_coverage > 0


def test_global_alignment_maps_multiple_primary_cues_to_one_caption_cue(tmp_path):
    settings = Settings(data_dir=tmp_path)
    primary = [
        _words_segment(1, 0, 4_000, list("甲乙丙丁")),
        _words_segment(2, 4_000, 8_000, list("戊己庚辛")),
    ]
    captions = [_words_segment(1, 0, 8_000, list("甲乙丙丁戊己庚辛"))]

    plan = build_reconciliation_plan(primary, captions, settings)

    assert plan.compared_segments == 2
    assert plan.alignment_coverage == 1.0
    assert plan.spans == ()


def test_plan_detects_word_level_low_confidence_and_disagreement(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        reconciliation_low_word_probability=0.7,
        reconciliation_similarity_threshold=0.9,
    )
    primary = [
        _words_segment(
            1,
            1_000,
            3_000,
            list("太目が覚める"),
            probabilities=[0.98, 0.4, 0.98, 0.98, 0.98, 0.98],
        )
    ]
    captions = [_words_segment(1, 1_100, 2_900, list("太めが覚める"))]

    plan = build_reconciliation_plan(primary, captions, settings)

    target = next(span for span in plan.spans if span.primary_text == "目")
    assert plan.compared_segments == 1
    assert target.caption_text == "め"
    assert target.triggers == (LOW_CONFIDENCE_TRIGGER, CAPTION_DISAGREEMENT_TRIGGER)
    assert target.word_start == 1
    assert target.priority_tier == 1


def test_budget_prioritizes_tier_one_before_tier_three(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        reconciliation_similarity_threshold=0.99,
        reconciliation_max_windows=1,
        reconciliation_max_total_ms=2_000,
        reconciliation_window_padding_ms=0,
    )
    primary = [
        _words_segment(
            1,
            0,
            1_000,
            list("甲乙丙丁戊己"),
            probabilities=[0.98, 0.4, 0.98, 0.98, 0.98, 0.98],
        ),
        _words_segment(2, 10_000, 11_000, list("戊己庚辛壬癸")),
    ]
    captions = [
        _words_segment(1, 0, 1_000, list("甲壬丙丁戊己")),
        _words_segment(2, 10_000, 11_000, list("戊子庚辛壬癸")),
    ]

    plan = build_reconciliation_plan(primary, captions, settings)

    assert len(plan.windows) == 1
    assert plan.spans[plan.windows[0].span_ids[0]].priority_tier == 1
    assert plan.skipped_span_ids


def test_apply_reconciliation_corrects_only_local_caption_secondary_consensus(tmp_path):
    settings = Settings(data_dir=tmp_path)
    primary = [_words_segment(1, 1_000, 3_000, list("太目が覚める"))]
    captions = [_words_segment(1, 1_000, 3_000, list("太めが覚める"))]
    plan = build_reconciliation_plan(primary, captions, settings)
    target = next(span for span in plan.spans if span.primary_text == "目")
    secondary = {
        plan.windows[0].index: [
            _words_segment(
                1,
                1_000,
                3_000,
                list("太めが覚める"),
                probabilities=[0.96] * 6,
            )
        ]
    }

    outcome = apply_reconciliation(primary, plan, secondary, settings)

    assert outcome.segments[0].text == "太めが覚める"
    assert outcome.segments[0].words[1].text == "め"
    assert outcome.reconciliation.corrected_words == 1
    assert outcome.reconciliation.corrected_segments == 1
    assert outcome.reconciliation.items[target.id].decision == DECISION_CORRECTED
    assert RECONCILED_WARNING in outcome.warnings


def test_apply_reconciliation_keeps_primary_when_caption_and_secondary_disagree(tmp_path):
    settings = Settings(data_dir=tmp_path)
    primary = [_words_segment(1, 1_000, 3_000, list("太目が覚める"))]
    captions = [_words_segment(1, 1_000, 3_000, list("太めが覚める"))]
    plan = build_reconciliation_plan(primary, captions, settings)
    target = next(span for span in plan.spans if span.primary_text == "目")
    secondary = {
        plan.windows[0].index: [
            _words_segment(1, target.start_ms, target.end_ms, ["ふと"], probabilities=[0.99])
        ]
    }

    outcome = apply_reconciliation(primary, plan, secondary, settings)

    assert outcome.segments[0].text == "太目が覚める"
    assert outcome.reconciliation.items[target.id].decision == DECISION_UNRESOLVED
    assert UNRESOLVED_WARNING in outcome.warnings


def test_apply_reconciliation_rejects_low_secondary_confidence(tmp_path):
    settings = Settings(data_dir=tmp_path)
    primary = [_words_segment(1, 1_000, 3_000, list("太目が覚める"))]
    captions = [_words_segment(1, 1_000, 3_000, list("太めが覚める"))]
    plan = build_reconciliation_plan(primary, captions, settings)
    target = next(span for span in plan.spans if span.primary_text == "目")
    secondary = {
        plan.windows[0].index: [
            _words_segment(1, target.start_ms, target.end_ms, ["め"], probabilities=[0.4])
        ]
    }

    outcome = apply_reconciliation(primary, plan, secondary, settings)

    assert outcome.segments[0].text == "太目が覚める"
    assert outcome.reconciliation.items[target.id].decision == DECISION_UNRESOLVED


def test_apply_reconciliation_rejects_secondary_partial_word_leakage(tmp_path):
    settings = Settings(data_dir=tmp_path)
    primary = [_words_segment(1, 0, 3_000, ["甲乙丙丁", "誤", "戊己庚辛"])]
    captions = [_words_segment(1, 0, 3_000, ["甲乙丙丁", "正", "戊己庚辛"])]
    plan = build_reconciliation_plan(primary, captions, settings)
    target = next(span for span in plan.spans if span.primary_text == "誤")
    secondary = {
        _window_index_for_span(plan, target.id): [
            _words_segment(1, 0, 3_000, ["甲乙丙丁正", "戊己庚辛"])
        ]
    }

    outcome = apply_reconciliation(primary, plan, secondary, settings)
    item = outcome.reconciliation.items[target.id]

    assert outcome.segments[0].text == "甲乙丙丁誤戊己庚辛"
    assert item.decision == DECISION_UNRESOLVED
    assert item.decision_reason == "secondary_range_partial_word"


def test_apply_reconciliation_rejects_low_caption_alignment_coverage(tmp_path):
    settings = Settings(data_dir=tmp_path)
    primary = [_words_segment(1, 0, 9_000, list("甲乙丙丁誤戊己庚辛"))]
    captions = [_words_segment(1, 0, 5_000, list("甲乙丙丁正"))]
    plan = build_reconciliation_plan(primary, captions, settings)
    target = next(span for span in plan.spans if span.primary_text == "誤")
    secondary = {
        _window_index_for_span(plan, target.id): [
            _words_segment(1, 0, 5_000, list("甲乙丙丁正"))
        ]
    }

    outcome = apply_reconciliation(primary, plan, secondary, settings)
    item = outcome.reconciliation.items[target.id]

    assert target.alignment_coverage < settings.reconciliation_min_alignment_coverage
    assert item.decision == DECISION_UNRESOLVED
    assert item.decision_reason == "alignment_coverage_low"


def test_apply_reconciliation_rejects_low_temporal_overlap(tmp_path):
    settings = Settings(data_dir=tmp_path, reconciliation_window_padding_ms=10_000)
    primary = [_words_segment(1, 0, 9_000, list("誤甲乙丙丁戊己庚辛"))]
    captions = [_words_segment(1, 500, 9_500, list("正甲乙丙丁戊己庚辛"))]
    plan = build_reconciliation_plan(primary, captions, settings)
    target = next(span for span in plan.spans if span.primary_text == "誤")
    secondary = {
        _window_index_for_span(plan, target.id): [
            _words_segment(1, 500, 9_500, list("正甲乙丙丁戊己庚辛"))
        ]
    }

    outcome = apply_reconciliation(primary, plan, secondary, settings)
    item = outcome.reconciliation.items[target.id]

    assert target.temporal_overlap < settings.reconciliation_min_temporal_overlap
    assert item.decision == DECISION_UNRESOLVED
    assert item.decision_reason == "temporal_overlap_low"


def test_apply_reconciliation_rejects_secondary_min_probability(tmp_path):
    settings = Settings(data_dir=tmp_path)
    primary = [_words_segment(1, 0, 3_000, ["甲乙丙丁", "誤誤誤誤", "戊己庚辛"])]
    captions = [_words_segment(1, 0, 3_000, ["甲乙丙丁", "正正正正", "戊己庚辛"])]
    plan = build_reconciliation_plan(primary, captions, settings)
    target = next(span for span in plan.spans if span.primary_text == "誤誤誤誤")
    secondary = {
        _window_index_for_span(plan, target.id): [
            _timed_segment(
                1,
                0,
                3_000,
                [
                    ("甲乙丙丁", 0, 1_000, 0.99),
                    ("正", 1_000, 1_250, 0.99),
                    ("正", 1_250, 1_500, 0.99),
                    ("正", 1_500, 1_750, 0.99),
                    ("正", 1_750, 2_000, 0.49),
                    ("戊己庚辛", 2_000, 3_000, 0.99),
                ],
            )
        ]
    }

    outcome = apply_reconciliation(primary, plan, secondary, settings)
    item = outcome.reconciliation.items[target.id]

    assert item.secondary_mean_probability >= settings.reconciliation_secondary_mean_probability
    assert item.secondary_min_probability < settings.reconciliation_secondary_min_probability
    assert item.decision == DECISION_UNRESOLVED
    assert item.decision_reason == "secondary_min_probability_low"


def test_corrected_rebuild_preserves_reconstructable_mixed_spacing(tmp_path):
    settings = Settings(data_dir=tmp_path)
    primary = [_words_segment(1, 0, 3_000, ["甲乙丙丁", " 太目", " 戊己庚辛"])]
    captions = [_words_segment(1, 0, 3_000, ["甲乙丙丁", " 太め", " 戊己庚辛"])]
    plan = build_reconciliation_plan(primary, captions, settings)
    target = next(
        span for span in plan.spans if normalize_japanese_text(span.primary_text) == "太目"
    )
    secondary = {
        _window_index_for_span(plan, target.id): [
            _words_segment(1, 0, 3_000, ["甲乙丙丁", " 太め", " 戊己庚辛"])
        ]
    }

    outcome = apply_reconciliation(primary, plan, secondary, settings)

    assert outcome.reconciliation.items[target.id].decision == DECISION_CORRECTED
    assert outcome.segments[0].text == "甲乙丙丁 太め 戊己庚辛"
    assert "".join(word.text for word in outcome.segments[0].words) == outcome.segments[0].text


def test_multiple_word_replacements_rebuild_segment_without_duplicates(tmp_path):
    settings = Settings(data_dir=tmp_path, reconciliation_window_padding_ms=5_000)
    primary = [_words_segment(1, 0, 10_000, list("甲乙丙丁戊己庚辛壬癸"))]
    captions = [_words_segment(1, 0, 10_000, list("甲子丙丁戊己庚辛丑癸"))]
    plan = build_reconciliation_plan(primary, captions, settings)
    targets = {span.primary_text: span for span in plan.spans}
    window_by_span_id = {
        span_id: window.index for window in plan.windows for span_id in window.span_ids
    }
    secondary = {
        window_by_span_id[targets["乙"].id]: [
            _words_segment(
                1,
                0,
                10_000,
                list("甲子丙丁戊己庚辛丑癸"),
                probabilities=[0.99] * 10,
            )
        ],
        window_by_span_id[targets["壬"].id]: [
            _words_segment(
                2,
                0,
                10_000,
                list("甲子丙丁戊己庚辛丑癸"),
                probabilities=[0.99] * 10,
            ),
        ]
    }

    outcome = apply_reconciliation(primary, plan, secondary, settings)

    assert outcome.segments[0].text == "甲子丙丁戊己庚辛丑癸"
    assert outcome.reconciliation.corrected_words == 2
    assert len(outcome.segments[0].words) == 10


def test_budget_skip_is_recorded_without_changing_primary(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        reconciliation_max_windows=1,
        reconciliation_max_total_ms=1_000,
        reconciliation_window_padding_ms=0,
    )
    primary = [
        _words_segment(1, 0, 1_000, list("甲乙丙丁"), probabilities=[0.4, 0.98, 0.98, 0.98]),
        _words_segment(2, 3_000, 4_000, list("戊己庚辛"), probabilities=[0.4, 0.98, 0.98, 0.98]),
    ]
    captions = [
        _words_segment(1, 0, 1_000, list("甲壬丙丁")),
        _words_segment(2, 3_000, 4_000, list("戊壬庚辛")),
    ]
    plan = build_reconciliation_plan(primary, captions, settings)
    outcome = apply_reconciliation(primary, plan, {}, settings)

    assert outcome.reconciliation.skipped_segments >= 1
    assert LIMIT_WARNING in outcome.warnings
    assert outcome.segments[0].text == "甲乙丙丁"
