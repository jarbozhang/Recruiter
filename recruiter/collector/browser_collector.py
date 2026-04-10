"""Boss直聘 Web 端候选人数据采集器

通过 BrowserDriver 接口（AdsPower/bb-browser/Playwright 等）
自动浏览 Boss直聘网页，采集候选人列表和简历数据并存入 DB。
"""

import logging
import random
import time
from dataclasses import dataclass, field

from recruiter.browser.base import BrowserDriver
from recruiter.db.models import Database

logger = logging.getLogger(__name__)

# Boss直聘真实 CSS 选择器（2026-04 实测）
SELECTORS = {
    "candidate_list": ".geek-item-wrap",
    "candidate_card": ".geek-item",
    "candidate_name": ".geek-name",
    "candidate_detail_link": ".geek-item",
    "chat_input": ".boss-chat-editor-input",
    "message_item": ".message-item",
    "resume_container": ".resume-container",
    "resume_text": ".resume-content",
    "geek_manage_card": ".geek-card",
    "next_page": ".pagination .next, .page-next",
}

BOSS_URLS = {
    "chat": "https://www.zhipin.com/web/chat/index",
    "geek_manage": "https://www.zhipin.com/web/chat/geek/manage_v2",
    "job_list": "https://www.zhipin.com/web/chat/job/list",
}

PAGE_TURN_WAIT_MIN = 3
PAGE_TURN_WAIT_MAX = 8
MAX_RETRIES = 1


class HealthCheckError(Exception):
    pass


class PageLoadError(Exception):
    pass


@dataclass
class CandidateInfo:
    platform_id: str
    name: str
    detail_url: str = ""
    resume_text: str = ""
    extra: dict = field(default_factory=dict)


class BossWebCollector:
    """Boss直聘 Web 端数据采集器

    Args:
        browser: 任何实现了 BrowserDriver 接口的驱动
        db: Database 实例
    """

    def __init__(self, browser: BrowserDriver, db: Database):
        self.browser = browser
        self.db = db

    def health_check(self, url: str) -> bool:
        try:
            self.browser.navigate(url)
        except Exception as e:
            raise PageLoadError(f"页面加载失败: {url}, {e}") from e

        if not self.browser.wait_for("body", timeout=30):
            raise PageLoadError(f"页面加载超时: {url}")

        missing = []
        for name in ["candidate_list", "candidate_card", "candidate_name"]:
            selector = SELECTORS[name]
            if not self.browser.find_elements(selector):
                missing.append(f"{name} ({selector})")

        if missing:
            msg = f"健康检查失败，以下选择器缺失: {', '.join(missing)}"
            logger.error(msg)
            raise HealthCheckError(msg)

        logger.info("健康检查通过")
        return True

    def _extract_candidates_from_page(self) -> list[CandidateInfo]:
        # 用 JS 一次性提取所有候选人数据，避免逐个查询
        data = self.browser.execute_js('''
            var cards = document.querySelectorAll(".geek-item");
            var result = [];
            cards.forEach(function(card) {
                var nameEl = card.querySelector(".geek-name");
                var name = nameEl ? nameEl.textContent.trim() : "";
                var dataId = card.getAttribute("data-id") || "";
                result.push({name: name, platform_id: dataId});
            });
            return JSON.stringify(result);
        ''')
        if not data:
            return []
        if isinstance(data, str):
            import json
            try:
                data = json.loads(data)
            except Exception:
                return []
        return [
            CandidateInfo(platform_id=item["platform_id"], name=item["name"])
            for item in data
            if item.get("platform_id")
        ]

    def _extract_resume(self, detail_url: str) -> str:
        try:
            self.browser.navigate(detail_url)
        except Exception as e:
            logger.warning("简历页加载失败: %s, %s", detail_url, e)
            return ""

        text = self.browser.get_text(SELECTORS["resume_text"])
        if not text:
            logger.warning("简历内容选择器不存在: %s", detail_url)
        return text

    def _navigate_with_retry(self, url: str) -> bool:
        for attempt in range(MAX_RETRIES + 1):
            try:
                self.browser.navigate(url)
                if self.browser.wait_for("body", timeout=30):
                    return True
            except Exception as e:
                if attempt < MAX_RETRIES:
                    logger.warning("页面加载失败，重试中 (%d/%d): %s", attempt + 1, MAX_RETRIES, e)
                    time.sleep(1)
                else:
                    logger.error("页面加载失败，已达最大重试次数: %s, %s", url, e)
        return False

    def collect_candidates(self, job_url: str) -> list[CandidateInfo]:
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

            for c in candidates:
                if c.detail_url:
                    c.resume_text = self._extract_resume(c.detail_url)
                    self._navigate_with_retry(job_url)

                self.db.upsert_candidate(
                    platform="boss",
                    platform_id=c.platform_id,
                    name=c.name,
                    resume_text=c.resume_text,
                    source="outbound",
                )

            all_candidates.extend(candidates)

            # 翻页
            if not self.browser.is_visible(SELECTORS["next_page"]):
                logger.info("没有下一页，采集结束")
                break

            disabled = self.browser.get_attribute(SELECTORS["next_page"], "disabled")
            if disabled is not None:
                logger.info("下一页按钮已禁用，采集结束")
                break

            self.browser.click(SELECTORS["next_page"])
            wait_sec = random.uniform(PAGE_TURN_WAIT_MIN, PAGE_TURN_WAIT_MAX)
            logger.info("翻页等待 %.1fs...", wait_sec)
            time.sleep(wait_sec)
            page_num += 1

        logger.info("采集完成，共 %d 位候选人", len(all_candidates))
        return all_candidates
