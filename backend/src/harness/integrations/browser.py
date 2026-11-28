"""Dedicated, policy constrained Playwright browser for the harness."""
from __future__ import annotations

import asyncio
import base64
import hmac
import importlib
import ipaddress
import os
import re
import socket
import secrets
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from harness.core.utils import ActionPreflightError

TIMEOUT = 20_000
PROFILE_MARKER = ".personal-assistant-profile"
PROFILE_ID = "personal-assistant-harness/browser-profile/v1"
MANUAL_SECONDS = 600
MANUAL_KEYS = {"Tab", "Shift+Tab", "Enter", "Escape", "Backspace", "Delete", "ArrowUp",
               "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown", "Control+a"}


class ManualBrowserError(ValueError):
    """An owner-facing takeover error that contains no page or input data."""


def is_public_browser_address(address: str) -> bool:
    """Return true only for ordinary globally routable addresses."""
    try:
        ip = ipaddress.ip_address(address.strip().strip("[]").split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv4Address):
        value = int(ip)
        a, b, c = (value >> 24, (value >> 16) & 255, (value >> 8) & 255)
        return not (
            a in (0, 10, 127) or a >= 224 or
            (a == 100 and 64 <= b <= 127) or (a == 169 and b == 254) or
            (a == 172 and 16 <= b <= 31) or
            (a == 192 and (b in (0, 168) or (b == 88 and c == 99))) or
            (a == 198 and (b in (18, 19) or (b == 51 and c == 100))) or
            (a == 203 and b == 0 and c == 113)
        )
    groups = ip.exploded.split(":")
    first, second = int(groups[0], 16), int(groups[1], 16)
    return first >= 0x2000 and first < 0x3fff and first != 0x2002 and not (
        first == 0x2001 and (second < 0x200 or second == 0x0db8)
    )


def validate_browser_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError("Browser URL must be a string up to 4096 characters")
    if re.search(r'[\x00-\x20\\]', value):
        raise ValueError('Ambiguous browser URL characters are blocked')
    try:
        parsed = urlsplit(value)
        # Accessing .port/.hostname validates malformed authority syntax.
        host = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Provide a complete public HTTP or HTTPS URL") from exc
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc or not host:
        raise ValueError("Provide a complete public HTTP or HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Credentials are not allowed in browser URLs")
    hostname = host.lower().rstrip(".")
    if re.fullmatch(r'(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*', hostname) and not _is_ip(hostname):
        raise ValueError('Ambiguous numeric browser destinations are blocked')
    if not hostname or "%" in hostname or (":" not in hostname and not _is_ip(hostname) and
            ("." not in hostname or re.search(r"(?:^|\.)(localhost|local|internal|lan|home|test|invalid)$", hostname))):
        raise ValueError("Local and internal browser destinations are blocked")
    if _is_ip(hostname) and not is_public_browser_address(hostname):
        raise ValueError("Private and reserved browser destinations are blocked")
    return value


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


async def assert_public_browser_url(
    value: str,
    address_lookup: Callable[[str], Any] | None = None,
) -> str:
    """Validate syntax and fail closed if any DNS result is non-public."""
    validate_browser_url(value)
    host = urlsplit(value).hostname or ""
    host = host.rstrip(".")
    if _is_ip(host):
        return value

    try:
        if address_lookup is None:
            rows = await asyncio.wait_for(asyncio.to_thread(socket.getaddrinfo, host, None, 0, socket.SOCK_STREAM), timeout=5)
            addresses = [row[4][0] for row in rows]
        else:
            result = address_lookup(host)
            if hasattr(result, "__await__"):
                result = await asyncio.wait_for(result, timeout=5)
            addresses = [item["address"] if isinstance(item, dict) else str(item) for item in result]
    except Exception as exc:
        code = getattr(exc, "errno", None) or getattr(exc, "code", None) or "LOOKUP_FAILED"
        code = re.sub(r"[^A-Z0-9_]", "", str(code).upper())[:40]
        raise ValueError(
            f"Browser DNS validation failed for {host} ({code}). Navigation is blocked because its public destination cannot be verified."
        ) from exc
    if not addresses or any(not is_public_browser_address(item) for item in addresses):
        raise ValueError("Browser destination resolves to a private or reserved address")
    return value


class BrowserService:
    """One isolated persistent browser profile, never the everyday browser profile."""

    def __init__(self, profile_dir: Path, executable_path: str | None = None, channel: str | None = None):
        self.profile_dir = Path(profile_dir).expanduser().resolve()
        if self.profile_dir.name != "browser-profile" or ".assistant" not in self.profile_dir.parts:
            raise ValueError("Dedicated browser profile must be an .assistant/browser-profile directory")
        self.executable_path = executable_path
        self.channel = channel
        self.context: Any = None
        self._playwright: Any = None
        self.visible = False
        self._lock = asyncio.Lock()
        self._page_ids: dict[int, str] = {}
        self.last_error: str | None = None
        self.mutations = 0
        self._manual_token = ""
        self._manual_until = 0.0
        self._manual_tab = ""
        self._manual_frame = None
        self._manual_sequence = 0
        self._manual_navigation = 0

    def _configure_page(self, page: Any) -> None:
        self._page_ids[id(page)] = str(uuid.uuid4())
        def invalidate_frame(frame):
            if frame == page.main_frame:
                self._manual_navigation += 1
        page.on("framenavigated", invalidate_frame)
        page.on("dialog", lambda dialog: asyncio.create_task(dialog.dismiss()))
        page.on("download", lambda download: asyncio.create_task(download.cancel()))

    def _page(self, tab_id: str) -> Any:
        if self.context is None:
            raise ValueError("Browser tab is missing or closed")
        for page in self.context.pages:
            if self._page_ids.get(id(page)) == tab_id and not page.is_closed():
                return page
        raise ValueError("Browser tab is missing or closed")

    async def status(self) -> dict[str, Any]:
        try:
            importlib.util.find_spec("playwright.async_api")
            installed = importlib.util.find_spec("playwright") is not None
        except (ImportError, ValueError):
            installed = False
        result: dict[str, Any] = {
            "available": installed, "active": self.context is not None,
            "visible": self.visible and self.context is not None,
            "manualControl": self.context is not None and bool(self.visible or self._manual_token),
            "manualViewerActive": bool(self._manual_token),
            "manualViewerExpired": bool(self._manual_token) and time.monotonic() >= self._manual_until,
            "profile": "dedicated", "browser": "chromium",
        }
        if self.last_error:
            result["error"] = self.last_error
        return result

    async def _launch_internal(self, visible: bool = False) -> None:
        if self.context is not None and self.visible == visible:
            return
        if self.context is not None:
            await self.context.close()
            self.context = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        marker = self.profile_dir / PROFILE_MARKER
        entries = list(self.profile_dir.iterdir())
        if entries and not marker.exists():
            raise ValueError("Refusing to use an existing unmarked browser profile")
        if marker.exists():
            if marker.is_symlink() or marker.read_text(encoding="utf-8").strip() != PROFILE_ID:
                raise ValueError("Browser profile ownership marker is invalid")
        else:
            marker.write_text(PROFILE_ID, encoding="utf-8")
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            self.last_error = "Install playwright to enable the dedicated browser"
            raise RuntimeError(self.last_error) from exc
        try:
            self._playwright = await async_playwright().start()
            options: dict[str, Any] = {
                "headless": not visible, "accept_downloads": False,
                "service_workers": "block", "viewport": {"width": 1365, "height": 900},
                "timeout": TIMEOUT,
            }
            if self.executable_path:
                options["executable_path"] = self.executable_path
            else:
                options["channel"] = self.channel or ("msedge" if os.name == "nt" else "chromium")
            self.context = await self._playwright.chromium.launch_persistent_context(str(self.profile_dir), **options)
            self.visible = visible
            self.last_error = None
            self.context.set_default_timeout(TIMEOUT)
            self.context.set_default_navigation_timeout(TIMEOUT)
            await self.context.route("**/*", self._route_request)
            route_ws = getattr(self.context, "route_web_socket", None)
            if route_ws:
                await route_ws("**/*", self._close_websocket)
            self.context.on("page", self._configure_page)
            for page in self.context.pages:
                self._configure_page(page)
        except Exception as exc:
            if self.context is not None:
                try:
                    await self.context.close()
                except Exception:
                    pass
            self.context = None
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None
            reason = re.search(r"EACCES|EPERM|ENOENT|executable doesn't exist|Timeout", str(exc), re.I)
            self.last_error = f"Dedicated Chromium could not start{f' ({reason.group(0)})' if reason else ''}. Check the browser installation, profile lock, and process permissions; configure BROWSER_EXECUTABLE_PATH if needed."
            raise RuntimeError(self.last_error) from exc

    async def _route_request(self, route: Any) -> None:
        try:
            await assert_public_browser_url(route.request.url)
            owner_active = False
            if self._manual_token and time.monotonic() < self._manual_until:
                try:
                    owner_active = self._page_ids.get(id(route.request.frame.page)) == self._manual_tab
                except Exception:
                    # A request without a frame cannot carry manual authorization.
                    pass
            if route.request.method not in ("GET", "HEAD", "OPTIONS") and not (self.mutations or self.visible or owner_active):
                await route.abort("blockedbyclient")
                return
            await route.continue_()
        except Exception:
            try:
                await route.abort("blockedbyclient")
            except Exception:
                pass

    async def _close_websocket(self, socket: Any) -> None:
        await socket.close()

    async def launch(self, visible: bool = False) -> dict[str, Any]:
        async with self._lock:
            self._require_automatic()
            await self._launch_internal(visible)
            return await self.status()

    def _require_automatic(self):
        if self._manual_token:
            raise ActionPreflightError("Browser is under manual user control")

    async def begin_manual(self, tab_id):
        async with self._lock:
            selected = tab_id or self._manual_tab
            page = self._page(selected)
            await assert_public_browser_url(page.url)
            self._manual_token = secrets.token_urlsafe(24)
            self._manual_until = time.monotonic() + MANUAL_SECONDS
            self._manual_tab = selected
            self._manual_frame = None
            return {"lease": self._manual_token}

    def _check_manual(self, lease, *, allow_expired=False):
        if not self._manual_token or not isinstance(lease, str) or not lease.isascii() or not hmac.compare_digest(self._manual_token, lease):
            raise ManualBrowserError("Manual browser session changed. Reconnect to continue.")
        if not allow_expired and time.monotonic() >= self._manual_until:
            raise ManualBrowserError("Manual browser session expired. Reconnect to continue.")

    async def manual_frame(self, lease):
        async with self._lock:
            self._check_manual(lease)
            try:
                page = self._page(self._manual_tab)
                url = page.url
                navigation = self._manual_navigation
                await assert_public_browser_url(url)
                size = page.viewport_size
                title = await page.title()
                data = await page.screenshot(type="jpeg", quality=75, timeout=4000)
                self._check_manual(lease)
                if self._manual_navigation != navigation or page.url != url or not size:
                    raise ManualBrowserError("Browser page changed. Refresh the view.")
            except ManualBrowserError:
                raise
            except Exception:
                raise ManualBrowserError("Browser view is unavailable. Reconnect to continue.") from None
            self._manual_sequence += 1
            self._manual_frame = (self._manual_sequence, url, size, navigation)
            return {"frame": self._manual_sequence, "image": base64.b64encode(data).decode("ascii"),
                    "origin": urlsplit(url).hostname, "title": title[:160], **size}

    async def manual_input(self, lease, body):
        async with self._lock:
            self._check_manual(lease)
            try:
                page = self._page(self._manual_tab)
                frame = self._manual_frame
                if not frame or frame[0] != body.get("frame"):
                    raise ManualBrowserError("Browser page changed. Refresh the view before entering input.")
                await assert_public_browser_url(frame[1])
                self._check_manual(lease)
                if frame[1] != page.url or frame[3] != self._manual_navigation:
                    raise ManualBrowserError("Browser page changed. Refresh the view before entering input.")
                self._manual_frame = None
                kind = body.get("kind")
                if kind == "click":
                    x, y = body.get("x"), body.get("y")
                    if type(x) is not int or type(y) is not int or not (0 <= x < frame[2]["width"] and 0 <= y < frame[2]["height"]):
                        raise ManualBrowserError("Click is outside the browser view.")
                    await page.mouse.click(x, y)
                elif kind == "text" and isinstance(body.get("value"), str) and 0 < len(body["value"]) <= 4096:
                    await page.keyboard.insert_text(body["value"])
                elif kind == "key" and isinstance(body.get("value"), str) and body["value"] in MANUAL_KEYS:
                    await page.keyboard.press(body["value"])
                else:
                    raise ManualBrowserError("Unsupported manual browser input.")
            except ManualBrowserError:
                raise
            except Exception:
                raise ManualBrowserError("Browser input could not be delivered. Refresh the view.") from None

    async def end_manual(self, lease):
        async with self._lock:
            self._check_manual(lease, allow_expired=True)
            # Destroy the sign-in DOM before the assistant can observe a new page.
            await self._close_session()

    async def tabs(self) -> list[dict[str, str]]:
        async with self._lock:
            self._require_automatic()
            if self.context is None:
                return []
            result = []
            for page in self.context.pages:
                try:
                    title = await page.title()
                except Exception:
                    title = ""
                result.append({"id": self._page_ids[id(page)], "url": page.url, "title": title})
            return result

    async def navigate(self, url: str) -> dict[str, Any]:
        async with self._lock:
            self._require_automatic()
            await assert_public_browser_url(url)
            await self._launch_internal(self.visible if self.context is not None else False)
            page = await self.context.new_page()
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT)
                await assert_public_browser_url(page.url)
                return {"id": self._page_ids[id(page)], "url": page.url, "title": await page.title(),
                        "status": response.status if response else None}
            except Exception:
                await page.close()
                raise

    async def snapshot(self, tab_id: str) -> dict[str, Any]:
        async with self._lock:
            self._require_automatic()
            page = self._page(tab_id)
            await assert_public_browser_url(page.url)
            content = await page.locator("body").inner_text(timeout=TIMEOUT)
            elements = await page.locator("a,button,input,textarea,select,[role=button],[role=link]").evaluate_all("""nodes => {
              const selector = e => { if (e.id) return `#${CSS.escape(e.id)}`; const a=[]; let n=e;
                while(n && n.tagName!=='HTML'){const same=n.parentElement?[...n.parentElement.children].filter(x=>x.tagName===n.tagName):[n];
                  a.unshift(`${n.tagName.toLowerCase()}:nth-of-type(${same.indexOf(n)+1})`);n=n.parentElement;} return a.join(' > '); };
              return nodes.filter(n=>n.getClientRects().length>0).slice(0,150).map(n=>({selector:selector(n),tag:n.tagName.toLowerCase(),
                role:n.getAttribute('role'),name:(n.getAttribute('aria-label')||n.getAttribute('placeholder')||n.textContent||'').trim().slice(0,180),
                href:n.getAttribute('href'),type:n.getAttribute('type')}));
            }""")
            return {"tabId": tab_id, "url": page.url, "title": await page.title(), "text": content[:24000],
                    "truncated": len(content) > 24000, "elements": elements, "untrustedContent": True}

    async def close_tab(self, tab_id: str) -> None:
        async with self._lock:
            self._require_automatic()
            await self._page(tab_id).close()

    async def screenshot(self, tab_id: str) -> dict[str, str]:
        async with self._lock:
            self._require_automatic()
            page = self._page(tab_id)
            await assert_public_browser_url(page.url)
            data = await page.screenshot(type="png", full_page=False, timeout=TIMEOUT)
            return {"tabId": tab_id, "mimeType": "image/png", "base64": base64.b64encode(data).decode("ascii")}

    async def click(self, tab_id: str, selector: str) -> dict[str, str]:
        async with self._lock:
            self._require_automatic()
            if self.visible:
                raise ValueError("Browser is under manual control. Resume headless mode before automatic actions.")
            if not selector or len(selector) > 2000:
                raise ValueError("Invalid browser selector")
            page = self._page(tab_id)
            await assert_public_browser_url(page.url)
            self.mutations += 1
            try:
                await page.locator(selector).click(timeout=TIMEOUT)
            finally:
                self.mutations -= 1
            return {"tabId": tab_id, "url": page.url, "title": await page.title()}

    async def fill(self, tab_id: str, selector: str, value: str) -> dict[str, Any]:
        async with self._lock:
            self._require_automatic()
            if self.visible:
                raise ValueError("Browser is under manual control. Resume headless mode before automatic actions.")
            if not selector or len(selector) > 2000 or not isinstance(value, str) or len(value) > 12000:
                raise ValueError("Invalid browser form input")
            page = self._page(tab_id)
            await assert_public_browser_url(page.url)
            field = page.locator(selector)
            if (await field.get_attribute("type") or "").lower() == "password":
                raise ValueError("Enter passwords manually in the visible dedicated browser")
            self.mutations += 1
            try:
                await field.fill(value, timeout=TIMEOUT)
            finally:
                self.mutations -= 1
            return {"tabId": tab_id, "filled": True}

    async def close(self) -> dict[str, bool]:
        async with self._lock:
            await self._close_session()
            return {"active": False}

    async def _close_session(self):
        if self.context is not None:
            await self.context.close()
        self.context = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        self._page_ids.clear()
        self._manual_token = ""
        self._manual_until = 0.0
        self._manual_tab = ""
        self._manual_frame = None
