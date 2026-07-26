#!/usr/bin/env python3
"""
STAGE step of the hybrid pipeline â€” runs on GitHub Actions (datacenter IP, where
TikTok profile fetch works reliably).

Picks the next unposted video for a slot, downloads it, and writes the video file
+ an upload-metadata JSON into staging/. The GitHub workflow then publishes that
folder as an artifact. The user's PC later downloads the artifact and performs the
actual YouTube upload from a residential IP (see local_upload.py).

This script does NOT upload to YouTube and does NOT modify the DB â€” it only reads
the DB (to know what's already posted) and produces staging files.

Usage: python stage_download.py --slot 1 --channel channel_9
"""
import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.config import load_config            # noqa: E402
from src.db import init_db                     # noqa: E402
from src.channel_runner import (               # noqa: E402
    _pick_next_video, _download_with_retry, _resolve_title,
)
from src.tiktok_downloader import is_short_video  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("stage")

ap = argparse.ArgumentParser()
ap.add_argument("--slot", type=int, required=True, choices=[1, 2])
ap.add_argument("--channel", default="channel_9")
args = ap.parse_args()

init_db()
config = load_config()
channel = next((c for c in config.get("channels", []) if c["id"] == args.channel), None)
if channel is None:
    log.error("Channel %s not found in channels.yaml", args.channel)
    sys.exit(1)

STAGING = Path("staging")
STAGING.mkdir(exist_ok=True)
# Clear any previous staging for this slot so we never upload a stale clip.
for f in STAGING.glob(f"slot{args.slot}.*"):
    f.unlink()

upload_mode = channel.get("upload_mode", "short_only")
max_candidates = int(channel.get("max_download_candidates", 8))

# Multi-candidate loop: some TikToks can't be downloaded (photo/slideshow posts
# have no video stream - sometimes served under a /video/ URL so the /photo/
# filter misses them - or a post is temporarily IP-blocked). Skip past those so
# the slot still gets a video instead of failing the whole run.
tried = set()
video = None
local_file = None
while len(tried) < max_candidates:
    candidate = _pick_next_video(channel, args.slot, upload_mode, exclude_ids=tried)
    if candidate is None:
        break
    tried.add(candidate["id"])
    log.info("Trying candidate %d/%d: %s | '%s'",
             len(tried), max_candidates, candidate["id"], candidate.get("title", ""))
    f = _download_with_retry(channel, candidate, dry_run=False)
    if f is not None:
        video, local_file = candidate, f
        break
    log.warning("Candidate %s not downloadable (photo post / blocked) - trying next.",
                candidate["id"])

if video is None or local_file is None:
    log.error("No downloadable video found for slot %d after %d candidate(s).",
              args.slot, len(tried))
    sys.exit(1)

log.info("Staging video %s | '%s'", video["id"], video.get("title", ""))

short = is_short_video(
    duration=video.get("duration"),
    width=video.get("width"),
    height=video.get("height"),
    max_seconds=channel.get("shorts_max_seconds", 180),
)

dest_mp4 = STAGING / f"slot{args.slot}.mp4"
shutil.move(str(local_file), str(dest_mp4))

meta = {
    "slot": args.slot,
    "channel_id": channel["id"],
    "tiktok_video_id": video["id"],
    "tiktok_url": video.get("url"),
    "tiktok_title": video.get("title"),
    "tiktok_timestamp": video.get("timestamp"),
    "title": _resolve_title(channel, video),
    "description": video.get("description") or "",
    "tags": list(channel.get("default_tags") or []),
    "category_id": str(channel.get("youtube_category_id", "22")),
    "is_short": short,
    "description_footer": channel.get("description_footer", ""),
    "mp4": dest_mp4.name,
}
(STAGING / f"slot{args.slot}.json").write_text(
    json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
)
log.info("Staged slot %d -> %s (%.1f MB), is_short=%s",
         args.slot, dest_mp4, dest_mp4.stat().st_size / 1e6, short)
