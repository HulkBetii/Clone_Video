import { useMutation } from "@tanstack/react-query";
import { ArrowRight, Captions, RefreshCw, Sparkles, WandSparkles } from "lucide-react";
import { type FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";

import { ErrorNotice, PageHeading } from "../components/ui";
import { ApiError, createWorkspace } from "../lib/api";
import { localizedErrorMessage } from "../lib/presentation";
import styles from "./CreatePage.module.css";

export function CreatePage() {
  const navigate = useNavigate();
  const [url, setUrl] = useState("");
  const [language, setLanguage] = useState("");
  const [autoRewrite, setAutoRewrite] = useState(true);
  const [forceRefresh, setForceRefresh] = useState(false);
  const createMutation = useMutation({
    mutationFn: createWorkspace,
    onSuccess: (workspace) => navigate(`/workspaces/${workspace.id}`),
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    createMutation.mutate({ url: url.trim(), language: language.trim() || undefined, auto_rewrite: autoRewrite, force_refresh: forceRefresh });
  }

  const error = createMutation.error instanceof ApiError ? createMutation.error : null;

  return (
    <div className="page">
      <PageHeading eyebrow="Production desk / 01" title="Biến một video thành kịch bản mới." description="Dán đường dẫn YouTube. Hệ thống lấy transcript, giữ lại ngôn ngữ gốc và có thể tự viết lại bằng phiên ChatGPT trên máy của bạn." />
      <div className={styles.layout}>
        <form className={`card ${styles.form}`} onSubmit={submit}>
          <div className={styles.formTop}><WandSparkles size={21} /><span>Khởi tạo workspace</span></div>
          <div className="field">
            <label htmlFor="video-url">Link video YouTube</label>
            <input id="video-url" className="input" type="url" required autoFocus placeholder="https://www.youtube.com/watch?v=..." value={url} onChange={(event) => setUrl(event.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="language">Ngôn ngữ ưu tiên <span className={styles.optional}>không bắt buộc</span></label>
            <input id="language" className="input" placeholder="ja, vi, en-US..." maxLength={20} value={language} onChange={(event) => setLanguage(event.target.value)} />
            <small className={styles.hint}>Để trống để hệ thống tự chọn ngôn ngữ gốc. Tiếng Nhật và Hàn dùng audio-first khi chỉ có auto-caption.</small>
          </div>
          <fieldset className={styles.modeGroup}>
            <legend>Luồng xử lý</legend>
            <label className={`${styles.mode} ${autoRewrite ? styles.selected : ""}`}>
              <input type="radio" name="mode" checked={autoRewrite} onChange={() => setAutoRewrite(true)} />
              <span className={styles.modeIcon}><Sparkles size={19} /></span>
              <span><strong>Transcript + GPT</strong><small>Tự lấy nội dung và viết lại hoàn chỉnh</small></span>
              <span className={styles.recommended}>Mặc định</span>
            </label>
            <label className={`${styles.mode} ${!autoRewrite ? styles.selected : ""}`}>
              <input type="radio" name="mode" checked={!autoRewrite} onChange={() => setAutoRewrite(false)} />
              <span className={styles.modeIcon}><Captions size={19} /></span>
              <span><strong>Chỉ transcript</strong><small>Dừng sau khi xuất TXT, SRT và JSON</small></span>
            </label>
          </fieldset>
          <label className={styles.checkbox}>
            <input type="checkbox" checked={forceRefresh} onChange={(event) => setForceRefresh(event.target.checked)} />
            <RefreshCw size={15} /><span>Bỏ qua cache và xử lý lại từ đầu</span>
          </label>
          {error && <ErrorNotice message={localizedErrorMessage(error.code, error.message)} code={error.code} />}
          <button className="button coral" type="submit" disabled={createMutation.isPending || !url.trim()}>
            {createMutation.isPending ? "Đang khởi tạo..." : "Bắt đầu xử lý"}<ArrowRight size={17} />
          </button>
        </form>
        <aside className={styles.aside}>
          <div className={styles.step}><span>01</span><div><strong>Lấy nội dung gốc</strong><p>Ưu tiên caption thủ công. Auto-caption Nhật/Hàn được thay bằng Whisper và đối chiếu bảo thủ.</p></div></div>
          <div className={styles.connector} />
          <div className={styles.step}><span>02</span><div><strong>Phân tích & viết lại</strong><p>Giữ giọng điệu, độ phủ ý và độ dài phù hợp cho TTS.</p></div></div>
          <div className={styles.connector} />
          <div className={styles.step}><span>03</span><div><strong>So sánh & xuất bản</strong><p>Đối chiếu hai bản, sao chép body hoặc tải artifact.</p></div></div>
          <div className={styles.note}>Browser ChatGPT sẽ mở riêng khi đến bước GPT. Không có mật khẩu hay cookie nào được sao chép vào ứng dụng.</div>
        </aside>
      </div>
    </div>
  );
}
