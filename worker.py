#!/usr/bin/env python3
import argparse
import json
import os
import random
import sys
import tempfile
import time
import requests
import yt_dlp

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import common

GRAPH_BASE = f"https://graph.facebook.com/{common.GRAPH_API_VERSION}"
DELAY_RANGE = (10, 25)


def log_default(msg):
    print(msg, flush=True)


def resolve_token(page):
    mapping_raw = os.environ.get("FB_PAGE_TOKENS_JSON", "")
    if mapping_raw:
        try:
            mapping = json.loads(mapping_raw)
            if isinstance(mapping, dict) and mapping.get(page["page_id"]):
                return mapping[page["page_id"]]
        except ValueError:
            pass
    if os.environ.get("FB_PAGE_ACCESS_TOKEN"):
        return os.environ["FB_PAGE_ACCESS_TOKEN"]
    return page.get("page_token") or ""


def fetch_and_pick_shorts(conn, page, log=log_default):
    url = (page.get("youtube_shorts_url") or "").strip()
    if not url:
        return [], "YouTube Shorts URL khaali hai."
    if not url.endswith("/shorts") and "/@" in url:
        url = url.rstrip("/") + "/shorts"

    per_day = int(page.get("posts_per_day", 1))
    ydl_opts = {"extract_flat": True, "playlistend": 15, "quiet": True}

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            entries = info.get("entries", []) if info else []
    except Exception as e:
        return [], f"YouTube feed error: {e}"

    picked = []
    for entry in entries:
        if len(picked) >= per_day:
            break
        vid = entry.get("id")
        if not vid:
            continue
        if common.already_posted(conn, page["page_id"], vid):
            log(f"  Skip {vid} — pehle hi post ho chuka hai.")
            continue
        picked.append({
            "video_id": vid,
            "title": entry.get("title") or "Trending Short #reels #viral",
            "youtube_url": f"https://www.youtube.com/shorts/{vid}"
        })
        log(f"  Short select hua: {vid} — {picked[-1]['title']}")
    return picked, None


def download_short(entry, tmpdir, log=log_default):
    outtmpl = os.path.join(tmpdir, "raw.%(ext)s")
    opts = {
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]/best",
        "merge_output_format": "mp4",
        "outtmpl": outtmpl,
        "quiet": True,
        "noplaylist": True,
        "socket_timeout": 30,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([entry["youtube_url"]])
    except Exception as e:
        log(f"  Download fail: {e}")
        return None
    for ext in ("mp4", "webm", "mkv"):
        p = os.path.join(tmpdir, f"raw.{ext}")
        if os.path.exists(p):
            return p
    return None


def publish_reel(page_id, token, video_path, description):
    try:
        r = requests.post(
            f"{GRAPH_BASE}/{page_id}/video_reels",
            data={"upload_phase": "start", "access_token": token},
            timeout=30)
        data = r.json()
    except Exception as e:
        return False, None, f"Reels start fail: {e}"
    if "video_id" not in data or "upload_url" not in data:
        return False, None, f"Reels start error: {data}"

    video_id = data["video_id"]
    upload_url = data["upload_url"]
    file_size = os.path.getsize(video_path)

    try:
        with open(video_path, "rb") as f:
            r = requests.post(
                upload_url,
                headers={"Authorization": f"OAuth {token}",
                         "offset": "0",
                         "file_size": str(file_size)},
                data=f, timeout=300)
    except Exception as e:
        return False, None, f"Binary upload fail: {e}"
    if r.status_code != 200:
        return False, None, f"Binary upload error {r.status_code}: {r.text[:250]}"

    try:
        r = requests.post(
            f"{GRAPH_BASE}/{page_id}/video_reels",
            data={"upload_phase": "finish", "video_id": video_id,
                  "video_state": "PUBLISHED", "description": description[:2200],
                  "access_token": token},
            timeout=30)
        data = r.json()
    except Exception as e:
        return False, None, f"Reels finish fail: {e}"
    if data.get("success") or data.get("id"):
        reel_id = data.get("id") or video_id
        return True, reel_id, f"Published! https://www.facebook.com/reel/{reel_id}"
    return False, None, f"Reels finish error: {data}"


def process_page(conn, page, log=log_default):
    summary = {"page": page["page_name"], "picked": 0, "posted": 0, "failed": 0}
    picked, fetch_err = fetch_and_pick_shorts(conn, page, log)
    if fetch_err:
        log(f"  ERROR: {fetch_err}")
        return summary

    summary["picked"] = len(picked)
    if not picked:
        log("  Koi nayi video nahi mili.")
        return summary

    settings = common.effective_ffmpeg_settings(conn, page)
    token = resolve_token(page)
    if not token:
        log("  ERROR: Page Access Token nahi mila.")
        summary["failed"] += len(picked)
        return summary

    for entry in picked:
        vid = entry["video_id"]
        with tempfile.TemporaryDirectory(prefix="ytfb_") as tmpdir:
            log(f"  Downloading {vid}...")
            raw = download_short(entry, tmpdir, log)
            if not raw:
                common.record_attempt(conn, page["page_id"], vid, entry["title"],
                                      status="failed", error="Download fail")
                summary["failed"] += 1
                continue

            formatted = os.path.join(tmpdir, "formatted.mp4")
            cmd = common.build_ffmpeg_cmd(raw, formatted, settings)
            log("  Editing video (FFmpeg)...")
            ok, msg = common.run_ffmpeg(cmd)
            if not ok:
                common.record_attempt(conn, page["page_id"], vid, entry["title"],
                                      status="failed", error=msg)
                log(f"  ERROR: {msg}")
                summary["failed"] += 1
                continue

            log("  Uploading to Facebook Reels...")
            ok, reel_id, msg = publish_reel(page["page_id"], token, formatted, entry["title"])
            log(f"  {msg}")
            if ok:
                common.record_attempt(conn, page["page_id"], vid, entry["title"],
                                      status="success", facebook_reel_id=reel_id,
                                      facebook_url=f"https://www.facebook.com/reel/{reel_id}")
                summary["posted"] += 1
            else:
                common.record_attempt(conn, page["page_id"], vid, entry["title"],
                                      status="failed", error=msg)
                summary["failed"] += 1
    return summary


def run_batch(conn, pages, log=log_default, delay=True):
    total = {"pages": 0, "picked": 0, "posted": 0, "failed": 0}
    for i, page in enumerate(pages):
        total["pages"] += 1
        log(f"\n=== Page {i + 1}/{len(pages)}: {page['page_name']} ({page['page_id']}) ===")
        try:
            s = process_page(conn, page, log)
            total["picked"] += s["picked"]
            total["posted"] += s["posted"]
            total["failed"] += s["failed"]
        except Exception as e:
            log(f"  ERROR: Page crash: {e}")
            total["failed"] += 1
        if delay and i < len(pages) - 1:
            secs = random.uniform(*DELAY_RANGE)
            log(f"  Agle page se pehle {secs:.0f}s wait...")
            time.sleep(secs)
    return total


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--no-delay", action="store_true")
    imp = sub.add_parser("import-pages")
    imp.add_argument("csv")
    args = parser.parse_args(argv)

    conn = common.get_db()
    if args.cmd == "import-pages":
        with open(args.csv, "r", encoding="utf-8") as f:
            pages, errs = common.pages_from_csv(f.read(), conn=conn)
        print(f"Imported {len(pages)} pages.")
        return 0
    if args.cmd == "run":
        pages = common.list_pages(conn, active_only=True)
        if not pages:
            print("Koi active page nahi mila.")
            return 0
        total = run_batch(conn, pages, delay=not args.no_delay)
        print(f"\nDONE: {total}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
