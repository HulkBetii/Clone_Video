import { AlertTriangle, Check, CircleDashed, Clock3, XCircle } from "lucide-react";
import type { ReactNode } from "react";

import type { WorkspaceStatus } from "../types";
import styles from "./ui.module.css";

const statusCopy: Record<WorkspaceStatus, string> = {
  queued: "Đang chờ",
  running: "Đang xử lý",
  waiting_for_user: "Cần thao tác",
  completed: "Hoàn tất",
  failed: "Thất bại",
};

export function StatusPill({ status }: { status: WorkspaceStatus }) {
  const Icon = status === "completed" ? Check : status === "failed" ? XCircle : status === "waiting_for_user" ? AlertTriangle : status === "queued" ? Clock3 : CircleDashed;
  return <span className={`${styles.status} ${styles[status]}`}><Icon size={13} />{statusCopy[status]}</span>;
}

export function ProgressBar({ value, label }: { value: number; label?: string }) {
  const safeValue = Math.min(100, Math.max(0, Math.round(value)));
  return <div className={styles.progressWrap}>{label && <div className={styles.progressLabel}><span>{label}</span><strong>{safeValue}%</strong></div>}<div className={styles.progressTrack}><span style={{ width: `${safeValue}%` }} /></div></div>;
}

export function PageHeading({ eyebrow, title, description, action }: { eyebrow: string; title: string; description?: string; action?: ReactNode }) {
  return <header className={styles.heading}><div><span className={styles.eyebrow}>{eyebrow}</span><h1>{title}</h1>{description && <p>{description}</p>}</div>{action}</header>;
}

export function EmptyState({ icon, title, children }: { icon: ReactNode; title: string; children: ReactNode }) {
  return <div className={styles.empty}><div className={styles.emptyIcon}>{icon}</div><h2>{title}</h2><p>{children}</p></div>;
}

export function ErrorNotice({ title = "Có lỗi xảy ra", message, code }: { title?: string; message: string; code?: string }) {
  return <div className={styles.error} role="alert"><AlertTriangle size={20} /><div><strong>{title}</strong><p>{message}</p>{code && <code>{code}</code>}</div></div>;
}

export function Skeleton({ height = 120 }: { height?: number }) {
  return <div className={styles.skeleton} style={{ height }} aria-label="Đang tải" />;
}
