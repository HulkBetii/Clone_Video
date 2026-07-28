import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from yt_pro_max.app import create_app
from yt_pro_max.errors import PipelineError
from yt_pro_max.models import RewriteStage, TranscriptSource, VideoMetadata
from yt_pro_max.pipeline import PipelineOutput


class FakeTranscriber:
    def health(self):
        return {"loaded": False, "device": "cpu", "cuda_runtime_available": True}


class WorkspaceTranscriptPipeline:
    def __init__(self, settings):
        self.settings = settings
        self.transcriber = FakeTranscriber()

    def process(self, *, job_id, request_url, requested_language, update):
        update("inspecting", 10)
        output_dir = self.settings.jobs_dir / job_id
        output_dir.mkdir(parents=True, exist_ok=True)
        paths = {
            extension: output_dir / f"video.vi.{extension}"
            for extension in ("srt", "txt", "json")
        }
        paths["srt"].write_text(
            "Title: Demo\n\n1\n00:00:00,000 --> 00:00:01,000\nOriginal content.\n",
            encoding="utf-8",
        )
        paths["txt"].write_text(
            "Title: Demo\n\nOriginal transcript content.\n",
            encoding="utf-8",
        )
        paths["json"].write_text("{}\n", encoding="utf-8")
        return PipelineOutput(
            source=TranscriptSource.MANUAL_CAPTION,
            language=requested_language or "vi",
            language_confidence=None,
            video=VideoMetadata(
                id="dQw4w9WgXcQ",
                title="Demo",
                channel="Channel",
                duration_seconds=60,
                webpage_url=request_url,
            ),
            warnings=[],
            artifact_paths=paths,
        )


class WorkspaceRewritePipeline:
    def __init__(self, settings, *, fail_once=False):
        self.settings = settings
        self.fail_once = fail_once
        self.calls = 0
        self.browser_running = False
        self.opened_url = None
        self.closed = False

    async def process(self, job, source_job, update):
        self.calls += 1
        if self.fail_once and self.calls == 1:
            update(
                RewriteStage.PREPARING_SOURCE,
                5,
                0,
                1,
                checkpoint={"analysis_completed": 0},
                conversation_url="https://chatgpt.com/c/workspace",
            )
            raise PipelineError("GPT_LOGIN_REQUIRED", "Manual ChatGPT login is required.")
        update(
            RewriteStage.REWRITING,
            60,
            1,
            1,
            checkpoint={"next_section": 2},
            conversation_url="https://chatgpt.com/c/workspace",
        )
        output_dir = self.settings.rewrite_jobs_dir / job.id
        output_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = output_dir / "rewrite.txt"
        artifact_path.write_text(
            "Title: SEO title\n\nRewritten transcript content with more detail.\n",
            encoding="utf-8",
        )
        return SimpleNamespace(
            artifact_path=artifact_path,
            title="SEO title",
            source_length=100,
            output_length=115,
            sections_total=1,
            sections_completed=1,
            warnings=[],
            conversation_url="https://chatgpt.com/c/workspace",
            checkpoint={"completed": True},
            work_files={},
            validation={
                "passed": True,
                "style_score": 92,
                "coverage_score": 94,
                "language_match": True,
                "tts_ready": True,
                "unsupported_claims": [],
                "missing_points": [],
                "length_ratio": 1.15,
            },
        )

    async def open_browser(self, conversation_url=None):
        self.browser_running = True
        self.opened_url = conversation_url
        return conversation_url or "https://chatgpt.com/"

    async def check_login(self):
        self.browser_running = True
        return True

    async def close_browser(self):
        self.browser_running = False

    async def close(self):
        self.closed = True
        self.browser_running = False

    def health(self):
        return {
            "profile_id": "PROFILE_GPT_1",
            "profile_exists": True,
            "browser_running": self.browser_running,
            "worker_concurrency": 1,
        }


def wait_for_workspace(client, workspace_id, statuses):
    for _ in range(200):
        response = client.get(f"/api/v1/workspaces/{workspace_id}")
        if response.json()["status"] in statuses:
            return response
        time.sleep(0.01)
    raise AssertionError(f"workspace did not reach {statuses}")


def test_workspace_runs_transcript_then_rewrite_and_lists_results(settings):
    rewrite_pipeline = WorkspaceRewritePipeline(settings)
    app = create_app(
        settings,
        pipeline=WorkspaceTranscriptPipeline(settings),
        rewrite_pipeline=rewrite_pipeline,
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/workspaces",
            json={"url": "https://youtu.be/dQw4w9WgXcQ"},
        )
        assert created.status_code == 202
        workspace_id = created.json()["id"]
        completed = wait_for_workspace(client, workspace_id, {"completed"})
        payload = completed.json()

        assert payload["phase"] == "completed"
        assert payload["transcript"]["request_url"].endswith("dQw4w9WgXcQ")
        assert payload["transcript"]["auto_rewrite_requested"] is True
        assert payload["rewrite"]["validation"]["coverage_score"] == 94

        listed = client.get("/api/v1/workspaces", params={"q": "Demo", "status": "completed"})
        assert listed.status_code == 200
        assert listed.json()["total"] == 1
        assert listed.json()["items"][0]["id"] == workspace_id

        cached = client.post(
            "/api/v1/workspaces",
            json={"url": "https://youtu.be/dQw4w9WgXcQ"},
        )
        assert cached.status_code == 200
        assert cached.json()["id"] == workspace_id

    assert rewrite_pipeline.calls == 1


def test_transcript_only_workspace_completes_without_rewrite(settings):
    rewrite_pipeline = WorkspaceRewritePipeline(settings)
    app = create_app(
        settings,
        pipeline=WorkspaceTranscriptPipeline(settings),
        rewrite_pipeline=rewrite_pipeline,
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/workspaces",
            json={
                "url": "https://youtu.be/dQw4w9WgXcQ",
                "auto_rewrite": False,
            },
        )
        completed = wait_for_workspace(client, created.json()["id"], {"completed"})

    assert completed.json()["rewrite"] is None
    assert rewrite_pipeline.calls == 0


def test_waiting_workspace_opens_gpt_and_resumes_same_rewrite(settings):
    rewrite_pipeline = WorkspaceRewritePipeline(settings, fail_once=True)
    app = create_app(
        settings,
        pipeline=WorkspaceTranscriptPipeline(settings),
        rewrite_pipeline=rewrite_pipeline,
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/workspaces",
            json={"url": "https://youtu.be/dQw4w9WgXcQ"},
        )
        workspace_id = created.json()["id"]
        waiting = wait_for_workspace(client, workspace_id, {"waiting_for_user"})
        rewrite_id = waiting.json()["rewrite"]["id"]
        assert waiting.json()["action_required"]["code"] == "GPT_LOGIN_REQUIRED"

        opened = client.post(
            "/api/v1/gpt-runtime/open",
            json={"rewrite_job_id": rewrite_id},
        )
        assert opened.status_code == 200
        checked = client.post("/api/v1/gpt-runtime/check")
        assert checked.json()["status"] == "ready"

        resumed = client.post(f"/api/v1/workspaces/{workspace_id}/resume")
        assert resumed.status_code == 202
        completed = wait_for_workspace(client, workspace_id, {"completed"})
        assert completed.json()["rewrite"]["id"] == rewrite_id
        assert rewrite_pipeline.opened_url == "https://chatgpt.com/c/workspace"

        closed = client.post("/api/v1/gpt-runtime/close")
        assert closed.status_code == 200
        assert closed.json()["browser_running"] is False

    assert rewrite_pipeline.calls == 2


def test_unknown_api_path_is_not_served_by_spa(settings):
    app = create_app(
        settings,
        pipeline=WorkspaceTranscriptPipeline(settings),
        rewrite_pipeline=WorkspaceRewritePipeline(settings),
    )
    with TestClient(app) as client:
        response = client.get("/api/v1/unknown")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


def test_spa_serves_index_and_client_routes(settings):
    app = create_app(
        settings,
        pipeline=WorkspaceTranscriptPipeline(settings),
        rewrite_pipeline=WorkspaceRewritePipeline(settings),
    )
    with TestClient(app) as client:
        index = client.get("/")
        client_route = client.get("/library")

    assert index.status_code == 200
    assert '<div id="root"></div>' in index.text
    assert client_route.status_code == 200
    assert client_route.text == index.text


def test_gpt_runtime_rejects_control_actions_while_worker_is_busy(settings):
    app = create_app(
        settings,
        pipeline=WorkspaceTranscriptPipeline(settings),
        rewrite_pipeline=WorkspaceRewritePipeline(settings),
    )
    with TestClient(app) as client:
        app.state.rewrite_worker._active_job_id = "rewrite-active"
        status = client.get("/api/v1/gpt-runtime")
        closed = client.post("/api/v1/gpt-runtime/close")
        app.state.rewrite_worker._active_job_id = None

    assert status.json()["status"] == "busy"
    assert status.json()["active_job_id"] == "rewrite-active"
    assert closed.status_code == 409
    assert closed.json()["detail"]["code"] == "GPT_BUSY"


def test_bulk_delete_removes_completed_workspace_and_artifacts(settings):
    app = create_app(
        settings,
        pipeline=WorkspaceTranscriptPipeline(settings),
        rewrite_pipeline=WorkspaceRewritePipeline(settings),
    )
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/workspaces",
            json={
                "url": "https://youtu.be/dQw4w9WgXcQ",
                "auto_rewrite": False,
            },
        )
        workspace_id = created.json()["id"]
        wait_for_workspace(client, workspace_id, {"completed"})
        artifact_dir = settings.jobs_dir / workspace_id

        deleted = client.post(
            "/api/v1/workspaces/bulk-delete",
            json={"transcript_job_ids": [workspace_id]},
        )
        missing = client.get(f"/api/v1/workspaces/{workspace_id}")

    assert deleted.status_code == 200
    assert deleted.json() == {"deleted_ids": [workspace_id]}
    assert missing.status_code == 404
    assert not artifact_dir.exists()


def test_bulk_delete_rejects_running_workspace(settings):
    app = create_app(
        settings,
        pipeline=WorkspaceTranscriptPipeline(settings),
        rewrite_pipeline=WorkspaceRewritePipeline(settings),
    )
    with TestClient(app) as client:
        running = app.state.repository.create_job(
            job_id="running-delete",
            cache_key="running-delete",
            request_url="https://www.youtube.com/watch?v=running-delete",
            requested_language=None,
            force_refresh=False,
        )
        app.state.repository.update_job(
            running.id,
            status="running",
            progress=40,
        )
        response = client.post(
            "/api/v1/workspaces/bulk-delete",
            json={"transcript_job_ids": [running.id]},
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "WORKSPACE_BUSY"
    assert app.state.repository.get_job(running.id) is not None
