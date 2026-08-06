import type {
  TranscriptArtifact,
  TranscriptReconciliationItem,
  TranscriptSource,
} from "../types";

export interface ReconciliationReasonCount {
  reason: string | null;
  count: number;
}

export interface TranscriptQualitySummary {
  schemaVersion: number;
  segmentCount: number;
  wordCount: number;
  segmentsWithWords: number;
  corrections: TranscriptReconciliationItem[];
  unresolvedReasons: ReconciliationReasonCount[];
}

const transcriptSources: TranscriptSource[] = ["manual_caption", "automatic_caption", "whisper"];

export function parseTranscriptArtifact(value: unknown): TranscriptArtifact | null {
  if (!isRecord(value)) return null;
  if (typeof value.schema_version !== "number" || typeof value.job_id !== "string") return null;
  if (typeof value.source !== "string" || !transcriptSources.includes(value.source as TranscriptSource)) return null;
  if (typeof value.language !== "string" || !Array.isArray(value.segments) || !Array.isArray(value.warnings)) return null;
  if (value.reconciliation != null && !isRecord(value.reconciliation)) return null;
  return value as unknown as TranscriptArtifact;
}

export function summarizeTranscriptArtifact(artifact: TranscriptArtifact): TranscriptQualitySummary {
  let wordCount = 0;
  let segmentsWithWords = 0;
  for (const segment of artifact.segments) {
    if (!isRecord(segment) || !Array.isArray(segment.words) || segment.words.length === 0) continue;
    segmentsWithWords += 1;
    wordCount += segment.words.length;
  }

  const items = Array.isArray(artifact.reconciliation?.items) ? artifact.reconciliation.items : [];
  const corrections = items.filter((item) => item.decision === "corrected" || item.corrected_words > 0);
  const reasonCounts = new Map<string | null, number>();
  for (const item of items) {
    if (item.decision !== "unresolved") continue;
    reasonCounts.set(item.decision_reason, (reasonCounts.get(item.decision_reason) ?? 0) + 1);
  }

  return {
    schemaVersion: artifact.schema_version,
    segmentCount: artifact.segments.length,
    wordCount,
    segmentsWithWords,
    corrections,
    unresolvedReasons: [...reasonCounts.entries()]
      .map(([reason, count]) => ({ reason, count }))
      .sort((first, second) => second.count - first.count),
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
