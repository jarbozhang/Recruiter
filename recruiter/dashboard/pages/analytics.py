"""数据分析面板

转化漏斗、回复率、匹配分布、每日趋势等。
"""

import streamlit as st
from recruiter.db.models import Database
from recruiter import config

st.set_page_config(page_title="数据分析", page_icon="📊", layout="wide")
st.title("📊 数据分析")

db = Database(config.DB_PATH)

# === 核心指标 ===
candidates = db.list_candidates(limit=99999)
conversations = db.list_conversations(limit=99999)
match_results = db.get_match_results()
jobs = db.list_jobs()

total_candidates = len(candidates)
total_matched = len(match_results)
matched_qualified = len([m for m in match_results if m["score"] >= config.MATCH_THRESHOLD_INITIAL])
total_conversations = len(conversations)

# 按状态统计
status_counts = {}
for c in conversations:
    s = c["status"]
    status_counts[s] = status_counts.get(s, 0) + 1

sent = status_counts.get("sent", 0) + status_counts.get("replied", 0)
replied = status_counts.get("replied", 0)

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("候选人总数", total_candidates)
col2.metric("已匹配", total_matched)
col3.metric("达标", matched_qualified)
col4.metric("已发送", sent)
col5.metric("已回复", replied, f"{replied/sent*100:.0f}%" if sent > 0 else "0%")

st.markdown("---")

# === 转化漏斗 ===
st.subheader("转化漏斗")

funnel_data = {
    "采集": total_candidates,
    "匹配达标": matched_qualified,
    "消息生成": total_conversations,
    "已发送": sent,
    "已回复": replied,
}

# 用柱状图模拟漏斗
import pandas as pd

funnel_df = pd.DataFrame({
    "阶段": list(funnel_data.keys()),
    "数量": list(funnel_data.values()),
})

st.bar_chart(funnel_df.set_index("阶段"))

# 转化率
if total_candidates > 0:
    st.markdown("**转化率：**")
    stages = list(funnel_data.items())
    rates = []
    for i in range(1, len(stages)):
        prev_name, prev_val = stages[i-1]
        curr_name, curr_val = stages[i]
        rate = curr_val / prev_val * 100 if prev_val > 0 else 0
        rates.append(f"{prev_name} → {curr_name}: **{rate:.1f}%**")
    st.markdown(" | ".join(rates))

st.markdown("---")

# === 匹配分数分布 ===
st.subheader("匹配分数分布")

if match_results:
    scores = [m["score"] for m in match_results if m["score"] >= 0]
    if scores:
        score_df = pd.DataFrame({"分数": scores})
        st.bar_chart(score_df["分数"].value_counts().sort_index())
        col_a, col_b, col_c = st.columns(3)
        col_a.metric("平均分", f"{sum(scores)/len(scores):.1f}")
        col_b.metric("最高分", max(scores))
        col_c.metric("最低分", min(scores))
else:
    st.info("暂无匹配数据")

st.markdown("---")

# === 对话状态分布 ===
st.subheader("对话状态分布")

if status_counts:
    status_df = pd.DataFrame({
        "状态": list(status_counts.keys()),
        "数量": list(status_counts.values()),
    })
    st.bar_chart(status_df.set_index("状态"))
else:
    st.info("暂无对话数据")

st.markdown("---")

# === 候选人来源分布 ===
st.subheader("候选人来源")

source_counts = {}
for c in candidates:
    s = c.get("source", "unknown")
    source_counts[s] = source_counts.get(s, 0) + 1

if source_counts:
    source_df = pd.DataFrame({
        "来源": list(source_counts.keys()),
        "数量": list(source_counts.values()),
    })
    st.bar_chart(source_df.set_index("来源"))

# === 职位概览 ===
st.subheader("职位概览")

if jobs:
    for job in jobs:
        job_matches = [m for m in match_results if m.get("job_id") == job["id"]]
        job_convs = [c for c in conversations if c.get("job_id") == job["id"]]
        job_replied = len([c for c in job_convs if c["status"] == "replied"])

        with st.expander(f"{job['title']} (ID: {job['id']})"):
            jc1, jc2, jc3, jc4 = st.columns(4)
            jc1.metric("匹配数", len(job_matches))
            jc2.metric("对话数", len(job_convs))
            jc3.metric("回复数", job_replied)
            avg = sum(m["score"] for m in job_matches if m["score"] >= 0) / len(job_matches) if job_matches else 0
            jc4.metric("平均匹配分", f"{avg:.1f}")
else:
    st.info("暂无职位数据")

db.close()
