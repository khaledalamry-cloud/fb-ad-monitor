"""
Facebook Ad Library Monitor → Slack  (NO META API TOKEN REQUIRED)
==================================================================
Scrapes the Facebook Ad Library directly in a headless browser,
downloads new ad media (videos, images, GIFs, carousels) posted
in the last 48 hours, deduplicates across daily runs, and posts
to two Slack channels:

  • #ads-videos  → video creatives
  • #ads-statics → image / GIF / carousel creatives

Usage:
  python3 fb_ad_monitor.py            # run once immediately
  python3 fb_ad_monitor.py --schedule # run daily at RUN_TIME (default 06:00)
"""

import os
import re
import json
import time
import sqlite3
import logging
import argparse
import requests
import schedule
import shutil
from langdetect import detect, LangDetectException
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ─── Setup ────────────────────────────────────────────────────────────────────

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

BASE_DIR   = Path(__file__).parent
DB_PATH    = BASE_DIR / "seen_ads.db"
BRANDS_FILE = BASE_DIR / "brands.json"
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

SLACK_TOKEN     = os.getenv("SLACK_BOT_TOKEN", "")
VIDEO_CHANNEL   = os.getenv("SLACK_VIDEO_CHANNEL", "")
STATIC_CHANNEL  = os.getenv("SLACK_STATIC_CHANNEL", "")
LOOKBACK_HOURS  = int(os.getenv("LOOKBACK_HOURS", "48"))
RUN_TIME        = os.getenv("RUN_TIME", "06:00")

# ─── Database (deduplication) ─────────────────────────────────────────────────

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
    conn.commit()
    conn.close()

def is_seen(ad_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT 1 FROM seen_ads WHERE ad_id=?", (ad_id,)).fetchone()
    conn.close()
    return row is not None

def mark_seen(ad_id: str, brand: str, ad_type: str):
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    # Handle both 4-column and 5-column schemas gracefully
    cols = [row[1] for row in conn.execute('PRAGMA table_info(seen_ads)').fetchall()]
    if len(cols) == 5:
        conn.execute(
            "INSERT OR IGNORE INTO seen_ads VALUES (?,?,?,?,?)",
            (ad_id, brand, ad_type, now, now),
        )
    else:
        conn.execute(
            "INSERT OR IGNORE INTO seen_ads VALUES (?,?,?,?)",
            (ad_id, brand, ad_type, now),
        )
    conn.commit()
    conn.close()

# ─── Date helpers ─────────────────────────────────────────────────────────────

MONTH_MAP = {
    "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
    "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
}

def parse_start_date(date_str: str) -> datetime | None:
    """Parse 'Started running on Mar 25, 2026' → datetime (UTC)."""
    m = re.search(r"([A-Za-z]+)\s+(\d+),\s+(\d{4})", date_str)
    if not m:
        return None
    month = MONTH_MAP.get(m.group(1).lower()[:3])
    if not month:
        return None
    return datetime(int(m.group(3)), month, int(m.group(2)), tzinfo=timezone.utc)

def is_within_lookback(date_str: str) -> bool:
    dt = parse_start_date(date_str)
    if dt is None:
        return True  # unknown date → include it
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    return dt >= cutoff

# ─── Scraper ──────────────────────────────────────────────────────────────────

def build_url(brand: dict) -> str:
    """Build the Ad Library URL for a brand entry."""
    # If a full URL is provided, use it directly
    if brand.get("url"):
        return brand["url"]
    # Otherwise build from search_query
    q = brand.get("search_query", "")
    return (
        f"https://www.facebook.com/ads/library/"
        f"?active_status=active&ad_type=all&country=US"
        f"&is_targeted_country=false&media_type=all"
        f"&q={q}&search_type=keyword_unordered"
        f"&sort_data[direction]=desc&sort_data[mode]=total_impressions"
    )

def scrape_brand(page, brand: dict) -> list[dict]:
    """
    Navigate to the Ad Library page for a brand, scroll to load all ads,
    and extract ad metadata + media URLs from the DOM.
    Returns a list of ad dicts.
    """
    url = build_url(brand)
    brand_name = brand.get("name", "Unknown")
    log.info(f"  Scraping: {url}")

    captured_media = {}   # ad_id → {"videos": [], "images": []}

    # Intercept network responses to capture media URLs as they load
    def on_response(response):
        resp_url = response.url
        if any(ext in resp_url for ext in [".mp4", ".mov", ".webm"]):
            # Try to associate with an ad — we'll match by order later
            captured_media.setdefault("_videos", [])
            if resp_url not in captured_media["_videos"]:
                captured_media["_videos"].append(resp_url)
        elif any(ext in resp_url for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"]):
            if "fbcdn" in resp_url or "cdninstagram" in resp_url:
                captured_media.setdefault("_images", [])
                if resp_url not in captured_media["_images"]:
                    captured_media["_images"].append(resp_url)

    page.on("response", on_response)

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(4000)
    except PWTimeout:
        log.warning(f"  Timeout loading page for {brand_name}")
        return []

    # Scroll down to load more ads (up to ~5 scrolls)
    for _ in range(5):
        page.keyboard.press("End")
        page.wait_for_timeout(2000)

    # ── Extract ad data from DOM ──────────────────────────────────────────────
    ads = page.evaluate("""
        () => {
            const results = [];
            const bodyText = document.body.innerText;

            // Match all ad blocks by Library ID
            const idPattern = /Library ID:\\s*(\\d+)/g;
            const datePattern = /Started running on ([A-Za-z]+ \\d+, \\d{4})/g;

            let idMatch, dateMatch;
            const ids = [];
            const dates = [];

            while ((idMatch = idPattern.exec(bodyText)) !== null) {
                ids.push(idMatch[1]);
            }
            while ((dateMatch = datePattern.exec(bodyText)) !== null) {
                dates.push(dateMatch[1]);
            }

            // Get all ad card elements for richer data
            // Facebook renders each ad in a container with the library ID visible
            const allText = document.body.innerHTML;

            for (let i = 0; i < ids.length; i++) {
                results.push({
                    id: ids[i],
                    start_date: dates[i] || "Unknown",
                });
            }

            return results;
        }
    """)

    # ── Extract videos per ad by visiting each ad's detail ───────────────────
    # Also grab all videos/images currently on the page
    page_videos = page.evaluate("""
        () => Array.from(document.querySelectorAll('video'))
            .map(v => ({ src: v.src || v.currentSrc, poster: v.poster }))
            .filter(v => v.src && v.src.startsWith('http'))
    """)

    page_images = page.evaluate("""
        () => Array.from(document.querySelectorAll('img'))
            .filter(img => img.naturalWidth > 150 && img.naturalHeight > 150)
            .map(img => img.src)
            .filter(src => src && (src.includes('fbcdn') || src.includes('cdninstagram')))
    """)

    # ── Get ad copy text per card ─────────────────────────────────────────────
    ad_copies = page.evaluate("""
        () => {
            // Each ad card has a sponsored label and body text
            const copies = [];
            // Look for text blocks that appear after "Sponsored"
            const allDivs = document.querySelectorAll('div[role="button"]');
            for (const div of allDivs) {
                const text = div.innerText || '';
                if (text.length > 20 && text.length < 1000 && !text.includes('Library ID')) {
                    copies.push(text.trim());
                }
            }
            return copies;
        }
    """)

    # ── Get page/advertiser names ─────────────────────────────────────────────
    page_names = page.evaluate("""
        () => Array.from(document.querySelectorAll('a[href*="/ads/library"]'))
            .map(a => a.innerText.trim())
            .filter(t => t.length > 0 && t.length < 100)
    """)

    # ── Detect media type per ad ──────────────────────────────────────────────
    # Map videos to ads by position (video i → ad i roughly)
    video_srcs = [v["src"] for v in page_videos if v.get("src")]
    image_srcs = page_images or []

    # Also include network-captured videos
    net_videos = captured_media.get("_videos", [])
    for nv in net_videos:
        if nv not in video_srcs:
            video_srcs.append(nv)

    net_images = captured_media.get("_images", [])
    for ni in net_images:
        if ni not in image_srcs:
            image_srcs.append(ni)

    # Build enriched ad list
    enriched = []
    for i, ad in enumerate(ads):
        ad_id = ad["id"]
        start_date = ad["start_date"]

        # Assign copy text (rough positional match)
        copy = ad_copies[i] if i < len(ad_copies) else ""

        # Assign media (rough positional match — 1 video per ad)
        if i < len(video_srcs):
            media_type = "video"
            media_urls = [video_srcs[i]]
        elif image_srcs:
            # Use images from the pool
            # Carousel: grab up to 3 consecutive images
            start_img = min(i * 2, len(image_srcs) - 1)
            end_img = min(start_img + 3, len(image_srcs))
            ad_images = image_srcs[start_img:end_img]
            if len(ad_images) > 1:
                media_type = "carousel"
            elif any(".gif" in u for u in ad_images):
                media_type = "gif"
            else:
                media_type = "image"
            media_urls = ad_images
        else:
            media_type = "unknown"
            media_urls = []

        enriched.append({
            "id": ad_id,
            "start_date": start_date,
            "copy": copy[:500],
            "media_type": media_type,
            "media_urls": media_urls,
            "snapshot_url": f"https://www.facebook.com/ads/library/?id={ad_id}",
        })

    page.remove_listener("response", on_response)
    log.info(f"  [{brand_name}] found {len(enriched)} ads on page")
    return enriched

# ─── Media Download ────────────────────────────────────────────────────────────

def download_media(ad: dict, brand_name: str) -> list[Path]:
    """Download media files for an ad. Returns list of local file paths."""
    ad_id = ad["id"]
    media_urls = ad.get("media_urls", [])
    if not media_urls:
        return []

    ad_dir = DOWNLOAD_DIR / f"{brand_name}_{ad_id}"
    ad_dir.mkdir(exist_ok=True)

    downloaded = []
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.facebook.com/",
    }

    for i, url in enumerate(media_urls[:5]):  # max 5 files per ad
        try:
            resp = requests.get(url, headers=headers, timeout=30, stream=True)
            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "")
            if "video" in content_type or ".mp4" in url:
                ext = ".mp4"
            elif "gif" in content_type or ".gif" in url:
                ext = ".gif"
            elif "png" in content_type or ".png" in url:
                ext = ".png"
            else:
                ext = ".jpg"

            filepath = ad_dir / f"media_{i}{ext}"
            with open(filepath, "wb") as f:
                for chunk in resp.iter_content(chunk_size=16384):
                    f.write(chunk)

            # Skip tiny files (likely tracking pixels)
            if filepath.stat().st_size < 5000:
                filepath.unlink()
                continue

            downloaded.append(filepath)
            log.info(f"    Downloaded {filepath.name} ({filepath.stat().st_size // 1024}KB)")

        except Exception as e:
            log.warning(f"    Failed to download media {i} for ad {ad_id}: {e}")

    return downloaded

# ─── Slack Posting ─────────────────────────────────────────────────────────────

def post_to_slack(client: WebClient, channel: str, ad: dict,
                  brand_name: str, files: list[Path], thread_ts: str = None):
    """Upload media and post ad metadata to Slack."""
    ad_id = ad["id"]
    media_type = ad.get("media_type", "unknown")
    start_date = ad.get("start_date", "Unknown")
    copy = ad.get("copy", "")
    snapshot_url = ad.get("snapshot_url", "")

    type_emoji = {"video": "🎬", "image": "🖼️", "gif": "🎞️",
                  "carousel": "🎠", "unknown": "📄"}.get(media_type, "📄")

    msg = (
        f"{type_emoji} *New {media_type.upper()} — {brand_name}*\n"
        f"*Ad ID:* `{ad_id}`  |  *Started:* {start_date}\n"
        f"*Copy:* {copy[:300]}{'...' if len(copy) > 300 else ''}\n"
        f"<{snapshot_url}|View in Ad Library>"
    )

    try:
        if files:
            primary = files[0]
            with open(primary, "rb") as f:
                client.files_upload_v2(
                    channel=channel,
                    file=f,
                    filename=primary.name,
                    initial_comment=msg,
                    thread_ts=thread_ts,
                )
            # Extra carousel slides
            for extra in files[1:]:
                with open(extra, "rb") as f:
                    client.files_upload_v2(
                        channel=channel,
                        file=f,
                        filename=extra.name,
                        initial_comment=f"↑ Carousel slide",
                        thread_ts=thread_ts,
                    )
        else:
            # No media — post text + link only
            client.chat_postMessage(channel=channel, text=msg, thread_ts=thread_ts)

        log.info(f"  ✅ Posted ad {ad_id} ({media_type}) → Slack")

    except SlackApiError as e:
        log.error(f"  Slack error for ad {ad_id}: {e.response['error']}")

# ─── Cleanup ──────────────────────────────────────────────────────────────────

def cleanup_downloads(days: int = 7):
    cutoff = time.time() - days * 86400
    removed = 0
    for item in DOWNLOAD_DIR.iterdir():
        if item.is_dir() and item.stat().st_mtime < cutoff:
            shutil.rmtree(item, ignore_errors=True)
            removed += 1
    if removed:
        log.info(f"Cleaned up {removed} old download folders.")

# ─── Main Run ─────────────────────────────────────────────────────────────────

def run():
    log.info("=" * 60)
    log.info(f"Facebook Ad Monitor — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    log.info("=" * 60)

    if not SLACK_TOKEN:
        log.error("SLACK_BOT_TOKEN not set in .env — cannot post to Slack.")
        return
    if not VIDEO_CHANNEL or not STATIC_CHANNEL:
        log.error("SLACK_VIDEO_CHANNEL or SLACK_STATIC_CHANNEL not set.")
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

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = context.new_page()

        for brand in brands:
            brand_name = brand.get("name", "Unknown")
            log.info(f"\n── Brand: {brand_name} ──")

            try:
                ads = scrape_brand(page, brand)
            except Exception as e:
                log.error(f"  Scrape failed for {brand_name}: {e}")
                continue

            brand_url = build_url(brand)

            # ── Pre-filter ads for this brand ────────────────────────────────
            eligible_ads = []
            for ad in ads:
                ad_id = ad["id"]
                start_date = ad.get("start_date", "")

                if not is_within_lookback(start_date):
                    log.debug(f"  Skipping old ad {ad_id} ({start_date})")
                    continue

                if is_seen(ad_id):
                    log.debug(f"  Already seen: {ad_id}")
                    continue

                copy_text = ad.get("copy", "")
                if copy_text.strip():
                    try:
                        lang = detect(copy_text)
                        if lang != "en":
                            log.debug(f"  Skipping non-English ad {ad_id} (detected: {lang})")
                            continue
                    except LangDetectException:
                        pass

                eligible_ads.append(ad)

            if not eligible_ads:
                log.info(f"  No new eligible ads for {brand_name}")
                continue

            # Count per channel
            video_count  = sum(1 for a in eligible_ads if a.get("media_type") == "video")
            static_count = sum(1 for a in eligible_ads if a.get("media_type") != "video")

            # Thread tracking for this brand
            brand_threads = {
                VIDEO_CHANNEL: None,
                STATIC_CHANNEL: None
            }

            for ad in eligible_ads:
                ad_id = ad["id"]
                start_date = ad.get("start_date", "")
                log.info(f"  New ad: {ad_id} | {start_date} | {ad['media_type']}")

                # Download media
                files = download_media(ad, brand_name)

                # Route to correct Slack channel
                media_type = ad.get("media_type", "unknown")
                channel = VIDEO_CHANNEL if media_type == "video" else STATIC_CHANNEL

                # Create a parent thread message if we don't have one for this channel
                if not brand_threads[channel]:
                    count_for_channel = video_count if channel == VIDEO_CHANNEL else static_count
                    try:
                        resp = slack.chat_postMessage(
                            channel=channel,
                            text=(
                                f"📁 *{brand_name}* — *{count_for_channel} new ad{'s' if count_for_channel != 1 else ''} today*\n"
                                f"🔗 <{brand_url}|View in Ad Library>"
                            )
                        )
                        brand_threads[channel] = resp["ts"]
                    except SlackApiError as e:
                        log.error(f"  Failed to create thread for {brand_name}: {e}")

                # Post to Slack in the thread
                post_to_slack(slack, channel, ad, brand_name, files, thread_ts=brand_threads[channel])

                # Mark as seen
                mark_seen(ad_id, brand_name, media_type)
                total_new += 1

                time.sleep(1.5)  # polite delay

        browser.close()

    cleanup_downloads()
    log.info(f"\n✅ Done. {total_new} new ads posted to Slack.")

# ─── Entry Point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Facebook Ad Library Monitor → Slack (no API token needed)"
    )
    parser.add_argument(
        "--schedule", action="store_true",
        help="Run on a daily schedule instead of once immediately",
    )
    parser.add_argument(
        "--time", default=RUN_TIME,
        help=f"Daily run time in 24h format (default: {RUN_TIME})",
    )
    args = parser.parse_args()

    if args.schedule:
        log.info(f"Scheduled to run daily at {args.time}")
        schedule.every().day.at(args.time).do(run)
        run()  # also run immediately on startup
        while True:
            schedule.run_pending()
            time.sleep(30)
    else:
        run()

if __name__ == "__main__":
    main()
