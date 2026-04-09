import json
import os
import tempfile
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from recruiter.db.models import Database
from recruiter.engine.matcher import ResumeMatcher


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database = Database(path)
    yield database
    database.close()
    os.unlink(path)


@pytest.fixture
def matcher(db):
    with patch("recruiter.engine.matcher.OpenAI"):
        m = ResumeMatcher(db)
    return m


def _make_llm_response(score, reason, dimensions):
    """构造模拟的 LLM ChatCompletion 返回。"""
    content = json.dumps({
        "score": score,
        "reason": reason,
        "dimensions": dimensions,
    }, ensure_ascii=False)
    message = MagicMock()
    message.content = content
    choice = MagicMock()
    choice.message = message
    resp = MagicMock()
    resp.choices = [choice]
    return resp


def _make_dims(tech=80, years=70, industry=60, edu=50, loc=90):
    return {"tech_stack": tech, "years": years, "industry": industry,
            "education": edu, "location": loc}


# --- 数据准备 ---

def _seed_java_job_and_candidate(db):
    job_id = db.create_job(
        "Java高级开发", "3年以上Java开发经验，熟悉Spring Boot、MySQL、Redis，有分布式系统经验优先")
    cid = db.upsert_candidate("boss", "u_java01", "张三",
                              "Java开发工程师，3年经验，精通Spring Boot、MyBatis、MySQL，参与过微服务改造项目",
                              "inbound")
    return job_id, cid


class TestHappyPath:
    def test_match_one_java(self, db, matcher):
        """Java JD + 3年 Java 简历 → score 60-100"""
        job_id, cid = _seed_java_job_and_candidate(db)
        dims = _make_dims(tech=85, years=75, industry=65, edu=60, loc=90)
        expected_score = 78  # 85*0.4 + 75*0.2 + 65*0.2 + 60*0.1 + 90*0.1 = 34+15+13+6+9=77 → 78 ok
        mock_resp = _make_llm_response(expected_score, "技术栈高度匹配", dims)
        matcher.client.chat.completions.create.return_value = mock_resp

        result = matcher.match_one(job_id, cid)

        assert 60 <= result["score"] <= 100
        assert result["reason"]
        assert all(k in result["dimensions"] for k in
                   ["tech_stack", "years", "industry", "education", "location"])
        assert result["prompt_version"]

        # 验证数据库也写入了
        db_results = db.get_match_results(job_id=job_id)
        assert len(db_results) == 1
        assert db_results[0]["score"] == expected_score

    def test_match_batch_five_resumes(self, db, matcher):
        """批量提交 5 个简历，全部返回有效分数，prompt_version 一致。"""
        job_id = db.create_job("Python开发", "3年Python经验")
        cids = []
        for i in range(5):
            cid = db.upsert_candidate("boss", f"u_py{i:02d}", f"候选人{i}",
                                      f"Python开发{i}年经验", "inbound")
            cids.append(cid)

        dims = _make_dims()
        mock_resp = _make_llm_response(72, "匹配度中等偏上", dims)
        matcher.client.chat.completions.create.return_value = mock_resp

        results = matcher.match_batch(job_id, cids)

        assert len(results) == 5
        versions = set()
        for r in results:
            assert 0 <= r["score"] <= 100
            assert r["reason"]
            versions.add(r["prompt_version"])
        # 所有 prompt_version 一致
        assert len(versions) == 1


class TestEdgeCases:
    def test_empty_resume(self, db, matcher):
        """空简历 → score 0 或 -1，不崩溃。"""
        job_id = db.create_job("Java开发", "需要Java经验")
        cid = db.upsert_candidate("boss", "u_empty", "空简历", "", "inbound")

        dims = _make_dims(tech=0, years=0, industry=0, edu=0, loc=0)
        mock_resp = _make_llm_response(0, "简历为空，无法评估", dims)
        matcher.client.chat.completions.create.return_value = mock_resp

        result = matcher.match_one(job_id, cid)
        assert result["score"] <= 0

    def test_unrelated_jd_and_resume(self, db, matcher):
        """Java JD + 厨师简历 → score < 30。"""
        job_id = db.create_job("Java高级开发", "5年Java经验，熟悉分布式系统")
        cid = db.upsert_candidate("boss", "u_chef", "李大厨",
                                  "中餐厨师，8年工作经验，擅长川菜、粤菜，曾获得厨艺大赛金奖",
                                  "inbound")

        dims = _make_dims(tech=5, years=10, industry=0, edu=10, loc=50)
        mock_resp = _make_llm_response(10, "完全不匹配，候选人为厨师", dims)
        matcher.client.chat.completions.create.return_value = mock_resp

        result = matcher.match_one(job_id, cid)
        assert result["score"] < 30


class TestErrorPaths:
    def test_llm_returns_non_json_then_retry_success(self, db, matcher):
        """LLM 第一次返回非 JSON，重试后成功。"""
        job_id, cid = _seed_java_job_and_candidate(db)

        # 第一次返回非 JSON，第二次返回正确 JSON
        bad_message = MagicMock()
        bad_message.content = "这不是JSON格式的回复"
        bad_choice = MagicMock()
        bad_choice.message = bad_message
        bad_resp = MagicMock()
        bad_resp.choices = [bad_choice]

        good_resp = _make_llm_response(75, "技术匹配", _make_dims())
        matcher.client.chat.completions.create.side_effect = [bad_resp, good_resp]

        result = matcher.match_one(job_id, cid)
        assert result["score"] == 75
        assert matcher.client.chat.completions.create.call_count == 2

    def test_llm_returns_non_json_twice_mark_negative(self, db, matcher):
        """LLM 两次都返回非 JSON → score=-1。"""
        job_id, cid = _seed_java_job_and_candidate(db)

        bad_message = MagicMock()
        bad_message.content = "抱歉我无法处理"
        bad_choice = MagicMock()
        bad_choice.message = bad_message
        bad_resp = MagicMock()
        bad_resp.choices = [bad_choice]

        matcher.client.chat.completions.create.return_value = bad_resp

        result = matcher.match_one(job_id, cid)
        assert result["score"] == -1
        assert matcher.client.chat.completions.create.call_count == 2

        # 数据库中也记录了 -1
        db_results = db.get_match_results(job_id=job_id)
        assert len(db_results) == 1
        assert db_results[0]["score"] == -1

    def test_llm_api_timeout(self, db, matcher):
        """LLM API 超时 → 直接返回 score=-1，不重试。"""
        job_id, cid = _seed_java_job_and_candidate(db)

        matcher.client.chat.completions.create.side_effect = TimeoutError("connection timeout")

        result = matcher.match_one(job_id, cid)
        assert result["score"] == -1
        assert "error" in result["reason"].lower() or "timeout" in result["reason"].lower()
        # 只调用了 1 次（不重试）
        assert matcher.client.chat.completions.create.call_count == 1

    def test_batch_skip_on_api_error(self, db, matcher):
        """批量匹配时 API 异常 → 该条 score=-1，不影响其他。"""
        job_id = db.create_job("Go开发", "3年Go经验")
        cid1 = db.upsert_candidate("boss", "u_go1", "A", "Go 3年", "inbound")
        cid2 = db.upsert_candidate("boss", "u_go2", "B", "Go 5年", "inbound")

        good_resp = _make_llm_response(80, "匹配", _make_dims())

        # 第一个成功，第二个超时
        matcher.client.chat.completions.create.side_effect = [
            good_resp,
            TimeoutError("timeout"),
        ]

        results = matcher.match_batch(job_id, [cid1, cid2])
        assert len(results) == 2
        assert results[0]["score"] == 80
        assert results[1]["score"] == -1


class TestPromptVersion:
    def test_prompt_version_is_consistent(self, matcher):
        """prompt_version 是 8 位十六进制字符串，多次调用一致。"""
        v1 = matcher._get_prompt_version()
        v2 = matcher._get_prompt_version()
        assert v1 == v2
        assert len(v1) == 8
        # 验证是合法十六进制
        int(v1, 16)

    def test_prompt_version_based_on_template(self, matcher):
        """prompt_version 基于模板（不含变量值）的 SHA256。"""
        from recruiter.engine.matcher import PROMPT_TEMPLATE
        import hashlib
        expected = hashlib.sha256(PROMPT_TEMPLATE.encode("utf-8")).hexdigest()[:8]
        assert matcher._get_prompt_version() == expected


class TestParseResponse:
    def test_parse_valid_json(self, matcher):
        content = json.dumps({
            "score": 75, "reason": "不错", "dimensions": _make_dims()
        })
        result = matcher._parse_response(content)
        assert result["score"] == 75

    def test_parse_json_in_code_block(self, matcher):
        """LLM 用 ```json ... ``` 包裹的情况。"""
        inner = json.dumps({
            "score": 60, "reason": "一般", "dimensions": _make_dims()
        })
        content = f"```json\n{inner}\n```"
        result = matcher._parse_response(content)
        assert result["score"] == 60

    def test_parse_invalid_json_raises(self, matcher):
        with pytest.raises(json.JSONDecodeError):
            matcher._parse_response("这不是JSON")

    def test_parse_missing_dimensions_raises(self, matcher):
        content = json.dumps({"score": 50, "reason": "ok"})
        with pytest.raises(ValueError, match="dimensions"):
            matcher._parse_response(content)
