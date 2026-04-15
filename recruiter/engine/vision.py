"""截图视觉分析模块

使用 Claude Vision API 从截图中提取候选人数据，
并分析页面 DOM 结构以反哺修复失败的 API 拦截和 DOM 解析。
"""

import base64
import json
import logging

import anthropic

from recruiter import config

logger = logging.getLogger(__name__)

EXTRACT_PROMPT = """分析这张 Boss直聘网页截图，完成两个任务：

## 任务 1：提取候选人列表
从截图左侧的聊天列表中提取所有可见的候选人信息。

## 任务 2：分析页面结构
观察截图中的 UI 元素，推测当前页面的 DOM 结构特征，用于修复 CSS 选择器。
关注以下元素：
- 候选人列表容器
- 单个候选人卡片
- 候选人姓名
- 聊天输入框
- 消息气泡

## 输出格式
严格输出以下 JSON，不要输出任何其他内容：
{
    "candidates": [
        {"name": "姓名", "title": "职位/描述", "last_message": "最后消息摘要"}
    ],
    "selectors_hint": {
        "candidate_card": "推测的候选人卡片 CSS 选择器或特征描述",
        "candidate_name": "推测的候选人姓名 CSS 选择器或特征描述",
        "chat_input": "推测的聊天输入框 CSS 选择器或特征描述",
        "observations": "页面结构变化的观察说明，比如：class name 是否有变化、布局是否有调整"
    }
}"""


RESUME_EXTRACT_PROMPT = """分析这张 Boss直聘在线简历截图，提取候选人的完整简历信息。

请提取以下内容（如果截图中可见）：
- 姓名、年龄、学历
- 求职意向（期望职位、期望城市、期望薪资）
- 个人优势/自我评价
- 工作经历（公司名称、职位、时间段、工作内容/业绩）
- 教育经历（学校、专业、学历、时间）
- 技能标签

请直接输出结构化的简历文本，不要输出 JSON 或其他格式标记。用换行分隔不同段落。"""


class VisionAnalyzer:
    """使用 Claude Vision 分析截图提取数据。"""

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=config.LLM_CHAT_API_KEY)
        self.model = config.LLM_CHAT_MODEL

    def _read_image(self, image_path: str) -> str | None:
        try:
            with open(image_path, "rb") as f:
                return base64.standard_b64encode(f.read()).decode("utf-8")
        except Exception as e:
            logger.error("读取截图失败: %s", e)
            return None

    def _call_vision(self, image_data: str, prompt: str) -> str | None:
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": image_data,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }],
            )
            return resp.content[0].text.strip()
        except Exception as e:
            logger.error("Claude Vision API 调用失败: %s", e)
            return None

    def extract_resume_from_screenshot(self, image_path: str) -> str | None:
        """从简历截图中提取完整简历文本。"""
        image_data = self._read_image(image_path)
        if not image_data:
            return None

        result = self._call_vision(image_data, RESUME_EXTRACT_PROMPT)
        if result:
            logger.info("简历截图识别完成 (%d chars)", len(result))
        return result

    def analyze_screenshot(self, image_path: str) -> dict | None:
        """分析截图，提取候选人列表和 DOM 结构线索。"""
        image_data = self._read_image(image_path)
        if not image_data:
            return None

        content = self._call_vision(image_data, EXTRACT_PROMPT)
        if not content:
            return None

        try:
            if content.startswith("```"):
                lines = content.split("\n")
                lines = [l for l in lines if not l.strip().startswith("```")]
                content = "\n".join(lines)
            result = json.loads(content)
            logger.info("视觉分析完成: 提取 %d 个候选人", len(result.get("candidates", [])))
            return result
        except json.JSONDecodeError as e:
            logger.error("视觉分析返回非 JSON: %s", e)
            return None


# 选择器修复报告文件路径
SELECTOR_REPORT_PATH = config.BASE_DIR / "data" / "selector_report.json"


def save_selector_report(selectors_hint: dict, failed_stage: str):
    """保存选择器修复线索到文件，供开发者查看和修复。"""
    import datetime
    report = {
        "timestamp": datetime.datetime.now().isoformat(),
        "failed_stage": failed_stage,
        "selectors_hint": selectors_hint,
    }
    SELECTOR_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SELECTOR_REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    logger.warning("选择器修复报告已保存: %s", SELECTOR_REPORT_PATH)
