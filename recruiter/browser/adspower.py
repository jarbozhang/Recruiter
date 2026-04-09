"""AdsPower Local API 客户端

通过 AdsPower 指纹浏览器的 Local API 启动/停止浏览器配置，
获取 Selenium WebDriver 连接信息。

API 文档: https://localapi-doc-en.adspower.com/docs/Rdw7Iu
"""

import logging
import time

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

logger = logging.getLogger(__name__)

# AdsPower Local API 默认地址
DEFAULT_API_BASE = "http://local.adspower.com:50325"


class AdsPowerClient:
    """AdsPower Local API 客户端，管理浏览器生命周期和 Selenium 连接。"""

    def __init__(self, api_base: str = DEFAULT_API_BASE):
        self.api_base = api_base.rstrip("/")
        self._active_profiles: dict[str, webdriver.Chrome] = {}

    def _api_get(self, path: str, params: dict = None) -> dict:
        """调用 AdsPower Local API (GET)。"""
        url = f"{self.api_base}{path}"
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"AdsPower API error: {data.get('msg', 'unknown')} (code={data.get('code')})")
        return data.get("data", {})

    def check_status(self) -> bool:
        """检查 AdsPower 是否在运行。"""
        try:
            resp = requests.get(f"{self.api_base}/status", timeout=5)
            return resp.status_code == 200
        except requests.ConnectionError:
            return False

    def start_browser(self, profile_id: str, open_tabs: int = 1,
                      ip_tab: bool = False) -> dict:
        """启动浏览器配置，返回连接信息。

        Args:
            profile_id: AdsPower 浏览器配置 ID
            open_tabs: 启动时打开的标签页数
            ip_tab: 是否打开 IP 检测页

        Returns:
            {
                "selenium_address": "127.0.0.1:xxxx",
                "webdriver_path": "/path/to/chromedriver",
                "debug_port": "xxxx"
            }
        """
        params = {
            "user_id": profile_id,
            "open_tabs": open_tabs,
            "ip_tab": 1 if ip_tab else 0,
        }
        data = self._api_get("/api/v1/browser/start", params)
        ws = data.get("ws", {})
        result = {
            "selenium_address": ws.get("selenium", ""),
            "webdriver_path": data.get("webdriver", ""),
            "debug_port": data.get("debug_port", ""),
        }
        logger.info("Browser started for profile %s: selenium=%s",
                     profile_id, result["selenium_address"])
        return result

    def stop_browser(self, profile_id: str) -> bool:
        """停止浏览器配置。"""
        try:
            self._api_get("/api/v1/browser/stop", {"user_id": profile_id})
            self._active_profiles.pop(profile_id, None)
            logger.info("Browser stopped for profile %s", profile_id)
            return True
        except Exception as e:
            logger.error("Failed to stop browser %s: %s", profile_id, e)
            return False

    def check_browser_active(self, profile_id: str) -> bool:
        """检查浏览器是否在运行。"""
        try:
            data = self._api_get("/api/v1/browser/active", {"user_id": profile_id})
            return data.get("status") == "Active"
        except Exception:
            return False

    def connect_selenium(self, profile_id: str) -> webdriver.Chrome:
        """启动浏览器并返回已连接的 Selenium WebDriver。

        如果该配置已有活跃的 driver，直接返回。
        """
        if profile_id in self._active_profiles:
            driver = self._active_profiles[profile_id]
            try:
                _ = driver.title  # 测试连接是否还活着
                return driver
            except Exception:
                self._active_profiles.pop(profile_id, None)

        # 启动浏览器
        info = self.start_browser(profile_id)

        # 连接 Selenium
        chrome_options = Options()
        chrome_options.add_experimental_option("debuggerAddress", info["selenium_address"])

        service = Service(executable_path=info["webdriver_path"])
        driver = webdriver.Chrome(service=service, options=chrome_options)

        self._active_profiles[profile_id] = driver
        logger.info("Selenium connected to profile %s", profile_id)
        return driver

    def disconnect(self, profile_id: str):
        """断开 Selenium 并停止浏览器。"""
        driver = self._active_profiles.pop(profile_id, None)
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        self.stop_browser(profile_id)

    def disconnect_all(self):
        """断开所有活跃连接。"""
        for pid in list(self._active_profiles.keys()):
            self.disconnect(pid)

    def list_profiles(self, page: int = 1, page_size: int = 100) -> list[dict]:
        """列出所有浏览器配置。"""
        data = self._api_get("/api/v1/user/list", {
            "page": page,
            "page_size": page_size,
        })
        return data.get("list", [])
