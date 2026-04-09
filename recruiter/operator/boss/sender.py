"""Boss直聘自动发送消息模块

通过 AdsPower 指纹浏览器 + Selenium 在 Boss直聘网页端自动发送已审核的招呼消息。
"""

import logging
import random
import time
from datetime import datetime, timedelta

from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from recruiter import config
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
            # 自动恢复
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
    """通过 Selenium WebDriver 在 Boss直聘发送已审核的消息。"""

    # 健康检查用的关键 selectors
    HEALTH_SELECTORS = [
        ".chat-conversation",  # 聊天页面主容器
        ".chat-input",         # 输入框
    ]

    def __init__(self, driver: WebDriver, db: Database):
        """
        Args:
            driver: Selenium WebDriver 实例（通过 AdsPower 获取）
            db: 数据库实例
        """
        self.driver = driver
        self.db = db
        self.circuit_breaker = CircuitBreaker()
        self.rate_limiter = RateLimiter()

    def health_check(self, url: str = "https://www.zhipin.com/web/boss/chat") -> bool:
        """检查 Boss直聘聊天页面关键元素是否存在。"""
        try:
            self.driver.get(url)
            WebDriverWait(self.driver, 30).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "body"))
            )
            for selector in self.HEALTH_SELECTORS:
                elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                if not elements:
                    logger.error("Health check failed: selector '%s' not found", selector)
                    return False
            logger.info("Health check passed")
            return True
        except (TimeoutException, WebDriverException) as e:
            logger.error("Health check error: %s", e)
            return False

    def send_message(self, conv_id: int) -> str:
        """发送单条消息，返回最终状态。

        Returns: "sent" | "failed" | "timeout"
        """
        conv = self.db.get_conversation(conv_id)
        if not conv or conv["status"] != "approved":
            logger.error("Conversation %s not in approved status", conv_id)
            return "failed"

        # 设为 sending
        self.db.update_conversation_status(conv_id, "sending")

        candidate = self.db.get_candidate(conv["candidate_id"])
        if not candidate:
            logger.error("Candidate %s not found", conv["candidate_id"])
            self.db.update_conversation_status(conv_id, "failed")
            return "failed"

        try:
            # 导航到候选人聊天页
            chat_url = f"https://www.zhipin.com/web/boss/chat?id={candidate['platform_id']}"
            self.driver.get(chat_url)

            # 等待输入框
            wait = WebDriverWait(self.driver, 10)
            input_el = wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".chat-input"))
            )

            # 输入消息
            input_el.clear()
            input_el.send_keys(conv["message"])

            # 发送
            try:
                send_btn = self.driver.find_element(By.CSS_SELECTOR, ".btn-send")
                send_btn.click()
            except NoSuchElementException:
                input_el.send_keys(Keys.ENTER)

            # 确认发送成功 - 检查消息是否出现在聊天记录
            try:
                WebDriverWait(self.driver, config.SEND_TIMEOUT).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".message-item:last-child"))
                )
                self.db.update_conversation_status(conv_id, "sent")
                self.circuit_breaker.record_success()
                return "sent"
            except TimeoutException:
                self.db.update_conversation_status(conv_id, "timeout")
                self.circuit_breaker.record_failure()
                return "timeout"

        except Exception as e:
            logger.error("Send failed for conv %s: %s", conv_id, e)
            self.db.update_conversation_status(conv_id, "failed")
            self.circuit_breaker.record_failure()
            return "failed"

    def process_queue(self) -> dict:
        """处理审核队列中所有 approved 的消息。

        Returns:
            {"sent": int, "failed": int, "timeout": int, "skipped": int, "reason": str}
        """
        stats = {"sent": 0, "failed": 0, "timeout": 0, "skipped": 0, "reason": ""}

        # 健康检查
        if not self.health_check():
            stats["reason"] = "health_check_failed"
            return stats

        # 获取 approved 消息
        conversations = self.db.list_conversations(status="approved")
        if not conversations:
            stats["reason"] = "no_approved_messages"
            return stats

        for conv in conversations:
            # Circuit breaker 检查
            if self.circuit_breaker.is_open:
                stats["skipped"] += 1
                stats["reason"] = "circuit_breaker_open"
                continue

            # 频率检查
            can_go, limit_reason = self.rate_limiter.can_proceed()
            if not can_go:
                stats["skipped"] += 1
                stats["reason"] = limit_reason
                continue

            result = self.send_message(conv["id"])
            stats[result] = stats.get(result, 0) + 1
            self.rate_limiter.record_operation()

            # 随机等待
            interval = self.rate_limiter.get_random_interval()
            time.sleep(interval)

        return stats
