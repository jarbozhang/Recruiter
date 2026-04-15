import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中，使 pages 能 import recruiter 模块
_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import streamlit as st

st.set_page_config(page_title="AI Recruiter", page_icon="🎯", layout="wide")

st.title("AI 智能招聘管理面板")
st.markdown("---")

st.markdown("""
### 功能导航

- **候选人总览** — 查看今日新增候选人、匹配度评分、AI 推荐理由
- **审核队列** — 审核 AI 生成的话术，批量通过/修改/拒绝
- **岗位配置** — 管理岗位 JD、匹配阈值、评分维度权重
- **沟通状态** — 查看候选人沟通进度和回复状态

请从左侧导航栏选择页面。
""")
