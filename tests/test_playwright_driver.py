"""PlaywrightAdsPowerDriver 单元测试

Mock Playwright 和 AdsPower API，不依赖真实浏览器。
"""

from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from recruiter.browser.base import Element


@pytest.fixture
def mock_page():
    page = MagicMock()
    page.title.return_value = "Test Page"
    page.url = "https://www.zhipin.com/web/chat/index"
    return page


@pytest.fixture
def mock_context(mock_page):
    ctx = MagicMock()
    ctx.pages = [mock_page]
    return ctx


@pytest.fixture
def mock_browser(mock_context):
    browser = MagicMock()
    browser.contexts = [mock_context]
    return browser


@pytest.fixture
def mock_playwright(mock_browser):
    pw = MagicMock()
    pw.chromium.connect_over_cdp.return_value = mock_browser
    return pw


@pytest.fixture
def driver(mock_playwright, mock_page):
    with patch("recruiter.browser.playwright_driver.requests") as mock_req, \
         patch("recruiter.browser.playwright_driver.sync_playwright") as mock_sp:

        # Mock AdsPower API
        resp = MagicMock()
        resp.json.return_value = {
            "code": 0,
            "data": {
                "ws": {"puppeteer": "ws://127.0.0.1:9222/devtools/browser/xxx"},
                "webdriver": "/path/to/chromedriver",
            },
        }
        resp.raise_for_status = MagicMock()
        mock_req.get.return_value = resp

        # Mock sync_playwright
        mock_sp.return_value.start.return_value = mock_playwright

        from recruiter.browser.playwright_driver import PlaywrightAdsPowerDriver
        d = PlaywrightAdsPowerDriver(
            api_key="test_key",
            profile_id="test_profile",
        )
        # Pre-connect
        d._playwright = mock_playwright
        d._browser = mock_playwright.chromium.connect_over_cdp.return_value
        d._page = mock_page
        yield d


class TestNavigate:
    def test_navigate(self, driver, mock_page):
        driver.navigate("https://www.zhipin.com")
        mock_page.goto.assert_called_once_with(
            "https://www.zhipin.com", wait_until="domcontentloaded"
        )


class TestFindElement:
    def test_find_element_found(self, driver, mock_page):
        loc = MagicMock()
        loc.text_content.return_value = "张三"
        loc.evaluate.return_value = "div"
        loc.get_attribute.return_value = ""
        mock_page.locator.return_value.first = loc

        el = driver.find_element(".geek-name")
        assert isinstance(el, Element)
        assert el.text == "张三"
        assert el.tag == "div"

    def test_find_element_not_found(self, driver, mock_page):
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        loc = MagicMock()
        loc.wait_for.side_effect = PlaywrightTimeout("timeout")
        mock_page.locator.return_value.first = loc

        el = driver.find_element(".nonexistent")
        assert el is None


class TestFindElements:
    def test_find_elements(self, driver, mock_page):
        item1 = MagicMock()
        item1.text_content.return_value = "候选人1"
        item1.evaluate.return_value = "div"
        item1.get_attribute.return_value = ""

        item2 = MagicMock()
        item2.text_content.return_value = "候选人2"
        item2.evaluate.return_value = "div"
        item2.get_attribute.return_value = ""

        loc = MagicMock()
        loc.first.wait_for.return_value = None
        loc.count.return_value = 2
        loc.nth.side_effect = [item1, item2]
        mock_page.locator.return_value = loc

        elements = driver.find_elements(".geek-item")
        assert len(elements) == 2
        assert elements[0].text == "候选人1"

    def test_find_elements_empty(self, driver, mock_page):
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        loc = MagicMock()
        loc.first.wait_for.side_effect = PlaywrightTimeout("timeout")
        mock_page.locator.return_value = loc

        elements = driver.find_elements(".nonexistent")
        assert elements == []


class TestClick:
    def test_click_success(self, driver, mock_page):
        mock_page.locator.return_value.first.click.return_value = None
        assert driver.click(".btn") is True

    def test_click_fail(self, driver, mock_page):
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        mock_page.locator.return_value.first.click.side_effect = PlaywrightTimeout("timeout")
        assert driver.click(".btn") is False


class TestFill:
    def test_fill_input(self, driver, mock_page):
        loc = MagicMock()
        loc.evaluate.return_value = False  # not contenteditable
        loc.fill.return_value = None
        mock_page.locator.return_value.first = loc

        assert driver.fill("input", "hello") is True
        loc.fill.assert_called_once_with("hello")

    def test_fill_contenteditable(self, driver, mock_page):
        loc = MagicMock()
        # 第一次 evaluate 检查 contentEditable，返回 True
        # 第二次 evaluate 设置 textContent
        loc.evaluate.side_effect = [True, None]
        mock_page.locator.return_value.first = loc

        assert driver.fill(".boss-chat-editor-input", "你好") is True
        assert loc.evaluate.call_count == 2


class TestExecuteJs:
    def test_execute_js(self, driver, mock_page):
        mock_page.evaluate.return_value = [{"name": "test"}]
        result = driver.execute_js("return []")
        assert result == [{"name": "test"}]


class TestScreenshot:
    def test_screenshot(self, driver, mock_page):
        path = driver.screenshot("/tmp/test.png")
        mock_page.screenshot.assert_called_once_with(path="/tmp/test.png", full_page=True)
        assert path == "/tmp/test.png"


class TestCurrentUrl:
    def test_current_url(self, driver, mock_page):
        assert driver.current_url() == "https://www.zhipin.com/web/chat/index"


class TestWaitFor:
    def test_wait_for_found(self, driver, mock_page):
        mock_page.locator.return_value.first.wait_for.return_value = None
        assert driver.wait_for(".geek-item", timeout=5) is True

    def test_wait_for_timeout(self, driver, mock_page):
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        mock_page.locator.return_value.first.wait_for.side_effect = PlaywrightTimeout("timeout")
        assert driver.wait_for(".geek-item", timeout=1) is False


class TestClose:
    def test_close(self, driver):
        with patch("recruiter.browser.playwright_driver.requests") as mock_req:
            resp = MagicMock()
            resp.json.return_value = {"code": 0, "data": {}}
            resp.raise_for_status = MagicMock()
            mock_req.get.return_value = resp

            driver.close()
            assert driver._browser is None
            assert driver._page is None
            assert driver._playwright is None


class TestGetAttribute:
    def test_get_attribute(self, driver, mock_page):
        mock_page.evaluate.return_value = "disabled"
        result = driver.get_attribute(".next-btn", "disabled")
        assert result == "disabled"


class TestIsVisible:
    def test_is_visible_true(self, driver, mock_page):
        loc = MagicMock()
        loc.text_content.return_value = "test"
        loc.evaluate.return_value = "div"
        loc.get_attribute.return_value = ""
        mock_page.locator.return_value.first = loc
        assert driver.is_visible(".geek-item") is True

    def test_is_visible_false(self, driver, mock_page):
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        loc = MagicMock()
        loc.wait_for.side_effect = PlaywrightTimeout("timeout")
        mock_page.locator.return_value.first = loc
        assert driver.is_visible(".nonexistent") is False
