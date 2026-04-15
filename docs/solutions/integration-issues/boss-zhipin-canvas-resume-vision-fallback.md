---
title: "Boss直聘 Canvas/WebAssembly 渲染简历无法 DOM 提取，改用 Vision API 截图识别"
date: 2026-04-15
category: integration-issues
module: recruiter/collector
problem_type: integration_issue
component: service_object
severity: high
symptoms:
  - "document.body.innerText returns empty string from Boss直聘 online resume iframe"
  - "iframe contentDocument access blocked by cross-origin policy"
  - "Opening iframe URL in new tab renders empty page (SPA needs parent context)"
  - "Polling iframe innerText for 30 seconds always returns 0 chars"
root_cause: wrong_api
resolution_type: code_fix
related_components:
  - assistant
tags:
  - boss-zhipin
  - canvas-webassembly
  - resume-extraction
  - vision-api
  - glm-4v
  - ocr-fallback
  - anti-scraping
---

# Boss直聘 Canvas/WebAssembly 渲染简历无法 DOM 提取，改用 Vision API 截图识别

## Problem

Boss直聘 (zhipin.com) 的在线简历页面使用 Canvas/WebAssembly 渲染，简历文本只存在于 Canvas 像素中，DOM 中没有任何文本节点。所有基于 DOM 的文本提取方法（innerText、textContent、frame_locator）均返回空字符串，导致无法通过常规爬虫手段获取候选人完整简历。

## Symptoms

- 点击「在线简历」后弹出 iframe（`https://www.zhipin.com/web/frame/c-resume/`），iframe 内 `document.body.innerText` 始终为空
- iframe contentDocument 访问被 cross-origin 策略阻止
- 将 iframe URL 在新标签页打开后页面空白（SPA 依赖父页面上下文）
- Playwright `frame_locator().inner_text()` 找到 body 但文本永远为空
- 轮询等待 30 秒，iframe 内容始终为 0 字符

## What Didn't Work

1. **iframe contentDocument 直接访问** — Cross-origin 限制，`iframe.contentDocument` 返回 null
2. **新标签页打开 iframe URL** — `page.context.new_page()` + `goto(iframe_src)` 页面渲染为空，SPA 需要父页面的 session 上下文
3. **轮询 iframe innerText** — 每 2 秒检查一次，等待 30 秒，始终 0 chars。内容是 Canvas 绘制，不是 DOM 节点
4. **Playwright frame_locator().inner_text()** — 能定位到 iframe 的 body 元素，但 textContent 只有一段 Vite legacy 加载脚本，无简历文本

## Solution

截图简历弹窗，使用 GLM-4.6V Vision API（智谱AI）进行 OCR/结构化识别：

```python
# recruiter/collector/browser_collector.py
def _extract_full_resume(self) -> str:
    page = self.browser._ensure_connected()

    # 点击在线简历按钮
    btn = page.locator('.resume-btn-online').first
    if not btn.is_visible(timeout=2000):
        return ""
    btn.click()

    # 等待简历弹窗 Canvas 渲染完成
    page.wait_for_selector('.resume-detail, .boss-dialog', timeout=8000)
    time.sleep(3)  # Canvas 渲染需要额外时间

    # 截图
    page.screenshot(path=screenshot_path)
    self._close_resume_dialog(page)

    # Vision API 识别
    analyzer = VisionAnalyzer()
    return analyzer.extract_resume_from_screenshot(screenshot_path)
```

```python
# recruiter/engine/vision.py
class VisionAnalyzer:
    def __init__(self):
        self.client = OpenAI(
            api_key=config.LLM_VISION_API_KEY,
            base_url="https://open.bigmodel.cn/api/paas/v4/",
        )
        self.model = config.LLM_VISION_MODEL  # glm-4.6v-flash

    def _call_vision(self, image_data: str, prompt: str) -> str | None:
        resp = self.client.chat.completions.create(
            model=self.model,
            max_tokens=4096,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{image_data}"},
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        return resp.choices[0].message.content.strip()
```

实测结果：成功提取 650+ 字符的结构化简历（姓名、技能栈、工作经历、学历、期望薪资）。

## Why This Works

Boss直聘使用 WebAssembly Canvas 渲染简历是一种反爬策略——文本只以像素形式存在于 Canvas 元素上，永远不会出现在 DOM 文本节点中。这通过其 API 响应中的 `chat_online_resume_wasm_canvas` 配置开关得到确认。

DOM 文本提取（innerText、textContent）从根本上就是错误的方法。正确做法是承认"文本在像素里"，用截图捕获像素，再用 Vision 模型从像素中识别文本。GLM-4.6V-flash（免费版）的 OCR 精度足够用于结构化简历提取。

## Prevention

- **检查 Canvas 渲染**：在假设 DOM 提取可行之前，先检查页面是否使用 Canvas/WebAssembly 渲染。快速检查方法：`document.querySelector('canvas')` 是否存在
- **关注反爬配置**：Boss直聘 API 响应中的 `chat_online_resume_wasm_canvas` 开关标识了这种行为，可以通过 API 拦截提前发现
- **Vision API 兜底应作为标准模式**：对于任何反爬 Canvas 渲染的网站，截图 + Vision API 都应该是降级策略的一部分
- **三层降级策略**：API 拦截 → DOM 解析 → 截图 Vision，确保总有一种方式能拿到数据

## Related Issues

- README.md 中的三层数据获取策略章节描述了整体降级架构
- `recruiter/engine/vision.py` 同时支持候选人列表的视觉识别（第三层兜底）和简历内容识别
- Vision 模块尚无单元测试（`tests/test_vision.py` 缺失）
