export type JobStatus = "queued" | "running" | "completed" | "failed";
export type WorkspaceStatus = JobStatus | "waiting_for_user";
export type WorkspacePhase = "transcript" | "rewrite" | "completed";

export interface ErrorInfo {
  code: string;
  message: string;
  retryable?: boolean;
  details?: Record<string, unknown>;
}

export interface VideoMetadata {
  id: string;
  title: string;
  channel?: string | null;
  duration_seconds?: number | null;
  webpage_url: string;
}

export interface ArtifactLinks {
  srt: string;
  txt: string;
  json?: string;
}

export interface TranscriptJob {
  id: string;
  request_url?: string;
  auto_rewrite_requested?: boolean;
  status: JobStatus;
  stage?: string | null;
  progress: number;
  source?: string | null;
  requested_language?: string | null;
  language?: string | null;
  language_confidence?: number | null;
  video?: VideoMetadata | null;
  artifacts?: ArtifactLinks | null;
  warnings: string[];
  error?: ErrorInfo | null;
  cached: boolean;
  created_at: string;
  updated_at: string;
}

export interface ValidationSummary {
  passed?: boolean;
  style_score?: number;
  coverage_score?: number;
  language_match?: boolean;
  tts_ready?: boolean;
  unsupported_claims?: string[];
  missing_points?: string[];
  length_ratio?: number;
}

export interface RewriteJob {
  id: string;
  transcript_job_id: string;
  status: JobStatus;
  stage?: string | null;
  progress: number;
  video?: VideoMetadata | null;
  language?: string | null;
  source_length?: number | null;
  output_length?: number | null;
  sections_completed: number;
  sections_total: number;
  title?: string | null;
  artifacts?: { txt: string } | null;
  warnings: string[];
  error?: ErrorInfo | null;
  cached: boolean;
  validation?: ValidationSummary | null;
  created_at: string;
  updated_at: string;
}

export interface WorkspaceAction {
  type?: string;
  code?: string;
  message?: string;
  [key: string]: unknown;
}

export interface Workspace {
  id: string;
  transcript_job_id?: string;
  status: WorkspaceStatus;
  phase: WorkspacePhase;
  progress: number;
  auto_rewrite: boolean;
  request_url?: string | null;
  transcript?: TranscriptJob | null;
  rewrite?: RewriteJob | null;
  action_required?: WorkspaceAction | string | null;
  created_at?: string;
  updated_at?: string;
}

export interface WorkspaceListResponse {
  items: Workspace[];
  total: number;
  limit?: number;
  offset?: number;
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
  profile_id?: string | null;
  profile_exists?: boolean;
  browser_running?: boolean;
  authenticated?: boolean | null;
  browser_state?: string | null;
  active_job_id?: string | null;
  queue_depth?: number;
  message?: string | null;
  error?: ErrorInfo | null;
}

export interface HealthResponse {
  status: "ok" | "degraded" | string;
  checks: Record<string, unknown>;
}

export interface CreateWorkspaceInput {
  url: string;
  language?: string;
  auto_rewrite: boolean;
  force_refresh: boolean;
}

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
