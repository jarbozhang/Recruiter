"""Boss直聘 Web 端候选人数据采集器

通过 BrowserDriver 接口（AdsPower/bb-browser/Playwright 等）
自动浏览 Boss直聘网页，采集候选人列表和简历数据并存入 DB。

三层数据获取策略：
1. API 拦截（Playwright）：拦截 getBossFriendListV2 接口
2. DOM 解析（通用）：JS querySelectorAll 从页面提取
3. 截图视觉分析（兜底）：Claude Vision 从截图识别候选人
   - 视觉成功后会生成选择器修复报告，反哺修复 1/2 层
"""

import json
import logging
import random
import time
from dataclasses import dataclass, field

from recruiter.browser.base import BrowserDriver
from recruiter.browser.human_delay import human_delay
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

    # ------ 截图视觉分析模式（最终兜底） ------

    def _collect_via_vision(self, failed_stage: str) -> list[CandidateInfo] | None:
        """通过截图 + Claude Vision 提取候选人。

        成功后会生成选择器修复报告，反哺上层失败的方案。

        Args:
            failed_stage: 上层失败的阶段名，用于报告
        """
        from recruiter.engine.vision import VisionAnalyzer, save_selector_report

        logger.info("尝试截图视觉分析模式...")

        # 确保在聊天页
        try:
            current = self.browser.current_url()
            if "web/chat" not in current:
                self.browser.navigate(BOSS_URLS["chat"])
                time.sleep(3)
        except Exception:
            pass

        # 截图
        screenshot_path = str(
            __import__("recruiter.config", fromlist=["BASE_DIR"]).BASE_DIR
            / "data" / "vision_fallback.png"
        )
        try:
            self.browser.screenshot(screenshot_path)
        except Exception as e:
            logger.error("截图失败: %s", e)
            return None

        # Claude Vision 分析
        try:
            analyzer = VisionAnalyzer()
        except Exception as e:
            logger.error("VisionAnalyzer 初始化失败（可能缺少 API Key）: %s", e)
            return None

        result = analyzer.analyze_screenshot(screenshot_path)
        if not result or not result.get("candidates"):
            logger.warning("视觉分析未提取到候选人")
            return None

        # 提取候选人（视觉模式没有 platform_id，用名字做临时标识）
        candidates = []
        for i, c in enumerate(result["candidates"]):
            name = c.get("name", "").strip()
            if not name:
                continue
            candidates.append(CandidateInfo(
                platform_id=f"vision_{i}_{name}",
                name=name,
                extra={
                    "source": "vision",
                    "title": c.get("title", ""),
                    "last_message": c.get("last_message", ""),
                },
            ))

        logger.info("视觉分析提取 %d 个候选人", len(candidates))

        # 反哺：保存选择器修复报告
        selectors_hint = result.get("selectors_hint", {})
        if selectors_hint:
            save_selector_report(selectors_hint, failed_stage)
            observations = selectors_hint.get("observations", "")
            if observations:
                logger.warning("视觉分析发现页面变化: %s", observations)

        return candidates

    # ------ 主入口 ------

    def collect_candidates(self, job_url: str = None) -> list[CandidateInfo]:
        """采集候选人列表。

        三层降级策略：API 拦截 → DOM 解析 → 截图视觉分析。
        视觉分析成功后会生成选择器修复报告（data/selector_report.json）。

        Args:
            job_url: 职位页 URL。API 模式下可省略（直接访问聊天页）。
        """
        # 1. 尝试 API 拦截
        candidates = self._collect_via_api()
        if candidates is not None and len(candidates) > 0:
            logger.info("[层级1] API 拦截成功，获取 %d 个候选人", len(candidates))
            self._save_candidates(candidates)
            return candidates

        # 2. 退化到 DOM 解析
        logger.info("[层级1→2] API 拦截不可用，尝试 DOM 解析")
        failed_stage = "api_intercept"
        try:
            # API 拦截阶段可能已 navigate+reload，先尝试当前页直接提取
            if self.browser.wait_for(SELECTORS["candidate_card"], timeout=5):
                direct = self._extract_candidates_from_page()
                if direct:
                    logger.info("[层级2] DOM 直接提取成功，获取 %d 个候选人", len(direct))
                    self._save_candidates(direct)
                    return direct

            # 当前页提取失败，尝试完整的 DOM 采集流程（含 navigate）
            url = job_url or BOSS_URLS["chat"]
            dom_candidates = self._collect_via_dom(url)
            if dom_candidates:
                logger.info("[层级2] DOM 解析成功，获取 %d 个候选人", len(dom_candidates))
                return dom_candidates
            failed_stage = "dom_parse_empty"
        except PageLoadError:
            failed_stage = "dom_parse_page_load"
        except Exception as e:
            logger.warning("DOM 解析异常: %s", e)
            failed_stage = "dom_parse_error"

        # 3. 最终兜底：截图视觉分析
        logger.info("[层级2→3] DOM 解析失败 (%s)，尝试截图视觉分析", failed_stage)
        vision_candidates = self._collect_via_vision(failed_stage)
        if vision_candidates:
            logger.info("[层级3] 视觉分析成功，获取 %d 个候选人", len(vision_candidates))
            self._save_candidates(vision_candidates)
            return vision_candidates

        logger.error("三层采集策略全部失败")
        try:
            from recruiter.logging_config import alert_all_layers_failed
            alert_all_layers_failed()
        except Exception:
            pass
        return []

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
            human_delay("page_turn")
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

    # ------ 简历详情采集 ------

    def collect_resumes(self, limit: int = 50) -> dict:
        """为没有简历的候选人逐个采集简历详情。

        通过点击聊天列表中的候选人，在聊天页提取简历摘要信息。

        Returns:
            {"total": int, "collected": int, "failed": int}
        """
        candidates = self.db.list_candidates(platform="boss", limit=99999)
        no_resume = [c for c in candidates if not c.get("resume_text")][:limit]

        if not no_resume:
            logger.info("所有候选人都已有简历，跳过")
            return {"total": 0, "collected": 0, "failed": 0}

        logger.info("待采集简历: %d 人", len(no_resume))

        # 先导航到聊天页
        self.browser.navigate(BOSS_URLS["chat"])
        if not self.browser.wait_for(".geek-item", timeout=10):
            logger.error("聊天列表未加载")
            return {"total": len(no_resume), "collected": 0, "failed": len(no_resume)}

        collected = 0
        failed = 0

        for c in no_resume:
            pid = c["platform_id"]
            # 点击候选人打开聊天，提取右侧简历摘要
            selector = f".geek-item[data-id*='{pid}']"
            if not self.browser.click(selector):
                logger.warning("候选人 %s (pid=%s) 未在列表中找到", c["name"], pid)
                failed += 1
                continue

            human_delay("click")

            # 提取简历摘要（右侧聊天面板顶部的 .conversation-box）
            resume_text = self.browser.execute_js('''
                var box = document.querySelector('.conversation-box');
                if (!box) return '';
                var parts = [];
                box.querySelectorAll('span, a').forEach(function(el) {
                    var t = el.textContent.trim();
                    if (t && t.length > 1 && t.length < 100 &&
                        !t.includes("在线简历") && !t.includes("附件简历") &&
                        !t.includes("更换职位") && !t.includes("沟通职位")) {
                        parts.push(t);
                    }
                });
                var seen = {};
                var unique = parts.filter(function(x) {
                    if (seen[x]) return false;
                    seen[x] = true;
                    return true;
                });
                return unique.join(' | ');
            ''')

            if resume_text:
                self.db.update_candidate_resume(c["id"], resume_text)
                collected += 1
                logger.info("采集简历: %s -> %s", c["name"], resume_text[:50])
            else:
                failed += 1

            human_delay("batch_item")

        stats = {"total": len(no_resume), "collected": collected, "failed": failed}
        logger.info("简历采集完成: %d/%d 成功", collected, len(no_resume))
        return stats
