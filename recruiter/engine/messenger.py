import logging

import anthropic

from recruiter import config
from recruiter.db.models import Database

logger = logging.getLogger(__name__)

PROMPT_TEMPLATE = """你是一位专业的猎头顾问，需要根据岗位信息和候选人背景，撰写一条个性化的招呼消息。

## 要求
- 语气专业、简洁、友好
- 突出岗位与候选人经历的匹配点
- 字数控制在 100-300 字
- 不要用"您好，我是XX公司的HR"这种模板化开头
- 自然地引出岗位亮点和候选人优势的关联

## 岗位信息
{jd_summary}

## 候选人背景
{candidate_summary}

## 匹配要点
{match_reason}

请直接输出招呼消息正文，不要加任何解释或格式标记。"""

# 降级用的通用话术模板
FALLBACK_TEMPLATE = "您好，我们目前有一个{job_title}的机会，看到您的经历和岗位需求非常匹配，方便的话可以了解一下吗？"


class MessageGenerator:
    def __init__(self, db: Database):
        self.db = db
        self.client = anthropic.Anthropic(api_key=config.LLM_CHAT_API_KEY)
        self.model = config.LLM_CHAT_MODEL

    def _build_prompt(self, jd_summary: str, candidate_summary: str,
                      match_reason: str) -> str:
        return PROMPT_TEMPLATE.format(
            jd_summary=jd_summary,
            candidate_summary=candidate_summary,
            match_reason=match_reason,
        )

    def _call_llm(self, prompt: str) -> str:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text

    def _truncate(self, text: str) -> str:
        """截断到 MESSAGE_MAX_LENGTH 字符。"""
        if len(text) > config.MESSAGE_MAX_LENGTH:
            return text[:config.MESSAGE_MAX_LENGTH]
        return text

    def generate_for_candidate(self, job_id: int, candidate_id: int,
                               match_reason: str = "") -> dict:
        """为单个候选人生成话术，存入 conversations 表。

        Returns:
            {"conversation_id": int, "message": str, "status": "pending"}
            or {"conversation_id": None, "message": str, "status": "fallback"} on error
        """
        job = self.db.get_job(job_id)
        candidate = self.db.get_candidate(candidate_id)
        if not job or not candidate:
            logger.error("job_id=%s or candidate_id=%s not found", job_id, candidate_id)
            return {"conversation_id": None, "message": "", "status": "error"}

        jd_summary = job.get("jd") or job.get("title", "")
        candidate_summary = candidate.get("resume_text") or candidate.get("name", "")

        try:
            prompt = self._build_prompt(jd_summary, candidate_summary, match_reason)
            message = self._call_llm(prompt)
            message = self._truncate(message.strip())
        except Exception as e:
            logger.error("Claude API error, using fallback template: %s", e)
            message = FALLBACK_TEMPLATE.format(job_title=job.get("title", "该岗位"))

        conv_id = self.db.create_conversation(
            candidate_id, job_id, message, direction="sent", status="pending",
        )
        return {"conversation_id": conv_id, "message": message, "status": "pending"}

    def generate_batch(self, job_id: int, candidate_ids: list[int],
                       match_reasons: dict[int, str] | None = None) -> list[dict]:
        """批量生成话术。match_reasons: {candidate_id: reason_str}"""
        results = []
        reasons = match_reasons or {}
        for cid in candidate_ids:
            reason = reasons.get(cid, "")
            r = self.generate_for_candidate(job_id, cid, reason)
            results.append(r)
        return results
