from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import yt_pro_max.gpt_playwright as gpt_module
from yt_pro_max.errors import PipelineError
from yt_pro_max.gpt_playwright import ChatGPTPlaywrightAdapter


class FakeLocator:
    def __init__(
        self,
        *,
        count: int = 0,
        visible: bool = False,
        enabled: bool = True,
        text: str = "",
        error: Exception | None = None,
        click_callback=None,
    ) -> None:
        self._count = count
        self._visible = visible
        self._enabled = enabled
        self._text = text
        self._error = error
        self._click_callback = click_callback
        self.clicked = False
        self.filled = ""
        self.pressed = ""
        self.files: list[str] = []
        self.evaluate_script = ""

    @property
    def first(self) -> FakeLocator:
        return self

    def nth(self, _index: int) -> FakeLocator:
        return self

    async def count(self) -> int:
        self._raise_if_needed()
        return self._count

    async def is_visible(self, timeout: int | None = None) -> bool:
        del timeout
        self._raise_if_needed()
        return self._visible

    async def is_enabled(self) -> bool:
        self._raise_if_needed()
        return self._enabled

    async def click(self) -> None:
        self._raise_if_needed()
        self.clicked = True
        if self._click_callback is not None:
            self._click_callback()

    async def fill(self, value: str) -> None:
        self._raise_if_needed()
        self.filled = value

    async def press(self, value: str) -> None:
        self._raise_if_needed()
        self.pressed = value
        if self._click_callback is not None:
            self._click_callback()

    async def set_input_files(self, value: str) -> None:
        self._raise_if_needed()
        self.files.append(value)
        if self._click_callback is not None:
            self._click_callback()

    async def inner_text(self, timeout: int | None = None) -> str:
        del timeout
        self._raise_if_needed()
        return self._text

    async def evaluate(self, script: str) -> str:
        self._raise_if_needed()
        self.evaluate_script = script
        return self._text

    def _raise_if_needed(self) -> None:
        if self._error is not None:
            raise self._error


class FakeAssistantLocator(FakeLocator):
    def __init__(self, page: FakePage) -> None:
        self.page = page

    async def count(self) -> int:
        return len(self.page.assistant_messages)

    def nth(self, index: int) -> FakeLocator:
        return FakeLocator(count=1, visible=True, text=self.page.assistant_messages[index])


class SequenceVisibleLocator(FakeLocator):
    def __init__(self, values: list[bool]) -> None:
        self.values = values

    async def count(self) -> int:
        return 1

    def nth(self, _index: int) -> SequenceVisibleLocator:
        return self

    async def is_visible(self, timeout: int | None = None) -> bool:
        del timeout
        return self.values.pop(0) if self.values else False


class SequenceTextLocator(FakeLocator):
    def __init__(self, values: list[str]) -> None:
        self.values = values

    async def count(self) -> int:
        return 1

    def nth(self, _index: int) -> SequenceTextLocator:
        return self

    async def inner_text(self, timeout: int | None = None) -> str:
        del timeout
        if len(self.values) > 1:
            return self.values.pop(0)
        return self.values[0]

    async def evaluate(self, _script: str) -> str:
        if len(self.values) > 1:
            return self.values.pop(0)
        return self.values[0]


class FakeMessagesLocator(FakeLocator):
    def __init__(self, page: FakePage) -> None:
        self.page = page

    async def evaluate_all(self, _script: str) -> list[dict[str, str]]:
        if self.page.message_snapshots:
            index = min(self.page.message_snapshot_index, len(self.page.message_snapshots) - 1)
            self.page.message_snapshot_index += 1
            return self.page.message_snapshots[index]
        return self.page.messages


class FakeAttachmentTilesLocator(FakeLocator):
    def __init__(self, page: FakePage) -> None:
        self.page = page

    async def evaluate_all(self, _script: str) -> list[dict[str, Any]]:
        if self.page.attachment_tile_snapshots:
            index = min(
                self.page.attachment_tile_snapshot_index,
                len(self.page.attachment_tile_snapshots) - 1,
            )
            self.page.attachment_tile_snapshot_index += 1
            return self.page.attachment_tile_snapshots[index]
        return self.page.attachment_tiles


class FakeChooser:
    def __init__(self, callback=None) -> None:
        self.files: list[str] = []
        self._callback = callback

    async def set_files(self, value: str) -> None:
        self.files.append(value)
        if self._callback is not None:
            self._callback()


class FakeChooserInfo:
    def __init__(self, chooser: FakeChooser) -> None:
        self._chooser = chooser

    def __await__(self):
        async def resolve() -> FakeChooser:
            return self._chooser

        return resolve().__await__()


class FakeChooserContext:
    def __init__(self, chooser: FakeChooser) -> None:
        self.value = FakeChooserInfo(chooser)

    async def __aenter__(self) -> FakeChooserContext:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback


class FakePage:
    def __init__(self, *, logged_in: bool = True) -> None:
        auth_status = "logged_in" if logged_in else "logged_out"
        self.url = gpt_module.CHATGPT_URL
        self.bootstrap = FakeLocator(
            count=1,
            visible=True,
            text=json.dumps({"authStatus": auth_status}),
        )
        self.profile = FakeLocator(count=1, visible=logged_in)
        self.file_input = FakeLocator()
        self.attach_button = FakeLocator()
        self.attachment_tiles: list[dict[str, Any]] = []
        self.attachment_tile_snapshots: list[list[dict[str, Any]]] = []
        self.attachment_tile_snapshot_index = 0
        self.attachment_error = FakeLocator()
        self.prompt = FakeLocator(count=1, visible=True)
        self.prompt_selector = gpt_module.PROMPT_SELECTORS[0]
        self.send_button = FakeLocator(
            count=1,
            visible=True,
            click_callback=self._complete_response,
        )
        self.stop_button = FakeLocator()
        self.assistant_messages: list[str] = []
        self.next_response = "Rewritten script"
        self.messages: list[dict[str, str]] = []
        self.message_snapshots: list[list[dict[str, str]]] = []
        self.message_snapshot_index = 0
        self.chooser = FakeChooser(self._complete_attachment)
        self.goto_calls: list[str] = []

    async def goto(self, url: str, **_kwargs: Any) -> None:
        self.url = url
        self.goto_calls.append(url)

    def locator(self, selector: str) -> FakeLocator:
        if selector == gpt_module.BOOTSTRAP_SELECTOR:
            return self.bootstrap
        if selector == gpt_module.PROFILE_BUTTON_SELECTOR:
            return self.profile
        if selector == gpt_module.FILE_INPUT_SELECTOR:
            return self.file_input
        if selector == gpt_module.ATTACHMENT_ERROR_SELECTOR:
            return self.attachment_error
        if selector == gpt_module.COMPOSER_ATTACHMENT_TILE_SELECTOR:
            return FakeAttachmentTilesLocator(self)
        if selector == gpt_module.ASSISTANT_MESSAGE_SELECTOR:
            return FakeAssistantLocator(self)
        if selector == gpt_module.CHAT_MESSAGE_SELECTOR:
            return FakeMessagesLocator(self)
        if selector == gpt_module.STOP_BUTTON_SELECTOR:
            return self.stop_button
        if selector in gpt_module.PROMPT_SELECTORS:
            return self.prompt if selector == self.prompt_selector else FakeLocator()
        if selector in gpt_module.SEND_BUTTON_SELECTORS:
            return (
                self.send_button
                if selector == gpt_module.SEND_BUTTON_SELECTORS[0]
                else FakeLocator()
            )
        if selector in gpt_module.ATTACH_BUTTON_SELECTORS:
            return (
                self.attach_button
                if selector == gpt_module.ATTACH_BUTTON_SELECTORS[0]
                else FakeLocator()
            )
        return FakeLocator()

    def expect_file_chooser(self, *, timeout: int) -> FakeChooserContext:
        del timeout
        return FakeChooserContext(self.chooser)

    def _complete_response(self) -> None:
        self.assistant_messages.append(self.next_response)

    def _complete_attachment(self) -> None:
        self.attachment_tiles = [{"label": "source.txt", "waiting": False}]


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.pages = [page]
        self.closed = False

    async def new_page(self) -> FakePage:
        return self.pages[0]

    async def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self, context: FakeContext | None = None, error: Exception | None = None) -> None:
        self.context = context
        self.error = error
        self.launch_kwargs: dict[str, Any] = {}

    async def launch_persistent_context(self, profile: str, **kwargs: Any) -> FakeContext:
        self.launch_kwargs = {"profile": profile, **kwargs}
        if self.error is not None:
            raise self.error
        assert self.context is not None
        return self.context


class FakePlaywright:
    def __init__(self, chromium: FakeChromium) -> None:
        self.chromium = chromium
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class FakePlaywrightStarter:
    def __init__(self, playwright: FakePlaywright) -> None:
        self.playwright = playwright

    async def start(self) -> FakePlaywright:
        return self.playwright


def playwright_loader(playwright: FakePlaywright):
    return lambda: lambda: FakePlaywrightStarter(playwright)


def running_adapter(tmp_path: Path, page: FakePage, **kwargs: Any) -> ChatGPTPlaywrightAdapter:
    profile = tmp_path / "profile"
    profile.mkdir(exist_ok=True)
    kwargs.setdefault("stream_start_grace_s", 0)
    adapter = ChatGPTPlaywrightAdapter(
        profile,
        poll_interval_s=0,
        stable_samples=2,
        **kwargs,
    )
    adapter._context = FakeContext(page)
    adapter._page = page
    return adapter


@pytest.mark.asyncio
async def test_start_launches_headed_persistent_profile_lazily(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    page = FakePage()
    context = FakeContext(page)
    chromium = FakeChromium(context)
    playwright = FakePlaywright(chromium)
    monkeypatch.setattr(gpt_module, "_load_async_playwright", playwright_loader(playwright))
    adapter = ChatGPTPlaywrightAdapter(profile)

    await adapter.start()

    assert chromium.launch_kwargs["profile"] == str(profile)
    assert chromium.launch_kwargs["headless"] is False
    assert page.goto_calls == [gpt_module.CHATGPT_URL]
    await adapter.close()
    assert context.closed
    assert playwright.stopped


@pytest.mark.asyncio
async def test_start_rejects_missing_or_locked_profile(tmp_path, monkeypatch):
    missing = ChatGPTPlaywrightAdapter(tmp_path / "missing")
    with pytest.raises(PipelineError) as missing_error:
        await missing.start()
    assert missing_error.value.info.code == "GPT_PROFILE_MISSING"

    profile = tmp_path / "profile"
    profile.mkdir()
    chromium = FakeChromium(error=RuntimeError("ProcessSingleton: profile is in use"))
    playwright = FakePlaywright(chromium)
    monkeypatch.setattr(gpt_module, "_load_async_playwright", playwright_loader(playwright))
    with pytest.raises(PipelineError) as locked_error:
        await ChatGPTPlaywrightAdapter(profile).start()
    assert locked_error.value.info.code == "GPT_PROFILE_LOCKED"
    assert not locked_error.value.info.retryable
    assert playwright.stopped


@pytest.mark.asyncio
async def test_open_browser_allows_manual_login_then_check_keeps_window_open(
    tmp_path, monkeypatch
):
    profile = tmp_path / "profile"
    profile.mkdir()
    context = FakeContext(FakePage(logged_in=False))
    playwright = FakePlaywright(FakeChromium(context))
    monkeypatch.setattr(gpt_module, "_load_async_playwright", playwright_loader(playwright))

    adapter = ChatGPTPlaywrightAdapter(profile)
    await adapter.open_browser()

    with pytest.raises(PipelineError) as error:
        await adapter.check_login()

    assert error.value.info.code == "GPT_LOGIN_REQUIRED"
    assert not context.closed
    await adapter.close()


@pytest.mark.asyncio
async def test_upload_prefers_direct_file_input(tmp_path):
    page = FakePage()
    page.file_input = FakeLocator(count=1, click_callback=page._complete_attachment)
    adapter = running_adapter(tmp_path, page)
    attachment = tmp_path / "source.txt"
    attachment.write_text("source", encoding="utf-8")

    await adapter.upload_file(attachment)

    assert page.file_input.files == [str(attachment)]
    assert not page.attach_button.clicked


@pytest.mark.asyncio
async def test_upload_falls_back_to_file_chooser(tmp_path):
    page = FakePage()
    page.attach_button = FakeLocator(count=1, visible=True)
    adapter = running_adapter(tmp_path, page)
    attachment = tmp_path / "source.txt"
    attachment.write_text("source", encoding="utf-8")

    await adapter.upload_file(attachment)

    assert page.attach_button.clicked
    assert page.chooser.files == [str(attachment)]


@pytest.mark.asyncio
async def test_upload_retry_reuses_ready_renamed_attachment(tmp_path):
    page = FakePage()
    page.attachment_tiles = [{"label": "source(3).txt", "waiting": False}]
    page.file_input = FakeLocator(count=1, click_callback=page._complete_attachment)
    adapter = running_adapter(tmp_path, page)
    attachment = tmp_path / "source.txt"
    attachment.write_text("source", encoding="utf-8")

    await adapter.upload_file(attachment)

    assert page.file_input.files == []
    assert page.attachment_tiles == [{"label": "source(3).txt", "waiting": False}]


@pytest.mark.asyncio
async def test_upload_waits_for_exact_cursor_wait_tile_to_be_ready(tmp_path):
    page = FakePage()

    def start_upload() -> None:
        page.attachment_tile_snapshots = [
            [{"label": "source.txt", "waiting": True}],
            [{"label": "source.txt", "waiting": False}],
        ]

    page.file_input = FakeLocator(count=1, click_callback=start_upload)
    adapter = running_adapter(tmp_path, page)
    attachment = tmp_path / "source.txt"
    attachment.write_text("source", encoding="utf-8")

    await adapter.upload_file(attachment)

    assert page.attachment_tile_snapshot_index == 2
    assert page.file_input.files == [str(attachment)]


@pytest.mark.asyncio
async def test_upload_recognizes_renamed_ready_tile_after_processing(tmp_path):
    page = FakePage()

    def start_upload() -> None:
        page.attachment_tile_snapshots = [
            [{"label": "source.txt", "waiting": True}],
            [{"label": "source(3).txt", "waiting": False}],
        ]

    page.file_input = FakeLocator(count=1, click_callback=start_upload)
    adapter = running_adapter(tmp_path, page)
    attachment = tmp_path / "source.txt"
    attachment.write_text("source", encoding="utf-8")

    await adapter.upload_file(attachment)

    assert page.attachment_tile_snapshot_index == 2


def test_attachment_label_match_rejects_remove_button_label():
    assert gpt_module._attachment_label_matches("source.txt", "source.txt")
    assert gpt_module._attachment_label_matches("source(12).txt", "source.txt")
    assert not gpt_module._attachment_label_matches(
        "Remove file 1: source(12).txt",
        "source.txt",
    )


@pytest.mark.asyncio
async def test_send_prompt_waits_for_new_stable_response(tmp_path):
    page = FakePage()
    page.url = "https://chatgpt.com/c/demo"
    adapter = running_adapter(tmp_path, page)

    response = await adapter.send_prompt("Rewrite this transcript")

    assert page.prompt.filled == "Rewrite this transcript"
    assert page.send_button.clicked
    assert response.text == "Rewritten script"
    assert response.conversation_url == "https://chatgpt.com/c/demo"


@pytest.mark.asyncio
async def test_send_prompt_accepts_navigation_fallback_textarea(tmp_path):
    page = FakePage()
    page.prompt_selector = 'textarea[name="prompt-textarea"]'
    adapter = running_adapter(tmp_path, page)

    response = await adapter.send_prompt("Validate this rewrite")

    assert page.prompt.filled == "Validate this rewrite"
    assert page.send_button.clicked
    assert response.text == "Rewritten script"


@pytest.mark.asyncio
async def test_assistant_text_excludes_interactive_citation_buttons(tmp_path):
    page = FakePage()
    adapter = running_adapter(tmp_path, page)
    message = FakeLocator(text='{"content_summary":"clean"}')

    text = await adapter._assistant_text(message)

    assert text == '{"content_summary":"clean"}'
    assert "querySelectorAll('button')" in message.evaluate_script


@pytest.mark.asyncio
async def test_send_prompt_waits_for_delayed_streaming_indicator(tmp_path):
    page = FakePage()
    page.stop_button = SequenceVisibleLocator([False, True, True, False])
    adapter = running_adapter(tmp_path, page, stream_start_grace_s=1)

    response = await adapter.send_prompt("Rewrite this transcript")

    assert response.text == "Rewritten script"
    assert page.stop_button.values == []


@pytest.mark.asyncio
async def test_stable_response_ignores_thinking_placeholder(tmp_path):
    page = FakePage()
    sequence = SequenceTextLocator(["Thinking...", "Thinking...", '{"body":"done"}'])
    original_locator = page.locator

    def locator(selector: str):
        if selector == gpt_module.ASSISTANT_MESSAGE_SELECTOR:
            return sequence
        return original_locator(selector)

    page.locator = locator
    adapter = running_adapter(tmp_path, page)

    text = await adapter._wait_for_stable_response(page)

    assert text == '{"body":"done"}'


@pytest.mark.asyncio
async def test_send_prompt_rejects_empty_assistant_response(tmp_path):
    page = FakePage()
    page.next_response = ""
    adapter = running_adapter(tmp_path, page, response_timeout_s=0.001)

    with pytest.raises(PipelineError) as error:
        await adapter.send_prompt("Rewrite")

    assert error.value.info.code == "GPT_OUTPUT_INVALID"
    assert not error.value.info.retryable


@pytest.mark.asyncio
async def test_send_prompt_classifies_timeout_and_browser_crash(tmp_path):
    timeout_page = FakePage()
    timeout_page.send_button = FakeLocator(count=1, visible=True)
    timeout_adapter = running_adapter(tmp_path, timeout_page, response_timeout_s=0)
    with pytest.raises(PipelineError) as timeout_error:
        await timeout_adapter.send_prompt("Rewrite")
    assert timeout_error.value.info.code == "GPT_RESPONSE_TIMEOUT"
    assert timeout_error.value.info.retryable

    crash_page = FakePage()
    crash_page.prompt = FakeLocator(error=RuntimeError("Target closed"))
    crash_adapter = running_adapter(tmp_path, crash_page)
    with pytest.raises(PipelineError) as browser_error:
        await crash_adapter.send_prompt("Rewrite")
    assert browser_error.value.info.code == "GPT_BROWSER_CRASHED"
    assert browser_error.value.info.retryable


@pytest.mark.asyncio
async def test_run_prompt_recovers_completed_request_without_resending(tmp_path):
    page = FakePage()
    page.url = "https://chatgpt.com/c/recovered"
    page.messages = [
        {"role": "user", "text": "[YT_PRO_MAX_REQUEST:section-2] Rewrite"},
        {"role": "assistant", "text": "Recovered result"},
    ]
    adapter = running_adapter(tmp_path, page)

    response = await adapter.run_prompt(
        "[YT_PRO_MAX_REQUEST:section-2] Rewrite",
        conversation_url=page.url,
        request_id="section-2",
    )

    assert response.text == "Recovered result"
    assert not page.send_button.clicked


@pytest.mark.asyncio
async def test_run_prompt_ignores_transient_recovery_placeholder(tmp_path):
    page = FakePage()
    page.url = "https://chatgpt.com/c/recovered"
    user_message = {"role": "user", "text": "[YT_PRO_MAX_REQUEST:section-3] Rewrite"}
    page.message_snapshots = [
        [user_message, {"role": "assistant", "text": "Thinking..."}],
        [user_message, {"role": "assistant", "text": "Thinking..."}],
        [user_message, {"role": "assistant", "text": "Finished rewrite"}],
        [user_message, {"role": "assistant", "text": "Finished rewrite"}],
    ]
    adapter = running_adapter(tmp_path, page)

    response = await adapter.run_prompt(
        "[YT_PRO_MAX_REQUEST:section-3] Rewrite",
        conversation_url=page.url,
        request_id="section-3",
    )

    assert response.text == "Finished rewrite"
    assert page.message_snapshot_index == 4
    assert not page.send_button.clicked


@pytest.mark.asyncio
async def test_open_conversation_rejects_external_url(tmp_path):
    adapter = running_adapter(tmp_path, FakePage())

    with pytest.raises(PipelineError) as error:
        await adapter.open_conversation("https://example.com/chat")

    assert error.value.info.code == "GPT_OUTPUT_INVALID"


@pytest.mark.asyncio
async def test_open_conversation_reopens_saved_chatgpt_url(tmp_path):
    page = FakePage()
    adapter = running_adapter(tmp_path, page)
    conversation_url = "https://chatgpt.com/c/saved-conversation"

    opened_page = await adapter.open_conversation(conversation_url)

    assert opened_page is page
    assert page.goto_calls == [conversation_url]
