import { CheckSquare, LibraryBig, Search, SlidersHorizontal, Trash2, X } from "lucide-react";
import { type FormEvent, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { WorkspaceCard } from "../components/WorkspaceCard";
import { EmptyState, ErrorNotice, PageHeading, Skeleton } from "../components/ui";
import { useWorkspaceList } from "../hooks/queries";
import { deleteWorkspaces } from "../lib/api";
import type { Workspace } from "../types";
import styles from "./LibraryPage.module.css";

const filters = [
  { value: "", label: "Tất cả" },
  { value: "running", label: "Đang chạy" },
  { value: "waiting_for_user", label: "Cần xử lý" },
  { value: "completed", label: "Hoàn tất" },
  { value: "failed", label: "Thất bại" },
];

export function LibraryPage() {
  const [filter, setFilter] = useState("");
  const [searchInput, setSearchInput] = useState("");
  const [query, setQuery] = useState("");
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [confirming, setConfirming] = useState(false);
  const queryClient = useQueryClient();
  const workspaces = useWorkspaceList({ status: filter || undefined, q: query || undefined, limit: 50, offset: 0 });
  const visibleWorkspaces = workspaces.data?.items ?? [];
  const selectableIds = visibleWorkspaces.filter(isDeletable).map(workspaceId);
  const allSelected = selectableIds.length > 0 && selectableIds.every((id) => selectedIds.has(id));
  const deleteMutation = useMutation({
    mutationFn: () => deleteWorkspaces([...selectedIds]),
    onSuccess: () => {
      setSelectedIds(new Set());
      setConfirming(false);
      queryClient.invalidateQueries({ queryKey: ["workspaces"] });
    },
  });

  function submitSearch(event: FormEvent) {
    event.preventDefault();
    setSelectedIds(new Set());
    setQuery(searchInput.trim());
  }

  function changeFilter(value: string) {
    setSelectedIds(new Set());
    setFilter(value);
  }

  return (
    <div className="page">
      <PageHeading eyebrow="Archive / 02" title="Thư viện nội dung" description="Theo dõi tất cả video từ transcript đầu tiên đến bản viết lại đã qua kiểm định." />
      <div className={`card ${styles.library}`}>
        <div className={styles.toolbar}>
          <form className={styles.search} onSubmit={submitSearch}>
            <Search size={16} />
            <input aria-label="Tìm workspace" placeholder="Tìm theo tiêu đề hoặc video ID" value={searchInput} onChange={(event) => setSearchInput(event.target.value)} />
          </form>
          <div className={styles.filters} aria-label="Lọc trạng thái"><SlidersHorizontal size={15} />{filters.map((item) => <button key={item.value} type="button" className={filter === item.value ? styles.active : ""} onClick={() => changeFilter(item.value)}>{item.label}</button>)}</div>
        </div>
        <div className={styles.count}>
          <span>{workspaces.data?.total ?? 0} workspace</span>
          <div className={styles.selectionTools}>
            {selectableIds.length > 0 && <button type="button" onClick={() => setSelectedIds(allSelected ? new Set() : new Set(selectableIds))}><CheckSquare size={13} />{allSelected ? "Bỏ chọn" : "Chọn tất cả"}</button>}
            {selectedIds.size > 0 && <button type="button" className={styles.deleteButton} onClick={() => setConfirming(true)}><Trash2 size={13} />Xóa ({selectedIds.size})</button>}
            {query && <button type="button" onClick={() => { setQuery(""); setSearchInput(""); }}><X size={13} />Xóa tìm kiếm</button>}
          </div>
        </div>
        {confirming && <div className={styles.confirmation} role="alertdialog" aria-label="Xác nhận xóa workspace"><div><strong>Xóa {selectedIds.size} workspace?</strong><p>Transcript, bản viết lại và artifact liên quan sẽ bị xóa khỏi máy.</p></div><div className={styles.confirmationActions}><button type="button" className="button secondary" onClick={() => setConfirming(false)} disabled={deleteMutation.isPending}>Hủy</button><button type="button" className="button coral" onClick={() => deleteMutation.mutate()} disabled={deleteMutation.isPending}>{deleteMutation.isPending ? "Đang xóa..." : "Xác nhận xóa"}</button></div></div>}
        {deleteMutation.isError && <div className={styles.notice}><ErrorNotice message={deleteMutation.error.message} /></div>}
        {workspaces.isLoading && <div className={styles.loading}><Skeleton height={130} /><Skeleton height={130} /><Skeleton height={130} /></div>}
        {workspaces.isError && <div className={styles.notice}><ErrorNotice message={workspaces.error.message} /></div>}
        {workspaces.data?.items.length === 0 && <EmptyState icon={<LibraryBig />} title="Chưa có nội dung">Tạo workspace đầu tiên để lịch sử xử lý xuất hiện tại đây.</EmptyState>}
        {visibleWorkspaces.map((workspace) => <WorkspaceCard key={workspace.id} workspace={workspace} selectable={isDeletable(workspace)} selected={selectedIds.has(workspace.id)} onSelect={(selected) => setSelectedIds((current) => { const next = new Set(current); if (selected) next.add(workspace.id); else next.delete(workspace.id); return next; })} />)}
      </div>
    </div>
  );
}

function workspaceId(workspace: Workspace) {
  return workspace.id;
}

function isDeletable(workspace: Workspace) {
  return workspace.status !== "queued" && workspace.status !== "running";
}
