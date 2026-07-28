from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from yt_pro_max.config import Settings
from yt_pro_max.errors import PipelineError
from yt_pro_max.gpt_playwright import ChatGPTResponse
from yt_pro_max.models import JobStatus
from yt_pro_max.repository import StoredJob
from yt_pro_max.rewrite_content import RewriteBrief
from yt_pro_max.rewrite_pipeline import CHATGPT_URL, RewritePipeline, _EditGroup
from yt_pro_max.rewrite_repository import StoredRewriteJob


class FakeGPT:
    def __init__(self, rewritten_body: str) -> None:
        self.rewritten_body = rewritten_body
        self.calls: list[dict[str, object]] = []
        self.closed = False

    async def run_prompt(
        self,
        prompt: str,
        *,
        attachment=None,
        conversation_url=None,
        request_id=None,
    ) -> ChatGPTResponse:
        self.calls.append(
            {
                "prompt": prompt,
                "attachment": attachment,
                "conversation_url": conversation_url,
                "request_id": request_id,
            }
        )
        if ":analysis:" in request_id:
            payload = {
                "style_profile": {"voice": "calm", "pacing": "steady"},
                "content_summary": "The source explains one central idea.",
                "required_points": ["central idea"],
                "sponsor_sections": [],
                "generic_cta_present": True,
            }
        elif request_id.endswith(":outline"):
            payload = {
                "global_style_profile": {"voice": "calm", "pacing": "steady"},
                "total_target_length": 110,
                "sections": [
                    {
                        "id": 1,
                        "purpose": "Explain the idea",
                        "required_points": ["central idea"],
                        "target_length": 110,
                    }
                ],
            }
        elif ":rewrite:" in request_id or ":edit:" in request_id or ":repair:" in request_id:
            payload = {"body": self.rewritten_body}
        elif ":seam:" in request_id and ":validate:" not in request_id:
            payload = {
                "previous_tail": self.rewritten_body,
                "next_head": self.rewritten_body,
            }
        elif ":validate:" in request_id:
            payload = {
                "passed": True,
                "language_match": True,
                "style_score": 92,
                "coverage_score": 94,
                "tts_ready": True,
                "unsupported_claims": [],
                "missing_points": [],
                "targeted_repairs": [],
            }
        elif ":title:" in request_id:
            payload = {"title": "A Better SEO Title"}
        else:
            raise AssertionError(f"Unexpected request: {request_id}")
        return ChatGPTResponse(
            text=json.dumps(payload),
            conversation_url="https://chatgpt.com/c/rewrite-demo",
        )

    async def close(self) -> None:
        self.closed = True


class RejectingGPT:
    async def run_prompt(self, *_args, **_kwargs):
        raise AssertionError("GPT should not be called when all checkpoints exist")

    async def close(self) -> None:
        return None


class InvalidGPT:
    async def run_prompt(self, *_args, **_kwargs) -> ChatGPTResponse:
        return ChatGPTResponse(
            text="This is not the required JSON.",
            conversation_url="https://chatgpt.com/c/invalid",
        )

    async def close(self) -> None:
        return None


class CrashingGPT:
    def __init__(self) -> None:
        self.close_calls = 0

    async def run_prompt(self, *_args, **_kwargs):
        raise PipelineError(
            "GPT_BROWSER_CRASHED",
            "Browser crashed.",
            retryable=True,
        )

    async def close(self) -> None:
        self.close_calls += 1


class InvalidValidatorGPT(FakeGPT):
    async def run_prompt(self, prompt: str, **kwargs) -> ChatGPTResponse:
        request_id = str(kwargs.get("request_id"))
        if ":validate:" not in request_id:
            return await super().run_prompt(prompt, **kwargs)
        self.calls.append({"prompt": prompt, **kwargs})
        return ChatGPTResponse(
            text="Invalid validator response",
            conversation_url="https://chatgpt.com/c/validator-persisted",
        )


class MultiSectionGPT(FakeGPT):
    async def run_prompt(self, prompt: str, **kwargs) -> ChatGPTResponse:
        request_id = str(kwargs.get("request_id"))
        if request_id.endswith(":outline"):
            self.calls.append({"prompt": prompt, **kwargs})
            payload = {
                "global_style_profile": {"voice": "calm", "pacing": "steady"},
                "total_target_length": 110,
                "sections": [
                    {"id": 1, "required_points": ["first"], "target_length": 55},
                    {"id": 2, "required_points": ["second"], "target_length": 55},
                ],
            }
            return ChatGPTResponse(
                text=json.dumps(payload),
                conversation_url="https://chatgpt.com/c/rewrite-demo",
            )
        if ":rewrite:1" in request_id or ":edit:1" in request_id:
            self.rewritten_body = "b" * 55
        elif ":rewrite:2" in request_id or ":edit:2" in request_id:
            self.rewritten_body = "d" * 55
        elif ":seam:" in request_id and ":validate:" not in request_id:
            self.calls.append({"prompt": prompt, **kwargs})
            return ChatGPTResponse(
                text=json.dumps(
                    {
                        "previous_tail": "b" * 55,
                        "next_head": "d" * 55,
                    }
                ),
                conversation_url="https://chatgpt.com/c/rewrite-demo",
            )
        return await super().run_prompt(prompt, **kwargs)


class RepairingMultiSectionGPT(MultiSectionGPT):
    async def run_prompt(self, prompt: str, **kwargs) -> ChatGPTResponse:
        request_id = str(kwargs.get("request_id"))
        if request_id.endswith(":validate:1:0"):
            self.calls.append({"prompt": prompt, **kwargs})
            return ChatGPTResponse(
                text=json.dumps(
                    {
                        "passed": False,
                        "language_match": True,
                        "style_score": 70,
                        "coverage_score": 70,
                        "tts_ready": True,
                        "unsupported_claims": [],
                        "missing_points": ["first"],
                        "targeted_repairs": ["Restore the first point"],
                    }
                ),
                conversation_url="https://chatgpt.com/c/validator-demo",
            )
        return await super().run_prompt(prompt, **kwargs)


class ValidationGPT(FakeGPT):
    def __init__(self, rewritten_body: str, verdict_changes: dict[str, object]) -> None:
        super().__init__(rewritten_body)
        self.verdict_changes = verdict_changes

    async def run_prompt(self, prompt: str, **kwargs) -> ChatGPTResponse:
        request_id = str(kwargs.get("request_id"))
        if ":validate:" not in request_id:
            return await super().run_prompt(prompt, **kwargs)
        self.calls.append({"prompt": prompt, **kwargs})
        payload = {
            "passed": True,
            "language_match": True,
            "style_score": 92,
            "coverage_score": 94,
            "tts_ready": True,
            "unsupported_claims": [],
            "missing_points": [],
            "targeted_repairs": [],
            **self.verdict_changes,
        }
        return ChatGPTResponse(
            text=json.dumps(payload),
            conversation_url="https://chatgpt.com/c/validator-demo",
        )


class LengthRepairGPT(FakeGPT):
    async def run_prompt(self, prompt: str, **kwargs) -> ChatGPTResponse:
        if ":repair:" in str(kwargs.get("request_id")):
            self.rewritten_body = "b" * 110
        return await super().run_prompt(prompt, **kwargs)


class ShrinkingRepairGPT(FakeGPT):
    async def run_prompt(self, prompt: str, **kwargs) -> ChatGPTResponse:
        request_id = str(kwargs.get("request_id"))
        if ":validate:" in request_id:
            self.calls.append({"prompt": prompt, **kwargs})
            return ChatGPTResponse(
                text=json.dumps(
                    {
                        "passed": False,
                        "language_match": True,
                        "style_score": 70,
                        "coverage_score": 70,
                        "tts_ready": True,
                        "unsupported_claims": [],
                        "missing_points": ["central idea"],
                        "targeted_repairs": ["Restore the central idea"],
                    }
                ),
                conversation_url="https://chatgpt.com/c/validator-demo",
            )
        if ":repair:" in request_id:
            self.rewritten_body = "b" * 90
        return await super().run_prompt(prompt, **kwargs)


def _source_job(source_path: Path) -> StoredJob:
    return StoredJob(
        id="transcript-job",
        cache_key="source-cache",
        request_url="https://youtube.com/watch?v=demo",
        auto_rewrite_requested=False,
        requested_language=None,
        force_refresh=False,
        status=JobStatus.COMPLETED,
        stage=None,
        progress=100,
        source=None,
        actual_language="en",
        language_confidence=None,
        video={
            "id": "demo",
            "title": "Source title",
            "channel": "Channel",
            "duration_seconds": 10,
            "webpage_url": "https://youtube.com/watch?v=demo",
        },
        artifact_paths={"txt": str(source_path)},
        warnings=[],
        error=None,
        cached=False,
        created_at="2026-07-27T00:00:00+00:00",
        updated_at="2026-07-27T00:00:00+00:00",
    )


def _rewrite_job(source_path: Path) -> StoredRewriteJob:
    return StoredRewriteJob(
        id="rewrite-job",
        transcript_job_id="transcript-job",
        source_hash=sha256(source_path.read_bytes()).hexdigest(),
        cache_key="rewrite-cache",
        force_refresh=False,
        status=JobStatus.QUEUED,
        stage=None,
        progress=0,
        source_language="en",
        source_length=None,
        output_length=None,
        sections_completed=0,
        sections_total=0,
        title=None,
        artifact_path=None,
        conversation_url=None,
        checkpoint=None,
        work_files=None,
        warnings=[],
        error=None,
        cached=False,
        created_at="2026-07-27T00:00:00+00:00",
        updated_at="2026-07-27T00:00:00+00:00",
    )


@pytest.mark.asyncio
async def test_rewrite_pipeline_generates_valid_txt_and_checkpoints(settings, tmp_path: Path):
    source_path = settings.jobs_dir / "source-job" / "source.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(f"Title: Source title\n\n{'a' * 100}\n", encoding="utf-8")
    fake_gpt = FakeGPT("b" * 110)
    pipeline = RewritePipeline(settings, gpt=fake_gpt)
    updates: list[tuple] = []

    result = await pipeline.process(
        _rewrite_job(source_path),
        _source_job(source_path),
        lambda *args, **kwargs: updates.append((args, kwargs)),
    )

    assert result.title == "A Better SEO Title"
    assert result.source_length == 100
    assert result.output_length == 110
    assert result.sections_total == 1
    assert result.artifact_path.read_text(encoding="utf-8") == (
        f"Title: A Better SEO Title\n\n{'b' * 110}\n"
    )
    assert result.checkpoint["completed"] is True
    assert result.validation == {
        "passed": True,
        "style_score": 92.0,
        "coverage_score": 94.0,
        "language_match": True,
        "tts_ready": True,
        "unsupported_claims": [],
        "missing_points": [],
        "length_ratio": 1.1,
    }
    assert any(call["attachment"] for call in fake_gpt.calls)
    assert any(args[0].value == "validating" for args, _kwargs in updates)
    edit_call = next(call for call in fake_gpt.calls if ":edit:" in str(call["request_id"]))
    validation_call = next(
        call for call in fake_gpt.calls if ":validate:" in str(call["request_id"])
    )
    assert edit_call["attachment"] is not None
    assert "b" * 110 not in str(edit_call["prompt"])
    assert validation_call["attachment"] is not None
    assert "a" * 100 not in str(validation_call["prompt"])
    assert "b" * 110 not in str(validation_call["prompt"])

    await pipeline.close()
    assert fake_gpt.closed


@pytest.mark.asyncio
async def test_length_repair_prompt_includes_exact_bounds(settings):
    source_path = settings.jobs_dir / "source-job" / "source.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(f"Title: Source title\n\n{'a' * 100}\n", encoding="utf-8")
    fake_gpt = LengthRepairGPT("b" * 90)
    pipeline = RewritePipeline(settings, gpt=fake_gpt)

    result = await pipeline.process(
        _rewrite_job(source_path),
        _source_job(source_path),
        lambda *_args, **_kwargs: None,
    )

    repair_call = next(
        call for call in fake_gpt.calls if ":repair:" in str(call["request_id"])
    )
    assert repair_call["attachment"] is not None
    assert "from 90 to at least 100 normalized characters (target about 110)" in str(
        repair_call["prompt"]
    )
    assert repair_call["conversation_url"] == CHATGPT_URL
    repair_text = Path(repair_call["attachment"]).read_text(encoding="utf-8")
    assert repair_text.strip() == "b" * 90
    assert result.output_length == 110


@pytest.mark.asyncio
async def test_repair_resume_does_not_chain_shrinking_candidates(settings):
    source_path = settings.jobs_dir / "source-job" / "source.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(f"Title: Source title\n\n{'a' * 100}\n", encoding="utf-8")
    fake_gpt = ShrinkingRepairGPT("b" * 110)
    pipeline = RewritePipeline(settings, gpt=fake_gpt)

    with pytest.raises(PipelineError) as error:
        await pipeline.process(
            _rewrite_job(source_path),
            _source_job(source_path),
            lambda *_args, **_kwargs: None,
        )

    assert error.value.info.code == "STYLE_VALIDATION_FAILED"
    repair_input = (
        settings.rewrite_temp_dir / "rewrite-job" / "repair-input-001-01.txt"
    ).read_text(encoding="utf-8")
    assert "b" * 110 in repair_input


def test_validation_summary_uses_minimum_scores_and_unions_issues(settings):
    pipeline = RewritePipeline(settings, gpt=RejectingGPT())

    summary = pipeline._aggregate_validation(
        [
            {
                "passed": True,
                "style_score": 95,
                "coverage_score": 91,
                "language_match": True,
                "tts_ready": True,
                "unsupported_claims": ["claim one"],
                "missing_points": [],
            },
            {
                "passed": False,
                "style_score": 88,
                "coverage_score": 93,
                "language_match": False,
                "tts_ready": True,
                "unsupported_claims": ["claim one", "claim two"],
                "missing_points": ["point one"],
            },
        ],
        source_length=100,
        output_length=115,
    )

    assert summary == {
        "passed": False,
        "style_score": 88.0,
        "coverage_score": 91.0,
        "language_match": False,
        "tts_ready": True,
        "unsupported_claims": ["claim one", "claim two"],
        "missing_points": ["point one"],
        "length_ratio": 1.15,
    }


def test_validation_boolean_parser_accepts_common_model_scalar_variants(settings):
    pipeline = RewritePipeline(settings, gpt=RejectingGPT())

    assert pipeline._as_bool(True, "passed") is True
    assert pipeline._as_bool(0, "passed") is False
    assert pipeline._as_bool("合格", "passed") is True
    assert pipeline._as_bool("不一致", "language_match") is False

    with pytest.raises(PipelineError) as error:
        pipeline._as_bool("conditionally passed", "passed")
    assert error.value.info.code == "GPT_OUTPUT_INVALID"
    assert error.value.info.details == {"field": "passed", "received_type": "str"}


@pytest.mark.asyncio
async def test_validation_resume_uses_persisted_repaired_body(settings):
    work_dir = settings.rewrite_temp_dir / "rewrite-job"
    work_dir.mkdir(parents=True)
    source_path = settings.jobs_dir / "source-job" / "source.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("Title: Source\n\n" + "a" * 100, encoding="utf-8")
    repaired_body = "b" * 110
    (work_dir / "repaired-001.txt").write_text(repaired_body, encoding="utf-8")
    fake_gpt = FakeGPT("unused")
    pipeline = RewritePipeline(settings, gpt=fake_gpt)
    groups = [_EditGroup(indexes=[0], source="a" * 100, draft="b" * 90, body="b" * 90)]
    brief = RewriteBrief(source_language="en", source_title="Source", source_length=100)

    validated, _validator_url, _writer_url, _records = await pipeline._validate_groups(
        _rewrite_job(source_path),
        brief,
        groups,
        {"voice": "calm"},
        {"sections": [{"id": 1}]},
        work_dir,
        {},
        CHATGPT_URL,
        lambda *_args, **_kwargs: None,
    )

    assert validated[0].body == repaired_body
    assert not any(":repair:" in str(call["request_id"]) for call in fake_gpt.calls)


@pytest.mark.asyncio
async def test_exhausted_browser_failure_closes_stale_runtime(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", gpt_retries=1)
    gpt = CrashingGPT()
    pipeline = RewritePipeline(settings, gpt=gpt)

    with pytest.raises(PipelineError) as error:
        await pipeline._run_gpt(
            "prompt",
            conversation_url=None,
            request_id=None,
        )

    assert error.value.info.code == "GPT_BROWSER_CRASHED"
    assert gpt.close_calls == 1


@pytest.mark.asyncio
async def test_rewrite_pipeline_resumes_from_persisted_work_files(settings, tmp_path: Path):
    source_path = settings.jobs_dir / "source-job" / "source.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(f"Title: Source title\n\n{'a' * 100}\n", encoding="utf-8")
    initial = RewritePipeline(settings, gpt=FakeGPT("b" * 110))
    first_result = await initial.process(
        _rewrite_job(source_path),
        _source_job(source_path),
        lambda *_args, **_kwargs: None,
    )
    resumed_job = replace(
        _rewrite_job(source_path),
        status=JobStatus.RUNNING,
        checkpoint=first_result.checkpoint,
        conversation_url=first_result.conversation_url,
        work_files=first_result.work_files,
    )
    resumed = RewritePipeline(settings, gpt=RejectingGPT())

    second_result = await resumed.process(
        resumed_job,
        _source_job(source_path),
        lambda *_args, **_kwargs: None,
    )

    assert second_result.output_length == 110
    assert second_result.artifact_path.is_file()


@pytest.mark.asyncio
async def test_rewrite_pipeline_uses_json_segments_for_long_source(tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "data", rewrite_chunk_max_chars=50)
    source_path = settings.jobs_dir / "source-job" / "source.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        f"Title: Source title\n\n{'a' * 50} {'c' * 50}\n",
        encoding="utf-8",
    )
    json_path = source_path.parent / "source.json"
    json_path.write_text(
        json.dumps(
            {
                "segments": [
                    {"text": "a" * 50},
                    {"text": "c" * 50},
                ]
            }
        ),
        encoding="utf-8",
    )
    source_job = replace(
        _source_job(source_path),
        artifact_paths={"txt": str(source_path), "json": str(json_path)},
    )
    fake_gpt = RepairingMultiSectionGPT("unused")
    pipeline = RewritePipeline(settings, gpt=fake_gpt)

    result = await pipeline.process(
        _rewrite_job(source_path),
        source_job,
        lambda *_args, **_kwargs: None,
    )

    assert result.sections_total == 2
    assert result.source_length == 100
    assert result.output_length == 110
    analysis_calls = [call for call in fake_gpt.calls if ":analysis:" in call["request_id"]]
    assert len(analysis_calls) == 2
    seam_calls = [
        call
        for call in fake_gpt.calls
        if ":seam:" in call["request_id"] and ":validate:" not in call["request_id"]
    ]
    assert len(seam_calls) == 1
    seam_validation_calls = [
        call for call in fake_gpt.calls if ":validate:seam:" in call["request_id"]
    ]
    assert len(seam_validation_calls) == 1
    request_ids = [str(call["request_id"]) for call in fake_gpt.calls]
    assert request_ids.index("rewrite-job:repair:1:0") < request_ids.index("rewrite-job:seam:1:0")
    repair_call = next(
        call for call in fake_gpt.calls if call["request_id"] == "rewrite-job:repair:1:0"
    )
    assert repair_call["attachment"] is not None
    assert "b" * 55 not in str(repair_call["prompt"])
    assert result.checkpoint["seams_total"] == 1
    assert result.checkpoint["seams_completed"] == 1

    resumed = RewritePipeline(settings, gpt=RejectingGPT())
    resumed_result = await resumed.process(
        replace(
            _rewrite_job(source_path),
            status=JobStatus.RUNNING,
            checkpoint={**result.checkpoint, "seams_completed": 0},
            conversation_url=result.conversation_url,
            work_files=result.work_files,
        ),
        source_job,
        lambda *_args, **_kwargs: None,
    )
    assert resumed_result.output_length == 110


@pytest.mark.parametrize(
    "verdict_changes",
    [
        {"language_match": False},
        {"unsupported_claims": ["Invented statistic"]},
        {"missing_points": ["Central idea"]},
    ],
)
@pytest.mark.asyncio
async def test_rewrite_pipeline_enforces_validation_contract(
    tmp_path: Path, verdict_changes: dict[str, object]
):
    settings = Settings(data_dir=tmp_path / "data", rewrite_repair_attempts=0)
    source_path = settings.jobs_dir / "source-job" / "source.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(f"Title: Source title\n\n{'a' * 100}\n", encoding="utf-8")
    pipeline = RewritePipeline(settings, gpt=ValidationGPT("b" * 110, verdict_changes))

    with pytest.raises(PipelineError) as error:
        await pipeline.process(
            _rewrite_job(source_path),
            _source_job(source_path),
            lambda *_args, **_kwargs: None,
        )

    assert error.value.info.code == "STYLE_VALIDATION_FAILED"


@pytest.mark.asyncio
async def test_rewrite_pipeline_rejects_source_path_outside_jobs_dir(settings, tmp_path: Path):
    source_path = tmp_path / "outside.txt"
    source_path.write_text("Title: Source title\n\nSource body.\n", encoding="utf-8")
    pipeline = RewritePipeline(settings, gpt=RejectingGPT())

    with pytest.raises(PipelineError) as error:
        await pipeline.process(
            _rewrite_job(source_path),
            _source_job(source_path),
            lambda *_args, **_kwargs: None,
        )

    assert error.value.info.code == "SOURCE_ARTIFACT_MISSING"


@pytest.mark.asyncio
async def test_rewrite_pipeline_classifies_malformed_gpt_response(settings):
    source_path = settings.jobs_dir / "source-job" / "source.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("Title: Source title\n\nSource body.\n", encoding="utf-8")
    pipeline = RewritePipeline(settings, gpt=InvalidGPT())

    with pytest.raises(PipelineError) as error:
        await pipeline.process(
            _rewrite_job(source_path),
            _source_job(source_path),
            lambda *_args, **_kwargs: None,
        )

    assert error.value.info.code == "GPT_OUTPUT_INVALID"


@pytest.mark.asyncio
async def test_rewrite_pipeline_persists_validator_url_before_parsing(settings):
    source_path = settings.jobs_dir / "source-job" / "source.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(f"Title: Source title\n\n{'a' * 100}\n", encoding="utf-8")
    updates: list[tuple] = []
    pipeline = RewritePipeline(settings, gpt=InvalidValidatorGPT("b" * 110))

    with pytest.raises(PipelineError) as error:
        await pipeline.process(
            _rewrite_job(source_path),
            _source_job(source_path),
            lambda *args, **kwargs: updates.append((args, kwargs)),
        )

    assert error.value.info.code == "GPT_OUTPUT_INVALID"
    assert any(
        kwargs.get("checkpoint", {}).get("validator_url")
        == "https://chatgpt.com/c/validator-persisted"
        for _args, kwargs in updates
    )


@pytest.mark.asyncio
async def test_rewrite_pipeline_does_not_recover_stale_validator_request(settings):
    source_path = settings.jobs_dir / "source-job" / "source.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(f"Title: Source title\n\n{'a' * 100}\n", encoding="utf-8")
    fake_gpt = FakeGPT("b" * 110)
    job = replace(
        _rewrite_job(source_path),
        checkpoint={"validator_url": "https://chatgpt.com/c/stale-validator"},
    )
    pipeline = RewritePipeline(settings, gpt=fake_gpt)

    await pipeline.process(
        job,
        _source_job(source_path),
        lambda *_args, **_kwargs: None,
    )

    validation_call = next(
        call for call in fake_gpt.calls if ":validate:" in str(call["request_id"])
    )
    assert validation_call["conversation_url"] == CHATGPT_URL


@pytest.mark.asyncio
async def test_rewrite_pipeline_rejects_source_changed_after_job_creation(settings):
    source_path = settings.jobs_dir / "source-job" / "source.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("Title: Source title\n\nOriginal body.\n", encoding="utf-8")
    job = _rewrite_job(source_path)
    source_path.write_text("Title: Source title\n\nChanged body.\n", encoding="utf-8")
    pipeline = RewritePipeline(settings, gpt=RejectingGPT())

    with pytest.raises(PipelineError) as error:
        await pipeline.process(
            job,
            _source_job(source_path),
            lambda *_args, **_kwargs: None,
        )

    assert error.value.info.code == "SOURCE_CHANGED"
