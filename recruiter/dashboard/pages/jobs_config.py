import streamlit as st

from recruiter.config import DB_PATH, MATCH_WEIGHTS
from recruiter.db.models import Database

st.header("岗位配置")

db = Database(DB_PATH)

# 新建岗位
st.subheader("新建岗位")
with st.form("new_job"):
    title = st.text_input("岗位名称")
    jd = st.text_area("职位描述 (JD)", height=200)
    threshold = st.slider("匹配度阈值", 0, 100, 60)
    submitted = st.form_submit_button("创建岗位")
    if submitted and title and jd:
        job_id = db.create_job(title, jd, match_threshold=threshold)
        st.success(f"岗位创建成功 (ID: {job_id})")
        st.rerun()

st.markdown("---")

# 现有岗位
st.subheader("现有岗位")

tab_active, tab_all = st.tabs(["活跃岗位", "全部岗位"])

with tab_active:
    jobs = db.list_jobs(status="active")
    if not jobs:
        st.info("暂无活跃岗位")
    for job in jobs:
        with st.expander(f"[{job['id']}] {job['title']}"):
            st.write(f"**阈值:** {job['match_threshold']}分")
            st.write(f"**创建时间:** {job['created_at']}")
            st.text_area("JD", job["jd"], height=150, disabled=True, key=f"jd_{job['id']}")

            col1, col2 = st.columns(2)
            with col1:
                new_threshold = st.number_input(
                    "修改阈值", 0, 100, job["match_threshold"],
                    key=f"thresh_{job['id']}"
                )
                if st.button("保存阈值", key=f"save_thresh_{job['id']}"):
                    db.conn.execute(
                        "UPDATE jobs SET match_threshold = ? WHERE id = ?",
                        (new_threshold, job["id"]),
                    )
                    db.conn.commit()
                    st.success("阈值已更新")
                    st.rerun()
            with col2:
                if st.button("暂停岗位", key=f"pause_{job['id']}"):
                    db.update_job_status(job["id"], "paused")
                    st.warning("岗位已暂停")
                    st.rerun()

with tab_all:
    # 查询所有状态的岗位
    all_jobs = []
    for status in ["active", "paused", "closed"]:
        all_jobs.extend(db.list_jobs(status=status))
    if not all_jobs:
        st.info("暂无岗位")
    for job in all_jobs:
        st.write(f"[{job['id']}] **{job['title']}** — 状态: {job['status']} | 阈值: {job['match_threshold']}")

st.markdown("---")

# 评分维度权重（只读展示，修改需改 config/环境变量）
st.subheader("评分维度权重")
st.write("当前权重配置（通过环境变量修改）：")
for dim, weight in MATCH_WEIGHTS.items():
    st.write(f"- **{dim}**: {weight}%")

db.close()
