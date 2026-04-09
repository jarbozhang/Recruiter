import hashlib
import json
import logging

from openai import OpenAI

from recruiter import config
from recruiter.db.models import Database

logger = logging.getLogger(__name__)

PROMPT_TEMPLATE = """你是一位专业的招聘顾问。请根据以下职位描述（JD）和候选人简历，从5个维度进行匹配评分。

## 评分维度和权重
- tech_stack（技术栈匹配）：权重 {w_tech}%
- years（工作年限匹配）：权重 {w_years}%
- industry（行业经验匹配）：权重 {w_industry}%
- education（学历匹配）：权重 {w_edu}%
- location（工作地点匹配）：权重 {w_loc}%

## 评分规则
- 每个维度打 0-100 分
- 总分 = 各维度分数 × 对应权重 / 100，取整
- 如果简历为空或无法解析，所有维度打 0 分

## 输出格式
严格输出以下 JSON，不要输出任何其他内容：
{{"score": <总分>, "reason": "<简要匹配理由>", "dimensions": {{"tech_stack": <分数>, "years": <分数>, "industry": <分数>, "education": <分数>, "location": <分数>}}}}

## 职位描述（JD）
{jd}

## 候选人简历
{resume}
"""


class ResumeMatcher:
    def __init__(self, db: Database):
        self.db = db
        self.client = OpenAI(
            api_key=config.LLM_MATCH_API_KEY,
            base_url=config.LLM_MATCH_BASE_URL,
        )
        self.model = config.LLM_MATCH_MODEL

    def _build_prompt(self, jd: str, resume: str) -> str:
        weights = config.MATCH_WEIGHTS
        return PROMPT_TEMPLATE.format(
            w_tech=weights["tech_stack"],
            w_years=weights["years"],
            w_industry=weights["industry"],
            w_edu=weights["education"],
            w_loc=weights["location"],
            jd=jd,
            resume=resume,
        )

    def _get_prompt_version(self) -> str:
        h = hashlib.sha256(PROMPT_TEMPLATE.encode("utf-8")).hexdigest()
        return h[:8]

    def _parse_response(self, content: str) -> dict:
        """解析 LLM 返回的 JSON，失败则抛出 ValueError。"""
        # 尝试提取 JSON 块（LLM 有时会包裹在 ```json ... ``` 中）
        text = content.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            # 去掉首尾的 ``` 行
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines)

        data = json.loads(text)

        # 校验必要字段
        if not isinstance(data.get("score"), (int, float)):
            raise ValueError("missing or invalid 'score'")
        if not isinstance(data.get("reason"), str):
            raise ValueError("missing or invalid 'reason'")
        dims = data.get("dimensions")
        if not isinstance(dims, dict):
            raise ValueError("missing or invalid 'dimensions'")
        required_dims = {"tech_stack", "years", "industry", "education", "location"}
        if not required_dims.issubset(dims.keys()):
            raise ValueError(f"missing dimensions: {required_dims - dims.keys()}")

        data["score"] = int(data["score"])
        for k in required_dims:
            dims[k] = int(dims[k])

        return data

    def _call_llm(self, prompt: str) -> str:
        """调用 LLM API，返回 content 文本。"""
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        return resp.choices[0].message.content

    def match_one(self, job_id: int, candidate_id: int) -> dict:
        job = self.db.get_job(job_id)
        candidate = self.db.get_candidate(candidate_id)
        if not job or not candidate:
            logger.error("job_id=%s or candidate_id=%s not found", job_id, candidate_id)
            return {"score": -1, "reason": "job or candidate not found", "dimensions": {}}

        jd = job["jd"] or ""
        resume = candidate.get("resume_text") or ""
        prompt = self._build_prompt(jd, resume)
        prompt_version = self._get_prompt_version()

        # 最多尝试 2 次（首次 + 1 次重试）
        last_error = None
        raw_content = None
        for attempt in range(2):
            try:
                raw_content = self._call_llm(prompt)
                result = self._parse_response(raw_content)
                result["prompt_version"] = prompt_version
                # 写入数据库
                self.db.create_match_result(
                    job_id, candidate_id, result["score"],
                    result["reason"], result["dimensions"], prompt_version,
                )
                return result
            except json.JSONDecodeError as e:
                last_error = e
                logger.warning("LLM returned non-JSON (attempt %d): %s", attempt + 1, e)
            except ValueError as e:
                last_error = e
                logger.warning("LLM response validation failed (attempt %d): %s", attempt + 1, e)
            except Exception as e:
                # API 超时等异常，不重试
                logger.error("LLM API error: %s", e)
                return {"score": -1, "reason": f"LLM API error: {e}", "dimensions": {}}

        # 两次都失败
        logger.error("LLM parse failed after retries. raw=%s, error=%s", raw_content, last_error)
        fail_result = {"score": -1, "reason": "LLM response parse failed", "dimensions": {}}
        self.db.create_match_result(
            job_id, candidate_id, -1,
            f"parse failed: {last_error}", None, prompt_version,
        )
        return fail_result

    def match_batch(self, job_id: int, candidate_ids: list[int]) -> list[dict]:
        results = []
        for cid in candidate_ids:
            try:
                r = self.match_one(job_id, cid)
                results.append(r)
            except Exception as e:
                logger.error("batch match error for candidate %s: %s", cid, e)
                results.append({"score": -1, "reason": f"error: {e}", "dimensions": {}})
        return results
