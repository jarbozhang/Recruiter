# AI Recruiter Agent

Boss直聘智能招聘自动化系统。自动采集候选人、AI 匹配评分、生成个性化消息、自动发送，全流程可通过 CLI 或 Dashboard 操作。

## 架构

```
候选人采集 → AI简历匹配 → 消息生成 → 人工审核 → 自动发送 → 回复检测
     │            │           │          │          │          │
 Playwright    Qwen API    Claude API  Dashboard  Playwright  Playwright
 API拦截/DOM                                      模拟操作    API拦截
```

### 三层数据获取策略

```
层级1: API 拦截 ──成功──→ 返回数据
         │
        失败
         ↓
层级2: DOM 解析 ──成功──→ 返回数据
         │
        失败
         ↓
层级3: 截图视觉分析 ──成功──→ 返回数据 + 生成选择器修复报告
         │                         ↑
        失败                    反哺修复层级1/2
         ↓
      返回空列表
```

| 层级 | 方式 | 依赖 | 数据量 |
|------|------|------|--------|
| 1 | **API 拦截** | Playwright `page.on("response")` 拦截 `getBossFriendListV2` | 60+ 人，30+ 字段 |
| 2 | **DOM 解析** | JS `querySelectorAll` 从页面 DOM 提取 | 当前页可见人数，2 字段 |
| 3 | **截图视觉分析** | GLM-4.6V Vision API 识别截图内容 | 可见人数，3 字段 |

**自愈机制**：当层级 3（视觉分析）成功后，会生成 `data/selector_report.json` 报告，包含 Claude 对页面 DOM 结构变化的分析和新选择器推测，用于修复失败的 API 拦截或 DOM 解析。

### 浏览器驱动插件架构

所有浏览器操作通过统一的 `BrowserDriver` 抽象接口，业务代码不依赖具体实现：

```
BrowserDriver (抽象接口)
├── PlaywrightAdsPowerDriver  ← 推荐，支持 API 拦截
├── AdsPowerDriver (Selenium)
└── BBBrowserDriver (bb-browser CLI)
```

使用 [AdsPower](https://www.adspower.com) 指纹浏览器绕过 Boss直聘的反爬检测。AdsPower 的 `/api/v1/browser/start` 返回 `ws.puppeteer` WebSocket 地址，Playwright 通过 `connect_over_cdp()` 连接。

## 安装

```bash
git clone https://github.com/jarbozhang/Recruiter.git
cd Recruiter
pip install -r requirements.txt
```

### 前置依赖

- Python 3.11+
- [AdsPower](https://www.adspower.com) 指纹浏览器（本地运行）
- Qwen API Key（简历匹配）
- Claude API Key（消息生成）

### 配置

```bash
cp .env.example .env
# 编辑 .env 填入你的配置
```

关键配置项：

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `BROWSER_DRIVER` | 浏览器驱动类型 | `playwright` |
| `ADSPOWER_API_KEY` | AdsPower API 密钥 | |
| `ADSPOWER_PROFILE_ID` | AdsPower 浏览器 profile ID | |
| `LLM_MATCH_API_KEY` | Qwen API 密钥（简历匹配） | |
| `LLM_CHAT_API_KEY` | Claude API 密钥（消息生成） | |
| `OP_HOURLY_LIMIT` | 每小时操作上限 | `30` |
| `OP_DAILY_LIMIT` | 每日操作上限 | `150` |
| `CB_FAILURE_THRESHOLD` | 熔断器连续失败阈值 | `3` |

## 使用

### CLI

```bash
# 采集候选人列表
python -m recruiter.main collect

# 采集候选人简历详情
python -m recruiter.main resumes --limit 50

# AI 简历匹配（需要先创建职位）
python -m recruiter.main match <job_id>

# 生成个性化招呼消息
python -m recruiter.main generate <job_id>

# 发送已审核的消息
python -m recruiter.main send

# 检测候选人回复
python -m recruiter.main replies

# 全流程执行
python -m recruiter.main run <job_id>
python -m recruiter.main run <job_id> --auto-approve  # 跳过人工审核

# 定时调度（Ctrl+C 停止）
python -m recruiter.main scheduler <job_id> \
  --collect-interval 60 \
  --reply-interval 10 \
  --send-interval 30

# 查看系统状态
python -m recruiter.main status
```

### Dashboard

```bash
streamlit run recruiter/dashboard/app.py
```

4 个页面：候选人列表、审核队列、职位管理、对话记录。

## 项目结构

```
recruiter/
├── browser/                 # 浏览器驱动层
│   ├── base.py              # BrowserDriver 抽象接口 + Element 数据类
│   ├── playwright_driver.py # Playwright + AdsPower（推荐）
│   ├── adspower.py          # Selenium + AdsPower
│   └── bb_browser.py        # bb-browser CLI
├── collector/
│   └── browser_collector.py # 候选人采集（API 拦截 + DOM 兜底）
├── engine/
│   ├── matcher.py           # AI 简历匹配（Qwen API）
│   ├── messenger.py         # 消息生成（Claude API）
│   └── vision.py            # 截图视觉分析（Claude Vision，层级3兜底）
├── operator/boss/
│   ├── sender.py            # 消息发送 + 熔断器 + 频率控制
│   └── reply_monitor.py     # 回复检测
├── dashboard/               # Streamlit Dashboard
│   ├── app.py
│   └── pages/
├── db/
│   ├── models.py            # SQLite 数据库操作
│   └── schema.sql           # 表结构
├── config.py                # 配置（环境变量）
├── pipeline.py              # 主流程编排
├── scheduler.py             # 定时调度器
└── main.py                  # CLI 入口
```

## 安全机制

| 机制 | 说明 |
|------|------|
| **频率控制** | 操作间随机间隔 30-120s，每小时上限 30 次，每日上限 150 次 |
| **熔断器** | 连续失败 3 次后暂停 2 小时，自动恢复 |
| **指纹浏览器** | 通过 AdsPower 伪装浏览器指纹，绕过反爬检测 |
| **状态机** | 对话状态严格按 pending→approved→sending→sent/failed 转换 |
| **人工审核** | 消息生成后默认进入 pending 状态，需人工审核或 --auto-approve |

## 测试

```bash
python -m pytest tests/ -v
```

87 个单元测试，覆盖所有模块。使用 mock 对象，不依赖真实浏览器或 API。
