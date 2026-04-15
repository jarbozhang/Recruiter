import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

import json

import streamlit as st

from recruiter.config import DB_PATH
from recruiter.db.models import Database

st.header("候选人总览")

db = Database(DB_PATH)

# 筛选条件
col1, col2 = st.columns(2)
with col1:
    platform_filter = st.selectbox("平台", ["全部", "boss", "liepin"], index=0)
with col2:
    source_filter = st.selectbox("来源", ["全部", "inbound", "outbound"], index=0)

platform = None if platform_filter == "全部" else platform_filter
source = None if source_filter == "全部" else source_filter
candidates = db.list_candidates(platform=platform, source=source, limit=200)

st.metric("候选人总数", len(candidates))

if not candidates:
    st.info("暂无候选人数据")
else:
    for c in candidates:
        with st.expander(f"{c['name'] or '未知'} — {c['platform']}:{c['platform_id']}"):
            st.write(f"**来源:** {c['source']}")
            st.write(f"**入库时间:** {c['created_at']}")

            # 查询匹配结果
            matches = db.get_match_results(candidate_id=c["id"])
            if matches:
                for m in matches:
                    job = db.get_job(m["job_id"])
                    job_title = job["title"] if job else "未知岗位"
                    score_color = "🟢" if m["score"] >= 80 else "🟡" if m["score"] >= 60 else "🔴"
                    st.write(f"{score_color} **{job_title}** — 匹配度: **{m['score']}分**")
                    if m.get("reason"):
                        st.write(f"  理由: {m['reason']}")
                    if m.get("dimensions"):
                        dims = m["dimensions"] if isinstance(m["dimensions"], dict) else json.loads(m["dimensions"])
                        st.write(f"  维度: 技术{dims.get('tech_stack', '-')} | 年限{dims.get('years', '-')} | "
                                 f"行业{dims.get('industry', '-')} | 学历{dims.get('education', '-')} | "
                                 f"地域{dims.get('location', '-')}")
            else:
                st.write("暂无匹配评分")

            if c.get("resume_text"):
                st.text_area("简历", c["resume_text"], height=150, disabled=True,
                             key=f"resume_{c['id']}")

db.close()
