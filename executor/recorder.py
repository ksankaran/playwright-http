"""Recording session manager — CDP-based user interaction capture.

Launches a headed Chromium browser, injects the recorder overlay script,
and streams raw DOM events back via a callback.
"""

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Awaitable

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Playwright,
)

from .logging import get_logger

logger = get_logger(__name__)

# Path to the JS overlay script injected into each page.
# Loaded dynamically on each recording start so JS changes take effect
# without an executor restart (no need to kill uvicorn after editing the JS).
_RECORDER_SCRIPT_PATH = Path(__file__).parent / "recorder_script.js"

RECORDER_EVENT_PREFIX = "__RECORDER__:"


@dataclass
class RecordingSession:
    """Represents an active recording session."""

    session_id: str
    base_url: str
    playwright: Playwright
    browser: Browser
    context: BrowserContext
    page: Page
    # Callback invoked for each captured raw event
    on_event: Callable[[dict], Awaitable[None]] | None = None
    _stopped: bool = False
    events: list[dict] = field(default_factory=list)


# ── Global session store ─────────────────────────────────────────────────────
_sessions: dict[str, RecordingSession] = {}


def get_session(session_id: str) -> RecordingSession | None:
    return _sessions.get(session_id)


def list_sessions() -> list[dict]:
    return [
        {
            "session_id": s.session_id,
            "base_url": s.base_url,
            "event_count": len(s.events),
        }
        for s in _sessions.values()
        if not s._stopped
    ]


async def start_recording(
    base_url: str,
    viewport: dict | None = None,
    on_event: Callable[[dict], Awaitable[None]] | None = None,
) -> RecordingSession:
    """Launch a headed browser and begin recording user interactions.

    Args:
        base_url: The initial URL to navigate to.
        viewport: Optional {width, height} dict.
        on_event: Async callback invoked for each raw event.

    Returns:
        A RecordingSession handle.
    """
    session_id = uuid.uuid4().hex[:12]
    vp = viewport or {"width": 1280, "height": 720}

    logger.info(f"[{session_id}] Starting recording session → {base_url}")

    pw = await async_playwright().start()

    chromium_args = [
        "--disable-blink-features=AutomationControlled",
        "--start-maximized",
        "--window-position=100,100",
    ]

    # Allow custom chromium path (e.g. system Chrome)
    launch_opts: dict = {"headless": False, "args": chromium_args}
    chromium_path = os.getenv("CHROMIUM_EXECUTABLE_PATH")
    if chromium_path:
        launch_opts["executable_path"] = chromium_path

    browser = await pw.chromium.launch(**launch_opts)

    context = await browser.new_context(viewport=vp)
    page = await context.new_page()

    session = RecordingSession(
        session_id=session_id,
        base_url=base_url,
        playwright=pw,
        browser=browser,
        context=context,
        page=page,
        on_event=on_event,
    )
    _sessions[session_id] = session

    # Inject recorder overlay script into every frame/navigation.
    # Read fresh on each session so JS edits take effect without restarting.
    recorder_script = _RECORDER_SCRIPT_PATH.read_text()
    await context.add_init_script(recorder_script)

    # Listen for recorder events sent via console.debug
    page.on("console", lambda msg: _handle_console(session, msg))

    # Capture top-level navigation events that the JS script can't detect
    page.on("framenavigated", lambda frame: _handle_navigation(session, frame))

    # Navigate to the starting URL
    try:
        await page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        logger.warning(f"[{session_id}] Initial navigation issue: {e}")

    logger.info(f"[{session_id}] Recording started. Browser window open.")
    return session


def _handle_console(session: RecordingSession, msg) -> None:
    """Parse console.debug messages that carry recorder events."""
    if session._stopped:
        return
    text = msg.text
    if not text.startswith(RECORDER_EVENT_PREFIX):
        return
    try:
        event = json.loads(text[len(RECORDER_EVENT_PREFIX):])
        session.events.append(event)
        if session.on_event:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(session.on_event(event))
            except RuntimeError:
                pass
    except json.JSONDecodeError:
        logger.warning(f"[{session.session_id}] Malformed recorder event")


def _handle_navigation(session: RecordingSession, frame) -> None:
    """Emit a navigate event when the main frame URL changes."""
    if session._stopped:
        return
    if frame != session.page.main_frame:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    # Use wall-clock time (time.time) to match Date.now() from recorder_script.js.
    # The click-navigate debounce in RecorderEventProcessor compares timestamps
    # between click events (Date.now()) and navigate events — they must share the
    # same time base for the 500ms window check to work.
    event = {
        "type": "navigate",
        "timestamp": int(time.time() * 1000),
        "selector": "",
        "tag": "",
        "text": "",
        "value": frame.url,
        "url": frame.url,
    }
    session.events.append(event)
    if session.on_event:
        loop.create_task(session.on_event(event))


async def stop_recording(session_id: str) -> dict:
    """Stop a recording session and close the browser.

    Returns:
        Summary dict with session_id, event_count, and events list.
    """
    session = _sessions.get(session_id)
    if not session:
        raise ValueError(f"No session with id {session_id}")

    session._stopped = True
    logger.info(
        f"[{session_id}] Stopping recording. {len(session.events)} events captured."
    )

    try:
        await session.page.close()
    except Exception:
        pass
    try:
        await session.context.close()
    except Exception:
        pass
    try:
        await session.browser.close()
    except Exception:
        pass
    try:
        await session.playwright.stop()
    except Exception:
        pass

    events = session.events.copy()
    del _sessions[session_id]

    return {
        "session_id": session_id,
        "event_count": len(events),
        "events": events,
    }
