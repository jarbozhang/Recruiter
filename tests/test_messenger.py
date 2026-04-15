import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from recruiter.db.models import Database
from recruiter.engine.messenger import MessageGenerator, FALLBACK_TEMPLATE


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database = Database(path)
    yield database
    database.close()
    os.unlink(path)


@pytest.fixture
def generator(db):
    with patch("recruiter.engine.messenger.anthropic") as mock_anthropic:
        gen = MessageGenerator(db)
        gen.client = MagicMock()
        yield gen


def _mock_response(text: str):
    """构造 Anthropic API 的模拟响应。"""
    content_block = MagicMock()
    content_block.text = text
    resp = MagicMock()
    resp.content = [content_block]
    return resp


class TestHappyPath:
    def test_generate_message_for_candidate(self, generator, db):
        job_id = db.create_job("Java高级开发", "3年以上Java经验，熟悉Spring Boot和微服务")
        cid = db.upsert_candidate("boss", "u_001", "张三", "Java 5年经验，精通Spring Boot", "inbound")

        msg_text = "看到您在Java和Spring Boot方面有丰富的经验，我们这边有一个高级开发岗位，技术栈高度吻合。团队正在做微服务架构升级，很需要您这样的实战型工程师，有兴趣聊聊吗？"
        generator.client.messages.create.return_value = _mock_response(msg_text)

        result = generator.generate_for_candidate(job_id, cid, "技术栈匹配度高")
        assert result["status"] == "pending"
        assert result["conversation_id"] is not None
        assert result["message"] == msg_text

        # 验证 DB 写入
        conv = db.get_conversation(result["conversation_id"])
        assert conv["status"] == "pending"
        assert conv["direction"] == "sent"
        assert conv["message"] == msg_text

    def test_generate_creates_pending_conversation(self, generator, db):
        job_id = db.create_job("Python开发", "Python 后端开发")
        cid = db.upsert_candidate("boss", "u_002", "李四", "Python 3年", "inbound")

        generator.client.messages.create.return_value = _mock_response("您好，看到您的Python经验很丰富" + "x" * 100)

        result = generator.generate_for_candidate(job_id, cid)
        convs = db.list_conversations(status="pending")
        assert len(convs) == 1
        assert convs[0]["id"] == result["conversation_id"]


class TestEdgeCases:
    def test_sparse_candidate_info(self, generator, db):
        """候选人信息很少仍能生成话术。"""
        job_id = db.create_job("前端开发", "React开发")
        cid = db.upsert_candidate("boss", "u_003", "王五", "", "inbound")

        generator.client.messages.create.return_value = _mock_response(
            "注意到您在前端方面有相关背景，我们有一个React开发的机会，感兴趣的话可以聊聊。" + "补充" * 20
        )

        result = generator.generate_for_candidate(job_id, cid)
        assert result["status"] == "pending"
        assert result["conversation_id"] is not None

    def test_message_truncated_to_max_length(self, generator, db):
        """超过 300 字的消息被截断。"""
        job_id = db.create_job("开发", "JD")
        cid = db.upsert_candidate("boss", "u_004", "赵六", "简历", "inbound")

        long_text = "你好" * 200  # 400 字
        generator.client.messages.create.return_value = _mock_response(long_text)

        result = generator.generate_for_candidate(job_id, cid)
        assert len(result["message"]) == 300

    def test_job_not_found(self, generator, db):
        """岗位不存在。"""
        cid = db.upsert_candidate("boss", "u_005", "test", "resume", "inbound")
        result = generator.generate_for_candidate(999, cid)
        assert result["status"] == "error"
        assert result["conversation_id"] is None

    def test_candidate_not_found(self, generator, db):
        """候选人不存在。"""
        job_id = db.create_job("开发", "JD")
        result = generator.generate_for_candidate(job_id, 999)
        assert result["status"] == "error"
        assert result["conversation_id"] is None


class TestErrorPaths:
    def test_api_unavailable_uses_fallback(self, generator, db):
        """Claude API 不可用时使用降级模板。"""
        job_id = db.create_job("Java高级开发", "JD")
        cid = db.upsert_candidate("boss", "u_006", "钱七", "简历", "inbound")

        generator.client.messages.create.side_effect = Exception("API timeout")

        result = generator.generate_for_candidate(job_id, cid)
        assert result["status"] == "pending"
        assert result["conversation_id"] is not None
        assert "Java高级开发" in result["message"]

        # 降级消息也写入 DB
        conv = db.get_conversation(result["conversation_id"])
        assert conv["status"] == "pending"


class TestBatch:
    def test_batch_generate(self, generator, db):
        job_id = db.create_job("Java开发", "JD")
        cid1 = db.upsert_candidate("boss", "u_010", "A", "Java 3年", "inbound")
        cid2 = db.upsert_candidate("boss", "u_011", "B", "Java 5年", "inbound")
        cid3 = db.upsert_candidate("boss", "u_012", "C", "Go 2年", "inbound")

        generator.client.messages.create.return_value = _mock_response(
            "看到您的技术背景和我们的岗位非常匹配，特别是在相关技术栈方面有丰富的实战经验，方便的话可以聊聊这个机会。" + "补充" * 5
        )

        reasons = {cid1: "Java匹配", cid2: "经验丰富", cid3: "有潜力"}
        results = generator.generate_batch(job_id, [cid1, cid2, cid3], reasons)

        assert len(results) == 3
        assert all(r["status"] == "pending" for r in results)
        assert all(r["conversation_id"] is not None for r in results)

        # DB 中有 3 条 pending 对话
        convs = db.list_conversations(status="pending")
        assert len(convs) == 3

    def test_batch_partial_api_failure(self, generator, db):
        """批量中部分 API 失败使用降级模板。"""
        job_id = db.create_job("Go开发", "JD")
        cid1 = db.upsert_candidate("boss", "u_020", "X", "Go 3年", "inbound")
        cid2 = db.upsert_candidate("boss", "u_021", "Y", "Go 5年", "inbound")

        normal_resp = _mock_response("看到您的Go开发经验丰富" + "x" * 80)
        generator.client.messages.create.side_effect = [
            normal_resp,
            Exception("rate limited"),
        ]

        results = generator.generate_batch(job_id, [cid1, cid2])
        assert len(results) == 2
        # 第一个正常
        assert "Go" in results[0]["message"]
        # 第二个降级
        assert "Go开发" in results[1]["message"]
        assert all(r["conversation_id"] is not None for r in results)
