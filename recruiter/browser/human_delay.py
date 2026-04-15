"""类真人随机延时

在浏览器操作之间注入随机延时，模拟人类行为节奏。
不同操作类型有不同的延时范围，避免固定间隔的机器人特征。
"""

import logging
import random
import time

logger = logging.getLogger(__name__)


# 延时配置（秒）：(最小, 最大)
DELAYS = {
    "click":       (0.3, 1.2),    # 点击后短暂停顿
    "fill":        (0.5, 1.5),    # 填入文本后等一下
    "navigate":    (1.0, 3.0),    # 页面跳转后等待加载
    "scroll":      (0.5, 2.0),    # 滚动后浏览
    "page_turn":   (3.0, 8.0),    # 翻页，慢一点
    "read":        (1.5, 4.0),    # 阅读内容
    "between_ops": (0.8, 2.5),    # 连续操作之间
    "batch_item":  (2.0, 5.0),    # 批量处理每个 item 之间
    "send_msg":    (1.0, 3.0),    # 发消息前后
}


def human_delay(action: str = "between_ops", jitter: bool = True):
    """执行一次类真人延时。

    Args:
        action: 操作类型，对应 DELAYS 中的 key
        jitter: 是否添加额外抖动（偶尔停顿更久，模拟走神）
    """
    low, high = DELAYS.get(action, DELAYS["between_ops"])
    delay = random.uniform(low, high)

    # 5% 概率额外停顿 2-6 秒（模拟走神/思考）
    if jitter and random.random() < 0.05:
        extra = random.uniform(2.0, 6.0)
        delay += extra
        logger.debug("人类延时: %.1fs (含走神 %.1fs) [%s]", delay, extra, action)
    else:
        logger.debug("人类延时: %.1fs [%s]", delay, action)

    time.sleep(delay)


def human_typing_delay(text: str):
    """模拟人类打字速度的延时，根据文本长度决定。"""
    # 每个字 0.05-0.15 秒
    chars = len(text)
    delay = chars * random.uniform(0.05, 0.15)
    # 加一个上限，避免太长的文本等太久
    delay = min(delay, 5.0)
    time.sleep(delay)
