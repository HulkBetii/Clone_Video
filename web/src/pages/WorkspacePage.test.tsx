import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { vi } from "vitest";

import { App } from "../App";
import { completedWorkspace } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

it("loads artifacts only when their tab is opened and copies body without Title", async () => {
  let transcriptRequests = 0;
  let rewriteRequests = 0;
  server.use(
    http.get("/api/v1/workspaces/source-001", () => HttpResponse.json(completedWorkspace)),
    http.get("/api/v1/transcript-jobs/source-001/artifacts/txt", () => { transcriptRequests += 1; return HttpResponse.text("Title: Gốc\n\nNội dung gốc."); }),
    http.get("/api/v1/rewrite-jobs/rewrite-001/artifacts/txt", () => { rewriteRequests += 1; return HttpResponse.text("Title: Tiêu đề SEO mới\n\nNội dung mới cho TTS."); }),
  );
  const clipboardSpy = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();

  renderApp(<App />, "/workspaces/source-001");
  expect(await screen.findByText("Nội dung gốc.")).toBeInTheDocument();
  expect(transcriptRequests).toBe(1);
  expect(rewriteRequests).toBe(0);

  await userEvent.click(screen.getByRole("tab", { name: /bản viết lại/i }));
  expect(await screen.findByText("Nội dung mới cho TTS.")).toBeInTheDocument();
  expect(rewriteRequests).toBe(1);
  await userEvent.click(screen.getByRole("button", { name: /copy cho tts/i }));
  await waitFor(() => expect(clipboardSpy).toHaveBeenCalledWith("Nội dung mới cho TTS."));
});

it("shows validation metrics in compare mode", async () => {
  server.use(
    http.get("/api/v1/workspaces/source-001", () => HttpResponse.json(completedWorkspace)),
    http.get("/api/v1/transcript-jobs/source-001/artifacts/txt", () => HttpResponse.text("Title: Gốc\n\nNội dung gốc.")),
    http.get("/api/v1/rewrite-jobs/rewrite-001/artifacts/txt", () => HttpResponse.text("Title: SEO\n\nNội dung mới.")),
  );
  renderApp(<App />, "/workspaces/source-001");
  await userEvent.click(await screen.findByRole("tab", { name: /so sánh/i }));
  expect(await screen.findByText("92/100")).toBeInTheDocument();
  expect(screen.getByText("95/100")).toBeInTheDocument();
  expect(screen.getByText("112.0%")).toBeInTheDocument();
});

it("does not offer rewrite resume for a failed transcript", async () => {
  const failedWorkspace = structuredClone(completedWorkspace);
  failedWorkspace.status = "failed";
  failedWorkspace.phase = "transcript";
  failedWorkspace.auto_rewrite = false;
  failedWorkspace.rewrite = null;
  failedWorkspace.transcript!.status = "failed";
  failedWorkspace.transcript!.artifacts = null;
  failedWorkspace.transcript!.error = { code: "NETWORK_TIMEOUT", message: "Timeout", retryable: true };
  server.use(http.get("/api/v1/workspaces/source-001", () => HttpResponse.json(failedWorkspace)));

  renderApp(<App />, "/workspaces/source-001");
  expect(await screen.findByText("Transcript thất bại")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /thử tiếp tục/i })).not.toBeInTheDocument();
});
