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
LOOKBACK_HOURS  = int(os.getenv("LOOKBACK_HOURS", "24"))
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

def is_within_lookback(date_str: str, hours_override: int = None) -> bool:
    dt = parse_start_date(date_str)
    if dt is None:
        return True  # unknown date → include it
    hours = hours_override if hours_override else LOOKBACK_HOURS
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
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
    Navigate to the Ad Library page for a brand, collect all ad IDs and dates,
    then visit each ad's individual detail page to extract the correct media.
    Returns a list of ad dicts with accurate per-ad media.
    """
    url = build_url(brand)
    brand_name = brand.get("name", "Unknown")
    log.info(f"  Scraping: {url}")

    # ── Step 1: Load the brand listing page and collect ad IDs + dates ────────
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(4000)
    except PWTimeout:
        log.warning(f"  Timeout loading page for {brand_name}")
        return []

    # Scroll aggressively to load ALL ads (infinite scroll)
    log.info(f"  [{brand_name}] scrolling to load all ads...")
    prev_count = 0
    no_change_rounds = 0
    for scroll_round in range(60):  # max 60 scrolls (~300 ads)
        page.keyboard.press("End")
        page.wait_for_timeout(2500)
        # Check how many ad IDs are visible now
        current_count = page.evaluate("""
            () => (document.body.innerText.match(/Library ID:/g) || []).length
        """)
        log.info(f"  [{brand_name}] scroll {scroll_round+1}: {current_count} ads visible")
        if current_count == prev_count:
            no_change_rounds += 1
            if no_change_rounds >= 3:  # 3 scrolls with no new ads = done
                log.info(f"  [{brand_name}] no new ads after 3 scrolls — stopping")
                break
        else:
            no_change_rounds = 0
        prev_count = current_count

    # Extract all ad IDs and start dates from the listing page
    raw_ads = page.evaluate("""
        () => {
            const results = [];
            const bodyText = document.body.innerText;
            const idPattern = /Library ID:\\s*(\\d+)/g;
            const datePattern = /Started running on ([A-Za-z]+ \\d+, \\d{4})/g;
            const ids = [], dates = [];
            let m;
            while ((m = idPattern.exec(bodyText)) !== null) ids.push(m[1]);
            while ((m = datePattern.exec(bodyText)) !== null) dates.push(m[1]);
            for (let i = 0; i < ids.length; i++) {
                results.push({ id: ids[i], start_date: dates[i] || "Unknown" });
            }
            return results;
        }
    """)

    log.info(f"  [{brand_name}] found {len(raw_ads)} ad IDs on listing page")

    # ── Step 2: Visit each ad's detail page to get accurate media ─────────────
    enriched = []
    for ad in raw_ads:
        ad_id = ad["id"]
        detail_url = f"https://www.facebook.com/ads/library/?id={ad_id}"

        ad_media = []
        ad_copy = ""
        media_type = "unknown"
        captured = {"videos": [], "images": []}

        def on_response(response):
            resp_url = response.url
            # Real ad videos come from video-*.xx.fbcdn.net or similar video CDNs
            if ("fbcdn.net" in resp_url or "cdninstagram" in resp_url) and \
               any(ext in resp_url for ext in [".mp4", ".mov", ".webm"]):
                if resp_url not in captured["videos"]:
                    captured["videos"].append(resp_url)
            # Real ad images: t39.35426 is the ad creative CDN path on fbcdn
            # Exclude: static.xx.fbcdn.net (UI sprites), rsrc.php (UI assets),
            #          t1.30497 (profile pics), hads-ak (old ad thumbnails)
            elif "t39.35426" in resp_url and \
                 any(ext in resp_url for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"]) and \
                 resp_url not in captured["images"]:
                captured["images"].append(resp_url)

        page.on("response", on_response)

        try:
            page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)

            # Extract copy text
            ad_copy = page.evaluate("""
                () => {
                    const el = document.querySelector('div[data-testid="ad-card-body"]')
                        || document.querySelector('div._4bl9')
                        || document.querySelector('div[class*="_7jyr"]');
                    if (el) return el.innerText.trim();
                    // Fallback: grab largest text block on the page
                    let best = '';
                    for (const d of document.querySelectorAll('div, p, span')) {
                        const t = (d.innerText || '').trim();
                        if (t.length > best.length && t.length < 2000
                            && !t.includes('Library ID') && !t.includes('Ad Library')) {
                            best = t;
                        }
                    }
                    return best;
                }
            """) or ""

            # Extract videos directly from DOM
            dom_videos = page.evaluate("""
                () => Array.from(document.querySelectorAll('video'))
                    .map(v => v.src || v.currentSrc || '')
                    .filter(s => s.startsWith('http'))
            """)

            # Extract images directly from DOM
            # Only t39.35426 path = ad creative CDN; exclude UI sprites (rsrc.php, static.xx, t1.30497)
            dom_images = page.evaluate("""
                () => Array.from(document.querySelectorAll('img'))
                    .filter(img => img.naturalWidth >= 200 && img.naturalHeight >= 200)
                    .map(img => img.src)
                    .filter(src => src && src.includes('t39.35426'))
            """)

        except PWTimeout:
            log.warning(f"    Timeout on detail page for ad {ad_id}")
            page.remove_listener("response", on_response)
            enriched.append({
                "id": ad_id,
                "start_date": ad["start_date"],
                "copy": "",
                "media_type": "unknown",
                "media_urls": [],
                "snapshot_url": detail_url,
            })
            continue
        except Exception as e:
            log.warning(f"    Error on detail page for ad {ad_id}: {e}")
            page.remove_listener("response", on_response)
            continue

        page.remove_listener("response", on_response)

        # Merge DOM + network-captured media, prefer DOM (more reliable)
        all_videos = list(dict.fromkeys(dom_videos + captured["videos"]))
        all_images = list(dict.fromkeys(dom_images + captured["images"]))

        if all_videos:
            media_type = "video"
            ad_media = all_videos[:1]  # primary video only
        elif all_images:
            if len(all_images) > 1:
                media_type = "carousel"
            elif any(".gif" in u for u in all_images):
                media_type = "gif"
            else:
                media_type = "image"
            ad_media = all_images[:5]  # up to 5 carousel slides
        else:
            media_type = "unknown"
            ad_media = []

        enriched.append({
            "id": ad_id,
            "start_date": ad["start_date"],
            "copy": ad_copy[:500],
            "media_type": media_type,
            "media_urls": ad_media,
            "snapshot_url": detail_url,
        })

        log.info(f"    Ad {ad_id}: {media_type}, {len(ad_media)} media file(s)")
        time.sleep(1)  # polite delay between detail pages

    log.info(f"  [{brand_name}] enriched {len(enriched)} ads with accurate media")
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

def run(channel_filter: str = None, lookback_hours: int = None):
    """
    channel_filter: if set, only process brands whose slack_channel matches this value.
                    Use "default" to process brands with no slack_channel override.
    lookback_hours: override the global LOOKBACK_HOURS for this run only.
    """
    effective_lookback = lookback_hours if lookback_hours else LOOKBACK_HOURS
    log.info("=" * 60)
    log.info(f"Facebook Ad Monitor — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    if channel_filter:
        log.info(f"Channel filter: {channel_filter} | Lookback: {effective_lookback}h")
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

    # Apply channel filter if specified
    if channel_filter:
        if channel_filter == "default":
            brands = [b for b in brands if not b.get("slack_channel")]
        else:
            brands = [b for b in brands if b.get("slack_channel") == channel_filter]
        log.info(f"Filtered to {len(brands)} brand(s) for channel {channel_filter}")

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

                if not is_within_lookback(start_date, hours_override=effective_lookback):
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

            # Count per media type
            video_count  = sum(1 for a in eligible_ads if a.get("media_type") == "video")
            static_count = sum(1 for a in eligible_ads if a.get("media_type") != "video")

            # Determine channel routing for this brand
            # Brands with a slack_channel override send ALL ads (videos + statics)
            # to that single channel, with separate parent messages per media type.
            # Default brands use VIDEO_CHANNEL / STATIC_CHANNEL split.
            brand_override_channel = brand.get("slack_channel", "")

            # Thread tracking: keyed by (channel, media_type_label)
            # e.g. ("C0AQUE8H17U", "video") or ("C0AQU7NJZS6", "video")
            brand_threads = {}

            for ad in eligible_ads:
                ad_id = ad["id"]
                start_date = ad.get("start_date", "")
                log.info(f"  New ad: {ad_id} | {start_date} | {ad['media_type']}")

                # Download media
                files = download_media(ad, brand_name)

                # Route to correct Slack channel
                media_type = ad.get("media_type", "unknown")
                is_video = media_type == "video"

                if brand_override_channel:
                    # Competitor / custom channel: all ads go here
                    channel = brand_override_channel
                    type_label = "video" if is_video else "static"
                    type_emoji = "🎬" if is_video else "🖼️"
                    type_word  = "Videos" if is_video else "Statics"
                    count_for_type = video_count if is_video else static_count
                else:
                    # Default split: videos vs statics channels
                    channel = VIDEO_CHANNEL if is_video else STATIC_CHANNEL
                    type_label = "video" if is_video else "static"
                    type_emoji = "🎬" if is_video else "🖼️"
                    type_word  = "Videos" if is_video else "Statics"
                    count_for_type = video_count if is_video else static_count

                thread_key = (channel, type_label)

                # Create a parent thread message if we don't have one for this channel+type
                if thread_key not in brand_threads:
                    try:
                        resp = slack.chat_postMessage(
                            channel=channel,
                            text=(
                                f"{type_emoji} *{brand_name} — {type_word}* — "
                                f"*{count_for_type} new ad{'s' if count_for_type != 1 else ''} today*\n"
                                f"🔗 <{brand_url}|View in Ad Library>"
                            )
                        )
                        brand_threads[thread_key] = resp["ts"]
                    except SlackApiError as e:
                        log.error(f"  Failed to create thread for {brand_name}: {e}")

                # Post to Slack in the thread
                post_to_slack(
                    slack, channel, ad, brand_name, files,
                    thread_ts=brand_threads.get(thread_key)
                )

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
    parser.add_argument(
        "--channel", default=None,
        help="Only process brands assigned to this Slack channel ID (use 'default' for brands with no channel override)",
    )
    parser.add_argument(
        "--hours", type=int, default=None,
        help="Override lookback window in hours for this run only (e.g. 48)",
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
        run(channel_filter=args.channel, lookback_hours=args.hours)

if __name__ == "__main__":
    main()
