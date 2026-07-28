import { useQuery } from "@tanstack/react-query";

import { fetchArtifact, getGptRuntime, getHealth, getWorkspace, listWorkspaces } from "../lib/api";
import type { WorkspaceStatus } from "../types";
import { usePageVisible } from "./useVisibility";

const activeStatuses: WorkspaceStatus[] = ["queued", "running"];

export function useWorkspace(workspaceId: string) {
  const visible = usePageVisible();
  return useQuery({
    queryKey: ["workspace", workspaceId],
    queryFn: () => getWorkspace(workspaceId),
    enabled: Boolean(workspaceId),
    refetchInterval: (query) => visible && query.state.data && activeStatuses.includes(query.state.data.status) ? 1_000 : false,
    refetchOnWindowFocus: true,
  });
}

export function useWorkspaceList(params: { status?: string; q?: string; limit?: number; offset?: number }) {
  const visible = usePageVisible();
  return useQuery({
    queryKey: ["workspaces", params],
    queryFn: () => listWorkspaces(params),
    refetchInterval: (query) => visible && query.state.data?.items.some((item) => activeStatuses.includes(item.status)) ? 1_000 : false,
  });
}

export function useArtifact(url: string | undefined, enabled: boolean) {
  return useQuery({
    queryKey: ["artifact", url],
    queryFn: () => fetchArtifact(url!),
    enabled: enabled && Boolean(url),
    staleTime: Infinity,
    retry: 1,
  });
}

export function useHealth() {
  const visible = usePageVisible();
  return useQuery({ queryKey: ["health"], queryFn: getHealth, refetchInterval: visible ? 5_000 : false });
}

export function useGptRuntime() {
  const visible = usePageVisible();
  return useQuery({ queryKey: ["gpt-runtime"], queryFn: getGptRuntime, refetchInterval: visible ? 3_000 : false });
}
