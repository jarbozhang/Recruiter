"""浏览器驱动抽象接口

所有浏览器 driver（AdsPower、bb-browser、Playwright 等）都实现这个接口。
业务逻辑（采集、发送）只依赖此接口，不关心底层实现。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Element:
    """页面元素的统一表示。"""
    text: str = ""
    tag: str = ""
    attributes: dict = None

    def __post_init__(self):
        if self.attributes is None:
            self.attributes = {}


class BrowserDriver(ABC):
    """浏览器操作的统一接口。"""

    @abstractmethod
    def navigate(self, url: str) -> None:
        """导航到指定 URL。"""

    @abstractmethod
    def find_element(self, selector: str) -> Element | None:
        """通过 CSS 选择器查找单个元素，未找到返回 None。"""

    @abstractmethod
    def find_elements(self, selector: str) -> list[Element]:
        """通过 CSS 选择器查找所有匹配元素。"""

    @abstractmethod
    def click(self, selector: str) -> bool:
        """点击匹配选择器的第一个元素，成功返回 True。"""

    @abstractmethod
    def fill(self, selector: str, text: str) -> bool:
        """向匹配选择器的输入框填入文本。"""

    @abstractmethod
    def get_text(self, selector: str) -> str:
        """获取匹配选择器的第一个元素的文本内容。"""

    @abstractmethod
    def execute_js(self, script: str) -> any:
        """执行 JavaScript 并返回结果。"""

    @abstractmethod
    def screenshot(self, path: str) -> str:
        """截图保存到指定路径，返回路径。"""

    @abstractmethod
    def current_url(self) -> str:
        """返回当前页面 URL。"""

    @abstractmethod
    def wait_for(self, selector: str, timeout: int = 10) -> bool:
        """等待元素出现，超时返回 False。"""

    @abstractmethod
    def close(self) -> None:
        """关闭浏览器连接。"""

    def get_attribute(self, selector: str, attr: str) -> str | None:
        """获取元素属性值。默认通过 JS 实现。"""
        result = self.execute_js(
            f"var el = document.querySelector('{selector}'); "
            f"return el ? el.getAttribute('{attr}') : null;"
        )
        return result

    def is_visible(self, selector: str) -> bool:
        """检查元素是否可见。"""
        el = self.find_element(selector)
        return el is not None
