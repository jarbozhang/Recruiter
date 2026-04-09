import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from recruiter.db.models import Database
from recruiter.operator.boss.sender import BossSender, CircuitBreaker, RateLimiter


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
    return MagicMock()


@pytest.fixture(autouse=True)
def mock_webdriver_wait():
    """Patch WebDriverWait to avoid real timeouts in tests."""
    with patch("recruiter.operator.boss.sender.WebDriverWait") as mock_cls:
        mock_wait = MagicMock()
        mock_wait.until.return_value = MagicMock()
        mock_cls.return_value = mock_wait
        yield mock_cls


@pytest.fixture
def sender(mock_driver, db):
    return BossSender(mock_driver, db)


def _setup_approved_conv(db):
    """创建一条 approved 状态的 conversation 并返回相关 ID。"""
    job_id = db.create_job("Java开发", "JD")
    cid = db.upsert_candidate("boss", "u_001", "张三", "简历", "inbound")
    conv_id = db.create_conversation(cid, job_id, "您好，我们有一个Java开发的机会")
    db.update_conversation_status(conv_id, "approved")
    return job_id, cid, conv_id


# -- CircuitBreaker --

class TestCircuitBreaker:
    def test_opens_after_threshold_failures(self):
        cb = CircuitBreaker(threshold=3, pause_seconds=7200)
        cb.record_failure()
        cb.record_failure()
        assert not cb.is_open
        cb.record_failure()  # 第 3 次
        assert cb.is_open

    def test_success_resets_counter(self):
        cb = CircuitBreaker(threshold=3, pause_seconds=7200)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        cb.record_failure()
        assert not cb.is_open

    def test_auto_recovery_after_pause(self):
        cb = CircuitBreaker(threshold=1, pause_seconds=1)
        cb.record_failure()
        assert cb.is_open
        cb.paused_until = datetime.now() - timedelta(seconds=1)
        assert not cb.is_open  # 自动恢复
        assert cb.consecutive_failures == 0


# -- RateLimiter --

class TestRateLimiter:
    def test_hourly_limit(self):
        rl = RateLimiter(hourly_limit=3, daily_limit=100)
        for _ in range(3):
            rl.record_operation()
        can, reason = rl.can_proceed()
        assert not can
        assert reason == "hourly_limit_reached"

    def test_daily_limit(self):
        rl = RateLimiter(hourly_limit=100, daily_limit=3)
        for _ in range(3):
            rl.record_operation()
        can, reason = rl.can_proceed()
        assert not can
        assert reason == "daily_limit_reached"

    def test_random_interval_in_range(self):
        rl = RateLimiter(interval_min=30, interval_max=120)
        for _ in range(20):
            interval = rl.get_random_interval()
            assert 30 <= interval <= 120

    def test_can_proceed_when_under_limit(self):
        rl = RateLimiter(hourly_limit=30, daily_limit=150)
        rl.record_operation()
        can, reason = rl.can_proceed()
        assert can
        assert reason == ""


# -- BossSender --

class TestSendMessage:
    def test_send_success(self, sender, db, mock_driver, mock_webdriver_wait):
        """approved → sending → sent 全链路。"""
        _, _, conv_id = _setup_approved_conv(db)
        mock_driver.find_element.return_value = MagicMock()  # send btn

        result = sender.send_message(conv_id)
        assert result == "sent"
        assert db.get_conversation(conv_id)["status"] == "sent"

    def test_send_page_error_marks_failed(self, sender, db, mock_driver):
        """页面异常 → status=failed。"""
        _, _, conv_id = _setup_approved_conv(db)
        mock_driver.get.side_effect = Exception("page crashed")

        result = sender.send_message(conv_id)
        assert result == "failed"
        assert db.get_conversation(conv_id)["status"] == "failed"

    def test_send_timeout(self, sender, db, mock_driver, mock_webdriver_wait):
        """发送后无法确认 → timeout。"""
        _, _, conv_id = _setup_approved_conv(db)

        from selenium.common.exceptions import TimeoutException
        call_count = [0]

        def mock_until(condition):
            call_count[0] += 1
            if call_count[0] == 1:
                return MagicMock()  # chat input found
            raise TimeoutException("confirmation timeout")

        mock_wait = MagicMock()
        mock_wait.until.side_effect = mock_until
        mock_webdriver_wait.return_value = mock_wait
        mock_driver.find_element.return_value = MagicMock()

        result = sender.send_message(conv_id)
        assert result == "timeout"
        assert db.get_conversation(conv_id)["status"] == "timeout"

    def test_not_approved_returns_failed(self, sender, db):
        """非 approved 状态的消息不发送。"""
        job_id = db.create_job("test", "JD")
        cid = db.upsert_candidate("boss", "u_099", "test", "resume", "inbound")
        conv_id = db.create_conversation(cid, job_id, "msg")  # status=pending

        result = sender.send_message(conv_id)
        assert result == "failed"


class TestCircuitBreakerIntegration:
    def test_three_failures_triggers_breaker(self, sender, db, mock_driver):
        """连续 3 次失败触发 circuit breaker。"""
        mock_driver.get.side_effect = Exception("page error")

        for i in range(3):
            job_id = db.create_job(f"job{i}", "JD")
            cid = db.upsert_candidate("boss", f"u_f{i}", f"name{i}", "resume", "inbound")
            conv_id = db.create_conversation(cid, job_id, f"msg{i}")
            db.update_conversation_status(conv_id, "approved")
            sender.send_message(conv_id)

        assert sender.circuit_breaker.is_open


class TestRateLimitIntegration:
    def test_hourly_limit_stops_sending(self, sender, db, mock_driver):
        """达到每小时上限后停止发送。"""
        sender.rate_limiter = RateLimiter(hourly_limit=2, daily_limit=100, interval_min=0, interval_max=0)
        sender.rate_limiter.record_operation()
        sender.rate_limiter.record_operation()

        can, reason = sender.rate_limiter.can_proceed()
        assert not can
        assert reason == "hourly_limit_reached"

    def test_daily_limit_stops_sending(self, sender, db, mock_driver):
        """达到每日上限后停止发送。"""
        sender.rate_limiter = RateLimiter(hourly_limit=100, daily_limit=2, interval_min=0, interval_max=0)
        sender.rate_limiter.record_operation()
        sender.rate_limiter.record_operation()

        can, reason = sender.rate_limiter.can_proceed()
        assert not can
        assert reason == "daily_limit_reached"


class TestHealthCheck:
    def test_health_check_pass(self, sender, mock_driver):
        mock_driver.find_elements.return_value = [MagicMock()]
        result = sender.health_check()
        assert result is True

    def test_health_check_fail_missing_selector(self, sender, mock_driver):
        mock_driver.find_elements.return_value = []
        result = sender.health_check()
        assert result is False

    def test_health_check_page_error(self, sender, mock_driver):
        from selenium.common.exceptions import WebDriverException
        mock_driver.get.side_effect = WebDriverException("network error")
        result = sender.health_check()
        assert result is False
