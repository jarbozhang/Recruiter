import streamlit as st

from recruiter.config import DB_PATH
from recruiter.db.models import Database

st.header("审核队列")

db = Database(DB_PATH)

pending = db.list_conversations(status="pending", limit=200)

st.metric("待审核", len(pending))

if not pending:
    st.info("当前没有待审核的消息")
else:
    # 批量操作
    st.subheader("批量操作")
    col_approve, col_reject = st.columns(2)

    selected_ids = []
    for conv in pending:
        candidate = db.get_candidate(conv["candidate_id"])
        job = db.get_job(conv["job_id"])
        name = candidate["name"] if candidate else "未知"
        job_title = job["title"] if job else "未知岗位"

        with st.container(border=True):
            cb_col, info_col = st.columns([0.05, 0.95])
            with cb_col:
                selected = st.checkbox("", key=f"sel_{conv['id']}")
                if selected:
                    selected_ids.append(conv["id"])
            with info_col:
                st.write(f"**{name}** → {job_title}")
                st.text_area(
                    "话术内容",
                    conv["message"],
                    height=100,
                    key=f"msg_{conv['id']}",
                    disabled=True,
                )

    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("✅ 批量通过", type="primary"):
            success = 0
            for cid in selected_ids:
                if db.update_conversation_status(cid, "approved"):
                    success += 1
            st.success(f"已通过 {success} 条消息")
            st.rerun()
    with col2:
        if st.button("❌ 批量拒绝"):
            # 拒绝 = 删除 conversation 记录（或标记为特殊状态）
            # MVP 直接删除
            for cid in selected_ids:
                db.conn.execute("DELETE FROM conversations WHERE id = ?", (cid,))
            db.conn.commit()
            st.warning(f"已拒绝 {len(selected_ids)} 条消息")
            st.rerun()
    with col3:
        if st.button("🔄 全选通过"):
            success = 0
            for conv in pending:
                if db.update_conversation_status(conv["id"], "approved"):
                    success += 1
            st.success(f"已全部通过 {success} 条消息")
            st.rerun()

db.close()
