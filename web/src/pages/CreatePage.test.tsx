import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";

import { App } from "../App";
import { completedWorkspace, manualTranscriptArtifact } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

it("creates a full workspace by default and opens its detail page", async () => {
  let requestBody: Record<string, unknown> = {};
  server.use(
    http.post("/api/v1/workspaces", async ({ request }) => {
      requestBody = await request.json() as Record<string, unknown>;
      return HttpResponse.json(completedWorkspace, { status: 202 });
    }),
    http.get("/api/v1/workspaces/source-001", () => HttpResponse.json(completedWorkspace)),
    http.get("/api/v1/transcript-jobs/source-001/artifacts/txt", () => HttpResponse.text("Title: Gốc\n\nNội dung gốc.")),
    http.get("/api/v1/transcript-jobs/source-001/artifacts/json", () => HttpResponse.json(manualTranscriptArtifact)),
  );

  renderApp(<App />);
  await userEvent.type(screen.getByLabelText("Link video YouTube"), "https://youtu.be/video001");
  await userEvent.click(screen.getByRole("button", { name: /bắt đầu xử lý/i }));

  expect(await screen.findByText("Tiêu đề video gốc")).toBeInTheDocument();
  expect(requestBody).toMatchObject({ auto_rewrite: true, force_refresh: false });
});
