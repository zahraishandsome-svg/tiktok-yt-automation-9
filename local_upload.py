#!/usr/bin/env python3
"""
LOCAL UPLOAD step of the hybrid pipeline â€” runs on the user's PC (residential IP).

Downloads the latest staged artifact for a slot (produced by stage.yml on GitHub),
uploads the video to YouTube from THIS machine's IP, then records it in the DB.
The wrapper (run_local.bat) git-pushes the updated DB afterwards.

Env: GH_TOKEN must be set (GitHub PAT). Usage: python local_upload.py --slot 1
"""
import argparse
import io
import json
import logging
import os
import sys
import urllib.request
import urllib.error
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.config import load_config                      # noqa: E402
from src import db                                       # noqa: E402
from src.db import init_db                               # noqa: E402
from src.youtube_uploader import get_authenticated_client, upload_video  # noqa: E402

REPO = "zahraishandsome-svg/tiktok-yt-automation-8"
TOKEN = os.environ.get("GH_TOKEN", "")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("local_upload")

ap = argparse.ArgumentParser()
ap.add_argument("--slot", type=int, required=True, choices=[1, 2])
ap.add_argument("--channel", default="channel_9")
args = ap.parse_args()

if not TOKEN:
    log.error("GH_TOKEN env var not set")
    sys.exit(1)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _retry(fn, what, attempts=6, delay=30):
    """Retry a network op through transient failures (DNS drop, timeout, 5xx).
    Today's miss was a getaddrinfo (DNS) blip at 8 PM â€” this rides those out."""
    import time
    for i in range(1, attempts + 1):
        try:
            return fn()
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            if i == attempts:
                raise
            log.warning("%s failed (attempt %d/%d): %s â€” retrying in %ds",
                        what, i, attempts, e, delay)
            time.sleep(delay)


def gh_json(url):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "ch9-local-upload",
    })
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def download_artifact_zip(archive_url):
    """GitHub redirects artifact downloads to a signed storage URL; don't forward
    the Authorization header to that redirect (storage rejects it)."""
    req = urllib.request.Request(archive_url, headers={
        "Authorization": f"Bearer {TOKEN}", "User-Agent": "ch9-local-upload",
    })
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(req) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307):
            with urllib.request.urlopen(e.headers["Location"]) as r:
                return r.read()
        raise


init_db()
config = load_config()
channel = next((c for c in config.get("channels", []) if c["id"] == args.channel), None)
if channel is None:
    log.error("Channel %s not found", args.channel)
    sys.exit(1)
channel_id = channel["id"]

if db.slot_already_ran(channel_id, args.slot):
    log.info("Slot %d already ran successfully today â€” skipping.", args.slot)
    sys.exit(0)

# Find the newest non-expired staged artifact for this slot
name = f"staged-slot{args.slot}"
arts = _retry(
    lambda: gh_json(f"https://api.github.com/repos/{REPO}/actions/artifacts?per_page=100"),
    "List artifacts",
)["artifacts"]
cands = [a for a in arts if a["name"] == name and not a["expired"]]
if not cands:
    log.error("No staged artifact '%s' found â€” did the stage workflow run?", name)
    sys.exit(1)
art = max(cands, key=lambda a: a["created_at"])
log.info("Downloading artifact %s (id=%s, created %s)", name, art["id"], art["created_at"])

zip_bytes = _retry(lambda: download_artifact_zip(art["archive_download_url"]), "Download artifact")
STAGING = Path("staging")
STAGING.mkdir(exist_ok=True)
with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
    zf.extractall(STAGING)

meta = json.loads((STAGING / f"slot{args.slot}.json").read_text(encoding="utf-8"))
mp4 = STAGING / meta["mp4"]
if not mp4.exists():
    log.error("Staged mp4 missing: %s", mp4)
    sys.exit(1)

# Safety net: never upload a video already marked uploaded. This is the last line
# of defence against duplicates if staging ever hands us an already-posted clip.
if meta["tiktok_video_id"] in db.get_posted_video_ids(channel_id, channel.get("upload_mode", "short_only")):
    log.warning("Video %s is already uploaded â€” skipping to avoid a DUPLICATE.",
                meta["tiktok_video_id"])
    for f in STAGING.glob(f"slot{args.slot}.*"):
        f.unlink()
    sys.exit(0)

log.info("Uploading from LOCAL IP: %s | title='%s' | is_short=%s",
         meta["tiktok_video_id"], meta["title"], meta["is_short"])

run_id = db.start_run(channel_id, args.slot)
yt = get_authenticated_client(
    credentials_file=channel["google_credentials_file"],
    token_file=channel["oauth_token_file"],
)
yt_id = upload_video(
    youtube_client=yt,
    video_path=mp4,
    title=meta["title"],
    description=meta["description"],
    tags=meta["tags"],
    category_id=meta["category_id"],
    is_short=meta["is_short"],
    description_footer=meta["description_footer"],
    publish_at=None,        # immediate public
    dry_run=False,
)

if yt_id:
    db.mark_uploaded(channel_id, meta["tiktok_video_id"], yt_id, format_type="short",
                     tiktok_url=meta["tiktok_url"], tiktok_title=meta["tiktok_title"],
                     tiktok_timestamp=meta["tiktok_timestamp"])
    db.finish_run(run_id, "success", videos_uploaded=1)
    log.info("SUCCESS (local IP): https://www.youtube.com/watch?v=%s", yt_id)
    for f in STAGING.glob(f"slot{args.slot}.*"):
        f.unlink()
else:
    db.finish_run(run_id, "failed", error_message="upload returned no id")
    log.error("Upload failed (no video id returned)")
    sys.exit(1)
