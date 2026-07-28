import { expect, test } from "@playwright/test";

import { workspace } from "./fixtures";

test("keeps compare content readable in the mobile layout", async ({ page }) => {
  await page.route("**/api/v1/workspaces/source-001", (route) => route.fulfill({ json: workspace }));
  await page.route("**/api/v1/transcript-jobs/source-001/artifacts/txt", (route) => route.fulfill({ contentType: "text/plain", body: "Title: Gốc\n\nNội dung gốc." }));
  await page.route("**/api/v1/rewrite-jobs/rewrite-001/artifacts/txt", (route) => route.fulfill({ contentType: "text/plain", body: "Title: SEO mới\n\nNội dung mới sẵn sàng cho TTS." }));

  await page.goto("/workspaces/source-001");
  await page.getByRole("tab", { name: /so sánh/i }).click();

  await expect(page.getByText("92/100")).toBeVisible();
  await expect(page.getByText("Nội dung gốc.")).toBeVisible();
  await expect(page.getByText("Nội dung mới sẵn sàng cho TTS.")).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Điều hướng di động" })).toBeVisible();
});
