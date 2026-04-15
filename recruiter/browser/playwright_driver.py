"""AdsPower + Playwright 浏览器驱动

通过 AdsPower Local API 启动指纹浏览器，用 Playwright connect_over_cdp 连接。
实现 BrowserDriver 接口。

相比 Selenium 版的优势：
- 内置自动等待，减少显式 wait/sleep
- page.route() 可拦截网络请求，直接从 API 响应拿数据
- locator 链式定位，抗 DOM 变化能力更强
"""

import logging

import requests
from playwright.sync_api import (
    Browser,
    Page,
    sync_playwright,
    TimeoutError as PlaywrightTimeout,
)

from recruiter.browser.base import BrowserDriver, Element

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "http://127.0.0.1:50325"


class PlaywrightAdsPowerDriver(BrowserDriver):
    """AdsPower + Playwright 实现。"""

    def __init__(self, api_key: str, profile_id: str,
                 api_base: str = DEFAULT_API_BASE):
        self.api_key = api_key
        self.profile_id = profile_id
        self.api_base = api_base.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._playwright = None
        self._browser: Browser | None = None
        self._page: Page | None = None

    def _api_get(self, path: str, params: dict = None) -> dict:
        url = f"{self.api_base}{path}"
        resp = requests.get(url, params=params, headers=self._headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"AdsPower API error: {data.get('msg')} (code={data.get('code')})")
        return data.get("data", {})

    def _ensure_connected(self) -> Page:
        if self._page:
            try:
                _ = self._page.title()
                return self._page
            except Exception:
                self._page = None
                self._browser = None

        # 启动 AdsPower 浏览器
        data = self._api_get("/api/v1/browser/start", {
            "user_id": self.profile_id,
            "open_tabs": 1,
            "ip_tab": 0,
        })
        ws_endpoint = data.get("ws", {}).get("puppeteer", "")
        if not ws_endpoint:
            raise RuntimeError("AdsPower 未返回 puppeteer WebSocket 地址")

        # Playwright connect_over_cdp
        if not self._playwright:
            self._playwright = sync_playwright().start()

        self._browser = self._playwright.chromium.connect_over_cdp(ws_endpoint)

        # 复用已有页面，或新建
        contexts = self._browser.contexts
        if contexts and contexts[0].pages:
            self._page = contexts[0].pages[0]
        else:
            context = self._browser.new_context()
            self._page = context.new_page()

        logger.info("Playwright connected: profile=%s, ws=%s", self.profile_id, ws_endpoint)
        return self._page

    def navigate(self, url: str) -> None:
        page = self._ensure_connected()
        page.goto(url, wait_until="domcontentloaded")

    def find_element(self, selector: str) -> Element | None:
        page = self._ensure_connected()
        loc = page.locator(selector).first
        try:
            loc.wait_for(state="attached", timeout=3000)
            return Element(
                text=(loc.text_content() or "").strip(),
                tag=loc.evaluate("el => el.tagName").lower(),
                attributes={"href": loc.get_attribute("href") or ""},
            )
        except PlaywrightTimeout:
            return None

    def find_elements(self, selector: str) -> list[Element]:
        page = self._ensure_connected()
        loc = page.locator(selector)
        try:
            loc.first.wait_for(state="attached", timeout=3000)
        except PlaywrightTimeout:
            return []

        count = loc.count()
        results = []
        for i in range(count):
            item = loc.nth(i)
            try:
                results.append(Element(
                    text=(item.text_content() or "").strip(),
                    tag=item.evaluate("el => el.tagName").lower(),
                    attributes={"href": item.get_attribute("href") or ""},
                ))
            except Exception:
                continue
        return results

    def click(self, selector: str) -> bool:
        page = self._ensure_connected()
        try:
            page.locator(selector).first.click(timeout=5000)
            return True
        except (PlaywrightTimeout, Exception):
            return False

    def fill(self, selector: str, text: str) -> bool:
        page = self._ensure_connected()
        try:
            loc = page.locator(selector).first
            # 检查是否为 contenteditable div（Boss直聘聊天输入框）
            is_editable = loc.evaluate("el => el.contentEditable === 'true'")
            if is_editable:
                loc.evaluate(
                    "(el, t) => { el.textContent = t; "
                    "el.dispatchEvent(new Event('input', {bubbles: true})); }",
                    text,
                )
            else:
                loc.fill(text)
            return True
        except (PlaywrightTimeout, Exception):
            return False

    def get_text(self, selector: str) -> str:
        el = self.find_element(selector)
        return el.text if el else ""

    def execute_js(self, script: str) -> any:
        page = self._ensure_connected()
        # Playwright evaluate 不支持顶层 return 语句（与 Selenium 不同）
        # 将 "return xxx" 包装为 "(() => { return xxx })()" 使其兼容
        stripped = script.strip()
        if "return " in stripped:
            wrapped = f"(() => {{ {stripped} }})()"
            return page.evaluate(wrapped)
        return page.evaluate(stripped)

    def screenshot(self, path: str) -> str:
        page = self._ensure_connected()
        page.screenshot(path=path, full_page=True)
        return path

    def current_url(self) -> str:
        page = self._ensure_connected()
        return page.url

    def wait_for(self, selector: str, timeout: int = 10) -> bool:
        page = self._ensure_connected()
        try:
            page.locator(selector).first.wait_for(
                state="attached", timeout=timeout * 1000
            )
            return True
        except PlaywrightTimeout:
            return False

    def close(self) -> None:
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
            self._page = None
        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        try:
            self._api_get("/api/v1/browser/stop", {"user_id": self.profile_id})
        except Exception:
            pass

    # --- Playwright 独有能力 ---

    def intercept_response(self, url_pattern: str, handler) -> None:
        """拦截匹配 URL 的网络响应，handler(response) 会收到响应对象。

        用途：直接从 Boss直聘 API 拿候选人/消息 JSON，比 DOM 解析更稳定。
        """
        page = self._ensure_connected()
        self._response_handler = lambda resp: handler(resp) if url_pattern in resp.url else None
        page.on("response", self._response_handler)

    def stop_intercept(self) -> None:
        """移除响应拦截监听器。"""
        if hasattr(self, '_response_handler') and self._response_handler:
            page = self._ensure_connected()
            try:
                page.remove_listener("response", self._response_handler)
            except Exception:
                pass
            self._response_handler = None

    def reload(self) -> None:
        """强制刷新当前页面，确保重新触发 API 请求。"""
        page = self._ensure_connected()
        page.reload(wait_until="domcontentloaded")
