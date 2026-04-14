"""Boss直聘回复监听

通过 API 拦截或 DOM 解析检测候选人新回复，更新对话状态为 replied。
"""

import json
import logging
import time

from recruiter.browser.base import BrowserDriver
from recruiter.db.models import Database

logger = logging.getLogger(__name__)

CHAT_URL = "https://www.zhipin.com/web/chat/index"
API_LAST_MSG = "userLastMsg"


class ReplyMonitor:
    """检测候选人回复并更新 DB 状态。"""

    def __init__(self, browser: BrowserDriver, db: Database):
        self.browser = browser
        self.db = db

    def _supports_intercept(self) -> bool:
        return hasattr(self.browser, 'intercept_response')

    def check_replies(self) -> dict:
        """检测所有 sent 状态对话的候选人是否有新回复。

        Returns:
            {"checked": int, "replied": int}
        """
        # 获取所有已发送的对话
        sent_convs = self.db.list_conversations(status="sent", limit=99999)
        if not sent_convs:
            return {"checked": 0, "replied": 0}

        # 构建 candidate_id → conv 的索引
        cid_to_convs = {}
        for conv in sent_convs:
            cid = conv["candidate_id"]
            cid_to_convs.setdefault(cid, []).append(conv)

        # 获取候选人 platform_id 映射
        cid_to_pid = {}
        for cid in cid_to_convs:
            candidate = self.db.get_candidate(cid)
            if candidate:
                cid_to_pid[cid] = candidate["platform_id"]

        # 尝试 API 拦截获取最新消息
        last_msgs = self._get_last_msgs_via_api()
        if last_msgs is None:
            last_msgs = self._get_last_msgs_via_dom(cid_to_pid)

        replied = 0
        for cid, convs in cid_to_convs.items():
            pid = cid_to_pid.get(cid, "")
            msg_info = last_msgs.get(pid) or last_msgs.get(str(cid))
            if not msg_info:
                continue

            # 检查最后一条消息是否来自候选人（fromId == 候选人 uid）
            if self._is_reply(msg_info, pid):
                for conv in convs:
                    if self.db.update_conversation_status(conv["id"], "replied"):
                        replied += 1
                        logger.info("候选人 %s 已回复，对话 %d 状态更新为 replied",
                                    pid, conv["id"])

        stats = {"checked": len(sent_convs), "replied": replied}
        logger.info("回复检测完成: 检查 %d 条, 发现回复 %d 条", stats["checked"], stats["replied"])
        return stats

    def _is_reply(self, msg_info: dict, platform_id: str) -> bool:
        """判断最后一条消息是否为候选人发出的回复。"""
        from_id = str(msg_info.get("fromId", ""))
        # platform_id 格式可能是 "96286852" 或 "96286852-0"
        uid = platform_id.split("-")[0] if "-" in platform_id else platform_id
        return from_id == uid

    def _get_last_msgs_via_api(self) -> dict | None:
        """通过 API 拦截获取所有候选人最后消息。"""
        if not self._supports_intercept():
            return None

        captured = {}

        def on_response(response):
            try:
                if API_LAST_MSG in response.url:
                    captured["data"] = json.loads(response.text())
            except Exception:
                pass

        self.browser.intercept_response("wapi", on_response)
        try:
            current = self.browser.current_url()
            if "web/chat" not in current:
                self.browser.navigate(CHAT_URL)
                time.sleep(2)
            self.browser.reload()

            for _ in range(20):
                if "data" in captured:
                    break
                time.sleep(0.5)

            if "data" not in captured:
                return None

            # 构建 uid → lastMsgInfo 的映射
            result = {}
            zp_data = captured["data"].get("zpData", [])
            if isinstance(zp_data, list):
                for item in zp_data:
                    uid = str(item.get("uid", ""))
                    msg_info = item.get("lastMsgInfo", {})
                    if uid and msg_info:
                        result[uid] = msg_info
            return result

        except Exception as e:
            logger.warning("API 拦截获取最后消息失败: %s", e)
            return None
        finally:
            self.browser.stop_intercept()

    def _get_last_msgs_via_dom(self, cid_to_pid: dict) -> dict:
        """DOM 兜底：无法获取结构化数据，返回空。"""
        logger.info("DOM 模式暂不支持回复检测，跳过")
        return {}
