"""Boss直聘 Web 端候选人数据采集器

通过 BrowserDriver 接口（AdsPower/bb-browser/Playwright 等）
自动浏览 Boss直聘网页，采集候选人列表和简历数据并存入 DB。

数据获取策略：API 拦截优先，DOM 解析兜底。
- Playwright driver: 拦截 /wapi/zprelation/friend/getBossFriendListV2 接口
- 其他 driver: 退化为 DOM 解析（JS querySelectorAll）
"""

import json
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

# API 拦截目标
API_FRIEND_LIST = "getBossFriendListV2"
API_LAST_MSG = "userLastMsg"

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

    def _supports_intercept(self) -> bool:
        """检查当前 driver 是否支持 API 拦截。"""
        return hasattr(self.browser, 'intercept_response')

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

    # ------ API 拦截模式（Playwright 专用） ------

    def _collect_via_api(self) -> list[CandidateInfo] | None:
        """通过拦截 Boss直聘 API 获取候选人列表。

        Returns:
            候选人列表，如果拦截失败返回 None（触发 DOM 兜底）。
        """
        if not self._supports_intercept():
            return None

        captured = {}

        def on_response(response):
            try:
                url = response.url
                if API_FRIEND_LIST in url:
                    captured["friends"] = json.loads(response.text())
                elif API_LAST_MSG in url:
                    captured["last_msgs"] = json.loads(response.text())
            except Exception as e:
                logger.debug("API 拦截解析失败: %s", e)

        self.browser.intercept_response("wapi", on_response)

        try:
            # 先导航到聊天页，再 reload 确保触发 API 请求
            current = self.browser.current_url()
            if "web/chat" not in current:
                self.browser.navigate(BOSS_URLS["chat"])
                time.sleep(2)
            self.browser.reload()

            # 等待 API 响应到达
            for _ in range(20):  # 最多等 10 秒
                if "friends" in captured:
                    break
                time.sleep(0.5)

            if "friends" not in captured:
                logger.warning("API 拦截超时，未捕获到好友列表接口")
                return None

            return self._parse_api_friends(captured)

        except Exception as e:
            logger.warning("API 拦截模式异常: %s", e)
            return None
        finally:
            self.browser.stop_intercept()

    def _parse_api_friends(self, captured: dict) -> list[CandidateInfo]:
        """解析 API 响应为 CandidateInfo 列表。"""
        friends_data = captured.get("friends", {})
        friend_list = friends_data.get("zpData", {}).get("friendList", [])

        # 构建 lastMsg 索引
        last_msgs = {}
        msgs_data = captured.get("last_msgs", {})
        for msg in msgs_data.get("zpData", []) if isinstance(msgs_data.get("zpData"), list) else []:
            last_msgs[msg.get("uid")] = msg.get("lastMsgInfo", {})

        candidates = []
        for f in friend_list:
            uid = f.get("uid", 0)
            platform_id = str(uid) if uid else f.get("encryptUid", "")
            if not platform_id or platform_id == "0":
                continue

            last_msg_info = last_msgs.get(uid, {})

            extra = {
                "avatar": f.get("avatar", ""),
                "job_name": f.get("jobName", ""),
                "degree": f.get("degree", ""),
                "expect_salary": f.get("expectSalary", ""),
                "last_work": f.get("lastWorkExpr", ""),
                "encrypt_uid": f.get("encryptUid", ""),
                "encrypt_job_id": f.get("encryptJobId", ""),
                "chat_status": f.get("chatStatus", 0),
                "relation_type": f.get("relationType", 0),
                "last_msg": last_msg_info.get("showText", ""),
                "last_msg_time": f.get("lastTS", 0),
                "source": "api_intercept",
            }

            candidates.append(CandidateInfo(
                platform_id=platform_id,
                name=f.get("name", ""),
                extra=extra,
            ))

        logger.info("API 拦截获取 %d 个候选人", len(candidates))
        return candidates

    # ------ DOM 解析模式（通用兜底） ------

    def _extract_candidates_from_page(self) -> list[CandidateInfo]:
        """用 JS 一次性提取所有候选人数据，避免逐个查询。"""
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
            try:
                data = json.loads(data)
            except Exception:
                return []
        return [
            CandidateInfo(
                platform_id=item["platform_id"],
                name=item["name"],
                extra={"source": "dom_parse"},
            )
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

    # ------ 主入口 ------

    def collect_candidates(self, job_url: str = None) -> list[CandidateInfo]:
        """采集候选人列表。

        策略：API 拦截优先 → DOM 解析兜底。

        Args:
            job_url: 职位页 URL。API 模式下可省略（直接访问聊天页）。
        """
        # 1. 尝试 API 拦截
        candidates = self._collect_via_api()

        if candidates is not None:
            logger.info("使用 API 拦截模式，获取 %d 个候选人", len(candidates))
            self._save_candidates(candidates)
            return candidates

        # 2. 退化到 DOM 解析
        logger.info("API 拦截不可用，退化到 DOM 解析模式")
        return self._collect_via_dom(job_url or BOSS_URLS["chat"])

    def _collect_via_dom(self, job_url: str) -> list[CandidateInfo]:
        """DOM 解析模式采集（原逻辑）。"""
        if not self._navigate_with_retry(job_url):
            raise PageLoadError(f"无法加载候选人列表页: {job_url}")

        all_candidates: list[CandidateInfo] = []
        page_num = 1

        while True:
            logger.info("正在采集第 %d 页候选人（DOM 模式）...", page_num)
            candidates = self._extract_candidates_from_page()

            if not candidates:
                logger.info("第 %d 页无候选人，采集结束", page_num)
                break

            for c in candidates:
                if c.detail_url:
                    c.resume_text = self._extract_resume(c.detail_url)
                    self._navigate_with_retry(job_url)

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

        self._save_candidates(all_candidates)
        logger.info("采集完成（DOM 模式），共 %d 位候选人", len(all_candidates))
        return all_candidates

    def _save_candidates(self, candidates: list[CandidateInfo]) -> None:
        """将候选人列表存入 DB。"""
        for c in candidates:
            self.db.upsert_candidate(
                platform="boss",
                platform_id=c.platform_id,
                name=c.name,
                resume_text=c.resume_text,
                source="outbound",
            )
