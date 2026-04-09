import streamlit as st

from recruiter.config import DB_PATH
from recruiter.db.models import Database

st.header("沟通状态")

db = Database(DB_PATH)

# 状态统计
statuses = ["pending", "approved", "sending", "sent", "failed", "timeout", "replied"]
cols = st.columns(len(statuses))
for i, status in enumerate(statuses):
    convs = db.list_conversations(status=status, limit=1000)
    with cols[i]:
        st.metric(status, len(convs))

st.markdown("---")

# 筛选
status_filter = st.selectbox("筛选状态", ["全部"] + statuses, index=0)
status_q = None if status_filter == "全部" else status_filter
conversations = db.list_conversations(status=status_q, limit=200)

if not conversations:
    st.info("暂无沟通记录")
else:
    for conv in conversations:
        candidate = db.get_candidate(conv["candidate_id"])
        job = db.get_job(conv["job_id"])
        name = candidate["name"] if candidate else "未知"
        job_title = job["title"] if job else "未知岗位"

        status_emoji = {
            "pending": "⏳", "approved": "✅", "sending": "📤",
            "sent": "📨", "failed": "❌", "timeout": "⏰", "replied": "💬",
        }
        emoji = status_emoji.get(conv["status"], "❓")

        with st.container(border=True):
            st.write(f"{emoji} **{name}** → {job_title} | 状态: **{conv['status']}** | "
                     f"更新: {conv['updated_at']}")

            if conv.get("message"):
                with st.expander("查看消息"):
                    st.write(conv["message"])

            if conv.get("intent"):
                st.write(f"意向度: **{conv['intent']}**")

            # failed 消息可重试
            if conv["status"] == "failed":
                if st.button("重新加入审核队列", key=f"retry_{conv['id']}"):
                    if db.update_conversation_status(conv["id"], "approved"):
                        st.success("已重新加入审核队列")
                        st.rerun()
                    else:
                        st.error("状态更新失败")

db.close()
