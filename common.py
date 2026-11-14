#!/usr/bin/env python3
import csv
import io
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.path.join(SCRIPT_DIR, "yt_fb_crosspost.db")
GRAPH_API_VERSION = "v21.0"

DEFAULT_FFMPEG_SETTINGS = {
    "scale_vertical": True,    # 1080x1920 vertical format
    "mirror": True,            # Horizontal flip (hflip)
    "speed": 1.04,             # 1.04x speed + audio pitch shift
    "zoom_crop_pct": 4,        # 4% zoom crop
    "watermark": "",           # Optional text overlay
}

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def get_db(path=DEFAULT_DB_PATH):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    return conn


def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS pages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        page_name TEXT NOT NULL,
        page_id TEXT NOT NULL UNIQUE,
        page_token TEXT NOT NULL DEFAULT '',
        youtube_channel_name TEXT NOT NULL DEFAULT 'YouTube Channel',
        youtube_shorts_url TEXT NOT NULL DEFAULT '',
        posts_per_day INTEGER NOT NULL DEFAULT 1 CHECK (posts_per_day IN (1, 2)),
        active INTEGER NOT NULL DEFAULT 1,
        ffmpeg_overrides TEXT DEFAULT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS posted_videos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        page_id TEXT NOT NULL,
        youtube_video_id TEXT NOT NULL,
        youtube_title TEXT DEFAULT '',
        facebook_reel_id TEXT DEFAULT NULL,
        facebook_url TEXT DEFAULT NULL,
        status TEXT NOT NULL DEFAULT 'success',
        error TEXT DEFAULT NULL,
        posted_at TEXT DEFAULT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (page_id, youtube_video_id)
    );
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """)
    conn.commit()


def add_page(conn, page):
    cur = conn.execute(
        """INSERT INTO pages (page_name, page_id, page_token,
               youtube_channel_name, youtube_shorts_url,
               posts_per_day, active, ffmpeg_overrides)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (page["page_name"], page["page_id"], page.get("page_token", ""),
         page.get("youtube_channel_name", "YouTube Channel"),
         page["youtube_shorts_url"],
         int(page.get("posts_per_day", 1)), int(page.get("active", 1)),
         page.get("ffmpeg_overrides")))
    conn.commit()
    return cur.lastrowid


def update_page(conn, row_id, fields):
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE pages SET {sets}, updated_at = datetime('now') "
                 f"WHERE id = ?", (*fields.values(), row_id))
    conn.commit()


def delete_page(conn, row_id):
    conn.execute("DELETE FROM pages WHERE id = ?", (row_id,))
    conn.commit()


def list_pages(conn, active_only=False):
    q = "SELECT * FROM pages ORDER BY page_name"
    if active_only:
        q = "SELECT * FROM pages WHERE active = 1 ORDER BY page_name"
    return [dict(r) for r in conn.execute(q)]


def get_page(conn, row_id):
    row = conn.execute("SELECT * FROM pages WHERE id = ?", (row_id,)).fetchone()
    return dict(row) if row else None


def already_posted(conn, page_id, youtube_video_id):
    row = conn.execute(
        "SELECT status FROM posted_videos "
        "WHERE page_id = ? AND youtube_video_id = ?",
        (page_id, youtube_video_id)).fetchone()
    return bool(row and row["status"] == "success")


def record_attempt(conn, page_id, youtube_video_id, youtube_title="",
                   status="success", facebook_reel_id=None, facebook_url=None,
                   error=None):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO posted_videos
               (page_id, youtube_video_id, youtube_title, facebook_reel_id,
                facebook_url, status, error, posted_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(page_id, youtube_video_id) DO UPDATE SET
               youtube_title = excluded.youtube_title,
               facebook_reel_id = excluded.facebook_reel_id,
               facebook_url = excluded.facebook_url,
               status = excluded.status,
               error = excluded.error,
               posted_at = excluded.posted_at,
               updated_at = excluded.updated_at""",
        (page_id, youtube_video_id, youtube_title, facebook_reel_id,
         facebook_url, status, error,
         now if status == "success" else None, now))
    conn.commit()


def list_history(conn, page_id=None, status=None, limit=500):
    q = ("SELECT pv.*, p.page_name FROM posted_videos pv "
         "LEFT JOIN pages p ON p.page_id = pv.page_id WHERE 1=1")
    args = []
    if page_id:
        q += " AND pv.page_id = ?"
        args.append(page_id)
    if status:
        q += " AND pv.status = ?"
        args.append(status)
    q += " ORDER BY pv.updated_at DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(q, args)]


def history_counts(conn):
    out = {}
    for r in conn.execute(
            "SELECT page_id, status, COUNT(*) c FROM posted_videos "
            "GROUP BY page_id, status"):
        out.setdefault(r["page_id"], {})[r["status"]] = r["c"]
    return out


PAGE_CSV_FIELDS = ["page_name", "page_id", "page_token",
                   "youtube_channel_name", "youtube_shorts_url",
                   "posts_per_day", "active"]


def pages_to_csv(pages):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=PAGE_CSV_FIELDS)
    w.writeheader()
    for p in pages:
        w.writerow({f: p.get(f, "") for f in PAGE_CSV_FIELDS})
    return buf.getvalue()


def pages_from_csv(csv_text, conn=None):
    reader = csv.DictReader(io.StringIO(csv_text))
    pages, errors = [], []
    for i, row in enumerate(reader, start=2):
        try:
            page = {
                "page_name": (row.get("page_name") or "").strip(),
                "page_id": (row.get("page_id") or "").strip(),
                "page_token": (row.get("page_token") or "").strip(),
                "youtube_channel_name": (row.get("youtube_channel_name") or "Channel").strip(),
                "youtube_shorts_url": (row.get("youtube_shorts_url") or "").strip(),
                "posts_per_day": int(row.get("posts_per_day") or 1),
                "active": int(row.get("active") or 1),
            }
            if not page["page_name"] or not page["page_id"] or not page["youtube_shorts_url"]:
                raise ValueError("page_name, page_id aur youtube_shorts_url zaroori hain")
            if page["posts_per_day"] not in (1, 2):
                raise ValueError("posts_per_day sirf 1 ya 2 ho sakta hai")
            pages.append(page)
        except Exception as e:
            errors.append(f"Row {i}: {e}")
    if conn is not None:
        for page in pages:
            try:
                add_page(conn, page)
            except sqlite3.IntegrityError:
                errors.append(f"Page {page['page_id']} pehle se mojood hai — skip.")
            except Exception as e:
                errors.append(f"Page {page['page_id']}: {e}")
    return pages, errors


def get_settings(conn):
    row = conn.execute("SELECT value FROM settings WHERE key = 'ffmpeg'").fetchone()
    if row:
        try:
            merged = dict(DEFAULT_FFMPEG_SETTINGS)
            merged.update(json.loads(row["value"]))
            return merged
        except (ValueError, TypeError):
            pass
    return dict(DEFAULT_FFMPEG_SETTINGS)


def save_settings(conn, settings):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('ffmpeg', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (json.dumps(settings),))
    conn.commit()


def effective_ffmpeg_settings(conn, page):
    settings = get_settings(conn)
    overrides = page.get("ffmpeg_overrides")
    if overrides:
        try:
            settings.update(json.loads(overrides))
        except (ValueError, TypeError):
            pass
    return settings


def build_ffmpeg_cmd(input_path, output_path, settings=None):
    s = dict(DEFAULT_FFMPEG_SETTINGS)
    if settings:
        s.update(settings)
    speed = max(0.5, min(2.0, float(s.get("speed") or 1.0)))
    zoom = max(0, min(20, float(s.get("zoom_crop_pct") or 0)))

    video_filters = []
    if s.get("mirror"):
        video_filters.append("hflip")
    if zoom > 0:
        f = 1 - zoom / 100.0
        video_filters.append(f"crop=iw*{f:.3f}:ih*{f:.3f}")
    if s.get("scale_vertical"):
        video_filters.append("scale=1080:1920:force_original_aspect_ratio=increase")
        video_filters.append("crop=1080:1920")
        video_filters.append("setsar=1")
    if speed != 1.0:
        video_filters.append(f"setpts={1.0 / speed:.4f}*PTS")

    watermark = (s.get("watermark") or "").strip()
    if watermark:
        draw = (
            "drawtext=text='{text}':fontcolor=white:fontsize=48:"
            "x=(w-text_w)/2:y=h-140:box=1:boxcolor=black@0.5:boxborderw=20"
        ).format(text=watermark.replace("'", "\\'").replace(":", "\\:"))
        if os.path.exists(FONT_PATH):
            draw = draw.replace("drawtext=", f"drawtext=fontfile={FONT_PATH}:", 1)
        video_filters.append(draw)

    cmd = ["ffmpeg", "-y", "-i", input_path]
    if video_filters:
        cmd += ["-vf", ",".join(video_filters)]
    if speed != 1.0:
        cmd += ["-af", f"atempo={speed:.4f}"]
    cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", output_path]
    return cmd


def run_ffmpeg(cmd, timeout=600):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "ffmpeg timeout."
    if proc.returncode != 0:
        return False, f"ffmpeg failed: {(proc.stderr or '')[-500:]}"
    return True, "ffmpeg OK"


def mask_token(token):
    if not token:
        return "(khali)"
    return "****" + token[-4:] if len(token) > 4 else "****"
