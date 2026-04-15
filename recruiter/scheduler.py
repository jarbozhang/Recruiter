"""定时调度器

定期执行采集、回复检测等任务。
"""

import logging
import signal
import time
from datetime import datetime

from recruiter import config
from recruiter.db.models import Database
from recruiter.pipeline import RecruiterPipeline

logger = logging.getLogger(__name__)


class Scheduler:
    """简单的定时调度器，支持多任务不同间隔。"""

    def __init__(self):
        self._running = False
        self._tasks: list[dict] = []

    def add_task(self, name: str, func, interval_minutes: int):
        self._tasks.append({
            "name": name,
            "func": func,
            "interval": interval_minutes * 60,
            "last_run": 0,
        })

    def run(self):
        self._running = True
        signal.signal(signal.SIGINT, lambda *_: self.stop())
        signal.signal(signal.SIGTERM, lambda *_: self.stop())

        logger.info("调度器启动，共 %d 个任务", len(self._tasks))
        for t in self._tasks:
            logger.info("  %s: 每 %d 分钟", t["name"], t["interval"] // 60)

        while self._running:
            now = time.time()
            for task in self._tasks:
                if now - task["last_run"] >= task["interval"]:
                    logger.info("[%s] 开始执行 %s",
                                datetime.now().strftime("%H:%M:%S"), task["name"])
                    try:
                        task["func"]()
                    except Exception as e:
                        logger.error("[%s] 执行失败: %s", task["name"], e)
                    task["last_run"] = time.time()
            time.sleep(10)  # 每 10 秒检查一次

        logger.info("调度器已停止")

    def stop(self):
        self._running = False


def run_scheduler(job_id: int, collect_interval: int = 60,
                  reply_interval: int = 10, send_interval: int = 30):
    """启动定时调度。

    Args:
        job_id: 职位 ID
        collect_interval: 采集间隔（分钟），默认 60
        reply_interval: 回复检测间隔（分钟），默认 10
        send_interval: 发送间隔（分钟），默认 30
    """
    pipeline = RecruiterPipeline()
    scheduler = Scheduler()

    def task_collect():
        pipeline.collect()

    def task_check_replies():
        pipeline.check_replies()

    def task_send():
        pipeline.send()

    scheduler.add_task("采集候选人", task_collect, collect_interval)
    scheduler.add_task("回复检测", task_check_replies, reply_interval)
    scheduler.add_task("发送消息", task_send, send_interval)

    try:
        scheduler.run()
    finally:
        pipeline.close()
