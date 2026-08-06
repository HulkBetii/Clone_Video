import { describe, expect, it } from "vitest";

import { koreanTranscriptArtifact } from "../test/fixtures";
import { transcriptWarningCopy } from "./presentation";
import { parseTranscriptArtifact, summarizeTranscriptArtifact } from "./transcriptQuality";

describe("transcript quality audit", () => {
  it("parses schema v3 and summarizes only published corrections", () => {
    const artifact = parseTranscriptArtifact(koreanTranscriptArtifact);
    expect(artifact).not.toBeNull();

    const summary = summarizeTranscriptArtifact(artifact!);
    expect(summary.corrections.map((item) => [item.primary_text, item.final_text])).toEqual([
      ["명은", "병은"],
      ["글을", "그를"],
    ]);
    expect(summary.unresolvedReasons).toEqual([
      { reason: "segment_words_not_reconstructable", count: 1 },
      { reason: "secondary_range_partial_word", count: 1 },
    ]);
    expect(summary.wordCount).toBe(2);
  });

  it("rejects malformed artifacts without throwing", () => {
    expect(parseTranscriptArtifact(null)).toBeNull();
    expect(parseTranscriptArtifact({ schema_version: 3, source: "whisper" })).toBeNull();
    expect(parseTranscriptArtifact({ ...koreanTranscriptArtifact, reconciliation: "invalid" })).toBeNull();
  });

  it("maps backend warning codes to friendly copy and preserves unknown warnings", () => {
    expect(transcriptWarningCopy("KOREAN_RECONCILIATION_UNRESOLVED").title).toBe("Còn đoạn tiếng Hàn chưa đủ bằng chứng");
    expect(transcriptWarningCopy("FUTURE_WARNING").title).toBe("Backend ghi nhận cảnh báo xử lý");
  });
});
