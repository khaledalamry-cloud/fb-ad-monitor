# Facebook Ad Library Monitor → Slack

**No Meta API token required.** This app opens the Facebook Ad Library in a headless browser — exactly like you do manually — and automatically pulls new ads every day.

It downloads new ad media (videos, images, GIFs, carousels) posted in the last 48 hours across all your tracked brands, and posts them to two Slack channels:

- `#ads-videos` → video creatives
- `#ads-statics` → images, GIFs, and carousel creatives

**Zero duplicates** — every ad ID is tracked in a local database and never posted twice.

---

## What You Need (One-Time Setup)

### 1. Slack App (the only thing you need to create)

1. Go to **https://api.slack.com/apps** → click **"Create New App"** → **"From Scratch"**
2. Give it a name (e.g., "Ad Library Monitor") and select your workspace → click **Create App**
3. In the left sidebar, click **"OAuth & Permissions"**
4. Scroll down to **"Bot Token Scopes"** and add these scopes:
   - `files:write`
   - `chat:write`
   - `channels:read`
   - `channels:join`
5. Scroll back up and click **"Install to Workspace"** → **Allow**
6. Copy the **Bot User OAuth Token** (it starts with `xoxb-`) — this is your `SLACK_BOT_TOKEN`
7. **Invite the bot to both channels** in Slack:
   - Open your `#ads-videos` channel → type `/invite @YourBotName` → press Enter
   - Open your `#ads-statics` channel → type `/invite @YourBotName` → press Enter
8. **Get the Channel IDs:**
   - In Slack, right-click the channel name → **"Copy Link"**
   - The URL looks like: `https://yourworkspace.slack.com/archives/C0XXXXXXXXX`
   - The last part (`C0XXXXXXXXX`) is the Channel ID
   - Do this for both channels

---

### 2. Configure Your .env File

Copy the example file and fill in your values:

```bash
cp .env.example .env
```

Open `.env` and fill in:

```
SLACK_BOT_TOKEN=xoxb-your-token-here
SLACK_VIDEO_CHANNEL=C0XXXXXXXXX
SLACK_STATIC_CHANNEL=C0YYYYYYYYY
LOOKBACK_HOURS=48
RUN_TIME=06:00
```

That's it. No Meta token needed.

---

### 3. Add Your Brands

Edit `brands.json` and add all the brands you want to monitor. You can either paste the full Ad Library URL directly, or just provide the search query:

**Option A — Paste the full URL (easiest):**
```json
[
  {
    "name": "Lemme",
    "url": "https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=US&is_targeted_country=false&media_type=all&q=lemmelive.com&search_type=keyword_unordered&sort_data[direction]=desc&sort_data[mode]=total_impressions"
  },
  {
    "name": "Obvi",
    "url": "https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=US&q=obvi.com&search_type=keyword_unordered"
  }
]
```

**Option B — Just the search query:**
```json
[
  {
    "name": "Lemme",
    "search_query": "lemmelive.com"
  },
  {
    "name": "PetLab Co",
    "search_query": "thepetlabco.com"
  }
]
```

You can add as many brands as you want. The file is re-read on every daily run, so you can add new brands at any time without restarting.

---

## Installation

```bash
# 1. Install Python dependencies
pip3 install playwright slack-sdk requests python-dotenv schedule

# 2. Install the Chromium browser (one-time, ~100MB)
python3 -m playwright install chromium
```

---

## Running the App

### Test it once right now:
```bash
python3 fb_ad_monitor.py
```

### Run on daily schedule (6am):
```bash
python3 fb_ad_monitor.py --schedule
```

### Run at a custom time:
```bash
python3 fb_ad_monitor.py --schedule --time 08:00
```

### Keep it running in the background (Linux/Mac):
```bash
nohup python3 fb_ad_monitor.py --schedule > monitor.log 2>&1 &
```

### Run as a system service (best for servers):

Create `/etc/systemd/system/fb-ad-monitor.service`:

```ini
[Unit]
Description=Facebook Ad Library Monitor
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/fb_ad_monitor
ExecStart=/usr/bin/python3 /home/ubuntu/fb_ad_monitor/fb_ad_monitor.py --schedule
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Then enable it:
```bash
sudo systemctl daemon-reload
sudo systemctl enable fb-ad-monitor
sudo systemctl start fb-ad-monitor
sudo systemctl status fb-ad-monitor
```

---

## How It Works

```
Every day at 6am:
  For each brand in brands.json:
    1. Open the Ad Library URL in a headless browser (no login needed)
    2. Scroll down to load all ads
    3. Extract Library IDs, start dates, ad copy, and media URLs
    4. Skip any ad started more than 48 hours ago
    5. Skip any ad already in the seen_ads.db database (no duplicates)
    6. Download the video / image / GIF / carousel files
    7. Post to #ads-videos (videos) or #ads-statics (everything else)
    8. Record the ad ID in the database
  Clean up downloaded files older than 7 days
```

---

## File Structure

```
fb_ad_monitor/
├── fb_ad_monitor.py   ← Main application (no API token needed)
├── brands.json        ← Your list of brands to monitor
├── .env               ← Your Slack tokens (never commit this)
├── .env.example       ← Template for .env
├── seen_ads.db        ← SQLite deduplication database (auto-created)
├── downloads/         ← Temporary media storage (auto-cleaned after 7 days)
└── README.md          ← This file
```

---

## Troubleshooting

| Problem | Solution |
|---|---|
| `SLACK_BOT_TOKEN not set` | Add your token to `.env` |
| `Slack error: not_in_channel` | Invite the bot: `/invite @BotName` in both channels |
| `0 ads found` | Check the Ad Library URL in your browser first — if it shows results, the scraper will too |
| Files not uploading to Slack | Make sure the bot has `files:write` scope |
| App stops running | Use `systemd` or `nohup` to keep it alive in the background |
