"""bb-browser 浏览器驱动

通过 bb-browser CLI 控制真实 Chrome 浏览器（复用已有登录态）。
实现 BrowserDriver 接口。

bb-browser: https://github.com/epiral/bb-browser
"""

import json
import logging
import subprocess
import time

from recruiter.browser.base import BrowserDriver, Element

logger = logging.getLogger(__name__)

# bb-browser CLI 路径（npx 或全局安装）
DEFAULT_CMD = "bb-browser"


class BBBrowserDriver(BrowserDriver):
    """bb-browser CLI 实现。"""

    def __init__(self, cmd: str = DEFAULT_CMD, port: int | None = None):
        self.cmd = cmd
        self.port = port

    def _run(self, *args, timeout: int = 30) -> str:
        """执行 bb-browser CLI 命令，返回 stdout。"""
        cmd_parts = [self.cmd] + list(args)
        if self.port:
            cmd_parts.extend(["--port", str(self.port)])
        cmd_parts.append("--json")

        logger.debug("bb-browser: %s", " ".join(cmd_parts))
        try:
            result = subprocess.run(
                cmd_parts,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode != 0:
                logger.warning("bb-browser stderr: %s", result.stderr.strip())
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            logger.error("bb-browser command timed out: %s", " ".join(cmd_parts))
            return ""
        except FileNotFoundError:
            logger.error("bb-browser not found. Install: npm install -g bb-browser")
            return ""

    def _parse_json(self, output: str) -> any:
        """解析 bb-browser JSON 输出。"""
        if not output:
            return None
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return output

    def navigate(self, url: str) -> None:
        self._run("open", url)
        time.sleep(1)  # 等待页面加载

    def find_element(self, selector: str) -> Element | None:
        # 用 JS 查找元素
        script = (
            f"var el = document.querySelector('{self._escape_selector(selector)}'); "
            f"if (!el) return null; "
            f"return JSON.stringify({{text: el.textContent.trim().substring(0,200), "
            f"tag: el.tagName, href: el.getAttribute('href') || ''}});"
        )
        output = self._run("eval", script)
        data = self._parse_json(output)
        if not data:
            return None
        # bb-browser --json wraps in {id, success, data}
        if isinstance(data, dict) and "data" in data:
            inner = data["data"]
        else:
            inner = data
        if inner is None or inner == "null":
            return None
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except json.JSONDecodeError:
                return None
        return Element(
            text=inner.get("text", ""),
            tag=inner.get("tag", ""),
            attributes={"href": inner.get("href", "")},
        )

    def find_elements(self, selector: str) -> list[Element]:
        script = (
            f"var els = document.querySelectorAll('{self._escape_selector(selector)}'); "
            f"var result = []; "
            f"els.forEach(function(el) {{ "
            f"  result.push({{text: el.textContent.trim().substring(0,200), "
            f"  tag: el.tagName, href: el.getAttribute('href') || ''}}); "
            f"}}); return JSON.stringify(result);"
        )
        output = self._run("eval", script)
        data = self._parse_json(output)
        if not data:
            return []
        if isinstance(data, dict) and "data" in data:
            inner = data["data"]
        else:
            inner = data
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except json.JSONDecodeError:
                return []
        if not isinstance(inner, list):
            return []
        return [
            Element(
                text=item.get("text", ""),
                tag=item.get("tag", ""),
                attributes={"href": item.get("href", "")},
            )
            for item in inner
        ]

    def click(self, selector: str) -> bool:
        # 先用 snapshot -i 获取 ref，或者直接用 JS click
        script = (
            f"var el = document.querySelector('{self._escape_selector(selector)}'); "
            f"if (el) {{ el.click(); return true; }} return false;"
        )
        output = self._run("eval", script)
        data = self._parse_json(output)
        if isinstance(data, dict) and "data" in data:
            return bool(data["data"])
        return bool(data)

    def fill(self, selector: str, text: str) -> bool:
        escaped_text = text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
        script = (
            f"var el = document.querySelector('{self._escape_selector(selector)}'); "
            f"if (!el) return false; "
            f"if (el.contentEditable === 'true') {{ "
            f"  el.textContent = '{escaped_text}'; "
            f"  el.dispatchEvent(new Event('input', {{bubbles: true}})); "
            f"}} else {{ "
            f"  el.value = '{escaped_text}'; "
            f"  el.dispatchEvent(new Event('input', {{bubbles: true}})); "
            f"}} return true;"
        )
        output = self._run("eval", script)
        data = self._parse_json(output)
        if isinstance(data, dict) and "data" in data:
            return bool(data["data"])
        return bool(data)

    def get_text(self, selector: str) -> str:
        el = self.find_element(selector)
        return el.text if el else ""

    def execute_js(self, script: str) -> any:
        output = self._run("eval", script)
        data = self._parse_json(output)
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data

    def screenshot(self, path: str) -> str:
        self._run("screenshot", path)
        return path

    def current_url(self) -> str:
        output = self._run("get", "url")
        data = self._parse_json(output)
        if isinstance(data, dict) and "data" in data:
            return str(data["data"])
        return str(data) if data else ""

    def wait_for(self, selector: str, timeout: int = 10) -> bool:
        """轮询等待元素出现。"""
        for _ in range(timeout * 2):  # 每 0.5 秒检查一次
            if self.find_element(selector) is not None:
                return True
            time.sleep(0.5)
        return False

    def close(self) -> None:
        pass  # bb-browser 复用已有 Chrome，不关闭

    @staticmethod
    def _escape_selector(selector: str) -> str:
        """转义 CSS 选择器中的单引号以安全嵌入 JS 字符串。"""
        return selector.replace("'", "\\'")
