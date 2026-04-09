"""Boss直聘 Web 端候选人数据采集器

通过 AdsPower 指纹浏览器 + Selenium 自动浏览 Boss直聘网页，
采集候选人列表和简历数据并存入 DB。
"""

import logging
import random
import time
from dataclasses import dataclass, field

from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from recruiter.db.models import Database

logger = logging.getLogger(__name__)

# 页面关键 CSS 选择器 —— 用于 health check（需要根据真实页面调整）
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

# 页面加载超时（秒）
PAGE_LOAD_TIMEOUT = 30

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
        driver: Selenium WebDriver 实例（通过 AdsPower 获取）
        db: Database 实例
    """

    def __init__(self, driver: WebDriver, db: Database):
        self.driver = driver
        self.db = db

    def health_check(self, url: str) -> bool:
        """验证关键页面元素是否存在（R9）

        导航到目标 URL，检查必要的 CSS 选择器是否存在。
        如果关键选择器缺失，说明页面结构已变化，抛出 HealthCheckError。
        """
        try:
            self.driver.get(url)
            WebDriverWait(self.driver, PAGE_LOAD_TIMEOUT).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "body"))
            )
        except (TimeoutException, WebDriverException) as e:
            raise PageLoadError(f"页面加载失败: {url}, {e}") from e

        missing = []
        check_selectors = ["candidate_list", "candidate_card", "candidate_name"]
        for name in check_selectors:
            selector = SELECTORS[name]
            elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
            if not elements:
                missing.append(f"{name} ({selector})")

        if missing:
            msg = f"健康检查失败，以下选择器缺失: {', '.join(missing)}"
            logger.error(msg)
            raise HealthCheckError(msg)

        logger.info("健康检查通过")
        return True

    def _extract_candidates_from_page(self) -> list[CandidateInfo]:
        """从当前页面提取候选人列表"""
        cards = self.driver.find_elements(By.CSS_SELECTOR, SELECTORS["candidate_card"])
        candidates = []

        for card in cards:
            try:
                try:
                    name_el = card.find_element(By.CSS_SELECTOR, ".name")
                    name = name_el.text.strip()
                except NoSuchElementException:
                    name = ""

                try:
                    link_el = card.find_element(By.CSS_SELECTOR, "a")
                    href = link_el.get_attribute("href") or ""
                except NoSuchElementException:
                    href = ""

                # 从链接中提取 platform_id
                platform_id = href.strip("/").split("/")[-1] if href else ""

                if platform_id:
                    candidates.append(CandidateInfo(
                        platform_id=platform_id,
                        name=name,
                        detail_url=href,
                    ))
            except Exception as e:
                logger.warning("提取候选人卡片信息失败: %s", e)
                continue

        return candidates

    def _extract_resume(self, detail_url: str) -> str:
        """从候选人详情页提取简历文本"""
        try:
            self.driver.get(detail_url)
            WebDriverWait(self.driver, PAGE_LOAD_TIMEOUT).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "body"))
            )
        except (TimeoutException, WebDriverException) as e:
            logger.warning("简历页加载失败: %s, %s", detail_url, e)
            return ""

        try:
            resume_el = self.driver.find_element(By.CSS_SELECTOR, SELECTORS["resume_text"])
            return resume_el.text.strip()
        except NoSuchElementException:
            logger.warning("简历内容选择器不存在: %s", detail_url)
            return ""

    def _navigate_with_retry(self, url: str) -> bool:
        """带重试的页面导航"""
        for attempt in range(MAX_RETRIES + 1):
            try:
                self.driver.get(url)
                WebDriverWait(self.driver, PAGE_LOAD_TIMEOUT).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "body"))
                )
                return True
            except (TimeoutException, WebDriverException) as e:
                if attempt < MAX_RETRIES:
                    logger.warning("页面加载失败，重试中 (%d/%d): %s", attempt + 1, MAX_RETRIES, e)
                    time.sleep(1)
                else:
                    logger.error("页面加载失败，已达最大重试次数: %s, %s", url, e)
                    return False

    def collect_candidates(self, job_url: str) -> list[CandidateInfo]:
        """采集候选人列表

        流程: 导航到职位候选人列表 → 逐页提取候选人 → 获取简历 → 存入 DB

        Args:
            job_url: Boss直聘职位候选人列表页 URL

        Returns:
            采集到的候选人信息列表
        """
        if not self._navigate_with_retry(job_url):
            raise PageLoadError(f"无法加载候选人列表页: {job_url}")

        all_candidates: list[CandidateInfo] = []
        page_num = 1

        while True:
            logger.info("正在采集第 %d 页候选人...", page_num)
            candidates = self._extract_candidates_from_page()

            if not candidates:
                logger.info("第 %d 页无候选人，采集结束", page_num)
                break

            # 获取每个候选人的简历
            for c in candidates:
                if c.detail_url:
                    c.resume_text = self._extract_resume(c.detail_url)
                    # 回到列表页
                    self._navigate_with_retry(job_url)

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
            next_btns = self.driver.find_elements(By.CSS_SELECTOR, SELECTORS["next_page"])
            if not next_btns:
                logger.info("没有下一页，采集结束")
                break

            next_btn = next_btns[0]
            is_disabled = next_btn.get_attribute("disabled")
            next_class = next_btn.get_attribute("class") or ""
            if is_disabled is not None or "disabled" in next_class:
                logger.info("下一页按钮已禁用，采集结束")
                break

            # 翻页 + 随机等待
            next_btn.click()
            wait_sec = random.uniform(PAGE_TURN_WAIT_MIN, PAGE_TURN_WAIT_MAX)
            logger.info("翻页等待 %.1fs...", wait_sec)
            time.sleep(wait_sec)
            page_num += 1

        logger.info("采集完成，共 %d 位候选人", len(all_candidates))
        return all_candidates
