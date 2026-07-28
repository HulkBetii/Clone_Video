import { ArrowUpRight, Captions, Check, Clock3, Sparkles } from "lucide-react";
import { Link } from "react-router-dom";

import { formatDate } from "../lib/content";
import type { Workspace } from "../types";
import { ProgressBar, StatusPill } from "./ui";
import styles from "./WorkspaceCard.module.css";

export function WorkspaceCard({ workspace, selected = false, selectable = false, onSelect }: { workspace: Workspace; selected?: boolean; selectable?: boolean; onSelect?: (selected: boolean) => void }) {
  const transcript = workspace.transcript;
  const rewrite = workspace.rewrite;
  const video = transcript?.video ?? rewrite?.video;
  const id = workspace.transcript_job_id || transcript?.id || workspace.id || "";
  const title = video?.title || "Đang lấy thông tin video...";
  const stage = rewrite?.stage || transcript?.stage;

  return (
    <div className={styles.row}>
      <label className={styles.selector} title={selectable ? "Chọn workspace" : "Workspace đang xử lý"}>
        <input type="checkbox" aria-label={`Chọn workspace ${title}`} checked={selected} disabled={!selectable} onChange={(event) => onSelect?.(event.target.checked)} />
        <span aria-hidden="true">{selected && <Check size={13} />}</span>
      </label>
      <Link to={`/workspaces/${id}`} className={styles.card}>
        <div className={styles.index}>{video?.id ? video.id.slice(0, 2).toUpperCase() : "YT"}</div>
        <div className={styles.content}>
          <div className={styles.topline}><StatusPill status={workspace.status} /><span><Clock3 size={12} />{formatDate(workspace.updated_at || transcript?.updated_at)}</span></div>
          <h2>{title}</h2>
          <div className={styles.meta}>
            <span><Captions size={14} />{transcript?.language || "Đang xác định"}</span>
            <span><Sparkles size={14} />{workspace.auto_rewrite ? "Có GPT Rewrite" : "Chỉ transcript"}</span>
          </div>
          {(workspace.status === "running" || workspace.status === "queued") && <ProgressBar value={workspace.progress} label={stage ? stage.replaceAll("_", " ") : "Đang chuẩn bị"} />}
        </div>
        <ArrowUpRight className={styles.arrow} size={20} />
      </Link>
    </div>
  );
}
