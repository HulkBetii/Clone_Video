import { expect, test } from "@playwright/test";

import { health, workspace } from "./fixtures";

test("creates a full workspace and opens its production view", async ({ page }) => {
  await page.route("**/api/v1/workspaces", async (route) => {
    if (route.request().method() === "POST") await route.fulfill({ status: 202, json: workspace });
    else await route.fallback();
  });
  await page.route("**/api/v1/workspaces/source-001", (route) => route.fulfill({ json: workspace }));
  await page.route("**/api/v1/transcript-jobs/source-001/artifacts/txt", (route) => route.fulfill({ contentType: "text/plain", body: "Title: Gốc\n\nNội dung gốc." }));

  await page.goto("/");
  await page.getByLabel("Link video YouTube").fill("https://youtu.be/video001");
  await page.getByRole("button", { name: /bắt đầu xử lý/i }).click();

  await expect(page).toHaveURL(/\/workspaces\/source-001$/);
  await expect(page.getByRole("heading", { level: 1, name: "Tiêu đề video gốc" })).toBeVisible();
  await expect(page.getByText("Nội dung gốc.")).toBeVisible();
});

test("checks a manual GPT login and returns runtime to ready", async ({ page }) => {
  let runtimeStatus = "login_required";
  await page.route("**/api/v1/health", (route) => route.fulfill({ json: health }));
  await page.route("**/api/v1/gpt-runtime", (route) => route.fulfill({ json: { status: runtimeStatus, profile_id: "PROFILE_GPT_1", profile_exists: true, browser_running: true, authenticated: runtimeStatus === "ready", active_job_id: null, queue_depth: 0, error: runtimeStatus === "ready" ? null : { code: "GPT_LOGIN_REQUIRED", message: "Cần đăng nhập", retryable: false, details: {} } } }));
  await page.route("**/api/v1/gpt-runtime/check", async (route) => {
    runtimeStatus = "ready";
    await route.fulfill({ json: { status: "ready", profile_id: "PROFILE_GPT_1", profile_exists: true, browser_running: true, authenticated: true, active_job_id: null, queue_depth: 0, error: null } });
  });

  await page.goto("/system");
  await expect(page.getByRole("heading", { name: "Cần đăng nhập" })).toBeVisible();
  await page.getByRole("button", { name: "Kiểm tra" }).click();
  await expect(page.getByRole("heading", { name: "Sẵn sàng" })).toBeVisible();
});
