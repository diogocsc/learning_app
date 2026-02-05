# admin_pages.py
import os
import streamlit as st
from db import (
    get_connection,
    admin_log,
    delete_uploaded_file_and_cards,
)
from auth import hash_password

def render_admin_users(real_user_id: int):
    st.title("🔐 Admin – User Management")

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, username FROM users ORDER BY username")
    users = cur.fetchall()

    if st.button("⬅ Back to App"):
        st.session_state.view = "main"
        st.rerun()

    st.markdown("### 👥 All Users")

    for uid, uname in users:
        st.markdown(f"#### User ID {uid}: {uname}")
        col1, col2, col3 = st.columns(3)

        # Rename user
        with col1:
            new_name = st.text_input(
                f"Rename {uname}",
                value=uname,
                key=f"rename_{uid}",
            )
            if st.button(f"Save username {uid}", key=f"btn_rename_{uid}"):
                cur.execute("UPDATE users SET username=? WHERE id=?", (new_name, uid))
                conn.commit()
                admin_log(real_user_id, uid, f"Renamed user '{uname}' to '{new_name}'")
                st.success("Username updated.")
                st.rerun()

        # Change password
        with col2:
            new_pw = st.text_input(
                f"New password for {uname}",
                type="password",
                key=f"newpw_{uid}",
            )
            if st.button(f"Change password {uid}", key=f"btn_pw_{uid}"):
                if len(new_pw) < 6:
                    st.error("Password must be at least 6 characters.")
                else:
                    pw_hash = hash_password(new_pw)
                    cur.execute("UPDATE users SET password_hash=? WHERE id=?", (pw_hash, uid))
                    conn.commit()
                    admin_log(real_user_id, uid, "Admin changed user password")
                    st.success("Password changed.")
                    st.rerun()

        # Delete user
        with col3:
            if uname == "admin":
                st.write("(cannot delete admin)")
            else:
                delete_key = f"delete_user_{uid}"
                confirm_key = f"confirm_delete_user_{uid}"

                if st.button("Delete user", key=delete_key):
                    st.session_state[confirm_key] = True

                if st.session_state.get(confirm_key):
                    st.warning(f"Delete user **{uname}** and ALL their data?")
                    c1, c2 = st.columns(2)

                    with c1:
                        if st.button("Yes, delete", key=f"yes_{delete_key}"):
                            # Delete attempts
                            cur.execute("DELETE FROM card_attempts WHERE user_id=?", (uid,))

                            # Delete uploaded files from disk
                            cur.execute("SELECT stored_path FROM uploaded_files WHERE user_id=?", (uid,))
                            for (path,) in cur.fetchall():
                                if path and os.path.exists(path):
                                    try:
                                        os.remove(path)
                                    except OSError:
                                        pass

                            # Delete uploaded files metadata
                            cur.execute("DELETE FROM uploaded_files WHERE user_id=?", (uid,))
                            # (add any other cascading deletes you already had here)
                            # Finally delete user
                            cur.execute("DELETE FROM users WHERE id=?", (uid,))
                            conn.commit()

                            admin_log(real_user_id, uid, "Deleted user account")
                            st.success("User deleted.")
                            st.session_state.pop(confirm_key, None)
                            st.rerun()

                    with c2:
                        if st.button("Cancel", key=f"cancel_{delete_key}"):
                            st.session_state.pop(confirm_key, None)
                            st.rerun()

    conn.close()