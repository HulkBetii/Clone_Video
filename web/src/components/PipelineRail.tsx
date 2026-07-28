import { Check, Circle, Sparkles, Subtitles } from "lucide-react";

import type { Workspace } from "../types";
import styles from "./PipelineRail.module.css";

const labels: Record<string, string> = {
  inspecting: "Kiểm tra video",
  fetching_caption: "Lấy caption",
  downloading_audio: "Tải âm thanh",
  loading_model: "Nạp Whisper",
  transcribing: "Nhận diện lời nói",
  rendering: "Xuất artifact",
  preparing_source: "Chuẩn bị nguồn",
  analyzing_style: "Phân tích phong cách",
  planning: "Lập dàn ý",
  uploading: "Tải file lên GPT",
  rewriting: "Viết lại nội dung",
  editing: "Biên tập",
  validating: "Kiểm định độc lập",
};

export function stageLabel(stage?: string | null) {
  return stage ? labels[stage] ?? stage.replaceAll("_", " ") : "Đang chờ";
}

export function PipelineRail({ workspace }: { workspace: Workspace }) {
  const transcriptComplete = workspace.transcript?.status === "completed";
  const rewriteComplete = workspace.rewrite?.status === "completed";
  const transcriptActive = workspace.phase === "transcript" && !transcriptComplete;
  const rewriteActive = workspace.phase === "rewrite" && !rewriteComplete;

  return (
    <div className={styles.rail}>
      <div className={`${styles.node} ${transcriptComplete ? styles.complete : transcriptActive ? styles.active : ""}`}>
        <span className={styles.icon}>{transcriptComplete ? <Check size={16} /> : <Subtitles size={16} />}</span>
        <div><small>Giai đoạn 01</small><strong>Transcript</strong><span>{stageLabel(workspace.transcript?.stage)}</span></div>
      </div>
      <div className={`${styles.line} ${transcriptComplete ? styles.complete : ""}`} />
      <div className={`${styles.node} ${rewriteComplete ? styles.complete : rewriteActive ? styles.active : ""} ${!workspace.auto_rewrite ? styles.disabled : ""}`}>
        <span className={styles.icon}>{rewriteComplete ? <Check size={16} /> : workspace.auto_rewrite ? <Sparkles size={16} /> : <Circle size={13} />}</span>
        <div><small>Giai đoạn 02</small><strong>GPT Rewrite</strong><span>{!workspace.auto_rewrite ? "Không yêu cầu" : stageLabel(workspace.rewrite?.stage)}</span></div>
      </div>
      <div className={`${styles.line} ${rewriteComplete ? styles.complete : ""}`} />
      <div className={`${styles.node} ${workspace.status === "completed" ? styles.complete : ""}`}>
        <span className={styles.icon}>{workspace.status === "completed" ? <Check size={16} /> : <Circle size={13} />}</span>
        <div><small>Giai đoạn 03</small><strong>Sẵn sàng</strong><span>Kiểm tra và tải xuống</span></div>
      </div>
    </div>
  );
}
