"""BossWebCollector 单元测试

使用 mock BrowserDriver 对象，不依赖真实浏览器。
"""

import json
import os
import tempfile
from unittest.mock import MagicMock

import pytest

from recruiter.browser.base import BrowserDriver, Element
from recruiter.db.models import Database
from recruiter.collector.browser_collector import (
    BossWebCollector,
    CandidateInfo,
    HealthCheckError,
    PageLoadError,
)


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database = Database(path)
    yield database
    database.close()
    os.unlink(path)


@pytest.fixture
def mock_browser():
    browser = MagicMock(spec=BrowserDriver)
    browser.wait_for.return_value = True
    return browser


@pytest.fixture
def collector(mock_browser, db):
    return BossWebCollector(mock_browser, db)


class TestHealthCheck:
    def test_health_check_pass(self, collector, mock_browser):
        mock_browser.find_elements.return_value = [Element(text="test")]
        result = collector.health_check("https://www.zhipin.com/test")
        assert result is True

    def test_health_check_fail_missing_selector(self, collector, mock_browser):
        mock_browser.find_elements.return_value = []
        with pytest.raises(HealthCheckError):
            collector.health_check("https://www.zhipin.com/test")

    def test_health_check_page_load_failure(self, collector, mock_browser):
        mock_browser.navigate.side_effect = Exception("timeout")
        with pytest.raises(PageLoadError):
            collector.health_check("https://www.zhipin.com/test")


class TestExtractCandidates:
    def test_extract_candidates_happy_path(self, collector, mock_browser):
        mock_browser.execute_js.return_value = json.dumps([
            {"name": "张三", "platform_id": "u_001"},
            {"name": "李四", "platform_id": "u_002"},
        ])
        candidates = collector._extract_candidates_from_page()
        assert len(candidates) == 2
        assert candidates[0].name == "张三"
        assert candidates[0].platform_id == "u_001"

    def test_extract_candidates_empty_list(self, collector, mock_browser):
        mock_browser.execute_js.return_value = "[]"
        candidates = collector._extract_candidates_from_page()
        assert candidates == []

    def test_extract_candidates_js_returns_none(self, collector, mock_browser):
        mock_browser.execute_js.return_value = None
        candidates = collector._extract_candidates_from_page()
        assert candidates == []


class TestExtractResume:
    def test_extract_resume_happy_path(self, collector, mock_browser):
        mock_browser.get_text.return_value = "Java 5年经验"
        result = collector._extract_resume("https://zhipin.com/profile/u_001")
        assert "Java" in result

    def test_extract_resume_page_load_fail(self, collector, mock_browser):
        mock_browser.navigate.side_effect = Exception("timeout")
        result = collector._extract_resume("https://zhipin.com/profile/u_001")
        assert result == ""

    def test_extract_resume_selector_missing(self, collector, mock_browser):
        mock_browser.get_text.return_value = ""
        result = collector._extract_resume("https://zhipin.com/profile/u_001")
        assert result == ""


class TestCollectCandidates:
    def test_collect_and_save_to_db(self, collector, mock_browser, db):
        # 第一次返回候选人，第二次返回空
        mock_browser.execute_js.side_effect = [
            json.dumps([{"name": "测试", "platform_id": "u_t001"}]),
            "[]",
        ]
        mock_browser.is_visible.return_value = False  # 没有下一页

        candidates = collector.collect_candidates("https://zhipin.com/job/123")
        assert len(candidates) == 1
        assert len(db.list_candidates()) == 1

    def test_collect_empty_list(self, collector, mock_browser):
        mock_browser.execute_js.return_value = "[]"
        candidates = collector.collect_candidates("https://zhipin.com/job/123")
        assert candidates == []

    def test_collect_page_load_failure_raises(self, collector, mock_browser):
        mock_browser.navigate.side_effect = Exception("network error")
        with pytest.raises(PageLoadError):
            collector.collect_candidates("https://zhipin.com/job/123")

    def test_collect_dedup(self, collector, mock_browser, db):
        mock_browser.execute_js.side_effect = [
            json.dumps([{"name": "去重", "platform_id": "u_dup"}]),
            "[]",
            json.dumps([{"name": "去重", "platform_id": "u_dup"}]),
            "[]",
        ]
        mock_browser.is_visible.return_value = False

        collector.collect_candidates("https://zhipin.com/job/1")
        collector.collect_candidates("https://zhipin.com/job/1")
        assert len(db.list_candidates()) == 1


class TestNavigateWithRetry:
    def test_retry_once_then_succeed(self, collector, mock_browser):
        mock_browser.navigate.side_effect = [Exception("fail"), None]
        result = collector._navigate_with_retry("https://zhipin.com")
        assert result is True

    def test_retry_exhausted(self, collector, mock_browser):
        mock_browser.navigate.side_effect = Exception("always fail")
        result = collector._navigate_with_retry("https://zhipin.com")
        assert result is False
