import { parseTextArtifact, ratio } from "./content";

describe("parseTextArtifact", () => {
  it("separates the metadata title from the TTS body", () => {
    expect(parseTextArtifact("\uFEFFTitle: Tiêu đề SEO\r\n\r\nĐoạn một.\r\nĐoạn hai.")).toEqual({
      title: "Tiêu đề SEO",
      body: "Đoạn một.\nĐoạn hai.",
    });
  });

  it("keeps plain transcript text without a title header", () => {
    expect(parseTextArtifact("Nội dung thuần.")).toEqual({ title: "", body: "Nội dung thuần." });
  });
});

describe("ratio", () => {
  it("returns null for an empty source", () => expect(ratio(0, 10)).toBeNull());
});
