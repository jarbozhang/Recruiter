"""自动跟进回复模块

候选人回复后，根据完整对话上下文用 Claude API 生成跟进消息。
支持自动发送或进入审核队列。
"""

import json
import logging
import time

from recruiter import config
from recruiter.browser.base import BrowserDriver
from recruiter.browser.human_delay import human_delay
from recruiter.db.models import Database

logger = logging.getLogger(__name__)

FOLLOW_UP_PROMPT = """你是一位专业的猎头顾问，正在和候选人进行招聘沟通。
根据以下对话历史和岗位信息，生成一条自然、专业的跟进回复。

## 要求
- 根据候选人的回复内容判断意图（感兴趣/犹豫/拒绝/提问）
- 如果候选人感兴趣，推进到下一步（约面试/要简历/加微信）
- 如果候选人提问，如实回答（基于岗位信息）
- 如果候选人犹豫，给出吸引点但不强求
- 如果候选人明确拒绝，礼貌结束并保持关系
- 语气自然、不模板化，100-200 字

## 岗位信息
{job_info}

## 对话历史
{conversation_history}

## 候选人最新回复
{candidate_reply}

请直接输出跟进消息正文，不要加任何解释或格式标记。"""


class FollowUpGenerator:
    """基于对话上下文生成跟进回复。"""

    def __init__(self, db: Database):
        self.db = db
        chat_base = getattr(config, "LLM_CHAT_BASE_URL", "")
        if "anthropic" in chat_base:
            import anthropic
            self._backend = "anthropic"
            self.client = anthropic.Anthropic(api_key=config.LLM_CHAT_API_KEY)
        else:
            from openai import OpenAI
            self._backend = "openai"
            self.client = OpenAI(
                api_key=config.LLM_CHAT_API_KEY,
                base_url=chat_base or None,
            )
        self.model = config.LLM_CHAT_MODEL

    def generate_follow_up(self, conv_id: int, candidate_reply: str) -> dict:
        """为一个对话生成跟进消息。

        Args:
            conv_id: 对话 ID（状态应为 replied）
            candidate_reply: 候选人的回复内容

        Returns:
            {"conversation_id": int, "message": str, "intent": str}
        """
        conv = self.db.get_conversation(conv_id)
        if not conv:
            return {"conversation_id": None, "message": "", "intent": "error"}

        job = self.db.get_job(conv["job_id"])
        candidate = self.db.get_candidate(conv["candidate_id"])
        if not job or not candidate:
            return {"conversation_id": None, "message": "", "intent": "error"}

        job_info = f"{job.get('title', '')} - {job.get('jd', '')[:200]}"
        conversation_history = f"我方: {conv['message']}"

        prompt = FOLLOW_UP_PROMPT.format(
            job_info=job_info,
            conversation_history=conversation_history,
            candidate_reply=candidate_reply,
        )

        try:
            if self._backend == "anthropic":
                resp = self.client.messages.create(
                    model=self.model,
                    max_tokens=1024,
                    messages=[{"role": "user", "content": prompt}],
                )
                message = resp.content[0].text.strip()
            else:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    max_tokens=1024,
                    messages=[{"role": "user", "content": prompt}],
                )
                message = resp.choices[0].message.content.strip()
            if len(message) > config.MESSAGE_MAX_LENGTH:
                message = message[:config.MESSAGE_MAX_LENGTH]
        except Exception as e:
            logger.error("Claude API 生成跟进消息失败: %s", e)
            message = "感谢您的回复！方便的话我们可以进一步沟通，期待您的反馈。"

        # 创建新的跟进对话记录
        new_conv_id = self.db.create_conversation(
            conv["candidate_id"], conv["job_id"], message,
            direction="sent", status="pending",
        )

        # 判断候选人意图
        intent = self._classify_intent(candidate_reply)
        self.db.update_conversation_intent(conv_id, intent)

        return {
            "conversation_id": new_conv_id,
            "message": message,
            "intent": intent,
        }

    def _classify_intent(self, reply: str) -> str:
        """简单的意图分类。"""
        positive = ["感兴趣", "可以", "好的", "行", "聊聊", "了解", "方便", "发我", "微信", "电话"]
        negative = ["不考虑", "不需要", "算了", "不合适", "已入职", "不看"]
        question = ["？", "?", "多少", "什么", "怎么", "几", "哪"]

        reply_lower = reply.strip()
        if any(w in reply_lower for w in negative):
            return "rejected"
        if any(w in reply_lower for w in positive):
            return "interested"
        if any(w in reply_lower for w in question):
            return "questioning"
        return "neutral"

    def process_replies(self, browser: BrowserDriver, auto_send: bool = False) -> dict:
        """处理所有已回复的对话，生成跟进消息。

        Args:
            browser: 浏览器驱动（用于获取候选人回复内容）
            auto_send: 是否自动发送（否则进入 pending 审核）

        Returns:
            {"processed": int, "generated": int, "auto_sent": int}
        """
        replied_convs = self.db.list_conversations(status="replied", limit=99999)
        if not replied_convs:
            return {"processed": 0, "generated": 0, "auto_sent": 0}

        stats = {"processed": len(replied_convs), "generated": 0, "auto_sent": 0}

        for conv in replied_convs:
            candidate = self.db.get_candidate(conv["candidate_id"])
            if not candidate:
                continue

            # 获取候选人最新回复内容
            reply_text = self._fetch_reply_text(browser, candidate["platform_id"])
            if not reply_text:
                logger.warning("无法获取候选人 %s 的回复内容", candidate["name"])
                continue

            result = self.generate_follow_up(conv["id"], reply_text)
            if result["conversation_id"]:
                stats["generated"] += 1
                logger.info("生成跟进消息: %s [%s] -> %s",
                            candidate["name"], result["intent"], result["message"][:50])

                if auto_send and result["intent"] != "rejected":
                    self.db.update_conversation_status(result["conversation_id"], "approved")
                    stats["auto_sent"] += 1

        return stats

    def _fetch_reply_text(self, browser: BrowserDriver, platform_id: str) -> str:
        """从聊天页获取候选人的最新回复内容。"""
        try:
            browser.navigate("https://www.zhipin.com/web/chat/index")
            human_delay("navigate")

            selector = f".geek-item[data-id*='{platform_id}']"
            if not browser.click(selector):
                return ""
            human_delay("click")

            # 提取最后一条来自候选人的消息
            text = browser.execute_js('''
                var msgs = document.querySelectorAll('.message-item');
                if (!msgs.length) return '';
                var last = msgs[msgs.length - 1];
                return last.textContent.trim().substring(0, 500);
            ''')
            return text or ""
        except Exception as e:
            logger.error("获取回复内容失败: %s", e)
            return ""
