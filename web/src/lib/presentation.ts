import type { TranscriptSource } from "../types";

export type NoticeTone = "info" | "success" | "warning";

interface NoticeCopy {
  title: string;
  description: string;
  tone: NoticeTone;
}

const sourceCopy: Record<TranscriptSource, { label: string; description: string }> = {
  manual_caption: {
    label: "Caption thủ công",
    description: "Subtitle do chủ video hoặc biên tập viên cung cấp.",
  },
  automatic_caption: {
    label: "Auto-caption YouTube",
    description: "Caption tự động của YouTube được dùng trực tiếp.",
  },
  whisper: {
    label: "Whisper từ audio",
    description: "Transcript được nhận diện trực tiếp từ âm thanh video.",
  },
};

const warningCopy: Record<string, NoticeCopy> = {
  JAPANESE_AUTO_CAPTION_REPLACED_BY_WHISPER: {
    title: "Đã dùng audio gốc cho tiếng Nhật",
    description: "Auto-caption YouTube được thay bằng Whisper để transcript bám sát lời nói hơn.",
    tone: "info",
  },
  KOREAN_AUTO_CAPTION_REPLACED_BY_WHISPER: {
    title: "Đã dùng audio gốc cho tiếng Hàn",
    description: "Auto-caption YouTube được thay bằng Whisper để transcript bám sát lời nói hơn.",
    tone: "info",
  },
  JAPANESE_TRANSCRIPT_RECONCILED: {
    title: "Đã đối chiếu transcript tiếng Nhật",
    description: "Whisper đã được kiểm tra với caption gốc và model phụ theo chính sách đồng thuận bảo thủ.",
    tone: "success",
  },
  KOREAN_TRANSCRIPT_RECONCILED: {
    title: "Đã đối chiếu transcript tiếng Hàn",
    description: "Whisper đã được kiểm tra với caption gốc và model phụ theo chính sách đồng thuận bảo thủ.",
    tone: "success",
  },
  JAPANESE_RECONCILIATION_UNRESOLVED: {
    title: "Còn đoạn tiếng Nhật chưa đủ bằng chứng",
    description: "Các đoạn không đạt đủ đồng thuận được giữ nguyên từ Whisper để tránh sửa sai.",
    tone: "warning",
  },
  KOREAN_RECONCILIATION_UNRESOLVED: {
    title: "Còn đoạn tiếng Hàn chưa đủ bằng chứng",
    description: "Các đoạn không đạt đủ đồng thuận được giữ nguyên từ Whisper để tránh sửa sai.",
    tone: "warning",
  },
  JAPANESE_RECONCILIATION_LIMIT_REACHED: {
    title: "Đã đạt giới hạn đối chiếu tiếng Nhật",
    description: "Một số span ít ưu tiên chưa được chạy model phụ vì đã hết budget xử lý.",
    tone: "warning",
  },
  KOREAN_RECONCILIATION_LIMIT_REACHED: {
    title: "Đã đạt giới hạn đối chiếu tiếng Hàn",
    description: "Một số span ít ưu tiên chưa được chạy model phụ vì đã hết budget xử lý.",
    tone: "warning",
  },
  JAPANESE_RECONCILIATION_UNAVAILABLE: {
    title: "Không chạy được lớp đối chiếu tiếng Nhật",
    description: "Transcript Whisper vẫn được xuất bản, nhưng caption hoặc model kiểm tra phụ không khả dụng.",
    tone: "warning",
  },
  KOREAN_RECONCILIATION_UNAVAILABLE: {
    title: "Không chạy được lớp đối chiếu tiếng Hàn",
    description: "Transcript Whisper vẫn được xuất bản, nhưng caption hoặc model kiểm tra phụ không khả dụng.",
    tone: "warning",
  },
  LOW_LANGUAGE_CONFIDENCE: {
    title: "Độ tin cậy ngôn ngữ thấp",
    description: "Whisper chưa đủ chắc chắn về ngôn ngữ được nhận diện; nên kiểm tra thủ công transcript.",
    tone: "warning",
  },
  GPU_RUNTIME_UNAVAILABLE_CPU_FALLBACK: {
    title: "Đã chuyển sang CPU",
    description: "CUDA không khả dụng khi nạp model; pipeline tiếp tục bằng CPU nên sẽ chậm hơn.",
    tone: "warning",
  },
  GPU_TRANSCRIPTION_FAILED_CPU_FALLBACK: {
    title: "GPU lỗi khi nhận diện",
    description: "Lần chạy CUDA thất bại; pipeline đã tự tiếp tục bằng CPU.",
    tone: "warning",
  },
};

const errorMessages: Record<string, string> = {
  INVALID_URL: "Link YouTube không hợp lệ hoặc không thuộc domain được hỗ trợ.",
  PLAYLIST_NOT_SUPPORTED: "Hệ thống chỉ xử lý một video, không xử lý playlist.",
  VIDEO_PRIVATE: "Video đang ở chế độ riêng tư và không thể truy cập.",
  VIDEO_MEMBERS_ONLY: "Video chỉ dành cho thành viên nên không thể xử lý.",
  LOGIN_REQUIRED: "YouTube yêu cầu đăng nhập để truy cập video này.",
  AGE_RESTRICTED: "Video bị giới hạn độ tuổi và backend không bypass quyền truy cập.",
  GEO_RESTRICTED: "Video bị giới hạn theo khu vực hiện tại.",
  VIDEO_DELETED: "Video đã bị xóa hoặc không còn khả dụng.",
  LIVE_NOT_FINISHED: "Livestream đang diễn ra hoặc chưa kết thúc.",
  VIDEO_TOO_LONG: "Video dài hơn giới hạn xử lý hiện tại.",
  LANGUAGE_NOT_AVAILABLE: "Video không có transcript đúng ngôn ngữ đã yêu cầu.",
  LANGUAGE_AMBIGUOUS: "Có nhiều ngôn ngữ khả dụng nhưng không xác định được track gốc.",
  NO_SPEECH_DETECTED: "Không phát hiện lời nói hợp lệ trong audio.",
  MODEL_LOAD_FAILED: "Không thể nạp model Whisper trên máy.",
  TRANSCRIPTION_FAILED: "Whisper không thể hoàn tất nhận diện lời nói.",
  GPT_PROFILE_MISSING: "Không tìm thấy profile ChatGPT đã cấu hình.",
  GPT_PROFILE_LOCKED: "Profile ChatGPT đang được một tiến trình khác sử dụng.",
  GPT_LOGIN_REQUIRED: "Cần đăng nhập ChatGPT trong cửa sổ Chromium được mở riêng.",
  GPT_UPLOAD_FAILED: "ChatGPT không nhận được file nguồn.",
  GPT_RESPONSE_TIMEOUT: "ChatGPT không trả lời trong thời gian cho phép.",
  GPT_OUTPUT_INVALID: "Nội dung ChatGPT trả về không đúng cấu trúc yêu cầu.",
  OUTPUT_TOO_SHORT: "Bản viết lại ngắn hơn giới hạn cho phép.",
  OUTPUT_TOO_LONG: "Bản viết lại dài hơn giới hạn cho phép.",
  STYLE_VALIDATION_FAILED: "Bản viết lại chưa đạt kiểm định phong cách và độ phủ nội dung.",
  SOURCE_NOT_COMPLETED: "Transcript nguồn chưa hoàn tất.",
  SOURCE_EMPTY: "Transcript nguồn không có nội dung sử dụng được.",
};

export function transcriptSourceCopy(source: TranscriptSource | null) {
  return source ? sourceCopy[source] : { label: "Đang xác định", description: "Nguồn transcript chưa được chốt." };
}

export function transcriptWarningCopy(code: string): NoticeCopy {
  return warningCopy[code] ?? {
    title: "Backend ghi nhận cảnh báo xử lý",
    description: "Cảnh báo này chưa có bản dịch giao diện; mã kỹ thuật được giữ để tra cứu.",
    tone: "warning",
  };
}

export function hasAttentionWarning(warnings: string[]) {
  return warnings.some((warning) =>
    warning.endsWith("_RECONCILIATION_UNAVAILABLE")
    || warning.endsWith("_RECONCILIATION_LIMIT_REACHED")
    || warning === "LOW_LANGUAGE_CONFIDENCE"
    || warning.includes("CPU_FALLBACK"),
  );
}

export function localizedErrorMessage(code: string | undefined, fallback: string) {
  return code ? errorMessages[code] ?? fallback : fallback;
}

export function reconciliationReasonLabel(reason: string | null) {
  const labels: Record<string, string> = {
    temporal_overlap_low: "Độ trùng timestamp thấp",
    alignment_coverage_low: "Độ phủ alignment thấp",
    secondary_mean_probability_low: "Confidence trung bình của model phụ thấp",
    secondary_min_probability_low: "Có word confidence của model phụ quá thấp",
    segment_words_not_reconstructable: "Không ánh xạ an toàn vào câu gốc",
    secondary_range_partial_word: "Model phụ chỉ khớp một phần word",
    secondary_missing: "Model phụ không trả đủ nội dung",
    consensus_missing: "Caption và model phụ không đồng thuận",
  };
  return reason ? labels[reason] ?? reason.replaceAll("_", " ") : "Không xác định";
}
