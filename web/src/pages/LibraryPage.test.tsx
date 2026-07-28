import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";

import { App } from "../App";
import { completedWorkspace } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

it("filters the library by status", async () => {
  let requestedStatus = "";
  server.use(http.get("/api/v1/workspaces", ({ request }) => {
    requestedStatus = new URL(request.url).searchParams.get("status") ?? "";
    return HttpResponse.json({ items: [completedWorkspace], total: 1 });
  }));
  renderApp(<App />, "/library");
  expect(await screen.findByText("Tiêu đề video gốc")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Hoàn tất" }));
  await screen.findByText("Tiêu đề video gốc");
  expect(requestedStatus).toBe("completed");
});

it("selects and deletes completed workspaces after confirmation", async () => {
  let items = [completedWorkspace];
  let deletedIds: string[] = [];
  server.use(
    http.get("/api/v1/workspaces", () => HttpResponse.json({ items, total: items.length })),
    http.post("/api/v1/workspaces/bulk-delete", async ({ request }) => {
      const payload = await request.json() as { transcript_job_ids: string[] };
      deletedIds = payload.transcript_job_ids;
      items = [];
      return HttpResponse.json({ deleted_ids: deletedIds });
    }),
  );

  renderApp(<App />, "/library");
  expect(await screen.findByText("Tiêu đề video gốc")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("checkbox", { name: "Chọn workspace Tiêu đề video gốc" }));
  await userEvent.click(screen.getByRole("button", { name: "Xóa (1)" }));
  expect(screen.getByRole("alertdialog", { name: "Xác nhận xóa workspace" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Xác nhận xóa" }));

  expect(deletedIds).toEqual(["source-001"]);
  expect(await screen.findByText("Chưa có nội dung")).toBeInTheDocument();
});
