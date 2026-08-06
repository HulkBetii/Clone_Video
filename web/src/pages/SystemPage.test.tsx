import { screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";

import { App } from "../App";
import { renderApp } from "../test/render";
import { server } from "../test/server";

it("locks all GPT runtime actions that require an idle browser while busy", async () => {
  server.use(
    http.get("/api/v1/health", () => HttpResponse.json({ status: "ok", checks: { sqlite: true, yt_dlp: "1.0", ffmpeg: true, ffprobe: true, whisper: { model: "turbo", loaded: true, device: "cpu", compute_type: "int8", warnings: [], cuda_runtime_available: false }, gpt_rewrite: { profile_id: "PROFILE_GPT_1", profile_exists: true, browser_running: true, conversation_url: null, worker_concurrency: 1 } } })),
    http.get("/api/v1/gpt-runtime", () => HttpResponse.json({ status: "busy", profile_id: "PROFILE_GPT_1", profile_exists: true, browser_running: true, authenticated: true, active_job_id: "rewrite-001", queue_depth: 1, error: null })),
  );

  renderApp(<App />, "/system");
  expect(await screen.findByText("Đang chạy job")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /mở chatgpt/i })).toBeDisabled();
  expect(screen.getByRole("button", { name: /kiểm tra/i })).toBeDisabled();
  expect(screen.getByRole("button", { name: /đóng browser/i })).toBeDisabled();
});

it("explains lazy Whisper loading and CPU fallback when CUDA userspace is missing", async () => {
  server.use(
    http.get("/api/v1/health", () => HttpResponse.json({ status: "degraded", checks: { sqlite: true, yt_dlp: "1.0", ffmpeg: true, ffprobe: true, whisper: { model: "turbo", loaded: false, device: "not_loaded", compute_type: "not_loaded", warnings: [], cuda_device_count: 1, cuda_runtime_available: false, cuda_dll_dir_configured: true, runtime_error: "cublas64_12.dll_NOT_FOUND" }, gpt_rewrite: { profile_id: "PROFILE_GPT_1", profile_exists: true, browser_running: false, conversation_url: null, worker_concurrency: 1 } } })),
    http.get("/api/v1/gpt-runtime", () => HttpResponse.json({ status: "not_checked", profile_id: "PROFILE_GPT_1", profile_exists: true, browser_running: false, authenticated: null, active_job_id: null, queue_depth: 0, error: null })),
  );

  renderApp(<App />, "/system");
  expect(await screen.findByText("CPU fallback")).toBeInTheDocument();
  expect(screen.getByText(/chưa nạp, sẽ tải khi cần/i)).toBeInTheDocument();
});
