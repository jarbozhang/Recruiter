"""Boss直聘 Web 端候选人数据采集器

通过 Playwright async API 自动浏览 Boss直聘网页，
采集候选人列表和简历数据并存入 DB。
"""

import asyncio
import logging
import random
from dataclasses import dataclass, field

from recruiter.db.models import Database

logger = logging.getLogger(__name__)

# 页面关键 CSS 选择器 —— 用于 health check
SELECTORS = {
    # 候选人列表页
    "candidate_list": ".candidate-list",
    "candidate_card": ".candidate-card",
    "candidate_name": ".candidate-card .name",
    "candidate_detail_link": ".candidate-card a",
    # 简历详情页
    "resume_container": ".resume-container",
    "resume_text": ".resume-content",
    # 分页
    "next_page": ".pagination .next",
}

# 翻页随机等待范围（秒）
PAGE_TURN_WAIT_MIN = 3
PAGE_TURN_WAIT_MAX = 8

# 页面加载超时（毫秒）
PAGE_LOAD_TIMEOUT = 30_000

# 重试次数
MAX_RETRIES = 1


class HealthCheckError(Exception):
    """页面结构校验失败"""
    pass


class PageLoadError(Exception):
    """页面加载失败"""
    pass


@dataclass
class CandidateInfo:
    """从页面提取的候选人信息"""
    platform_id: str
    name: str
    detail_url: str = ""
    resume_text: str = ""
    extra: dict = field(default_factory=dict)


class BossWebCollector:
    """Boss直聘 Web 端数据采集器

    Args:
        page: Playwright Page 对象（async API）
        db: Database 实例
    """

    def __init__(self, page, db: Database):
        self.page = page
        self.db = db

    async def health_check(self, url: str) -> bool:
        """验证关键页面元素是否存在（R9）

        导航到目标 URL，检查必要的 CSS 选择器是否存在。
        如果关键选择器缺失，说明页面结构已变化，抛出 HealthCheckError。
        """
        try:
            await self.page.goto(url, timeout=PAGE_LOAD_TIMEOUT)
        except Exception as e:
            raise PageLoadError(f"页面加载失败: {url}, {e}") from e

        missing = []
        for name, selector in SELECTORS.items():
            # 只检查列表页的关键选择器
            if name in ("candidate_list", "candidate_card", "candidate_name"):
                el = await self.page.query_selector(selector)
                if el is None:
                    missing.append(f"{name} ({selector})")

        if missing:
            msg = f"健康检查失败，以下选择器缺失: {', '.join(missing)}"
            logger.error(msg)
            raise HealthCheckError(msg)

        logger.info("健康检查通过")
        return True

    async def _extract_candidates_from_page(self) -> list[CandidateInfo]:
        """从当前页面提取候选人列表"""
        cards = await self.page.query_selector_all(SELECTORS["candidate_card"])
        candidates = []

        for card in cards:
            try:
                name_el = await card.query_selector(".name")
                name = (await name_el.inner_text()).strip() if name_el else ""

                link_el = await card.query_selector("a")
                href = await link_el.get_attribute("href") if link_el else ""

                # 从链接中提取 platform_id
                platform_id = href.strip("/").split("/")[-1] if href else ""

                if platform_id:
                    candidates.append(CandidateInfo(
                        platform_id=platform_id,
                        name=name,
                        detail_url=href,
                    ))
            except Exception as e:
                logger.warning(f"提取候选人卡片信息失败: {e}")
                continue

        return candidates

    async def _extract_resume(self, detail_url: str) -> str:
        """从候选人详情页提取简历文本"""
        try:
            await self.page.goto(detail_url, timeout=PAGE_LOAD_TIMEOUT)
        except Exception as e:
            logger.warning(f"简历页加载失败: {detail_url}, {e}")
            return ""

        resume_el = await self.page.query_selector(SELECTORS["resume_text"])
        if resume_el is None:
            logger.warning(f"简历内容选择器不存在: {detail_url}")
            return ""

        return (await resume_el.inner_text()).strip()

    async def _navigate_with_retry(self, url: str) -> bool:
        """带重试的页面导航"""
        for attempt in range(MAX_RETRIES + 1):
            try:
                await self.page.goto(url, timeout=PAGE_LOAD_TIMEOUT)
                return True
            except Exception as e:
                if attempt < MAX_RETRIES:
                    logger.warning(f"页面加载失败，重试中 ({attempt + 1}/{MAX_RETRIES}): {e}")
                    await asyncio.sleep(1)
                else:
                    logger.error(f"页面加载失败，已达最大重试次数: {url}, {e}")
                    return False

    async def collect_candidates(self, job_url: str) -> list[CandidateInfo]:
        """采集候选人列表

        流程: 导航到职位候选人列表 → 逐页提取候选人 → 获取简历 → 存入 DB

        Args:
            job_url: Boss直聘职位候选人列表页 URL

        Returns:
            采集到的候选人信息列表
        """
        if not await self._navigate_with_retry(job_url):
            raise PageLoadError(f"无法加载候选人列表页: {job_url}")

        all_candidates: list[CandidateInfo] = []
        page_num = 1

        while True:
            logger.info(f"正在采集第 {page_num} 页候选人...")
            candidates = await self._extract_candidates_from_page()

            if not candidates:
                logger.info(f"第 {page_num} 页无候选人，采集结束")
                break

            # 获取每个候选人的简历
            for c in candidates:
                if c.detail_url:
                    c.resume_text = await self._extract_resume(c.detail_url)
                    # 回到列表页
                    await self._navigate_with_retry(job_url)

                # 存入 DB，source 统一为 outbound（主动搜索）
                self.db.upsert_candidate(
                    platform="boss",
                    platform_id=c.platform_id,
                    name=c.name,
                    resume_text=c.resume_text,
                    source="outbound",
                )

            all_candidates.extend(candidates)

            # 检查是否有下一页
            next_btn = await self.page.query_selector(SELECTORS["next_page"])
            if next_btn is None:
                logger.info("没有下一页，采集结束")
                break

            # 是否禁用状态
            is_disabled = await next_btn.get_attribute("disabled")
            next_class = await next_btn.get_attribute("class") or ""
            if is_disabled is not None or "disabled" in next_class:
                logger.info("下一页按钮已禁用，采集结束")
                break

            # 翻页 + 随机等待
            await next_btn.click()
            wait_sec = random.uniform(PAGE_TURN_WAIT_MIN, PAGE_TURN_WAIT_MAX)
            logger.info(f"翻页等待 {wait_sec:.1f}s...")
            await asyncio.sleep(wait_sec)
            page_num += 1

        logger.info(f"采集完成，共 {len(all_candidates)} 位候选人")
        return all_candidates
