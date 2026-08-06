import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Bot, CheckCircle2, CircleOff, Cpu, Database, MonitorUp, RefreshCw, ServerCog, Square, TerminalSquare, TriangleAlert } from "lucide-react";

import { ErrorNotice, PageHeading, Skeleton } from "../components/ui";
import { useGptRuntime, useHealth } from "../hooks/queries";
import { ApiError, checkGptRuntime, closeGptRuntime, openGptRuntime } from "../lib/api";
import { localizedErrorMessage, transcriptWarningCopy } from "../lib/presentation";
import type { GptRuntimeStatus } from "../types";
import styles from "./SystemPage.module.css";

const runtimeLabels: Record<GptRuntimeStatus, string> = {
  not_checked: "Chưa kiểm tra",
  ready: "Sẵn sàng",
  login_required: "Cần đăng nhập",
  profile_locked: "Profile đang bị khóa",
  busy: "Đang chạy job",
  unavailable: "Không khả dụng",
  error: "Có lỗi",
};

export function SystemPage() {
  const health = useHealth();
  const runtime = useGptRuntime();
  const queryClient = useQueryClient();
  const refreshRuntime = () => queryClient.invalidateQueries({ queryKey: ["gpt-runtime"] });
  const openMutation = useMutation({ mutationFn: () => openGptRuntime(), onSuccess: refreshRuntime });
  const checkMutation = useMutation({ mutationFn: checkGptRuntime, onSuccess: refreshRuntime });
  const closeMutation = useMutation({ mutationFn: closeGptRuntime, onSuccess: refreshRuntime });

  const checks = health.data?.checks;
  const whisper = checks?.whisper;
  const gptHealth = checks?.gpt_rewrite;
  const runtimeStatus = runtime.data?.status ?? "not_checked";
  const busy = runtimeStatus === "busy";
  const whisperLoaded = whisper?.loaded === true;
  const cudaReady = whisper?.cuda_runtime_available === true;
  const whisperValue = whisperLoaded
    ? whisper.device.toUpperCase()
    : cudaReady
      ? "CUDA sẵn sàng"
      : "CPU fallback";
  const whisperDetail = whisperLoaded
    ? `Model ${whisper?.model ?? "turbo"} · ${whisper?.compute_type.toUpperCase()}`
    : `Model ${whisper?.model ?? "turbo"} · chưa nạp, sẽ tải khi cần · CUDA ${mark(whisper?.cuda_runtime_available)}`;

  return (
    <div className="page">
      <PageHeading eyebrow="Operations / 03" title="Tình trạng hệ thống" description="Những thành phần chạy hoàn toàn trên máy của bạn. Trạng thái degraded chỉ báo một phần chưa tối ưu, không nhất thiết chặn toàn bộ pipeline." action={health.data && <span className={`${styles.overall} ${health.data.status === "ok" ? styles.ok : styles.degraded}`}>{health.data.status === "ok" ? <CheckCircle2 size={15} /> : <TriangleAlert size={15} />}{health.data.status}</span>} />
      {health.isLoading ? <Skeleton height={270} /> : health.isError || !checks ? <ErrorNotice message={health.error?.message ?? "Không đọc được health response."} /> : <div className={styles.grid}>
        <SystemCard icon={<Database />} label="SQLite" value={checks.sqlite ? "Kết nối tốt" : "Không phản hồi"} healthy={checks.sqlite} detail="Kho trạng thái chính của job và checkpoint." />
        <SystemCard icon={<TerminalSquare />} label="yt-dlp" value={checks.yt_dlp || "Không rõ"} healthy={Boolean(checks.yt_dlp)} detail="Bộ đọc metadata, caption và audio YouTube." />
        <SystemCard icon={<ServerCog />} label="FFmpeg" value={checks.ffmpeg && checks.ffprobe ? "Đã tìm thấy" : "Thiếu runtime"} healthy={checks.ffmpeg && checks.ffprobe} detail={`ffmpeg ${mark(checks.ffmpeg)} · ffprobe ${mark(checks.ffprobe)}`} />
        <SystemCard icon={<Cpu />} label="Whisper" value={whisperValue} healthy={cudaReady || whisper?.device === "cpu"} detail={whisperDetail} />
      </div>}

      {whisper && (whisper.runtime_error || whisper.warnings.length > 0) && <section className={styles.whisperNotice}><TriangleAlert size={18} /><div><strong>Chi tiết Whisper runtime</strong>{whisper.runtime_error && <p>{whisper.runtime_error}</p>}{whisper.warnings.map((warning) => <p key={warning}>{transcriptWarningCopy(warning).title} <code>{warning}</code></p>)}</div></section>}

      <section className={`card ${styles.gptPanel}`}>
        <div className={styles.gptIcon}><Bot size={27} /></div>
        <div className={styles.gptCopy}>
          <span>GPT runtime</span>
          <h2>{runtimeLabels[runtimeStatus]}</h2>
          <p>{runtime.data?.error ? localizedErrorMessage(runtime.data.error.code, runtime.data.error.message) : runtimeMessage(runtimeStatus)}</p>
          <div className={styles.gptMeta}><span>Profile <strong>{runtime.data?.profile_id || gptHealth?.profile_id || "PROFILE_GPT_1"}</strong></span><span>Browser <strong>{(runtime.data?.browser_running ?? gptHealth?.browser_running) ? "running" : "closed"}</strong></span><span>Xác thực <strong>{runtime.data?.authenticated == null ? "chưa kiểm tra" : runtime.data.authenticated ? "đã đăng nhập" : "chưa đăng nhập"}</strong></span><span>Queue <strong>{runtime.data?.queue_depth ?? 0}</strong></span></div>
        </div>
        <div className={styles.gptActions}>
          <button className="button secondary" onClick={() => openMutation.mutate()} disabled={openMutation.isPending || busy}><MonitorUp size={15} />Mở ChatGPT</button>
          <button className="button secondary" onClick={() => checkMutation.mutate()} disabled={checkMutation.isPending || busy}><RefreshCw size={15} />Kiểm tra</button>
          <button className="button ghost" onClick={() => closeMutation.mutate()} disabled={closeMutation.isPending || busy}><Square size={14} />Đóng browser</button>
        </div>
      </section>
      {(runtime.isError || openMutation.isError || checkMutation.isError || closeMutation.isError) && <div className={styles.runtimeError}><ErrorNotice message={runtimeErrorMessage(runtime.error || openMutation.error || checkMutation.error || closeMutation.error)} /></div>}
      <section className={styles.security}><CircleOff size={17} /><div><strong>Phiên đăng nhập luôn nằm trong profile Chromium hiện có.</strong><p>YT Pro Max không đọc mật khẩu, TOTP, account.json hoặc nhập cookie từ auto_YT. Nếu profile bị khóa, hãy đóng tiến trình đang sử dụng profile thay vì xóa file lock.</p></div></section>
    </div>
  );
}

function SystemCard({ icon, label, value, detail, healthy }: { icon: React.ReactNode; label: string; value: string; detail: string; healthy: boolean }) {
  return <article className={`card ${styles.systemCard}`}><div className={`${styles.systemIcon} ${healthy ? styles.healthy : styles.unhealthy}`}>{icon}</div><span>{label}</span><h2>{value}</h2><p>{detail}</p></article>;
}

function mark(value: unknown) {
  return value === true ? "OK" : value === false ? "thiếu" : "chưa rõ";
}

function runtimeErrorMessage(error: Error | null) {
  if (error instanceof ApiError) return localizedErrorMessage(error.code, error.message);
  return error?.message || "Không thể điều khiển GPT runtime.";
}

function runtimeMessage(status: GptRuntimeStatus) {
  if (status === "ready") return "Profile đã xác thực và sẵn sàng nhận rewrite job.";
  if (status === "login_required") return "Mở browser, đăng nhập thủ công rồi nhấn Kiểm tra.";
  if (status === "profile_locked") return "Đóng auto_YT hoặc Chrome đang sử dụng profile này.";
  if (status === "busy") return "Một rewrite job đang điều khiển browser; không thể đóng lúc này.";
  return "Kiểm tra phiên ChatGPT trước khi chạy full pipeline lần đầu.";
}
