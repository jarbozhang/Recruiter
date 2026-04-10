"""Boss直聘自动发送消息模块

通过 BrowserDriver 接口在 Boss直聘网页端自动发送已审核的招呼消息。
支持 AdsPower、bb-browser 等任意浏览器驱动。
"""

import logging
import random
import time
from datetime import datetime, timedelta

from recruiter import config
from recruiter.browser.base import BrowserDriver
from recruiter.db.models import Database

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """连续失败 N 次后暂停指定时间。"""

    def __init__(self, threshold: int = config.CB_FAILURE_THRESHOLD,
                 pause_seconds: int = config.CB_PAUSE_SECONDS):
        self.threshold = threshold
        self.pause_seconds = pause_seconds
        self.consecutive_failures = 0
        self.paused_until: datetime | None = None

    def record_success(self):
        self.consecutive_failures = 0

    def record_failure(self):
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.threshold:
            self.paused_until = datetime.now() + timedelta(seconds=self.pause_seconds)
            logger.warning("Circuit breaker triggered: paused until %s", self.paused_until)

    @property
    def is_open(self) -> bool:
        if self.paused_until is None:
            return False
        if datetime.now() >= self.paused_until:
            self.paused_until = None
            self.consecutive_failures = 0
            logger.info("Circuit breaker recovered")
            return False
        return True


class RateLimiter:
    """频率控制：操作间随机间隔、每小时上限、每日上限。"""

    def __init__(self, interval_min: int = config.OP_INTERVAL_MIN,
                 interval_max: int = config.OP_INTERVAL_MAX,
                 hourly_limit: int = config.OP_HOURLY_LIMIT,
                 daily_limit: int = config.OP_DAILY_LIMIT):
        self.interval_min = interval_min
        self.interval_max = interval_max
        self.hourly_limit = hourly_limit
        self.daily_limit = daily_limit
        self._hourly_ops: list[datetime] = []
        self._daily_ops: list[datetime] = []

    def _cleanup(self):
        now = datetime.now()
        self._hourly_ops = [t for t in self._hourly_ops if now - t < timedelta(hours=1)]
        self._daily_ops = [t for t in self._daily_ops if now - t < timedelta(hours=24)]

    @property
    def hourly_count(self) -> int:
        self._cleanup()
        return len(self._hourly_ops)

    @property
    def daily_count(self) -> int:
        self._cleanup()
        return len(self._daily_ops)

    def can_proceed(self) -> tuple[bool, str]:
        self._cleanup()
        if len(self._daily_ops) >= self.daily_limit:
            return False, "daily_limit_reached"
        if len(self._hourly_ops) >= self.hourly_limit:
            return False, "hourly_limit_reached"
        return True, ""

    def record_operation(self):
        now = datetime.now()
        self._hourly_ops.append(now)
        self._daily_ops.append(now)

    def get_random_interval(self) -> float:
        return random.uniform(self.interval_min, self.interval_max)


class BossSender:
    """通过 BrowserDriver 在 Boss直聘发送已审核的消息。"""

    HEALTH_SELECTORS = [".geek-item", ".boss-chat-editor-input"]

    def __init__(self, browser: BrowserDriver, db: Database):
        self.browser = browser
        self.db = db
        self.circuit_breaker = CircuitBreaker()
        self.rate_limiter = RateLimiter()

    def health_check(self, url: str = "https://www.zhipin.com/web/chat/index") -> bool:
        try:
            self.browser.navigate(url)
            if not self.browser.wait_for("body", timeout=30):
                return False
            for selector in self.HEALTH_SELECTORS:
                if not self.browser.is_visible(selector):
                    logger.error("Health check failed: '%s' not found", selector)
                    return False
            logger.info("Health check passed")
            return True
        except Exception as e:
            logger.error("Health check error: %s", e)
            return False

    def send_message(self, conv_id: int) -> str:
        conv = self.db.get_conversation(conv_id)
        if not conv or conv["status"] != "approved":
            logger.error("Conversation %s not in approved status", conv_id)
            return "failed"

        self.db.update_conversation_status(conv_id, "sending")

        candidate = self.db.get_candidate(conv["candidate_id"])
        if not candidate:
            logger.error("Candidate %s not found", conv["candidate_id"])
            self.db.update_conversation_status(conv_id, "failed")
            return "failed"

        try:
            # 导航到聊天页
            self.browser.navigate("https://www.zhipin.com/web/chat/index")
            if not self.browser.wait_for(".geek-item", timeout=10):
                raise Exception("Chat list not loaded")

            # 点击候选人
            candidate_selector = f".geek-item[data-id*='{candidate['platform_id']}']"
            if not self.browser.click(candidate_selector):
                raise Exception(f"Candidate {candidate['platform_id']} not found in chat list")

            time.sleep(2)

            # 填入消息
            if not self.browser.wait_for(".boss-chat-editor-input", timeout=10):
                raise Exception("Chat input not found")

            self.browser.fill(".boss-chat-editor-input", conv["message"])
            time.sleep(0.5)

            # 发送（模拟 Enter）
            self.browser.execute_js('''
                var el = document.querySelector(".boss-chat-editor-input");
                if (el) {
                    var e = new KeyboardEvent("keydown", {key: "Enter", keyCode: 13, bubbles: true});
                    el.dispatchEvent(e);
                }
            ''')

            # 确认发送
            if self.browser.wait_for(".message-item:last-child", timeout=config.SEND_TIMEOUT):
                self.db.update_conversation_status(conv_id, "sent")
                self.circuit_breaker.record_success()
                return "sent"
            else:
                self.db.update_conversation_status(conv_id, "timeout")
                self.circuit_breaker.record_failure()
                return "timeout"

        except Exception as e:
            logger.error("Send failed for conv %s: %s", conv_id, e)
            self.db.update_conversation_status(conv_id, "failed")
            self.circuit_breaker.record_failure()
            return "failed"

    def process_queue(self) -> dict:
        stats = {"sent": 0, "failed": 0, "timeout": 0, "skipped": 0, "reason": ""}

        if not self.health_check():
            stats["reason"] = "health_check_failed"
            return stats

        conversations = self.db.list_conversations(status="approved")
        if not conversations:
            stats["reason"] = "no_approved_messages"
            return stats

        for conv in conversations:
            if self.circuit_breaker.is_open:
                stats["skipped"] += 1
                stats["reason"] = "circuit_breaker_open"
                continue

            can_go, limit_reason = self.rate_limiter.can_proceed()
            if not can_go:
                stats["skipped"] += 1
                stats["reason"] = limit_reason
                continue

            result = self.send_message(conv["id"])
            stats[result] = stats.get(result, 0) + 1
            self.rate_limiter.record_operation()

            interval = self.rate_limiter.get_random_interval()
            time.sleep(interval)

        return stats
