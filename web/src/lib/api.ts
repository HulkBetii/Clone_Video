import type {
  CreateWorkspaceInput,
  DeleteWorkspacesResponse,
  GptRuntime,
  HealthResponse,
  Workspace,
  WorkspaceListResponse,
} from "../types";

const API_PREFIX = "/api/v1";

export class ApiError extends Error {
  status: number;
  code?: string;
  details?: unknown;

  constructor(status: number, message: string, code?: string, details?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_PREFIX}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    let payload: { detail?: { code?: string; message?: string } | string } | null = null;
    try {
      payload = await response.json();
    } catch {
      // The status text still gives a useful local error when the server is unavailable.
    }
    const detail = payload?.detail;
    const message = typeof detail === "string" ? detail : detail?.message;
    throw new ApiError(response.status, message ?? response.statusText, typeof detail === "object" ? detail.code : undefined, detail);
  }
  return response.json() as Promise<T>;
}

export function createWorkspace(input: CreateWorkspaceInput) {
  return request<Workspace>("/workspaces", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function listWorkspaces(params: { status?: string; q?: string; limit?: number; offset?: number } = {}) {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== "") query.set(key, String(value));
  });
  return request<WorkspaceListResponse>(`/workspaces${query.size ? `?${query}` : ""}`);
}

export function getWorkspace(id: string) {
  return request<Workspace>(`/workspaces/${encodeURIComponent(id)}`);
}

export function deleteWorkspaces(transcriptJobIds: string[]) {
  return request<DeleteWorkspacesResponse>("/workspaces/bulk-delete", {
    method: "POST",
    body: JSON.stringify({ transcript_job_ids: transcriptJobIds }),
  });
}

export function resumeWorkspace(id: string) {
  return request<Workspace>(`/workspaces/${encodeURIComponent(id)}/resume`, { method: "POST" });
}

export function getHealth() {
  return request<HealthResponse>("/health");
}

export function getGptRuntime() {
  return request<GptRuntime>("/gpt-runtime");
}

export function openGptRuntime(rewriteJobId?: string) {
  return request<GptRuntime>("/gpt-runtime/open", {
    method: "POST",
    body: JSON.stringify(rewriteJobId ? { rewrite_job_id: rewriteJobId } : {}),
  });
}

export function checkGptRuntime() {
  return request<GptRuntime>("/gpt-runtime/check", { method: "POST" });
}

export function closeGptRuntime() {
  return request<GptRuntime>("/gpt-runtime/close", { method: "POST" });
}

export async function fetchArtifact(url: string) {
  const response = await fetch(url);
  if (!response.ok) throw new ApiError(response.status, "Không thể tải artifact.");
  return response.text();
}

export async function fetchJsonArtifact(url: string): Promise<unknown> {
  const response = await fetch(url);
  if (!response.ok) throw new ApiError(response.status, "Không thể tải JSON audit.");
  try {
    return await response.json();
  } catch {
    throw new ApiError(response.status, "JSON audit không hợp lệ.");
  }
}
