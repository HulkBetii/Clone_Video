import { Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "./components/AppShell";
import { CreatePage } from "./pages/CreatePage";
import { LibraryPage } from "./pages/LibraryPage";
import { SystemPage } from "./pages/SystemPage";
import { WorkspacePage } from "./pages/WorkspacePage";

export function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<CreatePage />} />
        <Route path="library" element={<LibraryPage />} />
        <Route path="system" element={<SystemPage />} />
        <Route path="workspaces/:workspaceId" element={<WorkspacePage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
