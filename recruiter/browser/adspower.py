"""AdsPower 浏览器驱动

通过 AdsPower 指纹浏览器 Local API 启动浏览器，用 Selenium 操作。
实现 BrowserDriver 接口。
"""

import logging

import requests
from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from recruiter.browser.base import BrowserDriver, Element

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "http://127.0.0.1:50325"


class AdsPowerDriver(BrowserDriver):
    """AdsPower + Selenium 实现。"""

    def __init__(self, api_key: str, profile_id: str,
                 api_base: str = DEFAULT_API_BASE):
        self.api_key = api_key
        self.profile_id = profile_id
        self.api_base = api_base.rstrip("/")
        self._driver: webdriver.Chrome | None = None
        self._headers = {"Authorization": f"Bearer {api_key}"}

    def _api_get(self, path: str, params: dict = None) -> dict:
        url = f"{self.api_base}{path}"
        resp = requests.get(url, params=params, headers=self._headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"AdsPower API error: {data.get('msg')} (code={data.get('code')})")
        return data.get("data", {})

    def _ensure_connected(self) -> webdriver.Chrome:
        if self._driver:
            try:
                _ = self._driver.title
                return self._driver
            except Exception:
                self._driver = None

        # 启动浏览器
        data = self._api_get("/api/v1/browser/start", {
            "user_id": self.profile_id,
            "open_tabs": 1,
            "ip_tab": 0,
        })
        ws = data.get("ws", {})
        selenium_addr = ws.get("selenium", "")
        webdriver_path = data.get("webdriver", "")

        chrome_options = Options()
        chrome_options.add_experimental_option("debuggerAddress", selenium_addr)
        service = Service(executable_path=webdriver_path)
        self._driver = webdriver.Chrome(service=service, options=chrome_options)

        logger.info("AdsPower connected: profile=%s, selenium=%s", self.profile_id, selenium_addr)
        return self._driver

    def navigate(self, url: str) -> None:
        driver = self._ensure_connected()
        driver.get(url)

    def find_element(self, selector: str) -> Element | None:
        driver = self._ensure_connected()
        try:
            el = driver.find_element(By.CSS_SELECTOR, selector)
            return Element(
                text=el.text.strip(),
                tag=el.tag_name,
                attributes={"href": el.get_attribute("href") or ""},
            )
        except NoSuchElementException:
            return None

    def find_elements(self, selector: str) -> list[Element]:
        driver = self._ensure_connected()
        elements = driver.find_elements(By.CSS_SELECTOR, selector)
        results = []
        for el in elements:
            try:
                results.append(Element(
                    text=el.text.strip(),
                    tag=el.tag_name,
                    attributes={"href": el.get_attribute("href") or ""},
                ))
            except Exception:
                continue
        return results

    def click(self, selector: str) -> bool:
        driver = self._ensure_connected()
        try:
            el = driver.find_element(By.CSS_SELECTOR, selector)
            driver.execute_script("arguments[0].click()", el)
            return True
        except (NoSuchElementException, WebDriverException):
            return False

    def fill(self, selector: str, text: str) -> bool:
        driver = self._ensure_connected()
        try:
            el = driver.find_element(By.CSS_SELECTOR, selector)
            # 支持 contenteditable div 和普通 input
            if el.get_attribute("contenteditable") == "true":
                driver.execute_script(
                    "arguments[0].textContent = arguments[1]; "
                    "arguments[0].dispatchEvent(new Event('input', {bubbles: true}));",
                    el, text,
                )
            else:
                el.clear()
                el.send_keys(text)
            return True
        except (NoSuchElementException, WebDriverException):
            return False

    def get_text(self, selector: str) -> str:
        el = self.find_element(selector)
        return el.text if el else ""

    def execute_js(self, script: str) -> any:
        driver = self._ensure_connected()
        return driver.execute_script(script)

    def screenshot(self, path: str) -> str:
        driver = self._ensure_connected()
        driver.save_screenshot(path)
        return path

    def current_url(self) -> str:
        driver = self._ensure_connected()
        return driver.current_url

    def wait_for(self, selector: str, timeout: int = 10) -> bool:
        driver = self._ensure_connected()
        try:
            WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, selector))
            )
            return True
        except TimeoutException:
            return False

    def close(self) -> None:
        if self._driver:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None
        try:
            self._api_get("/api/v1/browser/stop", {"user_id": self.profile_id})
        except Exception:
            pass
