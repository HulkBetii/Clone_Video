from __future__ import annotations

import os

import pytest

from yt_pro_max.config import Settings
from yt_pro_max.gpt_playwright import CHATGPT_URL, ChatGPTPlaywrightAdapter
from yt_pro_max.rewrite_content import parse_structured_response


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("YT_PRO_MAX_GPT_SMOKE") != "1",
    reason="Set YT_PRO_MAX_GPT_SMOKE=1 to run the live ChatGPT upload smoke test.",
)
async def test_live_chatgpt_upload_and_response(tmp_path):
    settings = Settings()
    attachment = tmp_path / "rewrite-smoke.txt"
    attachment.write_text("Title: Smoke test\n\nA short source transcript.\n", encoding="utf-8")
    adapter = ChatGPTPlaywrightAdapter(
        settings.gpt_profile_dir,
        response_timeout_s=settings.gpt_reply_timeout_seconds,
        attachment_timeout_s=settings.gpt_attachment_timeout_seconds,
    )
    try:
        response = await adapter.run_prompt(
            'Read the attached file and return only JSON: {"ok": true}.',
            attachment=attachment,
            conversation_url=CHATGPT_URL,
            request_id="rewrite-live-smoke",
        )
    finally:
        await adapter.close()

    assert parse_structured_response(response.text, ("ok",))["ok"] is True
