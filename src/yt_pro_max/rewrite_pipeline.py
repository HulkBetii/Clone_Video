from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yt_pro_max.config import Settings
from yt_pro_max.errors import PipelineError
from yt_pro_max.gpt_playwright import CHATGPT_URL, ChatGPTPlaywrightAdapter, ChatGPTResponse
from yt_pro_max.models import RewriteStage
from yt_pro_max.repository import StoredJob
from yt_pro_max.rewrite_content import (
    LengthPolicy,
    RewriteBrief,
    RewriteContentError,
    SectionRewriteContext,
    build_final_edit_prompt,
    build_outline_prompt,
    build_seam_edit_prompt,
    build_section_rewrite_prompt,
    build_style_analysis_prompt,
    build_title_prompt,
    build_validation_prompt,
    normalized_length,
    parse_structured_response,
    parse_transcript_txt,
    remove_duplicate_transitions,
    render_rewrite_txt_atomic,
    semantic_chunks,
    validate_rewrite,
)
from yt_pro_max.rewrite_repository import StoredRewriteJob

ProgressCallback = Callable[..., Any]
SEAM_CONTEXT_CHARS = 2_000
SEAM_MIN_LENGTH_RATIO = 0.85
SEAM_MAX_LENGTH_RATIO = 1.15
SEAM_SENTENCE_BOUNDARY_PATTERN = re.compile(r"[.!?。！？](?=\s)")


@dataclass(frozen=True)
class RewritePipelineOutput:
    artifact_path: Path
    title: str
    source_length: int
    output_length: int
    sections_total: int
    sections_completed: int
    warnings: list[str]
    conversation_url: str | None
    checkpoint: dict[str, Any]
    work_files: dict[str, str]


@dataclass
class _EditGroup:
    indexes: list[int]
    source: str
    draft: str
    body: str = ""


@dataclass(frozen=True)
class _ValidationDecision:
    passed: bool
    language_match: bool
    unsupported_claims: list[str]
    missing_points: list[str]
    repairs: list[str]


class RewritePipeline:
    def __init__(
        self,
        settings: Settings,
        *,
        gpt: ChatGPTPlaywrightAdapter | None = None,
    ) -> None:
        self.settings = settings
        self.gpt = gpt or ChatGPTPlaywrightAdapter(
            settings.gpt_profile_dir,
            response_timeout_s=settings.gpt_reply_timeout_seconds,
            attachment_timeout_s=settings.gpt_attachment_timeout_seconds,
        )
        self.length_policy = LengthPolicy(
            target_ratio=settings.rewrite_target_ratio,
            min_ratio=settings.rewrite_min_ratio,
            max_ratio=settings.rewrite_max_ratio,
        )

    async def close(self) -> None:
        await self.gpt.close()

    def health(self) -> dict[str, Any]:
        return {
            "profile_id": self.settings.gpt_profile_id,
            "profile_exists": self.settings.gpt_profile_dir.is_dir(),
            "browser_running": getattr(self.gpt, "_context", None) is not None,
            "worker_concurrency": 1,
        }

    async def process(
        self,
        job: StoredRewriteJob,
        source_job: StoredJob,
        update: ProgressCallback,
    ) -> RewritePipelineOutput:
        self.settings.ensure_directories()
        source_path = self._source_path(source_job)
        if hashlib.sha256(source_path.read_bytes()).hexdigest() != job.source_hash:
            raise PipelineError(
                "SOURCE_CHANGED",
                "The source transcript changed after the rewrite job was created.",
            )
        try:
            document = parse_transcript_txt(source_path.read_text(encoding="utf-8-sig"))
        except RewriteContentError as error:
            raise PipelineError(error.code, str(error)) from error
        except (OSError, UnicodeError) as error:
            raise PipelineError(
                "SOURCE_EMPTY", "The source transcript could not be read."
            ) from error

        source_language = source_job.actual_language
        if not source_language:
            raise PipelineError("SOURCE_LANGUAGE_MISSING", "The source language is unavailable.")

        source_length = normalized_length(document.body)
        brief = RewriteBrief(
            source_language=source_language,
            source_title=document.title,
            source_length=source_length,
            length_policy=self.length_policy,
        )
        chunks = self._source_chunks(source_job, document.body)
        if not chunks:
            raise PipelineError("SOURCE_EMPTY", "The source transcript contains no usable text.")

        work_dir = self._job_dir(self.settings.rewrite_temp_dir, job.id)
        output_dir = self._job_dir(self.settings.rewrite_jobs_dir, job.id)
        work_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        checkpoint = dict(job.checkpoint or {})
        writer_url = job.conversation_url or CHATGPT_URL
        chunk_paths = self._write_source_chunks(work_dir, document.title, chunks)
        self._update(
            update,
            RewriteStage.PREPARING_SOURCE,
            5,
            checkpoint,
            writer_url,
            work_dir,
            completed=checkpoint.get("sections_completed", 0),
            total=len(chunks),
        )

        analyses, writer_url = await self._analyze_chunks(
            job,
            brief,
            chunks,
            chunk_paths,
            work_dir,
            checkpoint,
            writer_url,
            update,
        )
        style_profile = self._combined_style_profile(analyses)
        summaries = [
            self._analysis_summary(index, analysis) for index, analysis in enumerate(analyses, 1)
        ]

        outline_path = work_dir / "outline.json"
        if outline_path.is_file():
            outline = self._read_json(outline_path)
        else:
            self._update(
                update,
                RewriteStage.PLANNING,
                27,
                checkpoint,
                writer_url,
                work_dir,
                total=len(chunks),
            )
            prompt = build_outline_prompt(
                brief,
                style_profile=style_profile,
                source_sections=summaries,
            )
            response = await self._run_gpt(
                self._tag_prompt(f"{job.id}:outline", prompt),
                conversation_url=writer_url,
                request_id=f"{job.id}:outline",
            )
            writer_url = response.conversation_url
            self._update(
                update,
                RewriteStage.PLANNING,
                30,
                checkpoint,
                writer_url,
                work_dir,
                total=len(chunks),
            )
            outline = self._parse_outline(response.text, len(chunks))
            self._write_json(outline_path, outline)
            checkpoint["outline_completed"] = True
            self._update(
                update,
                RewriteStage.PLANNING,
                32,
                checkpoint,
                writer_url,
                work_dir,
                total=len(chunks),
            )
        style_profile = outline["global_style_profile"]

        section_bodies, writer_url = await self._rewrite_sections(
            job,
            brief,
            chunks,
            chunk_paths,
            analyses,
            style_profile,
            outline,
            work_dir,
            checkpoint,
            writer_url,
            update,
        )

        groups = self._make_edit_groups(chunks, section_bodies)
        groups, writer_url = await self._edit_groups(
            job,
            brief,
            groups,
            style_profile,
            outline,
            work_dir,
            checkpoint,
            writer_url,
            update,
        )
        groups, validator_url, writer_url = await self._validate_groups(
            job,
            brief,
            groups,
            style_profile,
            outline,
            work_dir,
            checkpoint,
            writer_url,
            update,
        )
        groups, validator_url, writer_url = await self._edit_seams(
            job,
            brief,
            groups,
            style_profile,
            outline,
            work_dir,
            checkpoint,
            validator_url,
            writer_url,
            update,
        )
        checkpoint["validator_url"] = validator_url

        final_body = remove_duplicate_transitions(
            "\n\n".join(group.body.strip() for group in groups if group.body.strip())
        )
        body_check = validate_rewrite(
            title=document.title,
            body=final_body,
            source_body=document.body,
            length_policy=self.length_policy,
        )
        body_errors = [error for error in body_check.errors if not error.startswith("TITLE_")]
        if body_errors:
            raise self._validation_error(body_errors)
        final_body = body_check.body

        title, writer_url = await self._create_title(
            job,
            brief,
            final_body,
            writer_url,
            work_dir,
            checkpoint,
            update,
            len(chunks),
        )
        final_check = validate_rewrite(
            title=title,
            body=final_body,
            source_body=document.body,
            length_policy=self.length_policy,
        )
        if not final_check.is_valid:
            raise self._validation_error(list(final_check.errors))

        video_id = str((source_job.video or {}).get("id") or source_job.id)
        artifact_path = output_dir / f"{video_id}.{source_language}.rewrite.txt"
        self._update(
            update,
            RewriteStage.RENDERING,
            98,
            checkpoint,
            writer_url,
            work_dir,
            completed=len(chunks),
            total=len(chunks),
        )
        render_rewrite_txt_atomic(
            artifact_path,
            title=final_check.title,
            body=final_check.body,
        )
        checkpoint.update({"completed": True, "sections_completed": len(chunks)})
        warnings = list(dict.fromkeys(final_check.warnings))
        return RewritePipelineOutput(
            artifact_path=artifact_path,
            title=final_check.title,
            source_length=final_check.source_length,
            output_length=final_check.output_length,
            sections_total=len(chunks),
            sections_completed=len(chunks),
            warnings=warnings,
            conversation_url=writer_url,
            checkpoint=checkpoint,
            work_files=self._work_files(work_dir),
        )

    async def _analyze_chunks(
        self,
        job: StoredRewriteJob,
        brief: RewriteBrief,
        chunks: list[str],
        chunk_paths: list[Path],
        work_dir: Path,
        checkpoint: dict[str, Any],
        writer_url: str,
        update: ProgressCallback,
    ) -> tuple[list[dict[str, Any]], str]:
        analyses: list[dict[str, Any]] = []
        for index, (chunk, chunk_path) in enumerate(zip(chunks, chunk_paths, strict=True), 1):
            analysis_path = work_dir / f"analysis-{index:03}.json"
            if analysis_path.is_file():
                analyses.append(self._read_json(analysis_path))
                continue
            self._update(
                update,
                RewriteStage.UPLOADING,
                5 + int(15 * (index - 1) / len(chunks)),
                checkpoint,
                writer_url,
                work_dir,
                total=len(chunks),
            )
            chunk_brief = self._chunk_brief(brief, chunk)
            analysis_attachment = work_dir / f"analysis-source-{index:03}.txt"
            if not analysis_attachment.is_file():
                self._write_text(analysis_attachment, chunk_path.read_text(encoding="utf-8"))
            prompt = build_style_analysis_prompt(
                chunk_brief, attachment_name=analysis_attachment.name
            )
            request_id = f"{job.id}:analysis:{index}"
            response = await self._run_gpt(
                self._tag_prompt(request_id, prompt),
                attachment=analysis_attachment,
                conversation_url=writer_url,
                request_id=request_id,
            )
            writer_url = response.conversation_url
            self._update(
                update,
                RewriteStage.ANALYZING_STYLE,
                8 + int(17 * (index - 1) / len(chunks)),
                checkpoint,
                writer_url,
                work_dir,
                total=len(chunks),
            )
            analysis = self._parse_structured(
                response.text,
                ("style_profile", "content_summary", "required_points"),
            )
            if not isinstance(analysis.get("style_profile"), dict):
                raise PipelineError("GPT_OUTPUT_INVALID", "Style profile must be a JSON object.")
            self._write_json(analysis_path, analysis)
            analyses.append(analysis)
            checkpoint["analysis_completed"] = index
            self._update(
                update,
                RewriteStage.ANALYZING_STYLE,
                8 + int(17 * index / len(chunks)),
                checkpoint,
                writer_url,
                work_dir,
                total=len(chunks),
            )
        return analyses, writer_url

    async def _rewrite_sections(
        self,
        job: StoredRewriteJob,
        brief: RewriteBrief,
        chunks: list[str],
        chunk_paths: list[Path],
        analyses: list[dict[str, Any]],
        style_profile: dict[str, Any],
        outline: dict[str, Any],
        work_dir: Path,
        checkpoint: dict[str, Any],
        writer_url: str,
        update: ProgressCallback,
    ) -> tuple[list[str], str]:
        bodies: list[str] = []
        outline_sections = outline["sections"]
        for index, (chunk, chunk_path) in enumerate(zip(chunks, chunk_paths, strict=True), 1):
            section_path = work_dir / f"section-{index:03}.txt"
            if section_path.is_file():
                bodies.append(section_path.read_text(encoding="utf-8"))
                continue
            previous_tail = bodies[-1][-800:] if bodies else ""
            next_summary = (
                str(analyses[index].get("content_summary", "")) if index < len(analyses) else ""
            )
            context = SectionRewriteContext(
                index=index,
                total=len(chunks),
                source_text=chunk,
                outline_section=outline_sections[index - 1],
                style_profile=style_profile,
                previous_tail=previous_tail,
                next_summary=next_summary,
            )
            prompt = build_section_rewrite_prompt(self._chunk_brief(brief, chunk), context)
            request_id = f"{job.id}:rewrite:{index}"
            rewrite_attachment = work_dir / f"rewrite-source-{index:03}.txt"
            if not rewrite_attachment.is_file():
                self._write_text(rewrite_attachment, chunk_path.read_text(encoding="utf-8"))
            self._update(
                update,
                RewriteStage.UPLOADING,
                32 + int(35 * (index - 1) / len(chunks)),
                checkpoint,
                writer_url,
                work_dir,
                completed=index - 1,
                total=len(chunks),
            )
            response = await self._run_gpt(
                self._tag_prompt(request_id, prompt),
                attachment=rewrite_attachment,
                conversation_url=writer_url,
                request_id=request_id,
            )
            writer_url = response.conversation_url
            self._update(
                update,
                RewriteStage.REWRITING,
                35 + int(35 * (index - 1) / len(chunks)),
                checkpoint,
                writer_url,
                work_dir,
                completed=index - 1,
                total=len(chunks),
            )
            parsed = self._parse_structured(response.text, ("body",))
            body = str(parsed["body"]).strip()
            if not body:
                raise PipelineError("GPT_OUTPUT_INVALID", "A rewritten section is empty.")
            self._write_text(section_path, body)
            bodies.append(body)
            checkpoint.update({"sections_completed": index, "next_section": index + 1})
            self._update(
                update,
                RewriteStage.REWRITING,
                35 + int(35 * index / len(chunks)),
                checkpoint,
                writer_url,
                work_dir,
                completed=index,
                total=len(chunks),
            )
        return bodies, writer_url

    async def _edit_groups(
        self,
        job: StoredRewriteJob,
        brief: RewriteBrief,
        groups: list[_EditGroup],
        style_profile: dict[str, Any],
        outline: dict[str, Any],
        work_dir: Path,
        checkpoint: dict[str, Any],
        writer_url: str,
        update: ProgressCallback,
    ) -> tuple[list[_EditGroup], str]:
        for group_index, group in enumerate(groups, 1):
            edited_path = work_dir / f"edited-{group_index:03}.txt"
            if edited_path.is_file():
                group.body = edited_path.read_text(encoding="utf-8")
                continue
            group_brief = self._chunk_brief(brief, group.source)
            group_outline = {"sections": [outline["sections"][index] for index in group.indexes]}
            prompt = build_final_edit_prompt(
                group_brief,
                style_profile=style_profile,
                outline=group_outline,
                draft=group.draft,
            )
            draft_path = work_dir / f"draft-{group_index:03}.txt"
            self._write_text(draft_path, group.draft)
            request_id = f"{job.id}:edit:{group_index}"
            response = await self._run_gpt(
                self._tag_prompt(request_id, prompt),
                attachment=draft_path,
                conversation_url=writer_url,
                request_id=request_id,
            )
            writer_url = response.conversation_url
            self._update(
                update,
                RewriteStage.EDITING,
                70 + int(7 * (group_index - 1) / len(groups)),
                checkpoint,
                writer_url,
                work_dir,
                completed=sum(len(item.indexes) for item in groups[: group_index - 1]),
                total=sum(len(item.indexes) for item in groups),
            )
            parsed = self._parse_structured(response.text, ("body",))
            group.body = remove_duplicate_transitions(str(parsed["body"]))
            self._write_text(edited_path, group.body)
            checkpoint["editing_completed"] = group_index
            self._update(
                update,
                RewriteStage.EDITING,
                70 + int(7 * group_index / len(groups)),
                checkpoint,
                writer_url,
                work_dir,
                completed=sum(len(item.indexes) for item in groups[:group_index]),
                total=sum(len(item.indexes) for item in groups),
            )
        return groups, writer_url

    async def _edit_seams(
        self,
        job: StoredRewriteJob,
        brief: RewriteBrief,
        groups: list[_EditGroup],
        style_profile: dict[str, Any],
        outline: dict[str, Any],
        work_dir: Path,
        checkpoint: dict[str, Any],
        validator_url: str,
        writer_url: str,
        update: ProgressCallback,
    ) -> tuple[list[_EditGroup], str, str]:
        seam_total = max(0, len(groups) - 1)
        checkpoint["seams_total"] = seam_total
        for seam_index in range(1, len(groups)):
            previous_group = groups[seam_index - 1]
            next_group = groups[seam_index]
            previous_tail = self._tail_window(previous_group.body)
            next_head = self._head_window(next_group.body)
            previous_input_hash = self._content_hash(previous_group.body)
            next_input_hash = self._content_hash(next_group.body)
            result_path = work_dir / f"seam-edited-{seam_index:03}.json"
            if result_path.is_file():
                result = self._read_json(result_path)
                revised_previous_tail, revised_next_head = self._read_seam_result(
                    result,
                    seam_index,
                    previous_input_hash=previous_input_hash,
                    next_input_hash=next_input_hash,
                )
                previous_body, next_body = self._apply_seam(
                    previous_group.body,
                    next_group.body,
                    previous_tail,
                    next_head,
                    revised_previous_tail,
                    revised_next_head,
                )
                errors = self._seam_errors(
                    previous_group,
                    next_group,
                    previous_body,
                    next_body,
                    original_window=f"{previous_tail} {next_head}",
                    revised_window=f"{revised_previous_tail} {revised_next_head}",
                )
                if errors:
                    raise PipelineError(
                        "GPT_OUTPUT_INVALID",
                        "A persisted section seam is invalid.",
                        details={"seam": seam_index, "errors": errors},
                    )
            else:
                previous_source_tail = self._tail_window(previous_group.source)
                next_source_head = self._head_window(next_group.source)
                seam_outline = {
                    "previous_section": outline["sections"][previous_group.indexes[-1]],
                    "next_section": outline["sections"][next_group.indexes[0]],
                }
                seam_brief = RewriteBrief(
                    source_language=brief.source_language,
                    source_title=brief.source_title,
                    source_length=normalized_length(f"{previous_source_tail} {next_source_head}"),
                    length_policy=self.length_policy,
                )
                errors: list[str] = []
                for attempt in range(self.settings.rewrite_repair_attempts + 1):
                    input_path = work_dir / f"seam-input-{seam_index:03}-{attempt:02}.txt"
                    self._write_text(
                        input_path,
                        "SOURCE PREVIOUS TAIL\n"
                        f"{previous_source_tail}\n\nSOURCE NEXT HEAD\n{next_source_head}\n\n"
                        f"DRAFT PREVIOUS TAIL\n{previous_tail}\n\nDRAFT NEXT HEAD\n{next_head}",
                    )
                    prompt = build_seam_edit_prompt(
                        seam_brief,
                        style_profile=style_profile,
                        outline=seam_outline,
                        previous_source_tail=previous_source_tail,
                        next_source_head=next_source_head,
                        previous_tail=previous_tail,
                        next_head=next_head,
                    )
                    if errors:
                        prompt += "\nTARGETED REPAIRS\n" + json.dumps(errors, ensure_ascii=False)
                    request_id = f"{job.id}:seam:{seam_index}:{attempt}"
                    response = await self._run_gpt(
                        self._tag_prompt(request_id, prompt),
                        attachment=input_path,
                        conversation_url=writer_url,
                        request_id=request_id,
                    )
                    writer_url = response.conversation_url
                    self._update(
                        update,
                        RewriteStage.EDITING,
                        95,
                        checkpoint,
                        writer_url,
                        work_dir,
                        completed=sum(len(group.indexes) for group in groups[:seam_index]),
                        total=sum(len(group.indexes) for group in groups),
                    )
                    seam = self._parse_structured(
                        response.text,
                        ("previous_tail", "next_head"),
                    )
                    revised_previous_tail = str(seam["previous_tail"]).strip()
                    revised_next_head = str(seam["next_head"]).strip()
                    if not revised_previous_tail or not revised_next_head:
                        raise PipelineError(
                            "GPT_OUTPUT_INVALID", "A rewritten section seam is empty."
                        )
                    previous_body, next_body = self._apply_seam(
                        previous_group.body,
                        next_group.body,
                        previous_tail,
                        next_head,
                        revised_previous_tail,
                        revised_next_head,
                    )
                    local_errors = self._seam_errors(
                        previous_group,
                        next_group,
                        previous_body,
                        next_body,
                        original_window=f"{previous_tail} {next_head}",
                        revised_window=f"{revised_previous_tail} {revised_next_head}",
                    )
                    validation_path = work_dir / f"seam-validation-{seam_index:03}-{attempt:02}.txt"
                    self._write_text(
                        validation_path,
                        "SOURCE\n"
                        f"{previous_source_tail}\n\n{next_source_head}\n\nDRAFT\n"
                        f"{revised_previous_tail}\n\n{revised_next_head}",
                    )
                    validation_prompt = build_validation_prompt(
                        seam_brief,
                        style_profile=style_profile,
                        outline=seam_outline,
                        source_text=f"{previous_source_tail}\n\n{next_source_head}",
                        draft=f"{revised_previous_tail}\n\n{revised_next_head}",
                    )
                    validation_id = f"{job.id}:validate:seam:{seam_index}:{attempt}"
                    validation_response = await self._run_gpt(
                        self._tag_prompt(validation_id, validation_prompt),
                        attachment=validation_path,
                        conversation_url=validator_url,
                        request_id=validation_id,
                    )
                    validator_url = validation_response.conversation_url
                    checkpoint["validator_url"] = validator_url
                    self._update(
                        update,
                        RewriteStage.VALIDATING,
                        95 + int(seam_index / seam_total),
                        checkpoint,
                        writer_url,
                        work_dir,
                        completed=sum(len(group.indexes) for group in groups[:seam_index]),
                        total=sum(len(group.indexes) for group in groups),
                    )
                    decision = self._validation_decision(validation_response.text, local_errors)
                    if decision.passed:
                        break
                    if attempt >= self.settings.rewrite_repair_attempts:
                        if local_errors:
                            raise self._validation_error(local_errors)
                        raise PipelineError(
                            "STYLE_VALIDATION_FAILED",
                            "A rewritten section seam did not pass validation.",
                            details={
                                "seam": seam_index,
                                "language_match": decision.language_match,
                                "unsupported_claims": decision.unsupported_claims,
                                "missing_points": decision.missing_points,
                            },
                        )
                    errors = decision.repairs
                self._write_json(
                    result_path,
                    {
                        "schema_version": 1,
                        "previous_group": seam_index,
                        "next_group": seam_index + 1,
                        "previous_input_hash": previous_input_hash,
                        "next_input_hash": next_input_hash,
                        "previous_tail": revised_previous_tail,
                        "next_head": revised_next_head,
                    },
                )

            previous_group.body = previous_body
            next_group.body = next_body
            checkpoint["seams_completed"] = seam_index
            self._update(
                update,
                RewriteStage.VALIDATING,
                95 + int(seam_index / seam_total),
                checkpoint,
                writer_url,
                work_dir,
                completed=sum(len(group.indexes) for group in groups[:seam_index]),
                total=sum(len(group.indexes) for group in groups),
            )
        return groups, validator_url, writer_url

    async def _validate_groups(
        self,
        job: StoredRewriteJob,
        brief: RewriteBrief,
        groups: list[_EditGroup],
        style_profile: dict[str, Any],
        outline: dict[str, Any],
        work_dir: Path,
        checkpoint: dict[str, Any],
        writer_url: str,
        update: ProgressCallback,
    ) -> tuple[list[_EditGroup], str, str]:
        validator_url = str(checkpoint.get("validator_url") or CHATGPT_URL)
        for group_index, group in enumerate(groups, 1):
            validated_path = work_dir / f"validated-{group_index:03}.txt"
            if validated_path.is_file():
                group.body = validated_path.read_text(encoding="utf-8")
                continue
            group_brief = self._chunk_brief(brief, group.source)
            group_outline = {"sections": [outline["sections"][index] for index in group.indexes]}
            for attempt in range(self.settings.rewrite_repair_attempts + 1):
                local = validate_rewrite(
                    title=brief.source_title,
                    body=group.body,
                    source_body=group.source,
                    length_policy=self.length_policy,
                )
                local_errors = [error for error in local.errors if not error.startswith("TITLE_")]
                validation_file = work_dir / (f"validation-{group_index:03}-{attempt:02}.txt")
                self._write_text(
                    validation_file,
                    f"SOURCE\n{group.source}\n\nDRAFT\n{group.body}",
                )
                prompt = build_validation_prompt(
                    group_brief,
                    style_profile=style_profile,
                    outline=group_outline,
                    source_text=group.source,
                    draft=group.body,
                )
                request_id = f"{job.id}:validate:{group_index}:{attempt}"
                response = await self._run_gpt(
                    self._tag_prompt(request_id, prompt),
                    attachment=validation_file,
                    conversation_url=validator_url,
                    request_id=request_id,
                )
                validator_url = response.conversation_url
                checkpoint["validator_url"] = validator_url
                self._update(
                    update,
                    RewriteStage.VALIDATING,
                    80 + int(15 * (group_index - 1) / len(groups)),
                    checkpoint,
                    writer_url,
                    work_dir,
                    completed=sum(len(item.indexes) for item in groups[: group_index - 1]),
                    total=sum(len(item.indexes) for item in groups),
                )
                verdict = self._parse_structured(
                    response.text,
                    (
                        "passed",
                        "language_match",
                        "style_score",
                        "coverage_score",
                        "tts_ready",
                        "unsupported_claims",
                        "missing_points",
                        "targeted_repairs",
                    ),
                )
                language_match = self._as_bool(verdict["language_match"])
                unsupported_claims = self._as_text_list(
                    verdict["unsupported_claims"], "unsupported_claims"
                )
                missing_points = self._as_text_list(verdict["missing_points"], "missing_points")
                targeted_repairs = self._as_text_list(
                    verdict["targeted_repairs"], "targeted_repairs"
                )
                passed = (
                    self._as_bool(verdict["passed"])
                    and language_match
                    and self._as_bool(verdict["tts_ready"])
                    and self._as_score(verdict["style_score"])
                    >= self.settings.rewrite_validation_score
                    and self._as_score(verdict["coverage_score"])
                    >= self.settings.rewrite_validation_score
                    and not unsupported_claims
                    and not missing_points
                    and not local_errors
                )
                if passed:
                    break
                if attempt >= self.settings.rewrite_repair_attempts:
                    if local_errors:
                        raise self._validation_error(local_errors)
                    raise PipelineError(
                        "STYLE_VALIDATION_FAILED",
                        "The rewritten script did not pass style and content validation.",
                        details={
                            "group": group_index,
                            "language_match": language_match,
                            "unsupported_claims": unsupported_claims,
                            "missing_points": missing_points,
                            "local_errors": local_errors,
                        },
                    )
                repairs = [*targeted_repairs, *local_errors]
                if not language_match:
                    repairs.append("Restore the source language throughout the draft.")
                if unsupported_claims:
                    repairs.append("Remove unsupported claims: " + "; ".join(unsupported_claims))
                if missing_points:
                    repairs.append("Restore missing points: " + "; ".join(missing_points))
                if not repairs:
                    repairs.append("Resolve every failed validator rubric item.")
                repair_prompt = (
                    build_final_edit_prompt(
                        group_brief,
                        style_profile=style_profile,
                        outline=group_outline,
                        draft=group.body,
                    )
                    + f"\nTARGETED REPAIRS\n{json.dumps(repairs, ensure_ascii=False)}"
                )
                repair_id = f"{job.id}:repair:{group_index}:{attempt}"
                repaired = await self._run_gpt(
                    self._tag_prompt(repair_id, repair_prompt),
                    conversation_url=writer_url,
                    request_id=repair_id,
                )
                writer_url = repaired.conversation_url
                self._update(
                    update,
                    RewriteStage.VALIDATING,
                    80 + int(15 * (group_index - 1) / len(groups)),
                    checkpoint,
                    writer_url,
                    work_dir,
                    completed=sum(len(item.indexes) for item in groups[: group_index - 1]),
                    total=sum(len(item.indexes) for item in groups),
                )
                group.body = remove_duplicate_transitions(
                    str(self._parse_structured(repaired.text, ("body",))["body"])
                )
                self._write_text(work_dir / f"repaired-{group_index:03}.txt", group.body)
            self._write_text(validated_path, group.body)
            checkpoint["validation_completed"] = group_index
            checkpoint["validator_url"] = validator_url
            self._update(
                update,
                RewriteStage.VALIDATING,
                80 + int(15 * group_index / len(groups)),
                checkpoint,
                writer_url,
                work_dir,
                completed=sum(len(item.indexes) for item in groups[:group_index]),
                total=sum(len(item.indexes) for item in groups),
            )
        return groups, validator_url, writer_url

    async def _create_title(
        self,
        job: StoredRewriteJob,
        brief: RewriteBrief,
        final_body: str,
        writer_url: str,
        work_dir: Path,
        checkpoint: dict[str, Any],
        update: ProgressCallback,
        sections_total: int,
    ) -> tuple[str, str]:
        title_path = work_dir / "title.txt"
        if title_path.is_file():
            return title_path.read_text(encoding="utf-8").strip(), writer_url
        for attempt in range(self.settings.rewrite_repair_attempts + 1):
            final_script_path = work_dir / f"final-script-for-title-{attempt:02}.txt"
            self._write_text(final_script_path, final_body)
            prompt = build_title_prompt(
                brief,
                final_body=final_body,
                attachment_name=final_script_path.name,
            )
            request_id = f"{job.id}:title:{attempt}"
            response = await self._run_gpt(
                self._tag_prompt(request_id, prompt),
                attachment=final_script_path,
                conversation_url=writer_url,
                request_id=request_id,
            )
            writer_url = response.conversation_url
            self._update(
                update,
                RewriteStage.RENDERING,
                96,
                checkpoint,
                writer_url,
                work_dir,
                completed=sections_total,
                total=sections_total,
            )
            title = str(self._parse_structured(response.text, ("title",))["title"]).strip()
            title_check = validate_rewrite(
                title=title,
                body=final_body,
                source_body=final_body,
                length_policy=LengthPolicy(1, 1, 1),
            )
            title_errors = [error for error in title_check.errors if error.startswith("TITLE_")]
            if not title_errors:
                self._write_text(title_path, title_check.title)
                checkpoint["title_completed"] = True
                self._update(
                    update,
                    RewriteStage.RENDERING,
                    97,
                    checkpoint,
                    writer_url,
                    work_dir,
                    completed=sections_total,
                    total=sections_total,
                )
                return title_check.title, writer_url
            prompt += "\nTITLE REPAIR\nFix these title errors: " + ", ".join(title_errors)
        raise PipelineError("GPT_OUTPUT_INVALID", "GPT did not produce a valid SEO title.")

    async def _run_gpt(
        self,
        prompt: str,
        *,
        attachment: Path | None = None,
        conversation_url: str | None,
        request_id: str,
    ) -> ChatGPTResponse:
        last_error: PipelineError | None = None
        for attempt in range(self.settings.gpt_retries):
            try:
                return await self.gpt.run_prompt(
                    prompt,
                    attachment=attachment,
                    conversation_url=conversation_url,
                    request_id=request_id,
                )
            except PipelineError as error:
                last_error = error
                if not error.info.retryable or attempt + 1 >= self.settings.gpt_retries:
                    raise
                current_page = getattr(self.gpt, "_page", None)
                current_url = getattr(current_page, "url", None)
                if isinstance(current_url, str) and current_url.startswith("https://chatgpt.com"):
                    conversation_url = current_url
                if error.info.code == "GPT_BROWSER_CRASHED":
                    await self.gpt.close()
                await asyncio.sleep(min(2**attempt, 8))
        if last_error is not None:
            raise last_error
        raise PipelineError("GPT_BROWSER_CRASHED", "ChatGPT request failed.", retryable=True)

    def _source_path(self, source_job: StoredJob) -> Path:
        if not source_job.artifact_paths or not source_job.artifact_paths.get("txt"):
            raise PipelineError("SOURCE_EMPTY", "The source TXT artifact is missing.")
        path = self._safe_source_artifact_path(source_job.artifact_paths["txt"])
        if not path.is_file():
            raise PipelineError("SOURCE_EMPTY", "The source TXT artifact is missing.")
        return path

    def _source_chunks(self, source_job: StoredJob, body: str) -> list[str]:
        json_path_value = (source_job.artifact_paths or {}).get("json")
        if json_path_value:
            try:
                json_path = self._safe_source_artifact_path(json_path_value)
                artifact = json.loads(json_path.read_text(encoding="utf-8"))
                segment_texts = [
                    str(segment.get("text", "")).strip()
                    for segment in artifact.get("segments", [])
                    if str(segment.get("text", "")).strip()
                ]
                joined = " ".join(segment_texts)
                if joined and normalized_length(joined) == normalized_length(body):
                    return self._pack_segments(segment_texts)
            except (OSError, UnicodeError, json.JSONDecodeError, AttributeError, TypeError):
                pass
        return semantic_chunks(body, self.settings.rewrite_chunk_max_chars)

    def _safe_source_artifact_path(self, value: str) -> Path:
        path = Path(value).resolve()
        root = self.settings.jobs_dir.resolve()
        if not path.is_relative_to(root):
            raise PipelineError("SOURCE_ARTIFACT_MISSING", "The source artifact path is invalid.")
        return path

    def _job_dir(self, root: Path, job_id: str) -> Path:
        path = (root / job_id).resolve()
        if not path.is_relative_to(root.resolve()):
            raise PipelineError("REWRITE_FAILED", "The rewrite job path is invalid.")
        return path

    def _pack_segments(self, segments: list[str]) -> list[str]:
        chunks: list[str] = []
        current: list[str] = []
        current_length = 0
        for segment in segments:
            if len(segment) > self.settings.rewrite_chunk_max_chars:
                if current:
                    chunks.append(" ".join(current))
                    current = []
                    current_length = 0
                chunks.extend(semantic_chunks(segment, self.settings.rewrite_chunk_max_chars))
                continue
            added = len(segment) + (1 if current else 0)
            if current and current_length + added > self.settings.rewrite_chunk_max_chars:
                chunks.append(" ".join(current))
                current = [segment]
                current_length = len(segment)
            else:
                current.append(segment)
                current_length += added
        if current:
            chunks.append(" ".join(current))
        return chunks

    def _write_source_chunks(self, work_dir: Path, title: str, chunks: list[str]) -> list[Path]:
        paths: list[Path] = []
        for index, chunk in enumerate(chunks, 1):
            path = work_dir / f"source-{index:03}.txt"
            if not path.is_file():
                render_rewrite_txt_atomic(path, title=title, body=chunk)
            paths.append(path)
        return paths

    def _make_edit_groups(self, chunks: list[str], bodies: list[str]) -> list[_EditGroup]:
        groups: list[_EditGroup] = []
        current_indexes: list[int] = []
        current_sources: list[str] = []
        current_bodies: list[str] = []
        current_length = 0
        for index, (source, body) in enumerate(zip(chunks, bodies, strict=True)):
            body_length = len(body)
            if (
                current_indexes
                and current_length + body_length > self.settings.rewrite_chunk_max_chars
            ):
                groups.append(
                    _EditGroup(
                        indexes=current_indexes,
                        source="\n\n".join(current_sources),
                        draft="\n\n".join(current_bodies),
                    )
                )
                current_indexes, current_sources, current_bodies = [], [], []
                current_length = 0
            current_indexes.append(index)
            current_sources.append(source)
            current_bodies.append(body)
            current_length += body_length
        if current_indexes:
            groups.append(
                _EditGroup(
                    indexes=current_indexes,
                    source="\n\n".join(current_sources),
                    draft="\n\n".join(current_bodies),
                )
            )
        return groups

    def _head_window(self, text: str) -> str:
        if len(text) <= SEAM_CONTEXT_CHARS:
            return text
        window = text[:SEAM_CONTEXT_CHARS]
        sentence_boundaries = list(SEAM_SENTENCE_BOUNDARY_PATTERN.finditer(window))
        minimum = SEAM_CONTEXT_CHARS // 2
        if sentence_boundaries and sentence_boundaries[-1].end() >= minimum:
            return text[: sentence_boundaries[-1].end()]
        whitespace = max(window.rfind(" "), window.rfind("\n"), window.rfind("\t"))
        if whitespace >= minimum:
            return text[:whitespace]
        return window

    def _tail_window(self, text: str) -> str:
        if len(text) <= SEAM_CONTEXT_CHARS:
            return text
        start = len(text) - SEAM_CONTEXT_CHARS
        window = text[start:]
        sentence_boundary = SEAM_SENTENCE_BOUNDARY_PATTERN.search(window)
        if sentence_boundary and sentence_boundary.end() <= SEAM_CONTEXT_CHARS // 2:
            boundary_start = start + sentence_boundary.end()
            while boundary_start < len(text) and text[boundary_start].isspace():
                boundary_start += 1
            return text[boundary_start:]
        whitespace_offsets = [
            offset
            for offset in (window.find(" "), window.find("\n"), window.find("\t"))
            if offset >= 0
        ]
        if whitespace_offsets and min(whitespace_offsets) <= SEAM_CONTEXT_CHARS // 2:
            return text[start + min(whitespace_offsets) + 1 :]
        return window

    def _content_hash(self, text: str) -> str:
        return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()

    def _apply_seam(
        self,
        previous_body: str,
        next_body: str,
        previous_tail: str,
        next_head: str,
        revised_previous_tail: str,
        revised_next_head: str,
    ) -> tuple[str, str]:
        previous_prefix = previous_body[: -len(previous_tail)] if previous_tail else previous_body
        next_suffix = next_body[len(next_head) :] if next_head else next_body
        return (
            previous_prefix + revised_previous_tail,
            revised_next_head + next_suffix,
        )

    def _read_seam_result(
        self,
        result: dict[str, Any],
        seam_index: int,
        *,
        previous_input_hash: str,
        next_input_hash: str,
    ) -> tuple[str, str]:
        if (
            result.get("schema_version") != 1
            or result.get("previous_group") != seam_index
            or result.get("next_group") != seam_index + 1
            or result.get("previous_input_hash") != previous_input_hash
            or result.get("next_input_hash") != next_input_hash
        ):
            raise PipelineError("GPT_OUTPUT_INVALID", "A rewrite seam checkpoint is invalid.")
        values = tuple(
            str(result.get(field, "")).strip() for field in ("previous_tail", "next_head")
        )
        if not all(values):
            raise PipelineError("GPT_OUTPUT_INVALID", "A rewrite seam checkpoint is incomplete.")
        return values

    def _seam_errors(
        self,
        previous_group: _EditGroup,
        next_group: _EditGroup,
        previous_body: str,
        next_body: str,
        *,
        original_window: str,
        revised_window: str,
    ) -> list[str]:
        errors: list[str] = []
        for label, group, body in (
            ("PREVIOUS", previous_group, previous_body),
            ("NEXT", next_group, next_body),
        ):
            result = validate_rewrite(
                title="Temporary title",
                body=body,
                source_body=group.source,
                length_policy=self.length_policy,
            )
            errors.extend(
                f"{label}_{error}" for error in result.errors if not error.startswith("TITLE_")
            )
        original_length = normalized_length(original_window)
        revised_length = normalized_length(revised_window)
        if revised_length < int(original_length * SEAM_MIN_LENGTH_RATIO):
            errors.append("OUTPUT_TOO_SHORT")
        if revised_length > int(original_length * SEAM_MAX_LENGTH_RATIO):
            errors.append("OUTPUT_TOO_LONG")
        return errors

    def _parse_outline(self, response: str, expected_sections: int) -> dict[str, Any]:
        outline = self._parse_structured(response, ("global_style_profile", "sections"))
        if not isinstance(outline["global_style_profile"], dict):
            raise PipelineError("GPT_OUTPUT_INVALID", "GPT global style profile must be an object.")
        sections = outline.get("sections")
        if not isinstance(sections, list) or len(sections) != expected_sections:
            raise PipelineError(
                "GPT_OUTPUT_INVALID",
                "GPT outline must contain exactly one section for each source chunk.",
                details={"expected_sections": expected_sections},
            )
        if any(not isinstance(section, dict) for section in sections):
            raise PipelineError("GPT_OUTPUT_INVALID", "GPT outline sections must be objects.")
        return outline

    def _combined_style_profile(self, analyses: list[dict[str, Any]]) -> dict[str, Any]:
        profiles = [analysis["style_profile"] for analysis in analyses]
        representative_indexes = sorted({0, len(profiles) // 2, len(profiles) - 1})
        return {
            "dominant": profiles[0],
            "representative_profiles": [profiles[index] for index in representative_indexes],
        }

    def _analysis_summary(self, index: int, analysis: dict[str, Any]) -> str:
        return json.dumps(
            {
                "chunk_id": index,
                "content_summary": analysis.get("content_summary"),
                "required_points": analysis.get("required_points"),
                "sponsor_sections": analysis.get("sponsor_sections", []),
                "generic_cta_present": analysis.get("generic_cta_present", False),
            },
            ensure_ascii=False,
        )

    def _chunk_brief(self, brief: RewriteBrief, chunk: str) -> RewriteBrief:
        return RewriteBrief(
            source_language=brief.source_language,
            source_title=brief.source_title,
            source_length=normalized_length(chunk),
            length_policy=self.length_policy,
        )

    def _tag_prompt(self, request_id: str, prompt: str) -> str:
        return f"[YT_PRO_MAX_REQUEST:{request_id}]\n{prompt}"

    def _update(
        self,
        update: ProgressCallback,
        stage: RewriteStage,
        progress: int,
        checkpoint: dict[str, Any],
        conversation_url: str | None,
        work_dir: Path,
        *,
        completed: int = 0,
        total: int = 0,
    ) -> None:
        update(
            stage,
            progress,
            completed,
            total,
            checkpoint=dict(checkpoint),
            conversation_url=conversation_url,
            work_files=self._work_files(work_dir),
        )

    def _work_files(self, work_dir: Path) -> dict[str, str]:
        return {path.name: str(path) for path in sorted(work_dir.iterdir()) if path.is_file()}

    def _write_json(self, path: Path, value: Any) -> None:
        self._atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise PipelineError("GPT_OUTPUT_INVALID", "A rewrite checkpoint is invalid.") from error
        if not isinstance(value, dict):
            raise PipelineError("GPT_OUTPUT_INVALID", "A rewrite checkpoint must be an object.")
        return value

    def _write_text(self, path: Path, text: str) -> None:
        self._atomic_write(path, text.strip() + "\n")

    def _atomic_write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    def _as_bool(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "yes", "pass", "passed"}:
                return True
            if normalized in {"false", "no", "fail", "failed"}:
                return False
        raise PipelineError("GPT_OUTPUT_INVALID", "GPT validation boolean is invalid.")

    def _validation_decision(self, response: str, local_errors: list[str]) -> _ValidationDecision:
        verdict = self._parse_structured(
            response,
            (
                "passed",
                "language_match",
                "style_score",
                "coverage_score",
                "tts_ready",
                "unsupported_claims",
                "missing_points",
                "targeted_repairs",
            ),
        )
        language_match = self._as_bool(verdict["language_match"])
        unsupported_claims = self._as_text_list(verdict["unsupported_claims"], "unsupported_claims")
        missing_points = self._as_text_list(verdict["missing_points"], "missing_points")
        targeted_repairs = self._as_text_list(verdict["targeted_repairs"], "targeted_repairs")
        passed = (
            self._as_bool(verdict["passed"])
            and language_match
            and self._as_bool(verdict["tts_ready"])
            and self._as_score(verdict["style_score"]) >= self.settings.rewrite_validation_score
            and self._as_score(verdict["coverage_score"]) >= self.settings.rewrite_validation_score
            and not unsupported_claims
            and not missing_points
            and not local_errors
        )
        repairs = [*targeted_repairs, *local_errors]
        if not language_match:
            repairs.append("Restore the source language throughout the draft.")
        if unsupported_claims:
            repairs.append("Remove unsupported claims: " + "; ".join(unsupported_claims))
        if missing_points:
            repairs.append("Restore missing points: " + "; ".join(missing_points))
        if not repairs and not passed:
            repairs.append("Resolve every failed validator rubric item.")
        return _ValidationDecision(
            passed=passed,
            language_match=language_match,
            unsupported_claims=unsupported_claims,
            missing_points=missing_points,
            repairs=repairs,
        )

    def _parse_structured(self, response: str, required: tuple[str, ...]) -> dict[str, Any]:
        try:
            return parse_structured_response(response, required)
        except RewriteContentError as error:
            raise PipelineError(error.code, str(error)) from error

    def _as_score(self, value: Any) -> float:
        try:
            score = float(value)
        except (TypeError, ValueError) as error:
            raise PipelineError("GPT_OUTPUT_INVALID", "GPT validation score is invalid.") from error
        if not 0 <= score <= 100:
            raise PipelineError("GPT_OUTPUT_INVALID", "GPT validation score is invalid.")
        return score

    def _as_text_list(self, value: Any, field: str) -> list[str]:
        if not isinstance(value, list):
            raise PipelineError(
                "GPT_OUTPUT_INVALID", f"GPT validation field '{field}' must be a list."
            )
        return [str(item).strip() for item in value if str(item).strip()]

    def _validation_error(self, errors: list[str]) -> PipelineError:
        if any(error.endswith("OUTPUT_TOO_SHORT") for error in errors):
            code = "OUTPUT_TOO_SHORT"
        elif any(error.endswith("OUTPUT_TOO_LONG") for error in errors):
            code = "OUTPUT_TOO_LONG"
        else:
            code = "GPT_OUTPUT_INVALID"
        return PipelineError(
            code,
            "The rewritten script failed final validation.",
            details={"errors": errors},
        )
