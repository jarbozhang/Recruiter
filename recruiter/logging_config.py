"""日志配置 + 异常告警

日志写入文件 + 控制台，关键异常触发告警通知。
"""

import json
import logging
import logging.handlers
from datetime import datetime
from pathlib import Path

import requests

from recruiter import config

LOG_DIR = config.BASE_DIR / "data" / "logs"


def setup_logging(verbose: bool = False):
    """配置日志系统：控制台 + 文件轮转。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    level = logging.DEBUG if verbose else logging.INFO

    # 格式
    fmt = logging.Formatter(
        "%(asctime)s %(name)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(fmt)

    # 文件轮转（每天一个文件，保留 30 天）
    file_handler = logging.handlers.TimedRotatingFileHandler(
        LOG_DIR / "recruiter.log",
        when="midnight",
        backupCount=30,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)

    # 错误单独一个文件
    error_handler = logging.handlers.TimedRotatingFileHandler(
        LOG_DIR / "error.log",
        when="midnight",
        backupCount=30,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(console)
    root.addHandler(file_handler)
    root.addHandler(error_handler)


# === 告警通知 ===

WEBHOOK_URL = config.__dict__.get("ALERT_WEBHOOK_URL", "") or ""


class AlertManager:
    """异常告警管理器。支持钉钉/飞书/企业微信 Webhook。"""

    def __init__(self, webhook_url: str = ""):
        self.webhook_url = webhook_url or WEBHOOK_URL
        self.logger = logging.getLogger("alert")

    def send(self, title: str, content: str, level: str = "warning"):
        """发送告警通知。"""
        self.logger.log(
            logging.WARNING if level == "warning" else logging.ERROR,
            "[ALERT] %s: %s", title, content,
        )

        if not self.webhook_url:
            return

        try:
            # 自动检测 Webhook 类型
            if "dingtalk" in self.webhook_url or "oapi.dingtalk" in self.webhook_url:
                self._send_dingtalk(title, content)
            elif "feishu" in self.webhook_url or "lark" in self.webhook_url:
                self._send_feishu(title, content)
            elif "qyapi.weixin" in self.webhook_url:
                self._send_wechat(title, content)
            else:
                self._send_generic(title, content)
        except Exception as e:
            self.logger.error("告警发送失败: %s", e)

    def _send_dingtalk(self, title: str, content: str):
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": f"[Recruiter] {title}",
                "text": f"### {title}\n\n{content}\n\n---\n{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            },
        }
        requests.post(self.webhook_url, json=payload, timeout=10)

    def _send_feishu(self, title: str, content: str):
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": f"[Recruiter] {title}"}},
                "elements": [{"tag": "markdown", "content": content}],
            },
        }
        requests.post(self.webhook_url, json=payload, timeout=10)

    def _send_wechat(self, title: str, content: str):
        payload = {
            "msgtype": "markdown",
            "markdown": {"content": f"### {title}\n{content}"},
        }
        requests.post(self.webhook_url, json=payload, timeout=10)

    def _send_generic(self, title: str, content: str):
        payload = {"title": title, "content": content, "timestamp": datetime.now().isoformat()}
        requests.post(self.webhook_url, json=payload, timeout=10)


# 全局告警实例
alerter = AlertManager()


def alert_circuit_breaker_open():
    alerter.send("熔断器触发", "连续发送失败达到阈值，已暂停操作 2 小时", "error")


def alert_login_expired():
    alerter.send("登录失效", "Boss直聘登录态过期，请重新登录 AdsPower", "error")


def alert_all_layers_failed():
    alerter.send("采集全部失败", "API 拦截 + DOM 解析 + 视觉分析三层全部失败", "error")
