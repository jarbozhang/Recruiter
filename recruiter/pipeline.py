"""Recruiter Agent 主流程 Pipeline

采集 → 匹配 → 生成消息 → (人工审核) → 发送

每个阶段可以独立运行，也可以串联执行。
"""

import logging
import time

from recruiter import config
from recruiter.browser import create_driver
from recruiter.collector.browser_collector import BossWebCollector
from recruiter.db.models import Database
from recruiter.engine.matcher import ResumeMatcher
from recruiter.engine.messenger import MessageGenerator
from recruiter.engine.follow_up import FollowUpGenerator
from recruiter.operator.boss.reply_monitor import ReplyMonitor
from recruiter.operator.boss.sender import BossSender

logger = logging.getLogger(__name__)


class RecruiterPipeline:
    """招聘 Agent 主流程。"""

    def __init__(self, db: Database = None):
        self.db = db or Database(config.DB_PATH)
        self._driver = None

    @property
    def driver(self):
        if self._driver is None:
            self._driver = create_driver()
        return self._driver

    def close(self):
        if self._driver:
            self._driver.close()
            self._driver = None

    # === 阶段 1：采集候选人 ===

    def collect(self, job_url: str = None) -> dict:
        """采集候选人列表。

        Returns:
            {"total": int, "new": int}
        """
        logger.info("=== 阶段 1：采集候选人 ===")
        before_count = len(self.db.list_candidates(limit=99999))

        collector = BossWebCollector(self.driver, self.db)
        candidates = collector.collect_candidates(job_url)

        after_count = len(self.db.list_candidates(limit=99999))
        new_count = after_count - before_count

        stats = {"total": len(candidates), "new": new_count}
        logger.info("采集完成: 获取 %d 人, 新增 %d 人", stats["total"], stats["new"])
        return stats

    # === 阶段 1.5：简历详情采集 ===

    def collect_resumes(self, limit: int = 50) -> dict:
        """为没有简历的候选人采集简历详情。"""
        logger.info("=== 阶段 1.5：简历详情采集 ===")
        collector = BossWebCollector(self.driver, self.db)
        stats = collector.collect_resumes(limit)
        logger.info("简历采集: %d/%d 成功", stats["collected"], stats["total"])
        return stats

    # === 阶段 2：AI 匹配 ===

    def match(self, job_id: int, min_score: int = None) -> dict:
        """对所有未匹配的候选人执行 AI 简历匹配。

        Returns:
            {"matched": int, "qualified": int, "threshold": int}
        """
        logger.info("=== 阶段 2：AI 简历匹配 ===")
        threshold = min_score or config.MATCH_THRESHOLD_INITIAL
        matcher = ResumeMatcher(self.db)

        candidates = self.db.list_candidates(limit=99999)
        # 找出还没有对该职位做过匹配的候选人
        existing = self.db.get_match_results(job_id=job_id)
        matched_cids = {r["candidate_id"] for r in existing}
        to_match = [c for c in candidates if c["id"] not in matched_cids and c.get("resume_text")]

        logger.info("待匹配候选人: %d 人（已跳过 %d 人无简历或已匹配）",
                     len(to_match), len(candidates) - len(to_match))

        results = matcher.match_batch(job_id, [c["id"] for c in to_match])
        qualified = [r for r in results if r["score"] >= threshold]

        stats = {"matched": len(results), "qualified": len(qualified), "threshold": threshold}
        logger.info("匹配完成: %d 人, 达标 %d 人 (阈值=%d)", stats["matched"], stats["qualified"], threshold)
        return stats

    # === 阶段 3：生成消息 ===

    def generate_messages(self, job_id: int, min_score: int = None) -> dict:
        """为达标候选人生成个性化招呼消息。

        Returns:
            {"generated": int, "skipped": int}
        """
        logger.info("=== 阶段 3：生成消息 ===")
        threshold = min_score or config.MATCH_THRESHOLD_INITIAL
        messenger = MessageGenerator(self.db)

        # 获取达标且没有 conversation 的候选人
        match_results = self.db.get_match_results(job_id=job_id, min_score=threshold)
        existing_convs = self.db.list_conversations(limit=99999)
        conv_cids = {c["candidate_id"] for c in existing_convs}

        to_generate = [r for r in match_results if r["candidate_id"] not in conv_cids]

        logger.info("待生成消息: %d 人（已跳过 %d 人已有对话）",
                     len(to_generate), len(match_results) - len(to_generate))

        generated = 0
        for r in to_generate:
            result = messenger.generate_for_candidate(
                job_id, r["candidate_id"], match_reason=r.get("reason", "")
            )
            if result["conversation_id"]:
                generated += 1

        stats = {"generated": generated, "skipped": len(match_results) - len(to_generate)}
        logger.info("消息生成完成: %d 条", generated)
        return stats

    # === 阶段 4：发送消息 ===

    def send(self) -> dict:
        """发送所有已审核（approved）的消息。

        Returns:
            process_queue 的 stats dict
        """
        logger.info("=== 阶段 4：发送消息 ===")
        sender = BossSender(self.driver, self.db)
        stats = sender.process_queue()
        logger.info("发送完成: sent=%d, failed=%d, timeout=%d, skipped=%d, reason=%s",
                     stats["sent"], stats["failed"], stats["timeout"],
                     stats["skipped"], stats.get("reason", ""))
        return stats

    # === 阶段 5：回复检测 ===

    def check_replies(self) -> dict:
        """检测候选人是否有新回复。

        Returns:
            {"checked": int, "replied": int}
        """
        logger.info("=== 阶段 5：回复检测 ===")
        monitor = ReplyMonitor(self.driver, self.db)
        stats = monitor.check_replies()
        logger.info("回复检测: 检查 %d 条, 发现回复 %d 条",
                     stats["checked"], stats["replied"])
        return stats

    # === 阶段 6：自动跟进回复 ===

    def follow_up(self, auto_send: bool = False) -> dict:
        """为已回复的对话生成跟进消息。"""
        logger.info("=== 阶段 6：自动跟进回复 ===")
        generator = FollowUpGenerator(self.db)
        stats = generator.process_replies(self.driver, auto_send=auto_send)
        logger.info("跟进处理: %d 条, 生成 %d 条, 自动发送 %d 条",
                     stats["processed"], stats["generated"], stats["auto_sent"])
        return stats

    # === 全流程 ===

    def run(self, job_id: int, job_url: str = None,
            auto_approve: bool = False, skip_collect: bool = False,
            skip_match: bool = False, skip_generate: bool = False) -> dict:
        """执行完整 pipeline。

        Args:
            job_id: 职位 ID
            job_url: 采集页面 URL（可选，默认用聊天页）
            auto_approve: 是否自动审核通过（跳过人工审核）
            skip_collect: 跳过采集阶段
            skip_match: 跳过匹配阶段
            skip_generate: 跳过消息生成阶段
        """
        all_stats = {}

        try:
            # 1. 采集
            if not skip_collect:
                all_stats["collect"] = self.collect(job_url)

            # 2. 匹配
            if not skip_match:
                all_stats["match"] = self.match(job_id)

            # 3. 生成消息
            if not skip_generate:
                all_stats["generate"] = self.generate_messages(job_id)

            # 4. 自动审核（可选）
            if auto_approve:
                pending = self.db.list_conversations(status="pending")
                approved = 0
                for conv in pending:
                    if self.db.update_conversation_status(conv["id"], "approved"):
                        approved += 1
                all_stats["auto_approve"] = {"approved": approved}
                logger.info("自动审核: %d 条消息已批准", approved)

            # 5. 发送
            all_stats["send"] = self.send()

        finally:
            self.close()

        logger.info("=== Pipeline 完成 ===")
        return all_stats
