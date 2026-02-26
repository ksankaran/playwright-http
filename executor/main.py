"""FastAPI application for Playwright Executor service.

Provides REST API for browser automation test execution.
"""

import asyncio
import json
import time
import uuid
import os
from contextlib import asynccontextmanager
from dotenv import load_dotenv

# Load .env file before any other imports that read env vars
load_dotenv()
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from .browser import get_browser_manager, get_browser_info, startup_browser, shutdown_browser
from .runner import execute_test
from .recorder import start_recording, stop_recording, get_session, list_sessions
from .logging import setup_logging, get_logger, request_id_var

# Initialize logging
setup_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan - startup and shutdown."""
    # Startup: Start browser
    logger.info("Starting Playwright Executor service")
    await startup_browser()
    yield
    # Shutdown: Stop browser
    logger.info("Shutting down Playwright Executor service")
    await shutdown_browser()


app = FastAPI(
    title="Playwright Executor",
    description="Browser automation test execution service",
    version="0.1.0",
    lifespan=lifespan,
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log all HTTP requests with request ID for correlation."""
    # Use X-Request-ID from upstream (checkmate) or generate new one
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request_id_var.set(request_id)

    start = time.perf_counter()
    logger.info(f"{request.method} {request.url.path}")

    response = await call_next(request)

    duration_ms = (time.perf_counter() - start) * 1000
    logger.info(f"{response.status_code} ({duration_ms:.1f}ms)")

    # Return request ID in response header
    response.headers["X-Request-ID"] = request_id

    return response


# Request/Response models
class TestStep(BaseModel):
    """Individual test step."""

    action: str
    target: str | None = None
    value: str | None = None
    description: str | None = None


class ViewportSize(BaseModel):
    """Browser viewport dimensions."""
    width: int = 1280
    height: int = 720


class TestOptions(BaseModel):
    """Test execution options."""

    browser: str | None = None  # Browser ID (e.g., "chrome", "chromium-headless")
    viewport: ViewportSize | None = None  # Browser viewport size
    timeout: int = 30000
    screenshot_on_failure: bool = True
    viewport: ViewportSize | None = None  # Browser viewport size


class ExecuteRequest(BaseModel):
    """Test execution request."""

    test_id: str | None = None
    base_url: str
    steps: list[TestStep]
    options: TestOptions | None = None


class HealthResponse(BaseModel):
    """Health check response."""

    status: str
    browsers: list[str]
    default_browser: str


class BrowserInfo(BaseModel):
    """Browser information."""

    id: str
    name: str
    headless: bool


class BrowsersResponse(BaseModel):
    """Available browsers response."""

    browsers: list[BrowserInfo]
    default: str


class ConfigUpdate(BaseModel):
    """Update executor runtime configuration."""
    preload: bool


@app.get("/config")
async def get_executor_config() -> dict:
    """Get executor configuration (preload flag + per-browser running status)."""
    return get_browser_manager().get_config()


@app.post("/config")
async def update_executor_config(update: ConfigUpdate) -> dict:
    """Update executor configuration at runtime.

    Setting preload=true immediately starts any browsers not yet running.
    Setting preload=false only updates the flag; running browsers stay open.
    To make permanent, set BROWSER_PRELOAD=false in your .env file.
    """
    await get_browser_manager().set_preload(update.preload)
    return get_browser_manager().get_config()


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Health check endpoint.

    Returns service status and available browsers.
    """
    manager = get_browser_manager()

    return HealthResponse(
        status="ok" if manager.is_running else "degraded",
        browsers=manager.available_browsers,
        default_browser=manager.default_browser,
    )


@app.get("/browsers", response_model=BrowsersResponse)
async def get_browsers() -> BrowsersResponse:
    """Get available browsers.

    Returns list of browsers that can be used for test execution.
    """
    manager = get_browser_manager()

    browsers = [
        BrowserInfo(**get_browser_info(browser_id))
        for browser_id in manager.available_browsers
    ]

    return BrowsersResponse(
        browsers=browsers,
        default=manager.default_browser,
    )


@app.post("/execute")
async def execute_endpoint(request: Request) -> StreamingResponse:
    """Execute test steps with SSE streaming.

    Streams events as the test executes:
    - started: Test execution started
    - step_started: Individual step starting
    - step_completed: Individual step completed (with status, duration, error)
    - completed: Test execution completed (with summary)

    Returns:
        SSE stream of test execution events
    """
    # Parse request body
    body = await request.json()

    # Convert to dict for runner
    test_request = {
        "test_id": body.get("test_id"),
        "base_url": body.get("base_url", ""),
        "steps": body.get("steps", []),
        "options": body.get("options", {}),
    }

    async def event_stream():
        """Generate SSE events."""
        # Queue for events from the callback
        event_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def callback(event: dict[str, Any]) -> None:
            """Put events into queue."""
            await event_queue.put(event)

        async def run_test():
            """Run test and signal completion."""
            try:
                browser_manager = get_browser_manager()
                await execute_test(browser_manager, test_request, callback)
            except Exception as e:
                logger.error(f"Test execution error: {e}")
                await event_queue.put({
                    "type": "error",
                    "error": str(e),
                })
            finally:
                # Signal end of stream
                await event_queue.put(None)

        # Start test execution in background
        task = asyncio.create_task(run_test())

        try:
            while True:
                event = await event_queue.get()
                if event is None:
                    # End of stream
                    break
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable buffering in nginx
        },
    )


@app.post("/execute/sync")
async def execute_sync_endpoint(request: ExecuteRequest) -> JSONResponse:
    """Execute test steps synchronously (non-streaming).

    For clients that don't need real-time updates.

    Returns:
        JSON response with test results
    """
    test_request = {
        "test_id": request.test_id,
        "base_url": request.base_url,
        "steps": [s.model_dump() for s in request.steps],
        "options": request.options.model_dump() if request.options else {},
    }

    # Collect all events
    events: list[dict[str, Any]] = []

    async def callback(event: dict[str, Any]) -> None:
        events.append(event)

    browser_manager = get_browser_manager()
    result = await execute_test(browser_manager, test_request, callback)

    return JSONResponse(content={
        "result": result,
        "events": events,
    })


# ── Scan elements endpoint (used by the healer) ───────────────────────────────


class ScanElementsRequest(BaseModel):
    """Request to scan interactive elements on a page."""
    url: str
    timeout: int = 15000  # ms to wait for page load


class ScanElementsResponse(BaseModel):
    """Interactive elements visible on the scanned page."""
    url: str
    elements: list[str]   # visible text of each interactive element, deduped


@app.post("/scan-elements", response_model=ScanElementsResponse)
async def scan_elements(request: ScanElementsRequest) -> ScanElementsResponse:
    """Navigate headlessly to a URL and return all visible interactive element texts.

    Used by the Auto-Heal pipeline so the LLM knows what elements actually exist
    on the page — enabling it to match stale/misspelled selectors to real ones.
    """
    from playwright.async_api import async_playwright

    elements: list[str] = []

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": 1280, "height": 720},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()

            try:
                await page.goto(request.url, wait_until="domcontentloaded", timeout=request.timeout)
                # Brief wait for dynamic menus / hydration
                await page.wait_for_timeout(800)

                # Extract visible text from every interactive element
                raw: list[str] = await page.evaluate("""() => {
                    const selectors = [
                        'a', 'button', '[role="button"]', '[role="menuitem"]',
                        '[role="link"]', '[role="tab"]', 'nav *',
                        'h1', 'h2', 'h3',
                        'input[type="submit"]', 'input[type="button"]',
                        '[aria-label]',
                    ];
                    const seen = new Set();
                    const results = [];
                    selectors.forEach(sel => {
                        document.querySelectorAll(sel).forEach(el => {
                            // Prefer aria-label, then visible text
                            const label = el.getAttribute('aria-label') || el.innerText || el.textContent || '';
                            const text = label.trim().replace(/\\s+/g, ' ');
                            if (text && text.length <= 80 && !seen.has(text)) {
                                // Skip if element is hidden
                                const style = window.getComputedStyle(el);
                                if (style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0') {
                                    seen.add(text);
                                    results.push(text);
                                }
                            }
                        });
                    });
                    return results;
                }""")

                elements = raw[:120]  # cap to avoid token overload
                logger.info(f"scan-elements: found {len(elements)} elements at {request.url}")
            finally:
                await context.close()
                await browser.close()
    except Exception as exc:
        logger.warning(f"scan-elements failed for {request.url}: {exc}")
        # Return empty list — healer gracefully falls back to screenshot-only mode

    return ScanElementsResponse(url=request.url, elements=elements)


# ── Recorder endpoints ────────────────────────────────────────────────────────


class RecordStartRequest(BaseModel):
    """Start a recording session."""
    base_url: str
    viewport: ViewportSize | None = None


class RecordStartResponse(BaseModel):
    session_id: str
    ws_url: str


@app.post("/recorder/start", response_model=RecordStartResponse)
async def recorder_start(request: Request, body: RecordStartRequest) -> RecordStartResponse:
    """Start a new recording session with a headed browser."""
    vp = body.viewport.model_dump() if body.viewport else None
    session = await start_recording(base_url=body.base_url, viewport=vp)
    host = request.headers.get("host", "localhost:8932")
    ws_url = f"ws://{host}/recorder/ws/{session.session_id}"
    return RecordStartResponse(session_id=session.session_id, ws_url=ws_url)


@app.post("/recorder/stop")
async def recorder_stop(body: dict) -> JSONResponse:
    """Stop a recording session and return captured events."""
    session_id = body.get("session_id", "")
    try:
        result = await stop_recording(session_id)
        return JSONResponse(content=result)
    except ValueError as e:
        return JSONResponse(status_code=404, content={"detail": str(e)})


@app.get("/recorder/status")
async def recorder_status() -> JSONResponse:
    """List active recording sessions."""
    return JSONResponse(content={"sessions": list_sessions()})


@app.get("/recorder/events/{session_id}")
async def recorder_get_events(session_id: str) -> JSONResponse:
    """Return events captured so far for a session without stopping it."""
    session = get_session(session_id)
    if not session:
        return JSONResponse(status_code=404, content={"detail": "Session not found"})
    return JSONResponse(content={"events": session.events, "count": len(session.events)})


@app.websocket("/recorder/ws/{session_id}")
async def recorder_websocket(websocket: WebSocket, session_id: str):
    """Bidirectional WebSocket for a recording session.

    - Outbound: raw DOM events as they are captured
    - Inbound: control commands (pause, resume, stop)
    """
    session = get_session(session_id)
    if not session:
        await websocket.close(code=4004, reason="Session not found")
        return

    await websocket.accept()
    logger.info(f"[{session_id}] WebSocket connected")

    event_queue: asyncio.Queue[dict | None] = asyncio.Queue()

    # Wire up the session's on_event callback to push into our queue
    async def forward_event(event: dict):
        await event_queue.put(event)

    session.on_event = forward_event

    # Send any events that were captured before the WS connected
    for existing_event in session.events:
        await websocket.send_json(existing_event)

    async def send_events():
        """Forward events from queue to WebSocket."""
        try:
            while True:
                event = await event_queue.get()
                if event is None:
                    break
                await websocket.send_json(event)
        except Exception:
            pass

    async def receive_commands():
        """Listen for control commands from the client."""
        try:
            while True:
                data = await websocket.receive_json()
                cmd = data.get("command")
                if cmd == "stop":
                    await stop_recording(session_id)
                    await event_queue.put(None)
                    break
        except WebSocketDisconnect:
            logger.info(f"[{session_id}] WebSocket disconnected")
        except Exception as e:
            logger.warning(f"[{session_id}] WebSocket receive error: {e}")

    sender = asyncio.create_task(send_events())
    receiver = asyncio.create_task(receive_commands())

    try:
        done, pending = await asyncio.wait(
            [sender, receiver], return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info(f"[{session_id}] WebSocket closed")


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8932"))
    uvicorn.run(app, host="0.0.0.0", port=port)
