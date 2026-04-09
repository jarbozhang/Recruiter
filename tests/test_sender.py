import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

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
def mock_page():
    page = AsyncMock()
    return page


@pytest.fixture
def sender(mock_page, db):
    return BossSender(mock_page, db)


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
    @pytest.mark.asyncio
    async def test_send_success(self, sender, db, mock_page):
        """approved → sending → sent 全链路。"""
        _, _, conv_id = _setup_approved_conv(db)

        # mock 页面操作成功
        input_el = AsyncMock()
        send_btn = AsyncMock()
        mock_page.wait_for_selector.side_effect = [input_el, send_btn, MagicMock()]  # input, send btn, confirm
        mock_page.query_selector.return_value = MagicMock()

        result = await sender.send_message(conv_id)
        assert result == "sent"
        assert db.get_conversation(conv_id)["status"] == "sent"

    @pytest.mark.asyncio
    async def test_send_page_error_marks_failed(self, sender, db, mock_page):
        """页面异常 → status=failed。"""
        _, _, conv_id = _setup_approved_conv(db)
        mock_page.goto.side_effect = Exception("page crashed")

        result = await sender.send_message(conv_id)
        assert result == "failed"
        assert db.get_conversation(conv_id)["status"] == "failed"

    @pytest.mark.asyncio
    async def test_send_timeout(self, sender, db, mock_page):
        """发送后无法确认 → timeout。"""
        _, _, conv_id = _setup_approved_conv(db)

        input_el = AsyncMock()
        send_btn = AsyncMock()
        mock_page.wait_for_selector.side_effect = [
            input_el,        # chat input
            send_btn,        # send button
            Exception("timeout waiting for confirmation"),  # confirm element
        ]

        result = await sender.send_message(conv_id)
        assert result == "timeout"
        assert db.get_conversation(conv_id)["status"] == "timeout"

    @pytest.mark.asyncio
    async def test_not_approved_returns_failed(self, sender, db):
        """非 approved 状态的消息不发送。"""
        job_id = db.create_job("test", "JD")
        cid = db.upsert_candidate("boss", "u_099", "test", "resume", "inbound")
        conv_id = db.create_conversation(cid, job_id, "msg")  # status=pending

        result = await sender.send_message(conv_id)
        assert result == "failed"


class TestCircuitBreakerIntegration:
    @pytest.mark.asyncio
    async def test_three_failures_triggers_breaker(self, sender, db, mock_page):
        """连续 3 次失败触发 circuit breaker。"""
        mock_page.goto.side_effect = Exception("page error")

        for i in range(3):
            job_id = db.create_job(f"job{i}", "JD")
            cid = db.upsert_candidate("boss", f"u_f{i}", f"name{i}", "resume", "inbound")
            conv_id = db.create_conversation(cid, job_id, f"msg{i}")
            db.update_conversation_status(conv_id, "approved")
            await sender.send_message(conv_id)

        assert sender.circuit_breaker.is_open


class TestRateLimitIntegration:
    @pytest.mark.asyncio
    async def test_hourly_limit_stops_sending(self, sender, db, mock_page):
        """达到每小时上限后停止发送。"""
        sender.rate_limiter = RateLimiter(hourly_limit=2, daily_limit=100, interval_min=0, interval_max=0)

        # 手动记录 2 次操作
        sender.rate_limiter.record_operation()
        sender.rate_limiter.record_operation()

        can, reason = sender.rate_limiter.can_proceed()
        assert not can
        assert reason == "hourly_limit_reached"

    @pytest.mark.asyncio
    async def test_daily_limit_stops_sending(self, sender, db, mock_page):
        """达到每日上限后停止发送。"""
        sender.rate_limiter = RateLimiter(hourly_limit=100, daily_limit=2, interval_min=0, interval_max=0)

        sender.rate_limiter.record_operation()
        sender.rate_limiter.record_operation()

        can, reason = sender.rate_limiter.can_proceed()
        assert not can
        assert reason == "daily_limit_reached"


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_pass(self, sender, mock_page):
        mock_page.query_selector.return_value = MagicMock()  # 元素存在
        result = await sender.health_check()
        assert result is True

    @pytest.mark.asyncio
    async def test_health_check_fail_missing_selector(self, sender, mock_page):
        mock_page.query_selector.return_value = None  # 元素不存在
        result = await sender.health_check()
        assert result is False

    @pytest.mark.asyncio
    async def test_health_check_page_error(self, sender, mock_page):
        mock_page.goto.side_effect = Exception("network error")
        result = await sender.health_check()
        assert result is False
