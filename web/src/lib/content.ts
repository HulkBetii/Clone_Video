import type { ValidationSummary } from "../types";

export interface ParsedTextArtifact {
  title: string;
  body: string;
}

export function parseTextArtifact(text: string): ParsedTextArtifact {
  const normalized = text.replace(/^\uFEFF/, "").replace(/\r\n?/g, "\n").trim();
  const lines = normalized.split("\n");
  const titleLine = lines.findIndex((line) => /^\s*title\s*:/i.test(line));
  if (titleLine < 0) return { title: "", body: normalized };
  const title = lines[titleLine].replace(/^\s*title\s*:\s*/i, "").trim();
  const body = [...lines.slice(0, titleLine), ...lines.slice(titleLine + 1)].join("\n").replace(/^\n+|\n+$/g, "");
  return { title, body };
}

export function formatDuration(seconds?: number | null) {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remaining = Math.floor(seconds % 60);
  return hours ? `${hours} giờ ${String(minutes).padStart(2, "0")} phút` : `${minutes}:${String(remaining).padStart(2, "0")}`;
}

export function formatDate(value?: string) {
  if (!value) return "Chưa ghi nhận";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : new Intl.DateTimeFormat("vi-VN", { dateStyle: "medium", timeStyle: "short" }).format(date);
}

export function ratio(source?: number | null, output?: number | null) {
  if (!source || output == null) return null;
  return output / source;
}

export function validationLabel(validation?: ValidationSummary | null) {
  if (!validation) return "Chưa đánh giá";
  if (validation.passed === true) return "Đạt kiểm định";
  if (validation.passed === false) return "Cần xem lại";
  return "Đang kiểm định";
}
