"""BossWebCollector 单元测试

使用 mock WebDriver 对象模拟 Selenium 行为，不依赖真实浏览器。
"""

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

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
def mock_driver():
    driver = MagicMock()
    return driver


@pytest.fixture(autouse=True)
def mock_webdriver_wait():
    """Patch WebDriverWait to avoid real timeouts in tests."""
    with patch("recruiter.collector.browser_collector.WebDriverWait") as mock_cls:
        mock_wait = MagicMock()
        mock_wait.until.return_value = MagicMock()
        mock_cls.return_value = mock_wait
        yield mock_cls


@pytest.fixture
def collector(mock_driver, db):
    return BossWebCollector(mock_driver, db)


class TestHealthCheck:
    def test_health_check_pass(self, collector, mock_driver):
        """所有关键选择器都存在 → 通过"""
        mock_driver.find_elements.return_value = [MagicMock()]
        result = collector.health_check("https://www.zhipin.com/test")
        assert result is True

    def test_health_check_fail_missing_selector(self, collector, mock_driver):
        """关键选择器缺失 → 抛出 HealthCheckError"""
        mock_driver.find_elements.return_value = []
        with pytest.raises(HealthCheckError):
            collector.health_check("https://www.zhipin.com/test")

    def test_health_check_page_load_failure(self, collector, mock_driver):
        """页面加载失败 → 抛出 PageLoadError"""
        from selenium.common.exceptions import WebDriverException
        mock_driver.get.side_effect = WebDriverException("timeout")
        with pytest.raises(PageLoadError):
            collector.health_check("https://www.zhipin.com/test")


class TestExtractCandidates:
    def test_extract_candidates_happy_path(self, collector, mock_driver):
        """正常提取候选人列表"""
        card1 = MagicMock()
        name_el1 = MagicMock()
        name_el1.text = "张三"
        card1.find_element.side_effect = lambda by, sel: {
            ".name": name_el1,
            "a": MagicMock(**{"get_attribute.return_value": "/profile/u_001"}),
        }.get(sel, MagicMock())

        card2 = MagicMock()
        name_el2 = MagicMock()
        name_el2.text = "李四"
        card2.find_element.side_effect = lambda by, sel: {
            ".name": name_el2,
            "a": MagicMock(**{"get_attribute.return_value": "/profile/u_002"}),
        }.get(sel, MagicMock())

        mock_driver.find_elements.return_value = [card1, card2]

        candidates = collector._extract_candidates_from_page()
        assert len(candidates) == 2
        assert candidates[0].name == "张三"
        assert candidates[0].platform_id == "u_001"
        assert candidates[1].name == "李四"

    def test_extract_candidates_empty_list(self, collector, mock_driver):
        """空候选人列表 → 返回空列表"""
        mock_driver.find_elements.return_value = []
        candidates = collector._extract_candidates_from_page()
        assert candidates == []

    def test_extract_candidates_card_error_skipped(self, collector, mock_driver):
        """单个卡片提取失败 → 跳过，不影响其他"""
        bad_card = MagicMock()
        bad_card.find_element.side_effect = Exception("element error")

        good_card = MagicMock()
        name_el = MagicMock()
        name_el.text = "王五"
        good_card.find_element.side_effect = lambda by, sel: {
            ".name": name_el,
            "a": MagicMock(**{"get_attribute.return_value": "/profile/u_003"}),
        }.get(sel, MagicMock())

        mock_driver.find_elements.return_value = [bad_card, good_card]

        candidates = collector._extract_candidates_from_page()
        assert len(candidates) == 1
        assert candidates[0].name == "王五"


class TestExtractResume:
    def test_extract_resume_happy_path(self, collector, mock_driver):
        """正常提取简历文本"""
        resume_el = MagicMock()
        resume_el.text = "Java 5年经验，精通Spring Boot"
        mock_driver.find_element.return_value = resume_el

        result = collector._extract_resume("https://zhipin.com/profile/u_001")
        assert "Java" in result

    def test_extract_resume_page_load_fail(self, collector, mock_driver):
        """简历页加载失败 → 返回空字符串"""
        from selenium.common.exceptions import WebDriverException
        mock_driver.get.side_effect = WebDriverException("timeout")
        result = collector._extract_resume("https://zhipin.com/profile/u_001")
        assert result == ""

    def test_extract_resume_selector_missing(self, collector, mock_driver):
        """简历选择器不存在 → 返回空字符串"""
        from selenium.common.exceptions import NoSuchElementException
        mock_driver.find_element.side_effect = NoSuchElementException("not found")
        result = collector._extract_resume("https://zhipin.com/profile/u_001")
        assert result == ""


class TestCollectCandidates:
    def test_collect_and_save_to_db(self, collector, mock_driver, db):
        """采集候选人并写入数据库"""
        card = MagicMock()
        name_el = MagicMock()
        name_el.text = "测试用户"
        card.find_element.side_effect = lambda by, sel: {
            ".name": name_el,
            "a": MagicMock(**{"get_attribute.return_value": "/profile/u_test_001"}),
        }.get(sel, MagicMock())

        mock_driver.find_elements.side_effect = [
            [card],   # 第一页候选人
            [],       # 第二页为空 → 结束
        ]

        from selenium.common.exceptions import NoSuchElementException
        mock_driver.find_element.side_effect = NoSuchElementException("no resume")

        candidates = collector.collect_candidates("https://zhipin.com/job/123")
        assert len(candidates) == 1

        db_candidates = db.list_candidates()
        assert len(db_candidates) == 1
        assert db_candidates[0]["platform"] == "boss"

    def test_collect_empty_list(self, collector, mock_driver):
        """候选人列表为空 → 正常返回"""
        mock_driver.find_elements.return_value = []
        candidates = collector.collect_candidates("https://zhipin.com/job/123")
        assert candidates == []

    def test_collect_page_load_failure_raises(self, collector, mock_driver):
        """列表页加载失败 → 抛出 PageLoadError"""
        from selenium.common.exceptions import WebDriverException
        mock_driver.get.side_effect = WebDriverException("network error")
        with pytest.raises(PageLoadError):
            collector.collect_candidates("https://zhipin.com/job/123")

    def test_collect_dedup(self, collector, mock_driver, db):
        """重复候选人不重复插入"""
        card = MagicMock()
        name_el = MagicMock()
        name_el.text = "去重测试"
        card.find_element.side_effect = lambda by, sel: {
            ".name": name_el,
            "a": MagicMock(**{"get_attribute.return_value": "/profile/u_dedup_001"}),
        }.get(sel, MagicMock())

        mock_driver.find_elements.side_effect = [
            [card],
            [],
        ]

        from selenium.common.exceptions import NoSuchElementException
        mock_driver.find_element.side_effect = NoSuchElementException("no resume")
        collector.collect_candidates("https://zhipin.com/job/123")

        # 再采集一次
        mock_driver.find_elements.side_effect = [
            [card],
            [],
        ]
        mock_driver.find_element.side_effect = NoSuchElementException("no resume")
        collector.collect_candidates("https://zhipin.com/job/123")

        assert len(db.list_candidates()) == 1


class TestNavigateWithRetry:
    def test_retry_once_then_succeed(self, collector, mock_driver):
        """第一次失败，重试成功"""
        from selenium.common.exceptions import WebDriverException
        mock_driver.get.side_effect = [
            WebDriverException("first fail"),
            None,
        ]
        result = collector._navigate_with_retry("https://zhipin.com")
        assert result is True

    def test_retry_exhausted(self, collector, mock_driver):
        """重试用尽 → 返回 False"""
        from selenium.common.exceptions import WebDriverException
        mock_driver.get.side_effect = WebDriverException("always fail")
        result = collector._navigate_with_retry("https://zhipin.com")
        assert result is False
