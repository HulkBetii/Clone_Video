from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from yt_pro_max.errors import PipelineError

LOGGER = logging.getLogger(__name__)

CHATGPT_URL = "https://chatgpt.com/"
AUTH_HOST = "auth.openai.com"
PROFILE_BUTTON_SELECTOR = (
    '[data-testid="accounts-profile-button"], '
    '[data-testid="profile-button"], '
    '[data-testid="user-menu-button"]'
)
BOOTSTRAP_SELECTOR = "script#client-bootstrap"
PROMPT_SELECTORS = (
    "#prompt-textarea",
    'textarea[data-id="root"]',
    '[contenteditable="true"][data-testid="composer-text-input"]',
)
MODEL_SWITCHER_SELECTORS = (
    'button:has-text("Instant")',
    'button:has-text("Auto")',
    'button:has-text("Thinking")',
)
THINKING_OPTION_SELECTORS = (
    '[role="menuitem"]:has-text("Thinking")',
    '[role="menuitemradio"]:has-text("Thinking")',
)
SEND_BUTTON_SELECTORS = (
    'button[data-testid="send-button"]',
    'button[data-testid="composer-send-button"]',
    'button[aria-label*="Send"]',
)
FILE_INPUT_SELECTOR = 'input[type="file"]'
ATTACH_BUTTON_SELECTORS = (
    'button[data-testid="composer-plus-btn"]',
    'button[aria-label*="Attach"]',
    'button[aria-label*="Upload"]',
    'button:has-text("Add files")',
)
ATTACHMENT_ERROR_SELECTOR = (
    '[data-testid*="attachment"][data-state="error"], '
    '[data-testid*="upload-error"], '
    '[role="alert"]:has-text("upload")'
)
ATTACHMENT_PROGRESS_SELECTOR = (
    '[data-testid*="attachment"] [role="progressbar"], '
    '[data-testid*="upload"] [role="progressbar"], '
    '[data-testid*="attachment"] [aria-busy="true"]'
)
ASSISTANT_MESSAGE_SELECTOR = '[data-message-author-role="assistant"]'
CHAT_MESSAGE_SELECTOR = "[data-message-author-role]"
STOP_BUTTON_SELECTOR = (
    'button[data-testid="stop-button"], '
    'button[data-testid="composer-stop-button"], '
    'button[aria-label*="Stop generating"]'
)
TRANSIENT_RESPONSE_PATTERN = re.compile(
    r"^(?:thinking|working|searching|analyzing)(?:\s*\.{3}|\s*\u2026)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ChatGPTResponse:
    text: str
    conversation_url: str


class ChatGPTPlaywrightAdapter:
    def __init__(
        self,
        profile_path: Path | str,
        *,
        chat_url: str = CHATGPT_URL,
        response_timeout_s: float = 180,
        attachment_timeout_s: float = 60,
        navigation_timeout_s: float = 60,
        poll_interval_s: float = 0.5,
        stable_samples: int = 3,
        stream_start_grace_s: float = 2,
    ) -> None:
        self.profile_path = Path(profile_path)
        self.chat_url = chat_url
        self.response_timeout_s = response_timeout_s
        self.attachment_timeout_s = attachment_timeout_s
        self.navigation_timeout_ms = int(navigation_timeout_s * 1000)
        self.poll_interval_s = poll_interval_s
        self.stable_samples = max(1, stable_samples)
        self.stream_start_grace_s = max(0, stream_start_grace_s)

        self._playwright: Any | None = None
        self._context: Any | None = None
        self._page: Any | None = None

    async def start(self) -> None:
        if self._context is not None and self._page is not None:
            return
        if not self.profile_path.is_dir():
            raise PipelineError(
                "GPT_PROFILE_MISSING",
                "The configured ChatGPT browser profile does not exist.",
            )

        async_playwright = _load_async_playwright()
        try:
            self._playwright = await async_playwright().start()
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(self.profile_path),
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
                viewport={"width": 1280, "height": 800},
            )
            pages = list(self._context.pages)
            self._page = pages[0] if pages else await self._context.new_page()
            await self._navigate(self.chat_url)
            await self._require_login()
        except PipelineError:
            await self.close()
            raise
        except Exception as exc:
            await self.close()
            raise _classify_browser_exception(exc) from exc

    async def close(self) -> None:
        context, playwright = self._context, self._playwright
        self._page = None
        self._context = None
        self._playwright = None
        if context is not None:
            try:
                await context.close()
            except Exception:
                LOGGER.debug("Failed to close ChatGPT browser context", exc_info=True)
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                LOGGER.debug("Failed to stop Playwright", exc_info=True)

    async def open_conversation(self, url: str | None = None) -> Any:
        await self.start()
        if self._page is None:
            raise PipelineError(
                "GPT_BROWSER_CRASHED",
                "The ChatGPT browser page is unavailable.",
                retryable=True,
            )
        if url is None:
            return self._page
        target_url = url
        if not _is_chatgpt_url(target_url):
            raise PipelineError(
                "GPT_OUTPUT_INVALID",
                "The saved ChatGPT conversation URL is invalid.",
            )
        if _normalized_url(self._page.url) != _normalized_url(target_url):
            await self._navigate(target_url)
            await self._require_login()
        return self._page

    async def upload_file(self, path: Path | str) -> None:
        file_path = Path(path)
        if not file_path.is_file():
            raise PipelineError("GPT_UPLOAD_FAILED", "The attachment file does not exist.")
        page = await self.open_conversation()

        try:
            existing_matches = await page.get_by_text(file_path.name, exact=False).count()
            direct_input = page.locator(FILE_INPUT_SELECTOR).first
            if await direct_input.count() > 0:
                try:
                    await direct_input.set_input_files(str(file_path))
                except Exception:
                    LOGGER.debug("Direct ChatGPT file input failed; using chooser", exc_info=True)
                    await self._upload_with_file_chooser(page, file_path)
            else:
                await self._upload_with_file_chooser(page, file_path)
            await self._wait_for_attachment(page, file_path.name, existing_matches)
        except PipelineError:
            raise
        except Exception as exc:
            if _looks_like_browser_failure(exc):
                raise _classify_browser_exception(exc) from exc
            raise PipelineError(
                "GPT_UPLOAD_FAILED",
                "ChatGPT could not upload the attachment.",
                retryable=True,
            ) from exc

    async def send_prompt(self, prompt: str) -> ChatGPTResponse:
        if not prompt.strip():
            raise PipelineError("GPT_OUTPUT_INVALID", "The ChatGPT prompt is empty.")
        page = await self.open_conversation()
        try:
            await self._ensure_thinking_mode(page)
            previous_count = await page.locator(ASSISTANT_MESSAGE_SELECTOR).count()
            composer = await self._find_visible(page, PROMPT_SELECTORS)
            if composer is None:
                raise PipelineError(
                    "GPT_OUTPUT_INVALID",
                    "The ChatGPT prompt composer was not found.",
                )
            await composer.click()
            await composer.fill(prompt)
            await self._send(page, composer)
            await self._wait_for_new_assistant(page, previous_count)
            await self._wait_for_streaming(page)
            text = await self._wait_for_stable_response(page)
            return ChatGPTResponse(text=text, conversation_url=page.url or self.chat_url)
        except PipelineError:
            raise
        except Exception as exc:
            raise _classify_browser_exception(exc) from exc

    async def run_prompt(
        self,
        prompt: str,
        attachment: Path | str | None = None,
        conversation_url: str | None = None,
        request_id: str | None = None,
    ) -> ChatGPTResponse:
        page = await self.open_conversation(conversation_url)
        if request_id:
            recovered = await self._recover_request(page, request_id)
            if recovered is not None:
                return recovered
        if attachment is not None:
            await self.upload_file(attachment)
        return await self.send_prompt(prompt)

    async def _navigate(self, url: str) -> None:
        if self._page is None:
            raise PipelineError(
                "GPT_BROWSER_CRASHED",
                "The ChatGPT browser page is unavailable.",
                retryable=True,
            )
        try:
            await self._page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.navigation_timeout_ms,
            )
        except Exception as exc:
            raise _classify_browser_exception(exc) from exc

    async def _require_login(self) -> None:
        if self._page is None:
            raise PipelineError(
                "GPT_BROWSER_CRASHED",
                "The ChatGPT browser page is unavailable.",
                retryable=True,
            )
        page = self._page
        if urlparse(page.url).hostname == AUTH_HOST:
            raise PipelineError(
                "GPT_LOGIN_REQUIRED",
                "The ChatGPT profile requires manual login.",
            )

        try:
            bootstrap_text = await page.locator(BOOTSTRAP_SELECTOR).first.inner_text(timeout=3_000)
            bootstrap = json.loads(bootstrap_text)
            if bootstrap.get("authStatus") == "logged_in":
                return
            if bootstrap.get("authStatus") == "logged_out":
                raise PipelineError(
                    "GPT_LOGIN_REQUIRED",
                    "The ChatGPT profile requires manual login.",
                )
        except PipelineError:
            raise
        except Exception:
            pass

        try:
            profile_button = page.locator(PROFILE_BUTTON_SELECTOR).first
            if await profile_button.is_visible(timeout=5_000):
                return
        except Exception as exc:
            if _looks_like_browser_failure(exc):
                raise _classify_browser_exception(exc) from exc
        raise PipelineError(
            "GPT_LOGIN_REQUIRED",
            "The ChatGPT profile requires manual login.",
        )

    async def _upload_with_file_chooser(self, page: Any, file_path: Path) -> None:
        button = await self._find_visible(page, ATTACH_BUTTON_SELECTORS)
        if button is None:
            raise PipelineError(
                "GPT_UPLOAD_FAILED",
                "The ChatGPT attachment control was not found.",
                retryable=True,
            )
        try:
            async with page.expect_file_chooser(timeout=5_000) as chooser_info:
                await button.click()
            chooser = await chooser_info.value
            await chooser.set_files(str(file_path))
        except Exception as exc:
            if _looks_like_browser_failure(exc):
                raise _classify_browser_exception(exc) from exc
            raise PipelineError(
                "GPT_UPLOAD_FAILED",
                "ChatGPT did not open the attachment chooser.",
                retryable=True,
            ) from exc

    async def _wait_for_attachment(self, page: Any, filename: str, existing_matches: int) -> None:
        deadline = asyncio.get_running_loop().time() + self.attachment_timeout_s
        while asyncio.get_running_loop().time() < deadline:
            try:
                error = page.locator(ATTACHMENT_ERROR_SELECTOR).first
                if await error.count() > 0 and await error.is_visible():
                    raise PipelineError(
                        "GPT_UPLOAD_FAILED",
                        "ChatGPT rejected the attachment.",
                        retryable=True,
                    )
                filename_matches = page.get_by_text(filename, exact=False)
                match_count = await filename_matches.count()
                new_match = filename_matches.nth(existing_matches)
                if match_count > existing_matches and await new_match.is_visible():
                    progress = page.locator(ATTACHMENT_PROGRESS_SELECTOR)
                    if not await self._any_visible(progress):
                        return
            except PipelineError:
                raise
            except Exception as exc:
                if _looks_like_browser_failure(exc):
                    raise _classify_browser_exception(exc) from exc
            await asyncio.sleep(self.poll_interval_s)
        raise PipelineError(
            "GPT_UPLOAD_FAILED",
            "The ChatGPT attachment did not finish uploading in time.",
            retryable=True,
        )

    async def _send(self, page: Any, composer: Any) -> None:
        button = await self._find_visible(page, SEND_BUTTON_SELECTORS, require_enabled=True)
        if button is not None:
            await button.click()
            return
        await composer.press("Enter")

    async def _ensure_thinking_mode(self, page: Any) -> None:
        try:
            switcher = await self._find_visible(page, MODEL_SWITCHER_SELECTORS)
            if switcher is None:
                return
            label = (await switcher.inner_text()).strip()
            if "Thinking" in label:
                return
            await switcher.click()
            option = await self._find_visible(page, THINKING_OPTION_SELECTORS)
            if option is not None:
                await option.click()
        except Exception:
            LOGGER.warning("Could not switch ChatGPT to Thinking mode", exc_info=True)

    async def _wait_for_new_assistant(self, page: Any, previous_count: int) -> None:
        deadline = asyncio.get_running_loop().time() + self.response_timeout_s
        while asyncio.get_running_loop().time() < deadline:
            try:
                if await page.locator(ASSISTANT_MESSAGE_SELECTOR).count() > previous_count:
                    return
            except Exception as exc:
                raise _classify_browser_exception(exc) from exc
            await asyncio.sleep(self.poll_interval_s)
        raise PipelineError(
            "GPT_RESPONSE_TIMEOUT",
            "ChatGPT did not start a new response in time.",
            retryable=True,
        )

    async def _wait_for_streaming(self, page: Any) -> None:
        deadline = asyncio.get_running_loop().time() + self.response_timeout_s
        grace_deadline = min(
            deadline,
            asyncio.get_running_loop().time() + self.stream_start_grace_s,
        )
        saw_streaming = False
        while asyncio.get_running_loop().time() < deadline:
            try:
                streaming = await self._any_visible(page.locator(STOP_BUTTON_SELECTOR))
                saw_streaming = saw_streaming or streaming
                if saw_streaming and not streaming:
                    return
                if not saw_streaming and asyncio.get_running_loop().time() >= grace_deadline:
                    return
            except Exception as exc:
                raise _classify_browser_exception(exc) from exc
            await asyncio.sleep(self.poll_interval_s)
        raise PipelineError(
            "GPT_RESPONSE_TIMEOUT",
            "ChatGPT did not finish generating the response in time.",
            retryable=True,
        )

    async def _wait_for_stable_response(self, page: Any) -> str:
        deadline = asyncio.get_running_loop().time() + self.response_timeout_s
        stable_count = 0
        previous_text = ""
        latest_text = ""
        while asyncio.get_running_loop().time() < deadline:
            try:
                messages = page.locator(ASSISTANT_MESSAGE_SELECTOR)
                count = await messages.count()
                if count:
                    text = (await messages.nth(count - 1).inner_text()).strip()
                    latest_text = text or latest_text
                    if TRANSIENT_RESPONSE_PATTERN.fullmatch(text):
                        stable_count = 0
                    elif text and text == previous_text:
                        stable_count += 1
                    else:
                        stable_count = 1 if text else 0
                    previous_text = text
                    if stable_count >= self.stable_samples:
                        return text
            except Exception as exc:
                raise _classify_browser_exception(exc) from exc
            await asyncio.sleep(self.poll_interval_s)
        if not latest_text:
            raise PipelineError(
                "GPT_OUTPUT_INVALID",
                "ChatGPT returned an empty response.",
            )
        raise PipelineError(
            "GPT_RESPONSE_TIMEOUT",
            "The ChatGPT response did not become stable in time.",
            retryable=True,
        )

    async def _recover_request(self, page: Any, request_id: str) -> ChatGPTResponse | None:
        marker = f"[YT_PRO_MAX_REQUEST:{request_id}]"
        deadline = asyncio.get_running_loop().time() + self.response_timeout_s
        grace_deadline = min(
            deadline,
            asyncio.get_running_loop().time() + self.stream_start_grace_s,
        )
        previous_text = ""
        stable_count = 0
        saw_streaming = False
        while True:
            messages = await self._read_messages(page)
            request_index = next(
                (
                    index
                    for index, message in enumerate(messages)
                    if message["role"] == "user" and marker in message["text"]
                ),
                None,
            )
            if request_index is None:
                return None
            response_text = ""
            for message in messages[request_index + 1 :]:
                if message["role"] == "user":
                    break
                if message["role"] == "assistant" and message["text"].strip():
                    response_text = message["text"].strip()
                    break
            if TRANSIENT_RESPONSE_PATTERN.fullmatch(response_text):
                stable_count = 0
            elif response_text and response_text == previous_text:
                stable_count += 1
            else:
                previous_text = response_text
                stable_count = 1 if response_text else 0
            try:
                is_streaming = await self._any_visible(page.locator(STOP_BUTTON_SELECTOR))
            except Exception as exc:
                raise _classify_browser_exception(exc) from exc
            saw_streaming = saw_streaming or is_streaming
            stream_finished = saw_streaming and not is_streaming
            stream_grace_elapsed = (
                not saw_streaming and asyncio.get_running_loop().time() >= grace_deadline
            )
            if stable_count >= self.stable_samples and (stream_finished or stream_grace_elapsed):
                return ChatGPTResponse(
                    text=response_text,
                    conversation_url=page.url or self.chat_url,
                )
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(self.poll_interval_s)
        raise PipelineError(
            "GPT_RESPONSE_TIMEOUT",
            "The existing ChatGPT request did not finish in time.",
            retryable=True,
        )

    async def _read_messages(self, page: Any) -> list[dict[str, str]]:
        try:
            raw_messages = await page.locator(CHAT_MESSAGE_SELECTOR).evaluate_all(
                """nodes => nodes.map(node => ({
                    role: node.getAttribute('data-message-author-role') || '',
                    text: (node.innerText || node.textContent || '').trim()
                }))"""
            )
        except Exception as exc:
            raise _classify_browser_exception(exc) from exc
        return [
            {"role": str(item.get("role", "")), "text": str(item.get("text", ""))}
            for item in raw_messages
            if isinstance(item, dict)
        ]

    async def _find_visible(
        self,
        page: Any,
        selectors: tuple[str, ...],
        *,
        require_enabled: bool = False,
    ) -> Any | None:
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if await locator.is_visible(timeout=1_000) and (
                    not require_enabled or await locator.is_enabled()
                ):
                    return locator
            except Exception as exc:
                if _looks_like_browser_failure(exc):
                    raise _classify_browser_exception(exc) from exc
        return None

    async def _any_visible(self, locator: Any) -> bool:
        count = await locator.count()
        for index in range(count):
            if await locator.nth(index).is_visible():
                return True
        return False


def _load_async_playwright() -> Any:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise PipelineError(
            "GPT_BROWSER_UNAVAILABLE",
            "Playwright is not installed for the ChatGPT rewrite worker.",
        ) from exc
    return async_playwright


def _classify_browser_exception(exc: BaseException) -> PipelineError:
    if _looks_like_profile_lock(exc):
        return PipelineError(
            "GPT_PROFILE_LOCKED",
            "The configured ChatGPT browser profile is already in use.",
        )
    return PipelineError(
        "GPT_BROWSER_CRASHED",
        "The ChatGPT browser operation failed.",
        retryable=True,
    )


def _looks_like_profile_lock(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(
        fragment in message
        for fragment in (
            "processsingleton",
            "singletonlock",
            "profile is in use",
            "user data directory is already in use",
            "another browser is using",
        )
    )


def _looks_like_browser_failure(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(
        fragment in message
        for fragment in (
            "target closed",
            "browser has been closed",
            "browser closed",
            "context closed",
            "page closed",
            "has been disposed",
            "crashed",
        )
    )


def _is_chatgpt_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname in {"chatgpt.com", "www.chatgpt.com"}


def _normalized_url(url: str) -> str:
    return url.rstrip("/")
