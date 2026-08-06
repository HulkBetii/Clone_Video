export const transcriptStages = [
  "inspecting",
  "fetching_caption",
  "downloading_audio",
  "loading_model",
  "transcribing",
  "rendering",
] as const;

export const rewriteStages = [
  "preparing_source",
  "analyzing_style",
  "planning",
  "uploading",
  "rewriting",
  "editing",
  "validating",
  "rendering",
] as const;

export type JobStatus = "queued" | "running" | "completed" | "failed";
export type WorkspaceStatus = JobStatus | "waiting_for_user";
export type WorkspacePhase = "transcript" | "rewrite" | "completed";
export type TranscriptStage = (typeof transcriptStages)[number];
export type RewriteStage = (typeof rewriteStages)[number];
export type TranscriptSource = "manual_caption" | "automatic_caption" | "whisper";

export interface ErrorInfo {
  code: string;
  message: string;
  retryable: boolean;
  details: Record<string, unknown>;
}

export interface VideoMetadata {
  id: string;
  title: string;
  channel: string | null;
  duration_seconds: number | null;
  webpage_url: string;
}

export interface ArtifactLinks {
  srt: string;
  txt: string;
  json: string;
}

export interface TranscriptJob {
  id: string;
  request_url: string;
  auto_rewrite_requested: boolean;
  status: JobStatus;
  stage: TranscriptStage | null;
  progress: number;
  source: TranscriptSource | null;
  requested_language: string | null;
  language: string | null;
  language_confidence: number | null;
  video: VideoMetadata | null;
  artifacts: ArtifactLinks | null;
  warnings: string[];
  error: ErrorInfo | null;
  cached: boolean;
  created_at: string;
  updated_at: string;
}

export interface ValidationSummary {
  passed: boolean;
  style_score: number;
  coverage_score: number;
  language_match: boolean;
  tts_ready: boolean;
  unsupported_claims: string[];
  missing_points: string[];
  length_ratio: number;
}

export interface RewriteJob {
  id: string;
  transcript_job_id: string;
  status: JobStatus;
  stage: RewriteStage | null;
  progress: number;
  video: VideoMetadata | null;
  language: string | null;
  source_length: number | null;
  output_length: number | null;
  sections_completed: number;
  sections_total: number;
  title: string | null;
  validation: ValidationSummary | null;
  artifacts: { txt: string } | null;
  warnings: string[];
  error: ErrorInfo | null;
  cached: boolean;
  created_at: string;
  updated_at: string;
}

export interface Workspace {
  id: string;
  status: WorkspaceStatus;
  phase: WorkspacePhase;
  progress: number;
  auto_rewrite: boolean;
  request_url: string;
  transcript: TranscriptJob;
  rewrite: RewriteJob | null;
  action_required: ErrorInfo | null;
  created_at: string;
  updated_at: string;
}

export interface WorkspaceListResponse {
  items: Workspace[];
  total: number;
  limit: number;
  offset: number;
}

export interface DeleteWorkspacesResponse {
  deleted_ids: string[];
}

export type GptRuntimeStatus =
  | "not_checked"
  | "ready"
  | "login_required"
  | "profile_locked"
  | "busy"
  | "unavailable"
  | "error";

export interface GptRuntime {
  status: GptRuntimeStatus;
  profile_id: string;
  profile_exists: boolean;
  browser_running: boolean;
  authenticated: boolean | null;
  active_job_id: string | null;
  queue_depth: number;
  error: ErrorInfo | null;
}

export interface WhisperHealth {
  model: string;
  loaded: boolean;
  device: string;
  compute_type: string;
  warnings: string[];
  cuda_device_count?: number;
  cuda_runtime_available?: boolean;
  cuda_dll_dir_configured?: boolean;
  runtime_error?: string;
}

export interface GptRewriteHealth {
  profile_id: string;
  profile_exists: boolean;
  browser_running: boolean;
  conversation_url: string | null;
  worker_concurrency: number;
}

export interface HealthResponse {
  status: "ok" | "degraded";
  checks: {
    sqlite: boolean;
    yt_dlp: string;
    ffmpeg: boolean;
    ffprobe: boolean;
    whisper: WhisperHealth;
    gpt_rewrite: GptRewriteHealth;
  };
}

export interface CreateWorkspaceInput {
  url: string;
  language?: string;
  auto_rewrite: boolean;
  force_refresh: boolean;
}

export interface WordTimestamp {
  start_ms: number;
  end_ms: number;
  text: string;
  probability: number | null;
}

export interface TranscriptSegment {
  index: number;
  start_ms: number;
  end_ms: number;
  text: string;
  words: WordTimestamp[] | null;
}

export interface TranscriptReconciliationItem {
  segment_index: number;
  start_ms: number;
  end_ms: number;
  primary_text: string;
  caption_text: string | null;
  secondary_text: string | null;
  final_text: string;
  decision: string;
  triggers: string[];
  word_start: number | null;
  word_end: number | null;
  priority_tier: number | null;
  alignment_coverage: number | null;
  temporal_overlap: number | null;
  primary_probability: number | null;
  secondary_mean_probability: number | null;
  secondary_min_probability: number | null;
  decision_reason: string | null;
  corrected_words: number;
}

export interface TranscriptReconciliation {
  strategy: string;
  alignment_version: string;
  reference_source: string;
  secondary_model: string;
  alignment_coverage: number | null;
  compared_segments: number;
  suspicious_segments: number;
  selected_spans: number;
  processed_spans: number;
  selected_windows: number;
  selected_duration_ms: number;
  secondary_windows: number;
  secondary_duration_ms: number;
  corrected_segments: number;
  corrected_words: number;
  unresolved_segments: number;
  skipped_segments: number;
  items: TranscriptReconciliationItem[];
}

export interface TranscriptArtifact {
  schema_version: number;
  job_id: string;
  source: TranscriptSource;
  language: string;
  language_confidence: number | null;
  video: VideoMetadata;
  segments: TranscriptSegment[];
  warnings: string[];
  reconciliation: TranscriptReconciliation | null;
}
