from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

from yt_pro_max.config import Settings
from yt_pro_max.models import (
    TranscriptReconciliation,
    TranscriptReconciliationItem,
    TranscriptSegment,
    WordTimestamp,
)
from yt_pro_max.reconciliation_budget import WindowCandidate, select_windows

LOW_CONFIDENCE_TRIGGER = "low_word_confidence"
CAPTION_DISAGREEMENT_TRIGGER = "caption_disagreement"
DECISION_CORRECTED = "corrected"
DECISION_KEPT_PRIMARY = "kept_primary_consensus"
DECISION_UNRESOLVED = "unresolved"
DECISION_SKIPPED = "skipped"
RECONCILED_WARNING = "JAPANESE_TRANSCRIPT_RECONCILED"
UNRESOLVED_WARNING = "JAPANESE_RECONCILIATION_UNRESOLVED"
LIMIT_WARNING = "JAPANESE_RECONCILIATION_LIMIT_REACHED"
KOREAN_RECONCILED_WARNING = "KOREAN_TRANSCRIPT_RECONCILED"
KOREAN_UNRESOLVED_WARNING = "KOREAN_RECONCILIATION_UNRESOLVED"
KOREAN_LIMIT_WARNING = "KOREAN_RECONCILIATION_LIMIT_REACHED"
ALIGNMENT_VERSION = "monotonic_char_word_v3"

_NOISE_LABELS = (
    "\u97f3\u697d",
    "\u62cd\u624b",
    "\u6b53\u58f0",
    "\u7b11\u3044",
    "\uc74c\uc545",
    "\ubc15\uc218",
    "\uc6c3\uc74c",
    "\ud658\ud638",
    "music",
    "applause",
)
_NOISE_ALTERNATION = "|".join(re.escape(label) for label in _NOISE_LABELS)
_NOISE_PATTERN = re.compile(
    rf"(?:\[(?:{_NOISE_ALTERNATION})\]|"
    rf"\u3010(?:{_NOISE_ALTERNATION})\u3011|"
    rf"\((?:{_NOISE_ALTERNATION})\)|\u266a+)"
)

_WARNING_CODES = {
    "ja": (RECONCILED_WARNING, UNRESOLVED_WARNING, LIMIT_WARNING),
    "ko": (
        KOREAN_RECONCILED_WARNING,
        KOREAN_UNRESOLVED_WARNING,
        KOREAN_LIMIT_WARNING,
    ),
}


@dataclass(frozen=True)
class AlignmentCharacter:
    index: int
    text: str
    start_ms: int
    end_ms: int
    segment_position: int
    word_index: int | None
    source_start_ms: int
    source_end_ms: int


@dataclass(frozen=True)
class CharacterAlignment:
    pairs: tuple[tuple[int, int], ...]
    coverage: float
    temporal_overlap: float
    caption_text: str
    word_caption_text: Mapping[int, str]
    word_temporal_overlap: Mapping[int, float]


@dataclass(frozen=True)
class SuspiciousSpan:
    id: int
    segment_position: int
    segment_index: int
    word_start: int
    word_end: int
    start_ms: int
    end_ms: int
    primary_text: str
    caption_text: str | None
    similarity: float | None
    triggers: tuple[str, ...]
    low_confidence_words: tuple[str, ...]
    priority_tier: int
    priority_score: float
    alignment_coverage: float
    temporal_overlap: float
    primary_probability: float | None


@dataclass(frozen=True)
class ReconciliationWindow:
    index: int
    start_ms: int
    end_ms: int
    span_ids: tuple[int, ...]


@dataclass(frozen=True)
class ReconciliationPlan:
    compared_segments: int
    alignment_coverage: float
    spans: tuple[SuspiciousSpan, ...]
    windows: tuple[ReconciliationWindow, ...]
    skipped_span_ids: tuple[int, ...]


@dataclass(frozen=True)
class ReconciliationOutcome:
    segments: list[TranscriptSegment]
    reconciliation: TranscriptReconciliation
    warnings: list[str]


@dataclass(frozen=True)
class _TimelineWord:
    text: str
    start_ms: int
    end_ms: int
    word_index: int | None


@dataclass(frozen=True)
class _AnchorBlock:
    primary_start: int
    caption_start: int
    size: int
    time_distance_ms: float


@dataclass(frozen=True)
class _SecondaryEvidence:
    text: str
    words: list[WordTimestamp]
    alignment_coverage: float
    temporal_overlap: float
    mean_probability: float | None
    min_probability: float | None
    issue: str | None = None


@dataclass(frozen=True)
class _SecondaryWindowAlignment:
    primary: list[AlignmentCharacter]
    secondary: list[AlignmentCharacter]
    pairs: tuple[tuple[int, int], ...]
    segments: Sequence[TranscriptSegment]


def normalize_alignment_text(value: str, language: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    base_language = _base_language(language)
    if base_language == "ko":
        normalized = unicodedata.normalize("NFC", normalized)
    normalized = _NOISE_PATTERN.sub("", normalized)
    characters = []
    for character in normalized:
        codepoint = ord(character)
        if base_language == "ja" and 0x30A1 <= codepoint <= 0x30F6:
            character = chr(codepoint - 0x60)
        if unicodedata.category(character)[0] in {"P", "S", "Z", "C"}:
            continue
        characters.append(character)
    return "".join(characters)


def normalize_japanese_text(value: str) -> str:
    return normalize_alignment_text(value, "ja")


def text_similarity(first: str, second: str, language: str) -> float:
    normalized_first = normalize_alignment_text(first, language)
    normalized_second = normalize_alignment_text(second, language)
    if not normalized_first or not normalized_second:
        return 0.0
    if normalized_first == normalized_second:
        return 1.0
    return SequenceMatcher(None, normalized_first, normalized_second, autojunk=False).ratio()


def japanese_text_similarity(first: str, second: str) -> float:
    return text_similarity(first, second, "ja")


def build_character_timeline(
    segments: Sequence[TranscriptSegment],
    *,
    language: str = "ja",
    deduplicate_rolling: bool = False,
    min_anchor_chars: int = 4,
) -> list[AlignmentCharacter]:
    timeline: list[AlignmentCharacter] = []
    previous_text = ""
    previous_end_ms = -1
    for segment_position, segment in enumerate(segments):
        timeline_words = _timeline_words(segment)
        segment_text = "".join(
            normalize_alignment_text(word.text, language) for word in timeline_words
        )
        trim_characters = 0
        if deduplicate_rolling and previous_text and segment.start_ms <= previous_end_ms + 1_500:
            trim_characters = _rolling_prefix_length(
                previous_text,
                segment_text,
                min_anchor_chars=min_anchor_chars,
            )

        remaining_trim = trim_characters
        for timeline_word in timeline_words:
            normalized_word = normalize_alignment_text(timeline_word.text, language)
            if not normalized_word:
                continue
            word_trim = min(remaining_trim, len(normalized_word))
            remaining_trim -= word_trim
            original_length = len(normalized_word)
            for character_offset, character in enumerate(normalized_word[word_trim:], word_trim):
                start_ms, end_ms = _distributed_time(
                    timeline_word.start_ms,
                    timeline_word.end_ms,
                    character_offset,
                    original_length,
                )
                timeline.append(
                    AlignmentCharacter(
                        index=len(timeline),
                        text=character,
                        start_ms=start_ms,
                        end_ms=end_ms,
                        segment_position=segment_position,
                        word_index=timeline_word.word_index,
                        source_start_ms=segment.start_ms,
                        source_end_ms=segment.end_ms,
                    )
                )
        if segment_text:
            previous_text = segment_text
            previous_end_ms = segment.end_ms
    return timeline


def align_character_timelines(
    primary: Sequence[AlignmentCharacter],
    caption: Sequence[AlignmentCharacter],
    *,
    language: str = "ja",
    window_ms: int = 60_000,
    overlap_ms: int = 5_000,
    min_anchor_chars: int = 4,
) -> CharacterAlignment:
    if not primary or not caption:
        return CharacterAlignment((), 0.0, 0.0, "", {}, {})
    if window_ms <= 0:
        raise ValueError("window_ms must be greater than zero")
    if overlap_ms < 0 or overlap_ms >= window_ms:
        raise ValueError("overlap_ms must be between zero and window_ms")
    if min_anchor_chars <= 0:
        raise ValueError("min_anchor_chars must be greater than zero")

    pairs = _monotonic_pairs(
        primary,
        caption,
        window_ms=window_ms,
        overlap_ms=overlap_ms,
        min_anchor_chars=min_anchor_chars,
    )
    if not pairs:
        return CharacterAlignment((), 0.0, 0.0, "", {}, {})

    pair_map = {primary_index: caption_index for primary_index, caption_index in pairs}
    caption_indices = [caption_index for _, caption_index in pairs]
    word_caption_text: dict[int, str] = {}
    word_temporal_overlap: dict[int, float] = {}
    word_indexes = sorted(
        {character.word_index for character in primary if character.word_index is not None}
    )
    for word_index in word_indexes:
        word_characters = [character for character in primary if character.word_index == word_index]
        caption_range = _caption_range_for_word(word_characters, pair_map, caption)
        if not caption_range:
            continue
        word_caption_text[word_index] = "".join(character.text for character in caption_range)
        word_temporal_overlap[word_index] = _primary_temporal_coverage(
            min(character.start_ms for character in word_characters),
            max(character.end_ms for character in word_characters),
            min(character.source_start_ms for character in caption_range),
            max(character.source_end_ms for character in caption_range),
        )

    matched_primary = {primary_index for primary_index, _ in pairs}
    for word_index, caption_word_text in word_caption_text.items():
        primary_word_characters = [
            character for character in primary if character.word_index == word_index
        ]
        if normalize_alignment_text(caption_word_text, language) == "".join(
            character.text for character in primary_word_characters
        ):
            matched_primary.update(character.index for character in primary_word_characters)
    overlap_values = [
        _primary_temporal_coverage(
            primary[primary_index].start_ms,
            primary[primary_index].end_ms,
            caption[caption_index].source_start_ms,
            caption[caption_index].source_end_ms,
        )
        for primary_index, caption_index in pairs
    ]
    return CharacterAlignment(
        pairs=pairs,
        coverage=len(matched_primary) / len(primary),
        temporal_overlap=sum(overlap_values) / len(overlap_values),
        caption_text="".join(
            caption[index].text for index in range(min(caption_indices), max(caption_indices) + 1)
        ),
        word_caption_text=word_caption_text,
        word_temporal_overlap=word_temporal_overlap,
    )


def align_segment_to_captions(
    segment: TranscriptSegment,
    captions: Sequence[TranscriptSegment],
    settings: Settings,
    *,
    language: str = "ja",
) -> CharacterAlignment:
    primary_timeline = build_character_timeline([segment], language=language)
    caption_candidates = [
        caption
        for caption in captions
        if caption.start_ms < segment.end_ms + settings.reconciliation_alignment_overlap_ms
        and caption.end_ms > segment.start_ms - settings.reconciliation_alignment_overlap_ms
    ]
    caption_timeline = build_character_timeline(
        caption_candidates,
        language=language,
        deduplicate_rolling=True,
        min_anchor_chars=settings.reconciliation_min_anchor_chars,
    )
    return align_character_timelines(
        primary_timeline,
        caption_timeline,
        language=language,
        window_ms=settings.reconciliation_alignment_window_ms,
        overlap_ms=settings.reconciliation_alignment_overlap_ms,
        min_anchor_chars=settings.reconciliation_min_anchor_chars,
    )


def build_reconciliation_plan(
    primary_segments: Sequence[TranscriptSegment],
    caption_segments: Sequence[TranscriptSegment],
    settings: Settings,
    *,
    language: str = "ja",
) -> ReconciliationPlan:
    spans: list[SuspiciousSpan] = []
    compared_segments = 0
    primary_timeline = build_character_timeline(primary_segments, language=language)
    caption_timeline = build_character_timeline(
        caption_segments,
        language=language,
        deduplicate_rolling=True,
        min_anchor_chars=settings.reconciliation_min_anchor_chars,
    )
    pairs = _monotonic_pairs(
        primary_timeline,
        caption_timeline,
        window_ms=settings.reconciliation_alignment_window_ms,
        overlap_ms=settings.reconciliation_alignment_overlap_ms,
        min_anchor_chars=settings.reconciliation_min_anchor_chars,
    )
    pair_map = dict(pairs)
    matched_primary_indexes = {primary_index for primary_index, _ in pairs}
    for segment_position, segment in enumerate(primary_segments):
        words = segment.words or []
        if not words:
            continue
        segment_compared = False
        for word_index, word in enumerate(words):
            word_characters = [
                character
                for character in primary_timeline
                if character.segment_position == segment_position
                and character.word_index == word_index
            ]
            caption_range = _caption_range_for_word(
                word_characters,
                pair_map,
                caption_timeline,
            )
            caption_text = (
                "".join(character.text for character in caption_range)
                if caption_range
                else None
            )
            if caption_text:
                segment_compared = True
            similarity = (
                text_similarity(word.text, caption_text, language) if caption_text else None
            )
            low_confidence = (
                word.probability is not None
                and word.probability < settings.reconciliation_low_word_probability
            )
            disagreement = (
                bool(caption_text)
                and similarity is not None
                and similarity < settings.reconciliation_similarity_threshold
            )
            triggers = []
            if low_confidence:
                triggers.append(LOW_CONFIDENCE_TRIGGER)
            if disagreement:
                triggers.append(CAPTION_DISAGREEMENT_TRIGGER)
            if not triggers:
                continue
            priority_tier = 1 if low_confidence and disagreement else 2 if low_confidence else 3
            priority_score = (
                word.probability
                if low_confidence and word.probability is not None
                else similarity if similarity is not None else 1.0
            )
            spans.append(
                SuspiciousSpan(
                    id=len(spans),
                    segment_position=segment_position,
                    segment_index=segment.index,
                    word_start=word_index,
                    word_end=word_index + 1,
                    start_ms=word.start_ms,
                    end_ms=word.end_ms,
                    primary_text=word.text,
                    caption_text=caption_text,
                    similarity=similarity,
                    triggers=tuple(triggers),
                    low_confidence_words=(word.text,) if low_confidence else (),
                    priority_tier=priority_tier,
                    priority_score=priority_score,
                    alignment_coverage=_local_alignment_coverage(
                        word_characters,
                        pair_map,
                        primary_timeline,
                        min_anchor_chars=settings.reconciliation_min_anchor_chars,
                    ),
                    temporal_overlap=(
                        _primary_temporal_coverage(
                            word.start_ms,
                            word.end_ms,
                            min(character.source_start_ms for character in caption_range),
                            max(character.source_end_ms for character in caption_range),
                        )
                        if caption_range
                        else 0.0
                    ),
                    primary_probability=word.probability,
                )
            )
        if segment_compared:
            compared_segments += 1

    windows, skipped_span_ids = _build_windows(spans, settings, language=language)
    return ReconciliationPlan(
        compared_segments=compared_segments,
        alignment_coverage=(
            len(matched_primary_indexes) / len(primary_timeline) if primary_timeline else 0.0
        ),
        spans=tuple(spans),
        windows=tuple(windows),
        skipped_span_ids=tuple(sorted(skipped_span_ids)),
    )


def apply_reconciliation(
    primary_segments: Sequence[TranscriptSegment],
    plan: ReconciliationPlan,
    secondary_segments: Mapping[int, Sequence[TranscriptSegment]],
    settings: Settings,
    *,
    language: str = "ja",
) -> ReconciliationOutcome:
    replacements: dict[tuple[int, int, int], list[WordTimestamp]] = {}
    items = []
    corrected_span_ids = set()
    unresolved_count = 0
    skipped_count = 0
    processed_count = 0
    secondary_alignments = _build_secondary_window_alignments(
        primary_segments,
        plan.windows,
        secondary_segments,
        settings,
        language=language,
    )
    window_by_span_id = {
        span_id: window for window in plan.windows for span_id in window.span_ids
    }
    reconstructable_segments = {
        position
        for position, segment in enumerate(primary_segments)
        if segment.words and "".join(word.text for word in segment.words) == segment.text
    }
    surface_word_spans = {}
    for position, segment in enumerate(primary_segments):
        word_spans = _build_surface_word_spans(segment)
        if word_spans is not None:
            surface_word_spans[position] = word_spans
    surface_mapped_segments: set[int] = set()

    for span in plan.spans:
        selected_window = window_by_span_id.get(span.id)
        window_processed = (
            selected_window is not None and selected_window.index in secondary_segments
        )
        evidence = (
            _secondary_evidence(span, selected_window, secondary_alignments, settings)
            if selected_window is not None
            else None
        )
        decision, reason = _decide(span, evidence, settings, language=language)
        final_text = span.primary_text
        if span.id in plan.skipped_span_ids:
            decision = DECISION_SKIPPED
            reason = "budget_limit"
            skipped_count += 1
        else:
            if window_processed:
                processed_count += 1
            if (
                decision == DECISION_CORRECTED
                and span.segment_position not in reconstructable_segments
            ):
                if (
                    _base_language(language) == "ko"
                    and span.priority_tier == 1
                    and span.segment_position in surface_word_spans
                ):
                    surface_mapped_segments.add(span.segment_position)
                else:
                    decision = DECISION_UNRESOLVED
                    reason = "segment_words_not_reconstructable"
            if decision == DECISION_CORRECTED and evidence is not None:
                replacement_key = (span.segment_position, span.word_start, span.word_end)
                replacements[replacement_key] = evidence.words
                corrected_span_ids.add(span.id)
                final_text = evidence.text
            elif decision == DECISION_UNRESOLVED:
                unresolved_count += 1

        items.append(
            TranscriptReconciliationItem(
                segment_index=span.segment_index,
                start_ms=span.start_ms,
                end_ms=span.end_ms,
                primary_text=span.primary_text,
                caption_text=span.caption_text,
                secondary_text=evidence.text if evidence else None,
                final_text=final_text,
                decision=decision,
                triggers=list(span.triggers),
                word_start=span.word_start,
                word_end=span.word_end,
                priority_tier=span.priority_tier,
                alignment_coverage=(
                    min(span.alignment_coverage, evidence.alignment_coverage)
                    if evidence
                    else span.alignment_coverage
                ),
                temporal_overlap=(
                    min(span.temporal_overlap, evidence.temporal_overlap) if evidence else 0.0
                ),
                primary_probability=span.primary_probability,
                secondary_mean_probability=evidence.mean_probability if evidence else None,
                secondary_min_probability=evidence.min_probability if evidence else None,
                decision_reason=reason,
                corrected_words=(
                    span.word_end - span.word_start if span.id in corrected_span_ids else 0
                ),
            )
        )

    final_segments = _apply_word_replacements(
        primary_segments,
        replacements,
        surface_word_spans={
            position: surface_word_spans[position] for position in surface_mapped_segments
        },
    )
    corrected_segment_positions = {position for position, _, _ in replacements}
    corrected_words = sum(end - start for _, start, end in replacements)
    warnings = []
    reconciled_warning, unresolved_warning, limit_warning = _warning_codes(language)
    if replacements:
        warnings.append(reconciled_warning)
    if unresolved_count:
        warnings.append(unresolved_warning)
    if skipped_count:
        warnings.append(limit_warning)
    reconciliation = TranscriptReconciliation(
        alignment_version=ALIGNMENT_VERSION,
        secondary_model=settings.reconciliation_model,
        alignment_coverage=plan.alignment_coverage,
        compared_segments=plan.compared_segments,
        suspicious_segments=len(plan.spans),
        selected_spans=len(plan.spans) - len(plan.skipped_span_ids),
        processed_spans=processed_count,
        selected_windows=len(plan.windows),
        selected_duration_ms=sum(window.end_ms - window.start_ms for window in plan.windows),
        secondary_windows=len(secondary_alignments),
        secondary_duration_ms=sum(
            window.end_ms - window.start_ms
            for window in plan.windows
            if window.index in secondary_alignments
        ),
        corrected_segments=len(corrected_segment_positions),
        corrected_words=corrected_words,
        unresolved_segments=unresolved_count,
        skipped_segments=skipped_count,
        items=items,
    )
    return ReconciliationOutcome(
        segments=final_segments,
        reconciliation=reconciliation,
        warnings=warnings,
    )


def _monotonic_pairs(
    primary: Sequence[AlignmentCharacter],
    caption: Sequence[AlignmentCharacter],
    *,
    window_ms: int,
    overlap_ms: int,
    min_anchor_chars: int,
) -> tuple[tuple[int, int], ...]:
    if not primary or not caption:
        return ()
    blocks = _alignment_blocks(
        primary,
        caption,
        window_ms=window_ms,
        overlap_ms=overlap_ms,
        min_anchor_chars=min_anchor_chars,
    )
    selected_blocks = _best_monotonic_blocks(_merge_compatible_blocks(blocks))
    return tuple(
        (block.primary_start + offset, block.caption_start + offset)
        for block in selected_blocks
        for offset in range(block.size)
    )


def _alignment_blocks(
    primary: Sequence[AlignmentCharacter],
    caption: Sequence[AlignmentCharacter],
    *,
    window_ms: int,
    overlap_ms: int,
    min_anchor_chars: int,
) -> list[_AnchorBlock]:
    first_ms = min(primary[0].start_ms, caption[0].start_ms)
    last_ms = max(primary[-1].end_ms, caption[-1].end_ms)
    step_ms = window_ms - overlap_ms
    unique: dict[tuple[int, int, int], _AnchorBlock] = {}
    window_start_ms = first_ms
    while window_start_ms <= last_ms:
        window_end_ms = window_start_ms + window_ms
        primary_window = [
            character
            for character in primary
            if character.start_ms < window_end_ms and character.end_ms > window_start_ms
        ]
        caption_window = [
            character
            for character in caption
            if character.start_ms < window_end_ms and character.end_ms > window_start_ms
        ]
        if primary_window and caption_window:
            primary_text = "".join(character.text for character in primary_window)
            caption_text = "".join(character.text for character in caption_window)
            matcher = SequenceMatcher(
                None,
                primary_text,
                caption_text,
                autojunk=False,
            )
            for block in matcher.get_matching_blocks():
                if block.size < min_anchor_chars:
                    continue
                anchor_text = primary_text[block.a : block.a + block.size]
                occurrence_start = 0
                while True:
                    caption_offset = caption_text.find(anchor_text, occurrence_start)
                    if caption_offset < 0:
                        break
                    primary_start = primary_window[block.a].index
                    caption_start = caption_window[caption_offset].index
                    primary_mid = _range_midpoint(
                        primary_window[block.a].start_ms,
                        primary_window[block.a + block.size - 1].end_ms,
                    )
                    caption_mid = _range_midpoint(
                        caption_window[caption_offset].start_ms,
                        caption_window[caption_offset + block.size - 1].end_ms,
                    )
                    candidate = _AnchorBlock(
                        primary_start=primary_start,
                        caption_start=caption_start,
                        size=block.size,
                        time_distance_ms=abs(primary_mid - caption_mid),
                    )
                    key = (primary_start, caption_start, block.size)
                    current = unique.get(key)
                    if current is None or candidate.time_distance_ms < current.time_distance_ms:
                        unique[key] = candidate
                    occurrence_start = caption_offset + 1
        window_start_ms += step_ms
    return sorted(unique.values(), key=lambda block: (block.primary_start, block.caption_start))


def _best_monotonic_blocks(blocks: Sequence[_AnchorBlock]) -> list[_AnchorBlock]:
    if not blocks:
        return []
    scores = [float(block.size) - min(block.time_distance_ms, 60_000) / 600_000 for block in blocks]
    previous = [-1] * len(blocks)
    for current_index, current in enumerate(blocks):
        for candidate_index, candidate in enumerate(blocks[:current_index]):
            if (
                candidate.primary_start + candidate.size <= current.primary_start
                and candidate.caption_start + candidate.size <= current.caption_start
            ):
                candidate_score = scores[candidate_index] + current.size - min(
                    current.time_distance_ms, 60_000
                ) / 600_000
                if candidate_score > scores[current_index]:
                    scores[current_index] = candidate_score
                    previous[current_index] = candidate_index
    index = max(range(len(blocks)), key=lambda value: scores[value])
    selected = []
    while index >= 0:
        selected.append(blocks[index])
        index = previous[index]
    return list(reversed(selected))


def _merge_compatible_blocks(blocks: Sequence[_AnchorBlock]) -> list[_AnchorBlock]:
    merged = []
    by_diagonal: dict[int, list[_AnchorBlock]] = {}
    for block in blocks:
        by_diagonal.setdefault(block.primary_start - block.caption_start, []).append(block)
    for diagonal_blocks in by_diagonal.values():
        current = None
        for block in sorted(diagonal_blocks, key=lambda item: item.primary_start):
            if current is None:
                current = block
                continue
            current_end = current.primary_start + current.size
            block_end = block.primary_start + block.size
            if block.primary_start <= current_end:
                current = _AnchorBlock(
                    primary_start=current.primary_start,
                    caption_start=current.caption_start,
                    size=max(current_end, block_end) - current.primary_start,
                    time_distance_ms=min(current.time_distance_ms, block.time_distance_ms),
                )
                continue
            merged.append(current)
            current = block
        if current is not None:
            merged.append(current)
    return sorted(merged, key=lambda block: (block.primary_start, block.caption_start))


def _caption_range_for_word(
    word_characters: Sequence[AlignmentCharacter],
    pair_map: Mapping[int, int],
    caption: Sequence[AlignmentCharacter],
) -> list[AlignmentCharacter]:
    if not word_characters:
        return []
    primary_start = min(character.index for character in word_characters)
    primary_end = max(character.index for character in word_characters)
    previous_pairs = [
        (primary_index, caption_index)
        for primary_index, caption_index in pair_map.items()
        if primary_index < primary_start
    ]
    next_pairs = [
        (primary_index, caption_index)
        for primary_index, caption_index in pair_map.items()
        if primary_index > primary_end
    ]
    lower_bound = max(previous_pairs)[1] + 1 if previous_pairs else 0
    upper_bound = min(next_pairs)[1] if next_pairs else len(caption)
    if upper_bound <= lower_bound:
        return []

    primary_text = "".join(character.text for character in word_characters)
    bounded_text = "".join(character.text for character in caption[lower_bound:upper_bound])
    occurrences = [
        lower_bound + match.start()
        for match in re.finditer(re.escape(primary_text), bounded_text)
    ]
    if occurrences:
        primary_midpoint = _range_midpoint(
            min(character.start_ms for character in word_characters),
            max(character.end_ms for character in word_characters),
        )
        start_index = min(
            occurrences,
            key=lambda index: abs(
                _range_midpoint(
                    caption[index].start_ms,
                    caption[index + len(primary_text) - 1].end_ms,
                )
                - primary_midpoint
            ),
        )
        end_index = start_index + len(primary_text)
    elif previous_pairs and next_pairs:
        start_index = lower_bound
        end_index = upper_bound
    elif next_pairs:
        end_index = upper_bound
        start_index = max(lower_bound, end_index - len(word_characters))
    elif previous_pairs:
        start_index = lower_bound
        end_index = min(upper_bound, start_index + len(word_characters))
    else:
        overlapping = [
            character.index
            for character in caption
            if character.end_ms > min(item.start_ms for item in word_characters)
            and character.start_ms < max(item.end_ms for item in word_characters)
        ]
        if not overlapping:
            return []
        start_index = min(overlapping)
        end_index = max(overlapping) + 1
    if end_index <= start_index:
        return []
    max_length = max(4, len(word_characters) * 3)
    if end_index - start_index > max_length:
        return []
    return list(caption[start_index:end_index])


def _local_alignment_coverage(
    target_characters: Sequence[AlignmentCharacter],
    pair_map: Mapping[int, int],
    primary: Sequence[AlignmentCharacter],
    *,
    min_anchor_chars: int,
) -> float:
    if not target_characters or not primary:
        return 0.0
    target_start = min(character.index for character in target_characters)
    target_end = max(character.index for character in target_characters)
    scope_start = max(0, target_start - min_anchor_chars)
    scope_end = min(len(primary), target_end + min_anchor_chars + 1)
    scope = primary[scope_start:scope_end]
    target_indexes = {character.index for character in target_characters}
    target_scope = [character for character in scope if character.index in target_indexes]
    surrounding_scope = [character for character in scope if character.index not in target_indexes]
    coverage_values = []
    for evidence_scope in (target_scope, surrounding_scope):
        matched_count = sum(character.index in pair_map for character in evidence_scope)
        if matched_count >= min_anchor_chars:
            coverage_values.append(matched_count / len(evidence_scope))
    return max(coverage_values, default=0.0)


def _timeline_words(segment: TranscriptSegment) -> list[_TimelineWord]:
    if segment.words:
        return [
            _TimelineWord(word.text, word.start_ms, word.end_ms, index)
            for index, word in enumerate(segment.words)
        ]
    return [_TimelineWord(segment.text, segment.start_ms, segment.end_ms, None)]


def _rolling_prefix_length(previous: str, current: str, *, min_anchor_chars: int) -> int:
    maximum = min(len(previous), len(current))
    for length in range(maximum, min_anchor_chars - 1, -1):
        if previous[-length:] == current[:length]:
            return length
    return 0


def _distributed_time(
    start_ms: int,
    end_ms: int,
    character_offset: int,
    character_count: int,
) -> tuple[int, int]:
    duration = max(1, end_ms - start_ms)
    character_start = start_ms + round(duration * character_offset / character_count)
    character_end = start_ms + round(duration * (character_offset + 1) / character_count)
    return character_start, max(character_start + 1, character_end)


def _slice_character_timeline(
    timeline: Sequence[AlignmentCharacter],
    *,
    start_ms: int,
    end_ms: int,
) -> list[AlignmentCharacter]:
    return [
        AlignmentCharacter(
            index=index,
            text=character.text,
            start_ms=character.start_ms,
            end_ms=character.end_ms,
            segment_position=character.segment_position,
            word_index=character.word_index,
            source_start_ms=character.source_start_ms,
            source_end_ms=character.source_end_ms,
        )
        for index, character in enumerate(
            character
            for character in timeline
            if character.start_ms < end_ms and character.end_ms > start_ms
        )
    ]


def _build_windows(
    spans: Sequence[SuspiciousSpan],
    settings: Settings,
    *,
    language: str = "ja",
) -> tuple[list[ReconciliationWindow], set[int]]:
    raw_candidates: list[tuple[int, int, list[int]]] = []
    skipped_span_ids = set()
    for span in sorted(spans, key=lambda value: (value.start_ms, value.end_ms, value.id)):
        span_duration = span.end_ms - span.start_ms
        if span_duration <= 0 or span_duration > settings.reconciliation_window_max_ms:
            skipped_span_ids.add(span.id)
            continue
        padding_budget = settings.reconciliation_window_max_ms - span_duration
        left_padding = min(
            settings.reconciliation_window_padding_ms,
            span.start_ms,
            padding_budget // 2,
        )
        right_padding = min(
            settings.reconciliation_window_padding_ms,
            padding_budget - left_padding,
        )
        start_ms = span.start_ms - left_padding
        end_ms = span.end_ms + right_padding
        if raw_candidates:
            previous_start, previous_end, previous_ids = raw_candidates[-1]
            merged_end = max(previous_end, end_ms)
            if (
                start_ms <= previous_end + settings.reconciliation_window_merge_gap_ms
                and merged_end - previous_start <= settings.reconciliation_window_max_ms
            ):
                raw_candidates[-1] = (previous_start, merged_end, [*previous_ids, span.id])
                continue
        raw_candidates.append((start_ms, end_ms, [span.id]))

    span_by_id = {span.id: span for span in spans}
    candidates = []
    for start_ms, end_ms, span_ids in raw_candidates:
        best_span = min(
            (span_by_id[span_id] for span_id in span_ids),
            key=lambda span: (span.priority_tier, span.priority_score, span.start_ms),
        )
        candidates.append(
            WindowCandidate(
                start_ms=start_ms,
                end_ms=end_ms,
                span_ids=tuple(span_ids),
                priority_tier=best_span.priority_tier,
                priority_score=best_span.priority_score,
            )
        )
    selected, budget_skipped = select_windows(
        candidates,
        max_windows=(
            settings.reconciliation_korean_max_windows
            if _base_language(language) == "ko"
            else settings.reconciliation_max_windows
        ),
        max_total_ms=settings.reconciliation_max_total_ms,
    )
    skipped_span_ids.update(budget_skipped)
    windows = [
        ReconciliationWindow(
            index=index,
            start_ms=candidate.start_ms,
            end_ms=candidate.end_ms,
            span_ids=candidate.span_ids,
        )
        for index, candidate in enumerate(selected)
    ]
    return windows, skipped_span_ids


def _build_secondary_window_alignments(
    primary_segments: Sequence[TranscriptSegment],
    windows: Sequence[ReconciliationWindow],
    secondary_segments: Mapping[int, Sequence[TranscriptSegment]],
    settings: Settings,
    *,
    language: str = "ja",
) -> dict[int, _SecondaryWindowAlignment]:
    primary_timeline = build_character_timeline(primary_segments, language=language)
    alignments = {}
    for window in windows:
        segments = secondary_segments.get(window.index)
        if segments is None:
            continue
        primary_window = _slice_character_timeline(
            primary_timeline,
            start_ms=window.start_ms,
            end_ms=window.end_ms,
        )
        secondary_timeline = build_character_timeline(segments, language=language)
        pairs = _monotonic_pairs(
            primary_window,
            secondary_timeline,
            window_ms=settings.reconciliation_alignment_window_ms,
            overlap_ms=settings.reconciliation_alignment_overlap_ms,
            min_anchor_chars=settings.reconciliation_min_anchor_chars,
        )
        alignments[window.index] = _SecondaryWindowAlignment(
            primary=primary_window,
            secondary=secondary_timeline,
            pairs=pairs,
            segments=segments,
        )
    return alignments


def _secondary_evidence(
    span: SuspiciousSpan,
    window: ReconciliationWindow,
    alignments: Mapping[int, _SecondaryWindowAlignment],
    settings: Settings,
) -> _SecondaryEvidence | None:
    alignment = alignments.get(window.index)
    if alignment is None:
        return None
    target_characters = [
        character
        for character in alignment.primary
        if character.segment_position == span.segment_position
        and character.word_index is not None
        and span.word_start <= character.word_index < span.word_end
    ]
    pair_map = dict(alignment.pairs)
    secondary_range = _caption_range_for_word(
        target_characters,
        pair_map,
        alignment.secondary,
    )
    if not secondary_range:
        return None
    alignment_coverage = _local_alignment_coverage(
        target_characters,
        pair_map,
        alignment.primary,
        min_anchor_chars=settings.reconciliation_min_anchor_chars,
    )
    word_keys = []
    for character in secondary_range:
        key = (character.segment_position, character.word_index)
        if character.word_index is None or key in word_keys:
            continue
        word_keys.append(key)
    selected_indexes = {character.index for character in secondary_range}
    if not word_keys or any(
        any(character.index not in selected_indexes for character in alignment.secondary if (
            character.segment_position,
            character.word_index,
        ) == key)
        for key in word_keys
    ):
        return _SecondaryEvidence(
            text="",
            words=[],
            alignment_coverage=alignment_coverage,
            temporal_overlap=0.0,
            mean_probability=None,
            min_probability=None,
            issue="secondary_range_partial_word",
        )
    words = [
        alignment.segments[segment_position].words[word_index]
        for segment_position, word_index in word_keys
        if alignment.segments[segment_position].words is not None
    ]
    if len(words) != len(word_keys):
        return None
    evidence_start = min(word.start_ms for word in words)
    evidence_end = max(word.end_ms for word in words)
    probabilities = [word.probability for word in words if word.probability is not None]
    return _SecondaryEvidence(
        text="".join(word.text for word in words),
        words=[word.model_copy(deep=True) for word in words],
        alignment_coverage=alignment_coverage,
        temporal_overlap=_range_temporal_overlap(
            span.start_ms,
            span.end_ms,
            evidence_start,
            evidence_end,
        ),
        mean_probability=(sum(probabilities) / len(probabilities) if probabilities else None),
        min_probability=min(probabilities) if probabilities else None,
    )


def _decide(
    span: SuspiciousSpan,
    evidence: _SecondaryEvidence | None,
    settings: Settings,
    *,
    language: str = "ja",
) -> tuple[str, str]:
    if evidence is None:
        return DECISION_UNRESOLVED, "secondary_missing"
    if evidence.issue:
        return DECISION_UNRESOLVED, evidence.issue
    if (
        min(span.alignment_coverage, evidence.alignment_coverage)
        < settings.reconciliation_min_alignment_coverage
    ):
        return DECISION_UNRESOLVED, "alignment_coverage_low"
    primary = normalize_alignment_text(span.primary_text, language)
    caption = normalize_alignment_text(span.caption_text or "", language)
    secondary = normalize_alignment_text(evidence.text, language)
    if not primary or not caption or not secondary:
        return DECISION_UNRESOLVED, "evidence_empty"

    caption_secondary_consensus = caption == secondary
    temporal_tolerance = (
        settings.reconciliation_korean_temporal_overlap_tolerance
        if _base_language(language) == "ko"
        and span.priority_tier == 1
        and caption_secondary_consensus
        else 0.0
    )
    temporal_overlap = min(span.temporal_overlap, evidence.temporal_overlap)
    if (
        temporal_overlap + temporal_tolerance
        < settings.reconciliation_min_temporal_overlap
    ):
        return DECISION_UNRESOLVED, "temporal_overlap_low"
    if (
        evidence.mean_probability is None
        or evidence.mean_probability < settings.reconciliation_secondary_mean_probability
    ):
        return DECISION_UNRESOLVED, "secondary_mean_probability_low"
    if (
        evidence.min_probability is None
        or evidence.min_probability < settings.reconciliation_secondary_min_probability
    ):
        return DECISION_UNRESOLVED, "secondary_min_probability_low"

    if primary in (secondary, caption):
        return DECISION_KEPT_PRIMARY, "primary_consensus"
    if caption_secondary_consensus:
        return DECISION_CORRECTED, "caption_secondary_consensus"
    return DECISION_UNRESOLVED, "consensus_missing"


def _apply_word_replacements(
    primary_segments: Sequence[TranscriptSegment],
    replacements: Mapping[tuple[int, int, int], Sequence[WordTimestamp]],
    *,
    surface_word_spans: Mapping[int, Sequence[tuple[int, int]]] | None = None,
) -> list[TranscriptSegment]:
    results = []
    by_segment: dict[int, dict[tuple[int, int], Sequence[WordTimestamp]]] = {}
    for (segment_position, word_start, word_end), words in replacements.items():
        by_segment.setdefault(segment_position, {})[(word_start, word_end)] = words

    for segment_position, segment in enumerate(primary_segments):
        segment_replacements = by_segment.get(segment_position)
        if not segment_replacements or not segment.words:
            results.append(segment.model_copy(deep=True))
            continue
        rebuilt_words = []
        word_index = 0
        ordered = sorted(segment_replacements.items())
        replacement_by_start = {start: (end, words) for (start, end), words in ordered}
        while word_index < len(segment.words):
            replacement = replacement_by_start.get(word_index)
            if replacement is None:
                rebuilt_words.append(segment.words[word_index].model_copy(deep=True))
                word_index += 1
                continue
            end_index, replacement_words = replacement
            rebuilt_words.extend(word.model_copy(deep=True) for word in replacement_words)
            word_index = end_index
        rebuilt_text = "".join(word.text for word in rebuilt_words)
        segment_surface_spans = (surface_word_spans or {}).get(segment_position)
        if segment_surface_spans is not None:
            rebuilt_text = _apply_surface_text_replacements(
                segment.text,
                segment_surface_spans,
                segment_replacements,
            )
        results.append(
            segment.model_copy(
                update={
                    "text": rebuilt_text,
                    "words": rebuilt_words,
                }
            )
        )
    return results


def _build_surface_word_spans(
    segment: TranscriptSegment,
) -> tuple[tuple[int, int], ...] | None:
    if not segment.words:
        return None
    surface_positions = [
        index for index, character in enumerate(segment.text) if not character.isspace()
    ]
    compact_words = [
        "".join(character for character in word.text if not character.isspace())
        for word in segment.words
    ]
    if any(not word for word in compact_words):
        return None
    if "".join(compact_words) != "".join(
        character for character in segment.text if not character.isspace()
    ):
        return None

    spans = []
    compact_start = 0
    for word_position, compact_word in enumerate(compact_words):
        compact_end = compact_start + len(compact_word)
        surface_start = (
            0 if compact_start == 0 else surface_positions[compact_start - 1] + 1
        )
        surface_end = surface_positions[compact_end - 1] + 1
        if word_position == len(compact_words) - 1:
            surface_end = len(segment.text)
        spans.append((surface_start, surface_end))
        compact_start = compact_end
    return tuple(spans)


def _apply_surface_text_replacements(
    surface_text: str,
    word_spans: Sequence[tuple[int, int]],
    replacements: Mapping[tuple[int, int], Sequence[WordTimestamp]],
) -> str:
    result = surface_text
    for (word_start, word_end), replacement_words in sorted(
        replacements.items(),
        reverse=True,
    ):
        surface_start = word_spans[word_start][0]
        surface_end = word_spans[word_end - 1][1]
        original = result[surface_start:surface_end]
        leading_whitespace = original[: len(original) - len(original.lstrip())]
        trailing_whitespace = original[len(original.rstrip()) :]
        replacement = "".join(word.text for word in replacement_words).strip()
        result = (
            result[:surface_start]
            + leading_whitespace
            + replacement
            + trailing_whitespace
            + result[surface_end:]
        )
    return result


def _range_temporal_overlap(
    first_start: int,
    first_end: int,
    second_start: int,
    second_end: int,
) -> float:
    overlap = max(0, min(first_end, second_end) - max(first_start, second_start))
    longest = max(first_end - first_start, second_end - second_start)
    return overlap / longest if longest > 0 else 0.0


def _primary_temporal_coverage(
    primary_start: int,
    primary_end: int,
    evidence_start: int,
    evidence_end: int,
) -> float:
    overlap = max(0, min(primary_end, evidence_end) - max(primary_start, evidence_start))
    primary_duration = primary_end - primary_start
    return overlap / primary_duration if primary_duration > 0 else 0.0


def _range_midpoint(start_ms: int, end_ms: int) -> float:
    return start_ms + (end_ms - start_ms) / 2


def _base_language(language: str) -> str:
    return language.strip().lower().replace("_", "-").split("-", 1)[0]


def _warning_codes(language: str) -> tuple[str, str, str]:
    return _WARNING_CODES.get(_base_language(language), _WARNING_CODES["ja"])


def _contains_japanese_text(value: str) -> bool:
    return any(
        "\u3040" <= character <= "\u30ff"
        or "\u3400" <= character <= "\u4dbf"
        or "\u4e00" <= character <= "\u9fff"
        for character in value
    )
