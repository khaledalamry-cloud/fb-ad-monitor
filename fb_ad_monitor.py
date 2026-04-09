#!/usr/bin/env python3
"""
Facebook Ad Library Monitor → Slack (via Apify)
================================================
Uses Apify's Facebook Ad Library Scraper actor to fetch competitor ads
and posts new ones to Slack channels. Runs every hour.

Channels:
  - Default brands  → SLACK_VIDEO_CHANNEL (videos) / SLACK_STATIC_CHANNEL (images)
  - Brands with slack_channel set → their dedicated channel

Usage:
  python3 fb_ad_monitor.py           # run once immediately
  python3 fb_ad_monitor.py --schedule  # run every RUN_INTERVAL_HOURS hours
"""

import os
import json
import time
import sqlite3
import logging
import argparse
import requests
from datetime import datetime, timezone
from pathlib import Path

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from dotenv import load_dotenv

load_dotenv()

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
BASE_DIR        = Path(__file__).parent
DB_PATH         = Path(os.getenv("DB_PATH", str(BASE_DIR / "seen_ads.db")))
BRANDS_FILE     = BASE_DIR / "brands.json"

SLACK_TOKEN     = os.getenv("SLACK_BOT_TOKEN", "")
VIDEO_CHANNEL   = os.getenv("SLACK_VIDEO_CHANNEL", "")
STATIC_CHANNEL  = os.getenv("SLACK_STATIC_CHANNEL", "")
APIFY_TOKEN     = os.getenv("APIFY_TOKEN", "")

APIFY_ACTOR_ID  = "curious_coder~facebook-ads-library-scraper"
APIFY_BASE      = "https://api.apify.com/v2"

RUN_INTERVAL_HOURS = int(os.getenv("RUN_INTERVAL_HOURS", "12"))

# ─── Database ─────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_ads (
            ad_id      TEXT PRIMARY KEY,
            brand      TEXT,
            ad_type    TEXT,
            posted_at  TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_summary (
            summary_date  TEXT PRIMARY KEY,
            posted_at     TEXT
        )
    """)
    conn.commit()
    conn.close()

def already_posted_daily_summary() -> bool:
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT 1 FROM daily_summary WHERE summary_date=?", (today,)).fetchone()
    conn.close()
    return row is not None

def mark_daily_summary_posted():
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO daily_summary VALUES (?,?)",
        (today, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()

def is_seen(ad_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT 1 FROM seen_ads WHERE ad_id=?", (ad_id,)).fetchone()
    conn.close()
    return row is not None

def mark_seen(ad_id: str, brand: str, ad_type: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO seen_ads VALUES (?,?,?,?)",
        (ad_id, brand, ad_type, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()

# ─── Apify ────────────────────────────────────────────────────────────────────
def run_apify_scraper(url: str, max_items: int = 50) -> list:
    """
    Trigger an Apify run for the given Ad Library URL and wait for results.
    Returns a list of ad dicts.
    """
    resp = requests.post(
        f"{APIFY_BASE}/acts/{APIFY_ACTOR_ID}/runs",
        params={"token": APIFY_TOKEN},
        json={"urls": [{"url": url}], "maxItems": max_items},
        timeout=30,
    )
    resp.raise_for_status()
    run_data = resp.json()["data"]
    run_id = run_data["id"]
    dataset_id = run_data["defaultDatasetId"]
    log.info(f"    Apify run started: {run_id}")

    # Poll until done (max 5 minutes)
    for attempt in range(30):
        time.sleep(10)
        status_resp = requests.get(
            f"{APIFY_BASE}/acts/{APIFY_ACTOR_ID}/runs/{run_id}",
            params={"token": APIFY_TOKEN},
            timeout=15,
        )
        status = status_resp.json()["data"]["status"]
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            log.info(f"    Apify run finished: {status}")
            break
    else:
        log.warning("    Apify run timed out after 5 minutes")
        return []

    if status != "SUCCEEDED":
        log.warning(f"    Apify run did not succeed: {status}")
        return []

    items_resp = requests.get(
        f"{APIFY_BASE}/datasets/{dataset_id}/items",
        params={"token": APIFY_TOKEN, "limit": max_items},
        timeout=30,
    )
    items_resp.raise_for_status()
    items = items_resp.json()

    # Filter out error items
    ads = [i for i in items if "error" not in i]
    log.info(f"    Apify returned {len(ads)} ads")
    return ads

# ─── Ad parsing ───────────────────────────────────────────────────────────────
def parse_ad(item: dict) -> dict:
    """Normalize an Apify ad item into a consistent format."""
    snap = item.get("snapshot", {})
    images = snap.get("images", []) or []
    videos = snap.get("videos", []) or []

    media_type = "video" if videos else "image"

    image_urls = []
    for img in images:
        if isinstance(img, dict):
            url = img.get("original_image_url") or img.get("url") or img.get("resized_image_url")
            if url:
                image_urls.append(url)
        elif isinstance(img, str):
            image_urls.append(img)

    video_url = None
    video_thumb = None
    if videos:
        v = videos[0]
        if isinstance(v, dict):
            video_url = v.get("video_hd_url") or v.get("video_sd_url")
            video_thumb = v.get("video_preview_image_url")

    body = snap.get("body", "")
    if isinstance(body, dict):
        body = body.get("text", "")
    body = (body or "").strip()

    start_ts = item.get("start_date")
    start_date_str = ""
    if start_ts:
        try:
            start_date_str = datetime.fromtimestamp(int(start_ts), tz=timezone.utc).strftime("%b %d, %Y")
        except Exception:
            start_date_str = str(start_ts)

    ad_id = str(item.get("ad_archive_id", ""))
    library_url = f"https://www.facebook.com/ads/library/?id={ad_id}" if ad_id else ""

    return {
        "id": ad_id,
        "page_name": item.get("page_name", "Unknown"),
        "is_active": item.get("is_active", True),
        "media_type": media_type,
        "body": body,
        "title": snap.get("title", ""),
        "cta_text": snap.get("cta_text", ""),
        "link_url": snap.get("link_url", ""),
        "image_urls": image_urls,
        "video_url": video_url,
        "video_thumb": video_thumb,
        "start_date": start_date_str,
        "library_url": library_url,
    }

# ─── Slack posting ────────────────────────────────────────────────────────────
def post_ad_to_slack(slack: WebClient, channel: str, ad: dict, brand_name: str, thread_ts: str = None):
    """Post a single ad to Slack as a thread reply."""
    is_video = ad["media_type"] == "video"
    emoji = "🎬" if is_video else "🖼️"

    lines = []
    if ad["body"]:
        lines.append(ad["body"][:500])
    if ad["title"]:
        lines.append(f"*{ad['title']}*")
    if ad["cta_text"]:
        lines.append(f"CTA: _{ad['cta_text']}_")
    if ad["link_url"]:
        lines.append(f"<{ad['link_url']}|Landing page>")
    if ad["start_date"]:
        lines.append(f"Started: {ad['start_date']}")
    if ad["library_url"]:
        lines.append(f"<{ad['library_url']}|View in Ad Library>")

    text = "\n".join(lines) if lines else "(No copy)"

    try:
        if is_video and ad["video_url"]:
            # Download video and upload directly to Slack
            try:
                vid_resp = requests.get(ad["video_url"], timeout=60, stream=True)
                vid_resp.raise_for_status()
                video_bytes = vid_resp.content
                slack.files_upload_v2(
                    channel=channel,
                    content=video_bytes,
                    filename="ad_video.mp4",
                    initial_comment=f"{emoji} *{brand_name}* — video ad\n\n{text}",
                    thread_ts=thread_ts,
                )
            except Exception as vid_err:
                log.warning(f"    Could not upload video, trying thumbnail: {vid_err}")
                # Fallback: upload thumbnail image if available
                thumb_url = ad.get("video_thumb")
                if thumb_url:
                    try:
                        thumb_resp = requests.get(thumb_url, timeout=20)
                        thumb_resp.raise_for_status()
                        slack.files_upload_v2(
                            channel=channel,
                            content=thumb_resp.content,
                            filename="ad_thumbnail.jpg",
                            initial_comment=f"{emoji} *{brand_name}* — video ad (thumbnail)\n\n{text}",
                            thread_ts=thread_ts,
                        )
                    except Exception:
                        # Last resort: post text with link
                        slack.chat_postMessage(
                            channel=channel,
                            text=f"{emoji} *{brand_name}* — video ad\n{ad['video_url']}\n\n{text}",
                            thread_ts=thread_ts,
                        )
                else:
                    slack.chat_postMessage(
                        channel=channel,
                        text=f"{emoji} *{brand_name}* — video ad\n{ad['video_url']}\n\n{text}",
                        thread_ts=thread_ts,
                    )
        elif ad["image_urls"]:
            img_url = ad["image_urls"][0]
            try:
                img_resp = requests.get(img_url, timeout=20)
                img_resp.raise_for_status()
                slack.files_upload_v2(
                    channel=channel,
                    content=img_resp.content,
                    filename="ad_image.jpg",
                    initial_comment=f"{emoji} *{brand_name}* — image ad\n\n{text}",
                    thread_ts=thread_ts,
                )
            except Exception:
                # Fallback: post as text with image link
                slack.chat_postMessage(
                    channel=channel,
                    text=f"{emoji} *{brand_name}* — image ad\n{img_url}\n\n{text}",
                    thread_ts=thread_ts,
                )
        else:
            slack.chat_postMessage(
                channel=channel,
                text=f"📋 *{brand_name}* — ad\n\n{text}",
                thread_ts=thread_ts,
            )
    except SlackApiError as e:
        log.error(f"    Slack error posting ad {ad['id']}: {e.response['error']}")
    except Exception as e:
        log.error(f"    Error posting ad {ad['id']}: {e}")

# ─── Main run ─────────────────────────────────────────────────────────────────
def run():
    log.info("=" * 60)
    log.info(f"Facebook Ad Monitor — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    log.info("=" * 60)

    if not SLACK_TOKEN:
        log.error("SLACK_BOT_TOKEN not set — cannot post to Slack.")
        return
    if not VIDEO_CHANNEL or not STATIC_CHANNEL:
        log.error("SLACK_VIDEO_CHANNEL or SLACK_STATIC_CHANNEL not set.")
        return
    if not APIFY_TOKEN:
        log.error("APIFY_TOKEN not set — cannot run scraper.")
        return

    try:
        with open(BRANDS_FILE) as f:
            brands = json.load(f)
    except Exception as e:
        log.error(f"Could not load brands.json: {e}")
        return

    init_db()
    slack = WebClient(token=SLACK_TOKEN)

    total_new = 0
    brand_results = []

    for brand in brands:
        brand_name = brand.get("name", "Unknown")
        brand_url = brand.get("url") or ""
        brand_channel = brand.get("slack_channel", "")
        log.info(f"\n── Brand: {brand_name} ──")

        if not brand_url:
            log.warning(f"  No URL for {brand_name}, skipping")
            brand_results.append((brand_name, 0))
            continue

        try:
            raw_ads = run_apify_scraper(brand_url, max_items=15)
        except Exception as e:
            log.error(f"  Apify error for {brand_name}: {e}")
            brand_results.append((brand_name, -1))
            continue

        new_ads = []
        for item in raw_ads:
            ad = parse_ad(item)
            if not ad["id"]:
                continue
            if is_seen(ad["id"]):
                continue
            new_ads.append(ad)

        if not new_ads:
            log.info(f"  No new ads for {brand_name}")
            brand_results.append((brand_name, 0))
            continue

        log.info(f"  {len(new_ads)} new ads for {brand_name}")

        videos = [a for a in new_ads if a["media_type"] == "video"]
        statics = [a for a in new_ads if a["media_type"] != "video"]

        for media_group, type_label in [(videos, "video"), (statics, "image")]:
            if not media_group:
                continue

            if brand_channel:
                channel = brand_channel
            else:
                channel = VIDEO_CHANNEL if type_label == "video" else STATIC_CHANNEL

            count = len(media_group)
            try:
                parent = slack.chat_postMessage(
                    channel=channel,
                    text=f"*{brand_name}* — {count} new {type_label} ad{'s' if count > 1 else ''} found",
                )
                thread_ts = parent["ts"]
            except SlackApiError as e:
                log.error(f"  Could not post parent message: {e.response['error']}")
                continue

            for ad in media_group:
                post_ad_to_slack(slack, channel, ad, brand_name, thread_ts=thread_ts)
                mark_seen(ad["id"], brand_name, ad["media_type"])
                total_new += 1
                time.sleep(0.5)

        brand_results.append((brand_name, len(new_ads)))

    # ── Summary ───────────────────────────────────────────────────────────────
    log.info(f"\nRun complete. Total new ads posted: {total_new}")
    now_str = datetime.now().strftime("%b %d, %Y %H:%M")

    if total_new == 0:
        if not already_posted_daily_summary():
            summary = f"✅ *Ad Library Check Complete* — {now_str} CDT\nNo new ads found across {len(brands)} brands."
            channels_to_notify = set([VIDEO_CHANNEL, STATIC_CHANNEL])
            for b in brands:
                if b.get("slack_channel"):
                    channels_to_notify.add(b["slack_channel"])
            for ch in channels_to_notify:
                if ch:
                    try:
                        slack.chat_postMessage(channel=ch, text=summary)
                    except Exception:
                        pass
            mark_daily_summary_posted()
            log.info("Posted daily no-new-ads summary to Slack.")
        else:
            log.info("No new ads and daily summary already posted — skipping Slack notification.")
    else:
        log.info(f"✅ {total_new} new ad(s) posted across all brands.")

# ─── Entry point ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Facebook Ad Library Monitor → Slack (via Apify)")
    parser.add_argument("--schedule", action="store_true", help="Run on a recurring schedule")
    args = parser.parse_args()

    if args.schedule:
        log.info(f"Scheduled to run every {RUN_INTERVAL_HOURS} hour(s)")
        run()
        while True:
            time.sleep(RUN_INTERVAL_HOURS * 3600)
            run()
    else:
        run()

if __name__ == "__main__":
    main()
