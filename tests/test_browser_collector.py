"""BossWebCollector 单元测试

使用 mock Page 对象模拟 Playwright 行为，不依赖真实浏览器。
"""

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from recruiter.collector.browser_collector import (
    BossWebCollector,
    CandidateInfo,
    HealthCheckError,
    PageLoadError,
    SELECTORS,
)
from recruiter.db.models import Database


@pytest.fixture
def db():
    """临时测试数据库"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database = Database(path)
    yield database
    database.close()
    os.unlink(path)


def make_mock_element(name="张三", href="/candidate/uid_001"):
    """创建 mock 的候选人卡片 DOM 元素"""
    name_el = AsyncMock()
    name_el.inner_text = AsyncMock(return_value=name)

    link_el = AsyncMock()
    link_el.get_attribute = AsyncMock(return_value=href)

    card = AsyncMock()

    async def query_selector(selector):
        if selector == ".name":
            return name_el
        if selector == "a":
            return link_el
        return None

    card.query_selector = query_selector
    return card


def make_mock_page():
    """创建 mock 的 Playwright Page"""
    page = AsyncMock()
    page.goto = AsyncMock()
    page.query_selector = AsyncMock(return_value=None)
    page.query_selector_all = AsyncMock(return_value=[])
    return page


class TestHealthCheck:
    """健康检查测试"""

    @pytest.mark.asyncio
    async def test_health_check_pass(self, db):
        """所有关键选择器都存在 → 通过"""
        page = make_mock_page()
        # query_selector 对关键选择器返回非 None
        async def mock_qs(selector):
            if selector in (
                SELECTORS["candidate_list"],
                SELECTORS["candidate_card"],
                SELECTORS["candidate_name"],
            ):
                return MagicMock()  # 非 None 表示元素存在
            return None

        page.query_selector = mock_qs
        collector = BossWebCollector(page, db)

        result = await collector.health_check("https://www.zhipin.com/web/boss/recommend")
        assert result is True

    @pytest.mark.asyncio
    async def test_health_check_fail_missing_selector(self, db):
        """关键选择器缺失 → 抛出 HealthCheckError"""
        page = make_mock_page()
        # 所有 query_selector 返回 None
        page.query_selector = AsyncMock(return_value=None)
        collector = BossWebCollector(page, db)

        with pytest.raises(HealthCheckError, match="选择器缺失"):
            await collector.health_check("https://www.zhipin.com/web/boss/recommend")

    @pytest.mark.asyncio
    async def test_health_check_page_load_failure(self, db):
        """页面加载失败 → 抛出 PageLoadError"""
        page = make_mock_page()
        page.goto = AsyncMock(side_effect=Exception("net::ERR_CONNECTION_TIMED_OUT"))
        collector = BossWebCollector(page, db)

        with pytest.raises(PageLoadError, match="页面加载失败"):
            await collector.health_check("https://www.zhipin.com/web/boss/recommend")


class TestExtractCandidates:
    """候选人列表提取测试"""

    @pytest.mark.asyncio
    async def test_extract_candidates_happy_path(self, db):
        """正常提取候选人列表"""
        page = make_mock_page()
        card1 = make_mock_element("张三", "/candidate/uid_001")
        card2 = make_mock_element("李四", "/candidate/uid_002")
        page.query_selector_all = AsyncMock(return_value=[card1, card2])

        collector = BossWebCollector(page, db)
        candidates = await collector._extract_candidates_from_page()

        assert len(candidates) == 2
        assert candidates[0].name == "张三"
        assert candidates[0].platform_id == "uid_001"
        assert candidates[1].name == "李四"
        assert candidates[1].platform_id == "uid_002"

    @pytest.mark.asyncio
    async def test_extract_candidates_empty_list(self, db):
        """空候选人列表 → 返回空列表，无异常"""
        page = make_mock_page()
        page.query_selector_all = AsyncMock(return_value=[])

        collector = BossWebCollector(page, db)
        candidates = await collector._extract_candidates_from_page()

        assert candidates == []

    @pytest.mark.asyncio
    async def test_extract_candidates_card_error_skipped(self, db):
        """单个卡片提取失败不影响其他卡片"""
        page = make_mock_page()

        # 第一个卡片正常
        card1 = make_mock_element("张三", "/candidate/uid_001")

        # 第二个卡片 name 元素抛异常
        bad_card = AsyncMock()
        async def bad_qs(selector):
            raise Exception("element detached")
        bad_card.query_selector = bad_qs

        # 第三个卡片正常
        card3 = make_mock_element("王五", "/candidate/uid_003")

        page.query_selector_all = AsyncMock(return_value=[card1, bad_card, card3])

        collector = BossWebCollector(page, db)
        candidates = await collector._extract_candidates_from_page()

        assert len(candidates) == 2
        assert candidates[0].name == "张三"
        assert candidates[1].name == "王五"


class TestExtractResume:
    """简历提取测试"""

    @pytest.mark.asyncio
    async def test_extract_resume_happy_path(self, db):
        """正常提取简历文本"""
        page = make_mock_page()
        resume_el = AsyncMock()
        resume_el.inner_text = AsyncMock(return_value="  5年Java开发经验，熟悉Spring...  ")

        async def mock_qs(selector):
            if selector == SELECTORS["resume_text"]:
                return resume_el
            return None

        page.query_selector = mock_qs
        collector = BossWebCollector(page, db)

        text = await collector._extract_resume("https://www.zhipin.com/candidate/uid_001")
        assert text == "5年Java开发经验，熟悉Spring..."

    @pytest.mark.asyncio
    async def test_extract_resume_page_load_fail(self, db):
        """简历页加载失败 → 返回空字符串"""
        page = make_mock_page()
        page.goto = AsyncMock(side_effect=Exception("timeout"))
        collector = BossWebCollector(page, db)

        text = await collector._extract_resume("https://www.zhipin.com/candidate/uid_001")
        assert text == ""

    @pytest.mark.asyncio
    async def test_extract_resume_selector_missing(self, db):
        """简历选择器不存在 → 返回空字符串"""
        page = make_mock_page()
        page.query_selector = AsyncMock(return_value=None)
        collector = BossWebCollector(page, db)

        text = await collector._extract_resume("https://www.zhipin.com/candidate/uid_001")
        assert text == ""


class TestCollectCandidates:
    """完整采集流程测试"""

    @pytest.mark.asyncio
    async def test_collect_and_save_to_db(self, db):
        """采集候选人并存入数据库"""
        page = make_mock_page()

        card1 = make_mock_element("张三", "/candidate/uid_001")
        card2 = make_mock_element("李四", "/candidate/uid_002")

        # 第一次 query_selector_all 返回候选人列表
        # 之后返回空列表（导航回来后）
        call_count = {"value": 0}
        original_qsa = page.query_selector_all

        async def mock_qsa(selector):
            call_count["value"] += 1
            if call_count["value"] == 1:
                return [card1, card2]
            return []

        page.query_selector_all = mock_qsa

        # resume 提取
        resume_el = AsyncMock()
        resume_el.inner_text = AsyncMock(return_value="Java 5年经验")

        async def mock_qs(selector):
            if selector == SELECTORS["resume_text"]:
                return resume_el
            if selector == SELECTORS["next_page"]:
                return None  # 只有一页
            return None

        page.query_selector = mock_qs

        collector = BossWebCollector(page, db)

        with patch("recruiter.collector.browser_collector.asyncio.sleep", new_callable=AsyncMock):
            candidates = await collector.collect_candidates("https://www.zhipin.com/job/123/candidates")

        assert len(candidates) == 2

        # 验证已存入 DB
        db_candidates = db.list_candidates()
        assert len(db_candidates) == 2
        assert any(c["platform_id"] == "uid_001" for c in db_candidates)
        assert any(c["platform_id"] == "uid_002" for c in db_candidates)
        # source 应为 outbound
        assert all(c["source"] == "outbound" for c in db_candidates)

    @pytest.mark.asyncio
    async def test_collect_empty_list(self, db):
        """候选人列表为空 → 返回空列表，不报错"""
        page = make_mock_page()
        page.query_selector_all = AsyncMock(return_value=[])
        page.query_selector = AsyncMock(return_value=None)

        collector = BossWebCollector(page, db)

        with patch("recruiter.collector.browser_collector.asyncio.sleep", new_callable=AsyncMock):
            candidates = await collector.collect_candidates("https://www.zhipin.com/job/123/candidates")

        assert candidates == []
        assert db.list_candidates() == []

    @pytest.mark.asyncio
    async def test_collect_page_load_failure_with_retry(self, db):
        """页面加载失败 → 重试 1 次后抛出 PageLoadError"""
        page = make_mock_page()
        page.goto = AsyncMock(side_effect=Exception("net::ERR_TIMED_OUT"))

        collector = BossWebCollector(page, db)

        with patch("recruiter.collector.browser_collector.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(PageLoadError, match="无法加载"):
                await collector.collect_candidates("https://www.zhipin.com/job/123/candidates")

        # 应该尝试了 2 次（1 次 + 1 次重试）
        assert page.goto.call_count == 2

    @pytest.mark.asyncio
    async def test_collect_dedup(self, db):
        """重复候选人通过 upsert 去重"""
        page = make_mock_page()

        # 两次都返回相同候选人
        card = make_mock_element("张三", "/candidate/uid_001")

        call_count = {"value": 0}

        async def mock_qsa(selector):
            call_count["value"] += 1
            if call_count["value"] == 1:
                return [card]
            return []

        page.query_selector_all = mock_qsa

        resume_el = AsyncMock()
        resume_el.inner_text = AsyncMock(return_value="简历内容")

        async def mock_qs(selector):
            if selector == SELECTORS["resume_text"]:
                return resume_el
            return None

        page.query_selector = mock_qs

        collector = BossWebCollector(page, db)

        with patch("recruiter.collector.browser_collector.asyncio.sleep", new_callable=AsyncMock):
            await collector.collect_candidates("https://www.zhipin.com/job/123/candidates")

        # 先手动插入同一候选人
        db.upsert_candidate("boss", "uid_001", "张三旧", "旧简历", "inbound")

        # DB 中只有一条记录（upsert 去重）
        db_candidates = db.list_candidates(platform="boss")
        assert len(db_candidates) == 1


class TestNavigateWithRetry:
    """带重试的导航测试"""

    @pytest.mark.asyncio
    async def test_retry_once_then_succeed(self, db):
        """第一次失败，重试成功"""
        page = make_mock_page()
        page.goto = AsyncMock(side_effect=[Exception("timeout"), None])

        collector = BossWebCollector(page, db)

        with patch("recruiter.collector.browser_collector.asyncio.sleep", new_callable=AsyncMock):
            result = await collector._navigate_with_retry("https://example.com")

        assert result is True
        assert page.goto.call_count == 2

    @pytest.mark.asyncio
    async def test_retry_exhausted(self, db):
        """重试用完仍失败 → 返回 False"""
        page = make_mock_page()
        page.goto = AsyncMock(side_effect=Exception("timeout"))

        collector = BossWebCollector(page, db)

        with patch("recruiter.collector.browser_collector.asyncio.sleep", new_callable=AsyncMock):
            result = await collector._navigate_with_retry("https://example.com")

        assert result is False
        assert page.goto.call_count == 2  # 1 + 1 retry
