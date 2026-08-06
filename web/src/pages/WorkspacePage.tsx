import * as Tabs from "@radix-ui/react-tabs";
import * as Tooltip from "@radix-ui/react-tooltip";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowLeft, Check, Clipboard, Download, ExternalLink, FileJson, FileText, Gauge, Languages, RefreshCw, ShieldCheck, Sparkles, Subtitles } from "lucide-react";
import { type ReactNode, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { PipelineRail, stageLabel } from "../components/PipelineRail";
import { ErrorNotice, ProgressBar, Skeleton, StatusPill } from "../components/ui";
import { useArtifact, useTranscriptArtifact, useWorkspace } from "../hooks/queries";
import { checkGptRuntime, openGptRuntime, resumeWorkspace } from "../lib/api";
import { formatConfidence, formatDate, formatDuration, formatMilliseconds, parseTextArtifact, ratio, validationLabel } from "../lib/content";
import { localizedErrorMessage, reconciliationReasonLabel, transcriptSourceCopy, transcriptWarningCopy } from "../lib/presentation";
import { summarizeTranscriptArtifact, type TranscriptQualitySummary } from "../lib/transcriptQuality";
import type { TranscriptArtifact, TranscriptJob, ValidationSummary } from "../types";
import styles from "./WorkspacePage.module.css";

type ContentTab = "transcript" | "rewrite" | "compare";

export function WorkspacePage() {
  const { workspaceId = "" } = useParams();
  const [tab, setTab] = useState<ContentTab>("transcript");
  const [copied, setCopied] = useState(false);
  const workspaceQuery = useWorkspace(workspaceId);
  const queryClient = useQueryClient();
  const workspace = workspaceQuery.data;
  const transcriptUrl = workspace?.transcript?.artifacts?.txt;
  const transcriptJsonUrl = workspace?.transcript?.artifacts?.json;
  const rewriteUrl = workspace?.rewrite?.artifacts?.txt;
  const transcriptArtifact = useArtifact(transcriptUrl, tab === "transcript" || tab === "compare");
  const transcriptJsonArtifact = useTranscriptArtifact(transcriptJsonUrl, tab === "transcript" && workspace?.transcript.status === "completed");
  const rewriteArtifact = useArtifact(rewriteUrl, tab === "rewrite" || tab === "compare");
  const transcriptText = useMemo(() => parseTextArtifact(transcriptArtifact.data ?? ""), [transcriptArtifact.data]);
  const rewriteText = useMemo(() => parseTextArtifact(rewriteArtifact.data ?? ""), [rewriteArtifact.data]);
  const transcriptQuality = useMemo(() => transcriptJsonArtifact.data ? summarizeTranscriptArtifact(transcriptJsonArtifact.data) : null, [transcriptJsonArtifact.data]);
  const resumeMutation = useMutation({ mutationFn: () => resumeWorkspace(workspaceId), onSuccess: (data) => queryClient.setQueryData(["workspace", workspaceId], data) });
  const openMutation = useMutation({ mutationFn: () => openGptRuntime(workspace?.rewrite?.id) });
  const continueMutation = useMutation({
    mutationFn: async () => { await checkGptRuntime(); return resumeWorkspace(workspaceId); },
    onSuccess: (data) => queryClient.setQueryData(["workspace", workspaceId], data),
  });

  async function copyBody() {
    if (!rewriteText.body) return;
    await navigator.clipboard.writeText(rewriteText.body);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1_500);
  }

  if (workspaceQuery.isLoading) return <div className="page"><Skeleton height={170} /><div style={{ height: 20 }} /><Skeleton height={430} /></div>;
  if (workspaceQuery.isError || !workspace) return <div className="page"><Link className={styles.back} to="/library"><ArrowLeft size={15} />Thư viện</Link><ErrorNotice title="Không mở được workspace" message={workspaceQuery.error?.message ?? "Workspace không tồn tại."} /></div>;

  const video = workspace.transcript?.video ?? workspace.rewrite?.video;
  const currentStage = workspace.rewrite?.stage ?? workspace.transcript?.stage;
  const validation = workspace.rewrite?.validation;
  const rewriteError = workspace.rewrite?.error;
  const canResumeRewrite = Boolean(rewriteError && (rewriteError.code === "GPT_LOGIN_REQUIRED" || rewriteError.code === "GPT_PROFILE_LOCKED" || (rewriteError.code.startsWith("GPT_") && rewriteError.retryable)));
  const actionMessage = workspace.action_required ? localizedErrorMessage(workspace.action_required.code, workspace.action_required.message) : null;
  const sourceLength = workspace.rewrite?.source_length ?? transcriptText.body.length;
  const outputLength = workspace.rewrite?.output_length ?? rewriteText.body.length;
  const lengthRatio = validation?.length_ratio ?? ratio(sourceLength, outputLength);
  const source = transcriptSourceCopy(workspace.transcript.source);

  return (
    <div className="page">
      <Link className={styles.back} to="/library"><ArrowLeft size={15} />Quay lại thư viện</Link>
      <header className={styles.hero}>
        <div className={styles.heroCopy}>
          <div className={styles.kicker}><StatusPill status={workspace.status} /><span>{video?.id || workspaceId.slice(0, 8)}</span></div>
          <h1>{video?.title || "Đang lấy tiêu đề video..."}</h1>
          <div className={styles.videoMeta}><span>{video?.channel || "Kênh chưa xác định"}</span><span>{formatDuration(video?.duration_seconds)}</span><span>{workspace.transcript.language || "—"}</span><span>{source.label}</span><span>{workspace.transcript.cached ? "Từ cache" : "Xử lý mới"}</span><span>Cập nhật {formatDate(workspace.updated_at)}</span></div>
        </div>
        {video?.webpage_url && <a href={video.webpage_url} className="button secondary" target="_blank" rel="noreferrer">Mở YouTube <ExternalLink size={15} /></a>}
      </header>

      <section className={`card ${styles.pipeline}`}>
        <PipelineRail workspace={workspace} />
        {(workspace.status === "running" || workspace.status === "queued") && <div className={styles.progress}><ProgressBar value={workspace.progress} label={stageLabel(currentStage)} />{workspace.rewrite?.sections_total ? <span>Phần {workspace.rewrite.sections_completed}/{workspace.rewrite.sections_total}</span> : null}</div>}
      </section>

      {workspace.status === "waiting_for_user" && <section className={styles.actionPanel}>
        <AlertTriangle size={24} /><div><strong>ChatGPT cần bạn thao tác</strong><p>{actionMessage || "Mở cửa sổ ChatGPT, đăng nhập hoặc giải phóng profile, sau đó kiểm tra và tiếp tục đúng checkpoint."}</p></div>
        <button className="button secondary" onClick={() => openMutation.mutate()} disabled={openMutation.isPending}>Mở ChatGPT</button>
        <button className="button coral" onClick={() => continueMutation.mutate()} disabled={continueMutation.isPending}>{continueMutation.isPending ? "Đang kiểm tra..." : "Kiểm tra & tiếp tục"}</button>
      </section>}
      {workspace.status === "failed" && workspace.rewrite?.error && <ErrorNotice title="Rewrite thất bại" message={localizedErrorMessage(workspace.rewrite.error.code, workspace.rewrite.error.message)} code={workspace.rewrite.error.code} />}
      {workspace.status === "failed" && workspace.transcript.error && <ErrorNotice title="Transcript thất bại" message={localizedErrorMessage(workspace.transcript.error.code, workspace.transcript.error.message)} code={workspace.transcript.error.code} />}
      {workspace.status === "failed" && canResumeRewrite && <button className="button secondary" onClick={() => resumeMutation.mutate()} disabled={resumeMutation.isPending}><RefreshCw size={15} />Thử tiếp tục</button>}

      <Tabs.Root className={`card ${styles.contentCard}`} value={tab} onValueChange={(value) => setTab(value as ContentTab)}>
        <Tabs.List className={styles.tabs} aria-label="Nội dung workspace">
          <Tabs.Trigger value="transcript"><Subtitles size={16} />Transcript</Tabs.Trigger>
          <Tabs.Trigger value="rewrite" disabled={!workspace.rewrite}><Sparkles size={16} />Bản viết lại</Tabs.Trigger>
          <Tabs.Trigger value="compare" disabled={!workspace.rewrite}><Gauge size={16} />So sánh</Tabs.Trigger>
        </Tabs.List>
        <Tabs.Content value="transcript" className={styles.tabContent}>
          <TranscriptQualityPanel job={workspace.transcript} artifact={transcriptJsonArtifact.data} summary={transcriptQuality} loading={transcriptJsonArtifact.isLoading} failed={transcriptJsonArtifact.isError} />
          <ContentHeader title={transcriptText.title || video?.title || "Transcript gốc"} meta={`${sourceLength.toLocaleString("vi-VN")} ký tự`} actions={<ArtifactActions artifacts={workspace.transcript?.artifacts} />} />
          <ArtifactView query={transcriptArtifact} body={transcriptText.body} empty="Transcript sẽ xuất hiện sau khi giai đoạn đầu hoàn tất." />
        </Tabs.Content>
        <Tabs.Content value="rewrite" className={styles.tabContent}>
          <ContentHeader title={rewriteText.title || workspace.rewrite?.title || "Bản viết lại"} meta={`${outputLength.toLocaleString("vi-VN")} ký tự · ${validationLabel(validation)}`} actions={<><IconTip label={copied ? "Đã sao chép" : "Sao chép body"}><button className="button secondary" onClick={copyBody} disabled={!rewriteText.body}>{copied ? <Check size={15} /> : <Clipboard size={15} />}<span>{copied ? "Đã sao chép" : "Copy cho TTS"}</span></button></IconTip>{rewriteUrl && <a className="button" href={rewriteUrl} download><Download size={15} />Tải TXT</a>}</>} />
          <ArtifactView query={rewriteArtifact} body={rewriteText.body} empty="Bản viết lại sẽ xuất hiện sau khi GPT hoàn tất." />
        </Tabs.Content>
        <Tabs.Content value="compare" className={styles.tabContent}>
          <CompareMetrics sourceLength={sourceLength} outputLength={outputLength} lengthRatio={lengthRatio} language={workspace.rewrite?.language || workspace.transcript?.language} validation={validation} />
          <div className={styles.compareGrid}>
            <ComparePanel label="Bản gốc" title={transcriptText.title} query={transcriptArtifact} body={transcriptText.body} />
            <ComparePanel label="Bản viết lại" title={rewriteText.title} query={rewriteArtifact} body={rewriteText.body} />
          </div>
          <ValidationDetails validation={validation} />
        </Tabs.Content>
      </Tabs.Root>
      <ProcessingNotices warnings={[...workspace.transcript.warnings, ...(workspace.rewrite?.warnings ?? [])]} />
    </div>
  );
}

function TranscriptQualityPanel({ job, artifact, summary, loading, failed }: { job: TranscriptJob; artifact: TranscriptArtifact | null | undefined; summary: TranscriptQualitySummary | null; loading: boolean; failed: boolean }) {
  if (job.status !== "completed" || !job.artifacts?.json) return null;
  if (loading) return <div className={styles.qualityLoading}><Skeleton height={155} /></div>;
  if (failed || !artifact || !summary) return <section className={styles.qualityUnavailable}><ShieldCheck size={19} /><div><strong>Không đọc được quality audit</strong><p>Preview và download transcript vẫn hoạt động; JSON artifact có thể thuộc schema cũ hoặc không hợp lệ.</p></div></section>;

  const reconciliation = artifact.reconciliation;
  const source = transcriptSourceCopy(job.source);
  const metrics = [
    { label: "Nguồn", value: source.label },
    { label: "Confidence", value: formatConfidence(job.language_confidence ?? artifact.language_confidence) },
    { label: "Word timestamps", value: `${summary.segmentsWithWords}/${summary.segmentCount} đoạn` },
    { label: "Alignment", value: reconciliation?.alignment_coverage == null ? "Không áp dụng" : formatConfidence(reconciliation.alignment_coverage) },
    { label: "Spans", value: reconciliation ? `${reconciliation.processed_spans}/${reconciliation.selected_spans}` : "Không áp dụng" },
    { label: "Model phụ", value: reconciliation ? `${reconciliation.secondary_windows} cửa sổ · ${formatMilliseconds(reconciliation.secondary_duration_ms)}` : "Không chạy" },
    { label: "Đã sửa", value: reconciliation ? `${reconciliation.corrected_words} từ` : "0 từ" },
    { label: "Chưa chốt", value: reconciliation ? `${reconciliation.unresolved_segments} · bỏ qua ${reconciliation.skipped_segments}` : "Không có" },
  ];

  return <section className={styles.qualityPanel}>
    <header className={styles.qualityHeader}><div><span>Transcript quality / schema v{summary.schemaVersion}</span><h2>Đối chiếu với backend</h2><p>{source.description} {reconciliation ? `Audit dùng ${reconciliation.secondary_model} và ${reconciliation.alignment_version}.` : "Nguồn này không cần reconciliation ba nguồn."}</p></div><div className={styles.qualityState}><ShieldCheck size={16} />{job.cached ? "Artifact từ cache" : "Artifact mới"}</div></header>
    <div className={styles.qualityMetrics}>{metrics.map((metric) => <div key={metric.label}><span>{metric.label}</span><strong>{metric.value}</strong></div>)}</div>
    {summary.corrections.length > 0 && <section className={styles.corrections}><div className={styles.qualitySectionTitle}><strong>Correction đã xuất bản</strong><span>Chỉ các thay đổi đạt đồng thuận caption + model phụ</span></div><div className={styles.correctionList}>{summary.corrections.map((item) => <div key={`${item.segment_index}-${item.word_start}-${item.start_ms}`}><span>{formatMilliseconds(item.start_ms)}{item.priority_tier ? ` · Tier ${item.priority_tier}` : ""}</span><p><del>{item.primary_text}</del><b>→</b><ins>{item.final_text}</ins></p></div>)}</div></section>}
    {summary.unresolvedReasons.length > 0 && <section className={styles.unresolved}><div className={styles.qualitySectionTitle}><strong>Giữ nguyên theo chính sách bảo thủ</strong><span>{reconciliation?.unresolved_segments ?? 0} span chưa đủ bằng chứng để sửa</span></div><div className={styles.reasonList}>{summary.unresolvedReasons.map((item) => <span key={item.reason ?? "unknown"}>{reconciliationReasonLabel(item.reason)} <strong>{item.count}</strong></span>)}</div></section>}
  </section>;
}

function ProcessingNotices({ warnings }: { warnings: string[] }) {
  const uniqueWarnings = [...new Set(warnings)];
  if (!uniqueWarnings.length) return null;
  return <section className={styles.notices}><div className={styles.qualitySectionTitle}><strong>Thông tin xử lý</strong><span>Thông báo từ transcript và rewrite pipeline</span></div><div className={styles.noticeGrid}>{uniqueWarnings.map((warning) => { const copy = transcriptWarningCopy(warning); return <article key={warning} className={styles[copy.tone]}><div><strong>{copy.title}</strong><p>{copy.description}</p></div><code>{warning}</code></article>; })}</div></section>;
}

function ContentHeader({ title, meta, actions }: { title: string; meta: string; actions: ReactNode }) {
  return <div className={styles.contentHeader}><div><span>Nội dung đầu ra</span><h2>{title}</h2><p>{meta}</p></div><div className={styles.actions}>{actions}</div></div>;
}

function ArtifactActions({ artifacts }: { artifacts?: { txt: string; srt: string; json?: string } | null }) {
  if (!artifacts) return null;
  return <><a className="button secondary" href={artifacts.srt} download><Download size={15} />SRT</a>{artifacts.json && <a className="button secondary" href={artifacts.json} download><FileJson size={15} />JSON</a>}<a className="button" href={artifacts.txt} download><FileText size={15} />TXT</a></>;
}

function ArtifactView({ query, body, empty }: { query: ReturnType<typeof useArtifact>; body: string; empty: string }) {
  if (query.isLoading) return <Skeleton height={340} />;
  if (query.isError) return <ErrorNotice message={query.error.message} />;
  if (!body) return <div className={styles.emptyArtifact}>{empty}</div>;
  return <article className={styles.script}>{body}</article>;
}

function ComparePanel({ label, title, query, body }: { label: string; title: string; query: ReturnType<typeof useArtifact>; body: string }) {
  return <section className={styles.comparePanel}><div><span>{label}</span><strong>{title || "Chưa có tiêu đề"}</strong></div><ArtifactView query={query} body={body} empty="Nội dung chưa sẵn sàng." /></section>;
}

function CompareMetrics({ sourceLength, outputLength, lengthRatio, language, validation }: { sourceLength: number; outputLength: number; lengthRatio: number | null; language?: string | null; validation?: ValidationSummary | null }) {
  const metrics = [
    { label: "Độ dài gốc", value: sourceLength ? sourceLength.toLocaleString("vi-VN") : "—" },
    { label: "Độ dài mới", value: outputLength ? outputLength.toLocaleString("vi-VN") : "—" },
    { label: "Tỷ lệ", value: lengthRatio ? `${(lengthRatio * 100).toFixed(1)}%` : "—" },
    { label: "Phong cách", value: validation?.style_score != null ? `${validation.style_score}/100` : "—" },
    { label: "Độ phủ ý", value: validation?.coverage_score != null ? `${validation.coverage_score}/100` : "—" },
    { label: "Ngôn ngữ", value: language || "—" },
  ];
  return <div className={styles.metrics}>{metrics.map((metric) => <div key={metric.label}><span>{metric.label}</span><strong>{metric.value}</strong></div>)}</div>;
}

function ValidationDetails({ validation }: { validation?: ValidationSummary | null }) {
  if (!validation) return <div className={styles.validationEmpty}>Kết quả kiểm định chi tiết chưa được lưu cho workspace này.</div>;
  const missing = validation.missing_points ?? [];
  const unsupported = validation.unsupported_claims ?? [];
  return <div className={styles.validation}><div><Languages size={17} /><span>Khớp ngôn ngữ</span><strong>{validation.language_match === false ? "Không" : validation.language_match === true ? "Có" : "—"}</strong></div><div><Sparkles size={17} /><span>Sẵn sàng TTS</span><strong>{validation.tts_ready === false ? "Không" : validation.tts_ready === true ? "Có" : "—"}</strong></div><section><strong>Luận điểm còn thiếu</strong><p>{missing.length ? missing.join(" · ") : "Không phát hiện"}</p></section><section><strong>Claim chưa có nguồn</strong><p>{unsupported.length ? unsupported.join(" · ") : "Không phát hiện"}</p></section></div>;
}

function IconTip({ label, children }: { label: string; children: ReactNode }) {
  return <Tooltip.Root><Tooltip.Trigger asChild>{children}</Tooltip.Trigger><Tooltip.Portal><Tooltip.Content sideOffset={7} className={styles.tooltip}>{label}<Tooltip.Arrow className={styles.tooltipArrow} /></Tooltip.Content></Tooltip.Portal></Tooltip.Root>;
}
