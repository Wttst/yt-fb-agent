#!/usr/bin/env python3
import json
import os
import sys
import streamlit as st

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import common
import worker

st.set_page_config(page_title="YT → FB Auto Agent", page_icon="🚀", layout="wide")
conn = common.get_db()

st.success("✅ System LIVE hai — Auto-editing aur Direct Facebook Reels Publishing Active.")
st.title("🚀 YT Shorts → FB Reels Auto Agent")

tabs = st.tabs(["📄 Pages (Up to 100)", "▶️ Run & Logs", "📜 History", "⚙️ FFmpeg Settings"])

def page_row_display(p):
    return {
        "ID": p["id"],
        "Page Name": p["page_name"],
        "Page ID": p["page_id"],
        "Token": common.mask_token(p.get("page_token")),
        "YouTube Shorts URL": p.get("youtube_shorts_url") or "",
        "Per Day": p["posts_per_day"],
        "Active": "✅" if p["active"] else "⏸️",
    }

with tabs[0]:
    st.header("Page Manager")
    pages = common.list_pages(conn)
    if pages:
        st.dataframe([page_row_display(p) for p in pages], use_container_width=True, hide_index=True)
    else:
        st.info("Abhi koi page nahi hai — neeche se apna pehla page add karein.")

    st.subheader("➕ Add New Page")
    with st.form("add_page"):
        c1, c2 = st.columns(2)
        page_name = c1.text_input("Page Name (e.g. Keli Clips)")
        page_id = c2.text_input("Facebook Page ID (15 digits)")
        page_token = st.text_input("Facebook Page Access Token", type="password")
        c3, c4 = st.columns(2)
        shorts_url = c3.text_input("YouTube Channel Shorts URL", placeholder="https://www.youtube.com/@ChannelName/shorts")
        per_day = c4.selectbox("Videos Per Day", [1, 2])
        active = st.checkbox("Active", value=True)
        if st.form_submit_button("Save Page"):
            if not page_name.strip() or not page_id.strip() or not shorts_url.strip():
                st.error("Page Name, Page ID, aur YouTube Shorts URL teeno zaroori hain.")
            else:
                try:
                    common.add_page(conn, {
                        "page_name": page_name.strip(),
                        "page_id": page_id.strip(),
                        "page_token": page_token.strip(),
                        "youtube_channel_name": page_name.strip(),
                        "youtube_shorts_url": shorts_url.strip(),
                        "posts_per_day": per_day,
                        "active": 1 if active else 0
                    })
                    st.success(f"Page '{page_name}' add ho gaya!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")

    if pages:
        st.subheader("✏️ Edit / Delete Page")
        sel = st.selectbox("Page chunein", pages, format_func=lambda p: f"{p['page_name']} ({p['page_id']})")
        with st.form("edit_page"):
            c1, c2 = st.columns(2)
            e_name = c1.text_input("Page Name", value=sel["page_name"])
            e_active = c2.checkbox("Active", value=bool(sel["active"]))
            e_token = st.text_input("New Access Token (khaali chhorein agar purana rakhna ho)", type="password")
            e_shorts = st.text_input("YouTube Shorts URL", value=sel.get("youtube_shorts_url") or "")
            e_per_day = st.selectbox("Videos Per Day", [1, 2], index=0 if sel["posts_per_day"] == 1 else 1)
            col1, col2 = st.columns(2)
            if col1.form_submit_button("💾 Update"):
                fields = {
                    "page_name": e_name.strip(),
                    "active": 1 if e_active else 0,
                    "youtube_shorts_url": e_shorts.strip(),
                    "posts_per_day": e_per_day
                }
                if e_token.strip():
                    fields["page_token"] = e_token.strip()
                common.update_page(conn, sel["id"], fields)
                st.success("Updated!")
                st.rerun()
            if col2.form_submit_button("🗑️ Delete"):
                common.delete_page(conn, sel["id"])
                st.warning("Deleted!")
                st.rerun()

    st.subheader("📂 Bulk CSV Import / Export (For 100 Pages)")
    up = st.file_uploader("Upload CSV", type=["csv"])
    if up is not None:
        imported, errs = common.pages_from_csv(up.read().decode("utf-8"), conn=conn)
        st.success(f"{len(imported)} pages imported.")
        for e in errs:
            st.warning(e)
        st.rerun()
    st.download_button("⬇️ Download CSV Template / Backup", data=common.pages_to_csv(pages), file_name="pages.csv", mime="text/csv")

with tabs[1]:
    st.header("Manual Run & Live Logs")
    pages = common.list_pages(conn)
    active_pages = [p for p in pages if p["active"]]
    if not active_pages:
        st.info("Pehle Pages tab se koi active page add karein.")
    else:
        target = st.selectbox("Select Target", ["ALL active pages"] + [f"{p['page_name']} ({p['page_id']})" for p in active_pages])
        if st.button("▶️ Run Now & Publish to Facebook", type="primary"):
            chosen = active_pages if target == "ALL active pages" else [p for p in active_pages if f"{p['page_name']} ({p['page_id']})" == target]
            logs = []
            box = st.empty()
            def log_cb(msg):
                logs.append(msg)
                box.code("\n".join(logs[-80:]), language="text")
            total = worker.run_batch(conn, chosen, log_cb, delay=True)
            st.success(f"Done! Pages: {total['pages']} | Published: {total['posted']} | Failed: {total['failed']}")

with tabs[2]:
    st.header("Upload History")
    rows = common.list_history(conn)
    if rows:
        st.dataframe([{
            "Page": r.get("page_name") or r["page_id"],
            "YT Video ID": r["youtube_video_id"],
            "Title": r.get("youtube_title") or "",
            "Status": r["status"],
            "Reel URL": r.get("facebook_url") or "",
            "Error": r.get("error") or "",
            "Time": r["updated_at"]
        } for r in rows], use_container_width=True, hide_index=True)
    else:
        st.info("Abhi koi history nahi hai.")

with tabs[3]:
    st.header("FFmpeg Auto-Edit Settings")
    current = common.get_settings(conn)
    with st.form("global_settings"):
        c1, c2 = st.columns(2)
        scale = c1.checkbox("Vertical 1080x1920 Scale", value=current["scale_vertical"])
        mirror = c2.checkbox("Horizontal Flip (Mirror)", value=current["mirror"])
        speed = st.slider("Playback Speed", 0.5, 2.0, float(current["speed"]), 0.01)
        zoom = st.slider("Zoom Crop %", 0, 20, int(current["zoom_crop_pct"]))
        watermark = st.text_input("Optional Text Watermark", value=current["watermark"])
        if st.form_submit_button("💾 Save Global Settings"):
            common.save_settings(conn, {
                "scale_vertical": scale,
                "mirror": mirror,
                "speed": speed,
                "zoom_crop_pct": zoom,
                "watermark": watermark.strip()
            })
            st.success("Settings saved!")
            st.rerun()
