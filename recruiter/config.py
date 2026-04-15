import os
from pathlib import Path

# 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent

# 数据库
DB_PATH = os.getenv("RECRUITER_DB_PATH", str(BASE_DIR / "data" / "recruiter.db"))

# 浏览器驱动配置
BROWSER_DRIVER = os.getenv("BROWSER_DRIVER", "playwright")  # playwright / selenium / bb-browser
ADSPOWER_API_KEY = os.getenv("ADSPOWER_API_KEY", "")
ADSPOWER_PROFILE_ID = os.getenv("ADSPOWER_PROFILE_ID", "")
ADSPOWER_API_BASE = os.getenv("ADSPOWER_API_BASE", "http://127.0.0.1:50325")

# LLM API 配置
LLM_MATCH_API_KEY = os.getenv("LLM_MATCH_API_KEY", "")
LLM_MATCH_BASE_URL = os.getenv("LLM_MATCH_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")  # Qwen
LLM_MATCH_MODEL = os.getenv("LLM_MATCH_MODEL", "qwen-plus")

LLM_CHAT_API_KEY = os.getenv("LLM_CHAT_API_KEY", "")  # Claude API key
LLM_CHAT_BASE_URL = os.getenv("LLM_CHAT_BASE_URL", "https://api.anthropic.com")
LLM_CHAT_MODEL = os.getenv("LLM_CHAT_MODEL", "claude-sonnet-4-20250514")

# 简历匹配评分维度权重 (总和应为 100)
MATCH_WEIGHTS = {
    "tech_stack": int(os.getenv("MATCH_WEIGHT_TECH", "40")),
    "years": int(os.getenv("MATCH_WEIGHT_YEARS", "20")),
    "industry": int(os.getenv("MATCH_WEIGHT_INDUSTRY", "20")),
    "education": int(os.getenv("MATCH_WEIGHT_EDU", "10")),
    "location": int(os.getenv("MATCH_WEIGHT_LOCATION", "10")),
}

# 匹配度阈值
MATCH_THRESHOLD_INITIAL = int(os.getenv("MATCH_THRESHOLD_INITIAL", "60"))
MATCH_THRESHOLD_FOCUS = int(os.getenv("MATCH_THRESHOLD_FOCUS", "80"))

# 操作频率控制
OP_INTERVAL_MIN = int(os.getenv("OP_INTERVAL_MIN", "30"))   # 秒
OP_INTERVAL_MAX = int(os.getenv("OP_INTERVAL_MAX", "120"))  # 秒
OP_HOURLY_LIMIT = int(os.getenv("OP_HOURLY_LIMIT", "30"))
OP_DAILY_LIMIT = int(os.getenv("OP_DAILY_LIMIT", "150"))

# Circuit breaker
CB_FAILURE_THRESHOLD = int(os.getenv("CB_FAILURE_THRESHOLD", "3"))
CB_PAUSE_SECONDS = int(os.getenv("CB_PAUSE_SECONDS", "7200"))  # 2 hours

# 话术长度限制
MESSAGE_MIN_LENGTH = 100
MESSAGE_MAX_LENGTH = 300

# 发送超时（秒）
SEND_TIMEOUT = int(os.getenv("SEND_TIMEOUT", "60"))

# 视觉模型（用于简历截图识别）
LLM_VISION_API_KEY = os.getenv("LLM_VISION_API_KEY", "")
LLM_VISION_BASE_URL = os.getenv("LLM_VISION_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/")
LLM_VISION_MODEL = os.getenv("LLM_VISION_MODEL", "glm-4.6v-flash")

# 告警 Webhook（钉钉/飞书/企业微信）
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")
