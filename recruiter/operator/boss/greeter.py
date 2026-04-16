"""Boss直聘主动招呼模块

从「推荐牛人」页面主动向新候选人发起招呼。
推荐牛人页面内容在 iframe 中，需要通过 frame_locator 操作。
"""

import logging
import time

from recruiter.browser.base import BrowserDriver
from recruiter.browser.human_delay import human_delay
from recruiter.db.models import Database

logger = logging.getLogger(__name__)

RECOMMEND_URL = "https://www.zhipin.com/web/chat/recommend"
IFRAME_SELECTOR = 'iframe[src*="recommend"]'


class BossGreeter:
    """从推荐牛人页面主动发起招呼。"""

    def __init__(self, browser: BrowserDriver, db: Database):
        self.browser = browser
        self.db = db

    def greet_recommended(self, limit: int = 10) -> dict:
        """向推荐牛人发送招呼。

        Args:
            limit: 最多招呼人数

        Returns:
            {"total": int, "greeted": int, "skipped": int, "failed": int}
        """
        if not hasattr(self.browser, '_ensure_connected'):
            logger.error("主动招呼需要 Playwright driver")
            return {"total": 0, "greeted": 0, "skipped": 0, "failed": 0}

        page = self.browser._ensure_connected()
        page.goto(RECOMMEND_URL, wait_until="domcontentloaded")
        time.sleep(4)

        fl = page.frame_locator(IFRAME_SELECTOR)

        # 等待卡片加载
        try:
            fl.locator('.card-item').first.wait_for(state='attached', timeout=10000)
        except Exception:
            logger.error("推荐牛人页面加载失败")
            return {"total": 0, "greeted": 0, "skipped": 0, "failed": 0}

        # 关闭可能的 VIP 弹窗/引导遮罩
        self._dismiss_overlays(fl)

        stats = {"total": 0, "greeted": 0, "skipped": 0, "failed": 0}

        # 提取候选人列表
        candidates = self._extract_candidates(fl)
        stats["total"] = len(candidates)
        logger.info("推荐牛人: %d 人", len(candidates))

        for i, c in enumerate(candidates[:limit]):
            name = c.get("name", "")
            pid = c.get("encrypt_id", "")

            # 检查是否已在 DB 中（去重）
            if pid and self._already_exists(pid):
                logger.info("跳过已有候选人: %s", name)
                stats["skipped"] += 1
                continue

            # 点击打招呼（用 JS click 绕过弹窗遮挡）
            try:
                card = fl.locator('.card-item').nth(i)
                btn = card.locator('.button-chat')
                if btn.count() == 0:
                    stats["skipped"] += 1
                    continue

                btn.first.evaluate("el => el.click()")
                human_delay("click")

                # 保存到 DB
                self.db.upsert_candidate(
                    platform="boss",
                    platform_id=pid or f"recommend_{name}",
                    name=name,
                    resume_text=c.get("summary", ""),
                    source="outbound_greet",
                )

                stats["greeted"] += 1
                logger.info("已招呼: %s", name)
                human_delay("batch_item")

            except Exception as e:
                logger.warning("招呼失败 %s: %s", name, e)
                stats["failed"] += 1

        logger.info("主动招呼完成: 招呼 %d, 跳过 %d, 失败 %d",
                     stats["greeted"], stats["skipped"], stats["failed"])
        return stats

    def _extract_candidates(self, fl) -> list[dict]:
        """从 iframe 中提取推荐候选人列表。"""
        try:
            data = fl.locator('body').evaluate('''(body) => {
                var cards = body.querySelectorAll('.card-item');
                var result = [];
                cards.forEach(function(card) {
                    var nameEl = card.querySelector('.name, .geek-name, h3');
                    var name = nameEl ? nameEl.textContent.trim() : '';
                    var encId = card.getAttribute('data-id') || card.getAttribute('data-encryptgeekid') || '';
                    var text = card.textContent.trim().substring(0, 200);
                    if (name) {
                        result.push({name: name, encrypt_id: encId, summary: text});
                    }
                });
                return result;
            }''')
            return data
        except Exception as e:
            logger.error("提取推荐候选人失败: %s", e)
            return []

    def _dismiss_overlays(self, fl):
        """关闭 VIP 引导弹窗、功能提示等遮罩层。"""
        try:
            for sel in ['.vip-feature-guide .close', '.guide-close', '.boss-popup__close',
                        '[class*="close-btn"]', '[class*="guide"] .close']:
                close = fl.locator(sel)
                if close.count() > 0 and close.first.is_visible():
                    close.first.click()
                    time.sleep(0.5)
                    logger.info("关闭弹窗: %s", sel)
        except Exception:
            pass
        # ESC 兜底
        try:
            page = self.browser._ensure_connected()
            page.keyboard.press("Escape")
            time.sleep(0.3)
        except Exception:
            pass

    def _already_exists(self, platform_id: str) -> bool:
        """检查候选人是否已在 DB 中。"""
        row = self.db.conn.execute(
            "SELECT id FROM candidates WHERE platform_id = ?",
            (platform_id,),
        ).fetchone()
        return row is not None
