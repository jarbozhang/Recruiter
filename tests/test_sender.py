"""BossSender 单元测试

使用 mock BrowserDriver 对象，不依赖真实浏览器。
"""

import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from recruiter.browser.base import BrowserDriver
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
def mock_browser():
    browser = MagicMock(spec=BrowserDriver)
    browser.wait_for.return_value = True
    browser.is_visible.return_value = True
    browser.click.return_value = True
    browser.fill.return_value = True
    return browser


@pytest.fixture
def sender(mock_browser, db):
    return BossSender(mock_browser, db)


def _setup_approved_conv(db):
    job_id = db.create_job("Java开发", "JD")
    cid = db.upsert_candidate("boss", "u_001", "张三", "简历", "inbound")
    conv_id = db.create_conversation(cid, job_id, "您好，我们有一个Java开发的机会")
    db.update_conversation_status(conv_id, "approved")
    return job_id, cid, conv_id


class TestCircuitBreaker:
    def test_opens_after_threshold_failures(self):
        cb = CircuitBreaker(threshold=3, pause_seconds=7200)
        cb.record_failure()
        cb.record_failure()
        assert not cb.is_open
        cb.record_failure()
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
        assert not cb.is_open
        assert cb.consecutive_failures == 0


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
            assert 30 <= rl.get_random_interval() <= 120

    def test_can_proceed_when_under_limit(self):
        rl = RateLimiter(hourly_limit=30, daily_limit=150)
        rl.record_operation()
        can, reason = rl.can_proceed()
        assert can


class TestSendMessage:
    def test_send_success(self, sender, db, mock_browser):
        _, _, conv_id = _setup_approved_conv(db)
        result = sender.send_message(conv_id)
        assert result == "sent"
        assert db.get_conversation(conv_id)["status"] == "sent"

    def test_send_page_error_marks_failed(self, sender, db, mock_browser):
        _, _, conv_id = _setup_approved_conv(db)
        mock_browser.navigate.side_effect = Exception("page crashed")
        result = sender.send_message(conv_id)
        assert result == "failed"
        assert db.get_conversation(conv_id)["status"] == "failed"

    def test_send_timeout(self, sender, db, mock_browser):
        _, _, conv_id = _setup_approved_conv(db)
        # wait_for 对 .message-item:last-child 返回 False（确认超时）
        call_count = [0]
        def mock_wait_for(selector, timeout=10):
            call_count[0] += 1
            if "message-item" in selector:
                return False  # 确认超时
            return True
        mock_browser.wait_for.side_effect = mock_wait_for

        result = sender.send_message(conv_id)
        assert result == "timeout"
        assert db.get_conversation(conv_id)["status"] == "timeout"

    def test_not_approved_returns_failed(self, sender, db):
        job_id = db.create_job("test", "JD")
        cid = db.upsert_candidate("boss", "u_099", "test", "resume", "inbound")
        conv_id = db.create_conversation(cid, job_id, "msg")
        result = sender.send_message(conv_id)
        assert result == "failed"


class TestCircuitBreakerIntegration:
    def test_three_failures_triggers_breaker(self, sender, db, mock_browser):
        mock_browser.navigate.side_effect = Exception("page error")
        for i in range(3):
            job_id = db.create_job(f"job{i}", "JD")
            cid = db.upsert_candidate("boss", f"u_f{i}", f"n{i}", "r", "inbound")
            conv_id = db.create_conversation(cid, job_id, f"msg{i}")
            db.update_conversation_status(conv_id, "approved")
            sender.send_message(conv_id)
        assert sender.circuit_breaker.is_open


class TestRateLimitIntegration:
    def test_hourly_limit_stops_sending(self, sender):
        sender.rate_limiter = RateLimiter(hourly_limit=2, daily_limit=100)
        sender.rate_limiter.record_operation()
        sender.rate_limiter.record_operation()
        can, reason = sender.rate_limiter.can_proceed()
        assert not can
        assert reason == "hourly_limit_reached"

    def test_daily_limit_stops_sending(self, sender):
        sender.rate_limiter = RateLimiter(hourly_limit=100, daily_limit=2)
        sender.rate_limiter.record_operation()
        sender.rate_limiter.record_operation()
        can, reason = sender.rate_limiter.can_proceed()
        assert not can
        assert reason == "daily_limit_reached"


class TestHealthCheck:
    def test_health_check_pass(self, sender, mock_browser):
        assert sender.health_check() is True

    def test_health_check_fail_missing_selector(self, sender, mock_browser):
        mock_browser.is_visible.return_value = False
        assert sender.health_check() is False

    def test_health_check_page_error(self, sender, mock_browser):
        mock_browser.navigate.side_effect = Exception("network error")
        assert sender.health_check() is False
