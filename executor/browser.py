"""Browser lifecycle management.

Manages multiple browser instances that are reused across requests.
Each test gets a fresh browser context for isolation.
"""

import asyncio
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright

from .logging import get_logger

logger = get_logger(__name__)


# Stealth script to mask automation detection in headless mode
# This helps bypass basic bot detection on sites like ADP
# Note: Platform-specific values are injected at runtime via STEALTH_SCRIPT_TEMPLATE
STEALTH_SCRIPT_TEMPLATE = """
// Mask navigator.webdriver
Object.defineProperty(navigator, 'webdriver', {{
    get: () => undefined,
    configurable: true
}});

// Mask chrome.runtime for older detection methods
if (!window.chrome) {{
    window.chrome = {{}};
}}
if (!window.chrome.runtime) {{
    window.chrome.runtime = {{}};
}}

// Mask permissions query
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications' ?
        Promise.resolve({{ state: Notification.permission }}) :
        originalQuery(parameters)
);

// Mask plugins (headless has 0 plugins)
Object.defineProperty(navigator, 'plugins', {{
    get: () => [
        {{ name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' }},
        {{ name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' }},
        {{ name: 'Native Client', filename: 'internal-nacl-plugin' }},
    ],
    configurable: true
}});

// Mask languages
Object.defineProperty(navigator, 'languages', {{
    get: () => ['en-US', 'en'],
    configurable: true
}});

// Mask platform to match user-agent (injected at runtime)
Object.defineProperty(navigator, 'platform', {{
    get: () => '{platform}',
    configurable: true
}});

// Mask hardware concurrency (headless often has different value)
Object.defineProperty(navigator, 'hardwareConcurrency', {{
    get: () => 8,
    configurable: true
}});

// Mask deviceMemory (headless may have unusual values)
Object.defineProperty(navigator, 'deviceMemory', {{
    get: () => 8,
    configurable: true
}});

// Mask connection type (headless often missing)
if (navigator.connection) {{
    Object.defineProperty(navigator.connection, 'effectiveType', {{
        get: () => '4g',
        configurable: true
    }});
}}

// Mask WebGL vendor/renderer (headless has different values)
const getParameterProxyHandler = {{
    apply: function(target, thisArg, args) {{
        const param = args[0];
        const gl = thisArg;
        // UNMASKED_VENDOR_WEBGL
        if (param === 37445) {{
            return 'Google Inc. (NVIDIA)';
        }}
        // UNMASKED_RENDERER_WEBGL
        if (param === 37446) {{
            return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1080 Direct3D11 vs_5_0 ps_5_0, D3D11)';
        }}
        return Reflect.apply(target, thisArg, args);
    }}
}};

// Apply WebGL masking
const canvas = document.createElement('canvas');
const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
if (gl) {{
    const originalGetParameter = gl.getParameter.bind(gl);
    gl.getParameter = new Proxy(originalGetParameter, getParameterProxyHandler);
}}
const gl2 = canvas.getContext('webgl2');
if (gl2) {{
    const originalGetParameter2 = gl2.getParameter.bind(gl2);
    gl2.getParameter = new Proxy(originalGetParameter2, getParameterProxyHandler);
}}

// Console debug message
console.debug('Stealth script loaded (platform: {platform})');
"""


def get_stealth_script(is_linux: bool = False) -> str:
    """Get stealth script with platform-appropriate values.

    Args:
        is_linux: True if running on Linux (e.g., containers)

    Returns:
        JavaScript stealth script with correct platform
    """
    platform = "Linux x86_64" if is_linux else "MacIntel"
    return STEALTH_SCRIPT_TEMPLATE.format(platform=platform)


# Browser display names for UI
# "chromium" = Playwright's bundled Chromium build (not system Chrome)
# "chrome"   = system Google Chrome installed on the machine (channel="chrome")
BROWSER_DISPLAY_NAMES = {
    "chromium": "Google Chrome",
    "chromium-headless": "Google Chrome (Headless)",
    "chrome": "Chrome",
    "chrome-headless": "Chrome (Headless)",
    "firefox": "Firefox",
    "firefox-headless": "Firefox (Headless)",
    "webkit": "Safari",
    "webkit-headless": "Safari (Headless)",
}


def parse_available_browsers() -> list[str]:
    """Parse AVAILABLE_BROWSERS from environment.

    Format: comma-separated list of browser identifiers.
    Examples:
        - "chrome,chrome-headless" (local dev)
        - "chromium-headless" (CI/production)
        - "chrome,chromium-headless,firefox" (full testing)

    Valid identifiers:
        - chromium, chromium-headless
        - chrome, chrome-headless
        - firefox, firefox-headless
        - webkit, webkit-headless
    """
    env_value = os.getenv("AVAILABLE_BROWSERS", "chromium,chromium-headless")
    browsers = [b.strip().lower() for b in env_value.split(",") if b.strip()]

    # Validate browser identifiers
    valid_browsers = []
    for browser in browsers:
        if browser in BROWSER_DISPLAY_NAMES:
            valid_browsers.append(browser)
        else:
            logger.warning(f"Unknown browser identifier: {browser}")

    if not valid_browsers:
        logger.warning("No valid browsers configured, defaulting to chromium-headless")
        valid_browsers = ["chromium-headless"]

    return valid_browsers


def get_browser_info(browser_id: str) -> dict:
    """Get browser info for API response."""
    return {
        "id": browser_id,
        "name": BROWSER_DISPLAY_NAMES.get(browser_id, browser_id),
        "headless": browser_id.endswith("-headless"),
    }


def _macos_activate_browser(base_type: str) -> None:
    """Best-effort macOS window activation via osascript.

    On macOS, focus stealing is restricted by default. osascript can bypass
    this restriction for apps with the correct bundle ID. This is a no-op
    on non-macOS platforms or if the app name is unknown.
    """
    # Map browser base type to the macOS app name osascript uses
    app_names: dict[str, str] = {
        "firefox": "Firefox",
        "chrome": "Google Chrome",
        "chromium": "Chromium",
        # WebKit: Playwright bundles its own WebKit binary — no standard app name
    }
    app_name = app_names.get(base_type)
    if not app_name:
        return
    try:
        subprocess.run(
            ["osascript", "-e", f'tell application "{app_name}" to activate'],
            timeout=2,
            capture_output=True,
        )
    except Exception:
        pass  # Best-effort only — never block test execution


class BrowserManager:
    """Manages multiple browser instances.

    Browser instances are reused across requests for performance.
    Each test execution gets a fresh context (isolated cookies/storage).
    """

    def __init__(self):
        self._playwright: Playwright | None = None
        self._browsers: dict[str, Browser] = {}  # browser_id -> Browser instance
        self._available_browsers: list[str] = []
        self._timeout: int = 30000
        self._default_browser: str = "chromium-headless"
        self._preload: bool = True

    async def start(self, timeout: int = 30000) -> None:
        """Start available browsers.

        Args:
            timeout: Default timeout for operations in ms
        """
        self._timeout = timeout
        self._available_browsers = parse_available_browsers()
        self._default_browser = self._available_browsers[0] if self._available_browsers else "chromium-headless"

        self._preload = os.getenv("BROWSER_PRELOAD", "true").lower() not in ("false", "0", "no")
        self._playwright = await async_playwright().start()

        if self._preload:
            logger.info(f"Starting browsers (eager): {self._available_browsers}")
            for browser_id in self._available_browsers:
                await self._start_browser(browser_id)
            logger.info(f"All browsers started. Default: {self._default_browser}")
        else:
            logger.info(f"Lazy mode: browsers start on first use. Configured: {self._available_browsers}")

    async def _start_browser(self, browser_id: str) -> None:
        """Start a specific browser instance."""
        if browser_id in self._browsers:
            logger.warning(f"Browser {browser_id} already started")
            return

        # Parse browser type and headless mode from identifier
        headless = browser_id.endswith("-headless")
        base_type = browser_id.replace("-headless", "")

        logger.info(f"Starting browser: {browser_id} (type={base_type}, headless={headless})")

        # Common launch args for chromium-based browsers
        chromium_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
        ]

        # Allow ignoring SSL errors for enterprise proxies
        if os.getenv("BROWSER_IGNORE_SSL_ERRORS", "").lower() in ("true", "1", "yes"):
            chromium_args.append("--ignore-certificate-errors")

        # For non-headless mode, add args to ensure window is visible
        if not headless:
            chromium_args.extend([
                "--start-maximized",
                "--window-position=100,100",
            ])

        # Check for custom executable paths (for system-installed browsers)
        chromium_path = os.getenv("CHROMIUM_EXECUTABLE_PATH")
        firefox_path = os.getenv("FIREFOX_EXECUTABLE_PATH")
        webkit_path = os.getenv("WEBKIT_EXECUTABLE_PATH")

        try:
            if base_type == "chrome":
                browser = await self._playwright.chromium.launch(
                    headless=headless,
                    channel="chrome",
                    args=chromium_args,
                )
            elif base_type == "firefox":
                launch_opts: dict = {"headless": headless}
                if not headless:
                    # Force window to foreground on macOS; harmless on other platforms
                    launch_opts["args"] = ["-foreground"]
                if firefox_path:
                    launch_opts["executable_path"] = firefox_path
                browser = await self._playwright.firefox.launch(**launch_opts)
            elif base_type == "webkit":
                launch_opts = {"headless": headless}
                if webkit_path:
                    launch_opts["executable_path"] = webkit_path
                browser = await self._playwright.webkit.launch(**launch_opts)
            else:
                # Default: chromium (bundled with Playwright or system-installed)
                launch_opts = {"headless": headless, "args": chromium_args}
                if chromium_path:
                    launch_opts["executable_path"] = chromium_path
                browser = await self._playwright.chromium.launch(**launch_opts)

            self._browsers[browser_id] = browser
            logger.info(f"Browser {browser_id} started successfully")

        except Exception as e:
            logger.error(f"Failed to start browser {browser_id}: {e}")
            # Remove from available list if failed to start
            if browser_id in self._available_browsers:
                self._available_browsers.remove(browser_id)

    async def _ensure_browser(self, browser_id: str) -> None:
        """Start a browser on demand if not already running (lazy mode)."""
        if browser_id not in self._browsers:
            if browser_id not in self._available_browsers:
                raise RuntimeError(
                    f"Browser '{browser_id}' not available. Available: {', '.join(self._available_browsers)}"
                )
            await self._start_browser(browser_id)

    async def stop(self) -> None:
        """Stop all browsers and cleanup resources."""
        for browser_id, browser in list(self._browsers.items()):
            logger.info(f"Stopping browser: {browser_id}")
            try:
                await browser.close()
            except Exception as e:
                logger.error(f"Error closing browser {browser_id}: {e}")

        self._browsers.clear()

        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

        logger.info("All browsers stopped")

    def get_browser(self, browser_id: str | None = None) -> Browser | None:
        """Get a browser instance by ID.

        Args:
            browser_id: Browser identifier (e.g., "chrome", "chromium-headless")
                       If None, returns the default browser.
        """
        if browser_id is None:
            browser_id = self._default_browser
        return self._browsers.get(browser_id)

    @property
    def available_browsers(self) -> list[str]:
        """Get list of available browser IDs."""
        return self._available_browsers.copy()

    @property
    def default_browser(self) -> str:
        """Get the default browser ID."""
        return self._default_browser

    @property
    def is_running(self) -> bool:
        """Check if at least one browser is running."""
        return any(b.is_connected() for b in self._browsers.values())

    def get_config(self) -> dict:
        """Return current preload setting and per-browser running status."""
        return {
            "preload": self._preload,
            "browsers": [
                {**get_browser_info(bid), "running": bid in self._browsers}
                for bid in self._available_browsers
            ],
        }

    async def set_preload(self, value: bool) -> None:
        """Enable or disable browser pre-warming.

        When enabling, immediately starts any browsers not yet running.
        When disabling, running browsers are left open (non-disruptive).
        """
        self._preload = value
        if value:
            for browser_id in self._available_browsers:
                if browser_id not in self._browsers:
                    await self._start_browser(browser_id)

    @asynccontextmanager
    async def new_context(
        self,
        browser_id: str | None = None,
        viewport: dict | None = None,
        user_agent: str | None = None,
    ) -> AsyncGenerator[BrowserContext, None]:
        """Create a new browser context for test isolation.

        Contexts are isolated - they have separate cookies, storage, etc.

        Args:
            browser_id: Which browser to use (default: first available)
            viewport: Viewport size {width, height}
            user_agent: Custom user agent string

        Yields:
            Browser context for test execution
        """
        if browser_id is None:
            browser_id = self._default_browser

        await self._ensure_browser(browser_id)

        browser = self.get_browser(browser_id)
        if not browser:
            available = ", ".join(self._available_browsers)
            raise RuntimeError(
                f"Browser '{browser_id}' not available. Available: {available}"
            )

        is_headless = browser_id.endswith("-headless")

        context_options = {
            "viewport": viewport or {"width": 1280, "height": 720},
        }

        # Detect if running on Linux (containers, CI, etc.)
        import platform
        is_linux = platform.system() == "Linux"

        # For headless mode, use a realistic user-agent to avoid detection
        if user_agent:
            context_options["user_agent"] = user_agent
        elif is_headless:
            # Use a realistic Chrome user-agent matching the actual platform
            # Keep Chrome version current (update periodically)
            if is_linux:
                context_options["user_agent"] = (
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                )
            else:
                context_options["user_agent"] = (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                )

        context = await browser.new_context(**context_options)
        context.set_default_timeout(self._timeout)

        # Add stealth script to mask automation detection
        if is_headless:
            await context.add_init_script(get_stealth_script(is_linux=is_linux))

        try:
            yield context
        finally:
            await context.close()

    @asynccontextmanager
    async def new_page(
        self,
        browser_id: str | None = None,
        viewport: dict | None = None,
        user_agent: str | None = None,
    ) -> AsyncGenerator[Page, None]:
        """Create a new page in a fresh context.

        Convenience method that creates context and page together.

        Args:
            browser_id: Which browser to use (default: first available)
            viewport: Viewport size {width, height}
            user_agent: Custom user agent string

        Yields:
            Page for test execution
        """
        async with self.new_context(
            browser_id=browser_id, viewport=viewport, user_agent=user_agent
        ) as context:
            page = await context.new_page()
            # For headed mode, activate the window so it's visible on screen
            effective_browser = browser_id or self._default_browser
            if not effective_browser.endswith("-headless"):
                await page.bring_to_front()
                # Give macOS time to process the window activation event
                await asyncio.sleep(0.3)
                # On macOS, use osascript to force the browser app to the foreground
                # since Playwright's bring_to_front() only works at the tab level
                if sys.platform == "darwin":
                    base_type = effective_browser.replace("-headless", "")
                    _macos_activate_browser(base_type)
            try:
                yield page
            finally:
                await page.close()


# Global browser manager instance
_browser_manager: BrowserManager | None = None


def get_browser_manager() -> BrowserManager:
    """Get the global browser manager instance."""
    global _browser_manager
    if _browser_manager is None:
        _browser_manager = BrowserManager()
    return _browser_manager


async def startup_browser() -> None:
    """Start browsers on application startup.

    Reads configuration from environment variables:
    - AVAILABLE_BROWSERS: Comma-separated list of browsers (default: chromium-headless)
    - BROWSER_TIMEOUT: Default timeout in ms (default: 30000)
    """
    manager = get_browser_manager()
    timeout = int(os.getenv("BROWSER_TIMEOUT", "30000"))
    await manager.start(timeout=timeout)


async def shutdown_browser() -> None:
    """Stop all browsers on application shutdown."""
    manager = get_browser_manager()
    await manager.stop()
