import os
import tempfile
import pytest
from recruiter.db.models import Database, VALID_STATUS_TRANSITIONS


@pytest.fixture
def db():
    """每个测试用临时数据库"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database = Database(path)
    yield database
    database.close()
    os.unlink(path)


class TestJobs:
    def test_create_and_get_job(self, db):
        job_id = db.create_job("Java开发", "3年经验，熟悉Spring", platform="boss")
        job = db.get_job(job_id)
        assert job is not None
        assert job["title"] == "Java开发"
        assert job["jd"] == "3年经验，熟悉Spring"
        assert job["platform"] == "boss"
        assert job["match_threshold"] == 60
        assert job["status"] == "active"

    def test_list_jobs_by_status(self, db):
        db.create_job("Java开发", "JD1")
        db.create_job("Python开发", "JD2")
        job_id3 = db.create_job("Go开发", "JD3")
        db.update_job_status(job_id3, "closed")

        active = db.list_jobs(status="active")
        assert len(active) == 2
        closed = db.list_jobs(status="closed")
        assert len(closed) == 1
        assert closed[0]["title"] == "Go开发"


class TestCandidates:
    def test_create_and_get_candidate(self, db):
        cid = db.upsert_candidate("boss", "u_12345", "张三", "Java 3年经验", "inbound")
        candidate = db.get_candidate(cid)
        assert candidate is not None
        assert candidate["name"] == "张三"
        assert candidate["platform"] == "boss"
        assert candidate["platform_id"] == "u_12345"
        assert candidate["source"] == "inbound"

    def test_duplicate_candidate_ignored(self, db):
        """同 platform + platform_id 的候选人不重复插入"""
        cid1 = db.upsert_candidate("boss", "u_12345", "张三", "简历1", "inbound")
        cid2 = db.upsert_candidate("boss", "u_12345", "张三更新", "简历2", "outbound")
        assert cid1 == cid2  # 返回相同 ID

        candidates = db.list_candidates()
        assert len(candidates) == 1
        # 原始数据不被覆盖（INSERT OR IGNORE 行为）
        assert candidates[0]["name"] == "张三"

    def test_different_platform_not_duplicate(self, db):
        """不同平台的相同 platform_id 不算重复"""
        cid1 = db.upsert_candidate("boss", "u_12345", "张三", "简历", "inbound")
        cid2 = db.upsert_candidate("liepin", "u_12345", "张三", "简历", "inbound")
        assert cid1 != cid2

        candidates = db.list_candidates()
        assert len(candidates) == 2


class TestMatchResults:
    def test_create_and_query_match_result(self, db):
        job_id = db.create_job("Java开发", "JD")
        cid = db.upsert_candidate("boss", "u_001", "李四", "Java 5年", "inbound")

        dims = {"tech_stack": 85, "years": 70, "industry": 60, "education": 50, "location": 90}
        mr_id = db.create_match_result(job_id, cid, 75, "技术栈匹配度高", dims, "abc12345")

        results = db.get_match_results(job_id=job_id)
        assert len(results) == 1
        assert results[0]["score"] == 75
        assert results[0]["prompt_version"] == "abc12345"
        assert results[0]["dimensions"]["tech_stack"] == 85

    def test_filter_by_min_score(self, db):
        job_id = db.create_job("Java开发", "JD")
        cid1 = db.upsert_candidate("boss", "u_001", "A", "简历", "inbound")
        cid2 = db.upsert_candidate("boss", "u_002", "B", "简历", "inbound")
        cid3 = db.upsert_candidate("boss", "u_003", "C", "简历", "inbound")

        db.create_match_result(job_id, cid1, 85, "好")
        db.create_match_result(job_id, cid2, 55, "一般")
        db.create_match_result(job_id, cid3, 70, "还行")

        above_60 = db.get_match_results(job_id=job_id, min_score=60)
        assert len(above_60) == 2
        assert all(r["score"] >= 60 for r in above_60)


class TestConversations:
    def test_create_conversation(self, db):
        job_id = db.create_job("Java开发", "JD")
        cid = db.upsert_candidate("boss", "u_001", "王五", "简历", "inbound")

        conv_id = db.create_conversation(cid, job_id, "您好，我们有一个Java高级开发的职位...")
        conv = db.get_conversation(conv_id)
        assert conv is not None
        assert conv["status"] == "pending"
        assert conv["direction"] == "sent"

    def test_valid_status_transitions(self, db):
        """测试合法的状态转换链：pending → approved → sending → sent → replied"""
        job_id = db.create_job("Java开发", "JD")
        cid = db.upsert_candidate("boss", "u_001", "王五", "简历", "inbound")
        conv_id = db.create_conversation(cid, job_id, "话术内容")

        assert db.update_conversation_status(conv_id, "approved") is True
        assert db.get_conversation(conv_id)["status"] == "approved"

        assert db.update_conversation_status(conv_id, "sending") is True
        assert db.get_conversation(conv_id)["status"] == "sending"

        assert db.update_conversation_status(conv_id, "sent") is True
        assert db.get_conversation(conv_id)["status"] == "sent"

        assert db.update_conversation_status(conv_id, "replied") is True
        assert db.get_conversation(conv_id)["status"] == "replied"

    def test_invalid_status_transition_rejected(self, db):
        """测试非法的状态转换被拒绝：pending 不能直接跳到 sent"""
        job_id = db.create_job("Java开发", "JD")
        cid = db.upsert_candidate("boss", "u_001", "王五", "简历", "inbound")
        conv_id = db.create_conversation(cid, job_id, "话术内容")

        assert db.update_conversation_status(conv_id, "sent") is False
        assert db.get_conversation(conv_id)["status"] == "pending"  # 状态未变

    def test_sending_to_failed_and_retry(self, db):
        """sending → failed → approved（重试）"""
        job_id = db.create_job("Java开发", "JD")
        cid = db.upsert_candidate("boss", "u_001", "王五", "简历", "inbound")
        conv_id = db.create_conversation(cid, job_id, "话术内容")

        db.update_conversation_status(conv_id, "approved")
        db.update_conversation_status(conv_id, "sending")
        assert db.update_conversation_status(conv_id, "failed") is True

        # failed 可以重新进入 approved 队列
        assert db.update_conversation_status(conv_id, "approved") is True
        assert db.get_conversation(conv_id)["status"] == "approved"

    def test_sending_to_timeout(self, db):
        """sending → timeout（需人工确认）"""
        job_id = db.create_job("Java开发", "JD")
        cid = db.upsert_candidate("boss", "u_001", "王五", "简历", "inbound")
        conv_id = db.create_conversation(cid, job_id, "话术内容")

        db.update_conversation_status(conv_id, "approved")
        db.update_conversation_status(conv_id, "sending")
        assert db.update_conversation_status(conv_id, "timeout") is True

        # timeout 不能自动转换
        assert db.update_conversation_status(conv_id, "approved") is False

    def test_list_conversations_by_status(self, db):
        job_id = db.create_job("Java开发", "JD")
        cid = db.upsert_candidate("boss", "u_001", "王五", "简历", "inbound")

        conv_id1 = db.create_conversation(cid, job_id, "话术1")
        conv_id2 = db.create_conversation(cid, job_id, "话术2")
        db.update_conversation_status(conv_id1, "approved")

        pending = db.list_conversations(status="pending")
        assert len(pending) == 1
        approved = db.list_conversations(status="approved")
        assert len(approved) == 1

    def test_update_intent(self, db):
        job_id = db.create_job("Java开发", "JD")
        cid = db.upsert_candidate("boss", "u_001", "王五", "简历", "inbound")
        conv_id = db.create_conversation(cid, job_id, "话术", direction="received")

        db.update_conversation_intent(conv_id, "high")
        conv = db.get_conversation(conv_id)
        assert conv["intent"] == "high"
