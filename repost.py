#!/usr/bin/env python3
"""
Repost fallback: keep the channel posting when TikTok has nothing new.

Runs right after `run.py --slot N` in every upload workflow. If that slot did not
upload anything today, re-upload one of the channel's own best-performing videos.

Rules (owner, 2026-10-25):
  * Pool        = the channel's own YouTube uploads with >= 100,000 views, published
                  at least 7 days ago. Reposts themselves are never reposted.
  * Order       = most viewed first, down the list to 100K, then the cycle restarts
                  from the top.
  * Cooldown    = a video is not reposted again within 15 days of its last repost.
  * Daily cap   = at most ONE repost per day (UTC run date). So: new TikTok in one
                  slot + repost in the other = 2 posts; nothing new = 1 repost.
  * Never skip a day: if no 100K+ video is available (none yet, or all cooling
                  down), the best-viewed eligible video below 100K is used instead.

The video file comes from the original TikTok (stored in posted_videos), downloaded
with this repo's own hardened downloader; if that fails, from the YouTube upload.

Usage:  python repost.py --slot 1 --channel channel_5 [--dry-run]
Env:    YT_API_KEY (public Data API key), YT_CHANNEL_ID (UC...), DB_PAGE_ID,
        TIKTOK_COOKIES_FILE (optional), DRY_RUN in .env (optional)
"""
import argparse
import json
import logging
import os
import sqlite3
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

MIN_VIEWS = 100_000
MIN_AGE_DAYS = 7
COOLDOWN_DAYS = 15
MAX_ATTEMPTS = 6

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)-8s] repost: %(message)s",
                    handlers=[logging.StreamHandler(open(sys.stdout.fileno(), "w", encoding="utf-8",
                                                         closefd=False))])
log = logging.getLogger("repost")


# ── helpers ────────────────────────────────────────────────────────────────
def _env_dry_run() -> bool:
    if os.environ.get("DRY_RUN", "").lower() == "true":
        return True
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip().lower() == "dry_run=true":
                return True
    return False


def _db() -> sqlite3.Connection:
    page = os.environ.get("DB_PAGE_ID")
    path = ROOT / "data" / (f"{page}.db" if page else "automation.db")
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute("""
        CREATE TABLE IF NOT EXISTS reposts (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id     TEXT NOT NULL,
            source_yt_id   TEXT NOT NULL,    -- the original upload being reposted
            repost_yt_id   TEXT,             -- the new upload
            cycle          INTEGER NOT NULL, -- 0 = below-100K fallback
            views_at_pick  INTEGER,
            run_date       TEXT NOT NULL,
            slot           INTEGER,
            reposted_at    TEXT
        )""")
    return con


def _yt(path: str, **params) -> dict:
    params["key"] = os.environ["YT_API_KEY"]
    url = f"https://www.googleapis.com/youtube/v3/{path}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=40) as r:
        return json.load(r)


def _channel_uploads(channel_id_uc: str) -> list:
    """Every upload on the channel with views, publish time, title, description."""
    ids, token = [], None
    while True:
        p = {"part": "contentDetails", "playlistId": "UU" + channel_id_uc[2:], "maxResults": 50}
        if token:
            p["pageToken"] = token
        r = _yt("playlistItems", **p)
        ids += [i["contentDetails"]["videoId"] for i in r.get("items", [])]
        token = r.get("nextPageToken")
        if not token:
            break
    out = []
    for i in range(0, len(ids), 50):
        for it in _yt("videos", part="statistics,snippet", id=",".join(ids[i:i + 50])).get("items", []):
            sn = it["snippet"]
            out.append({
                "id": it["id"],
                "views": int(it.get("statistics", {}).get("viewCount", 0)),
                "published": datetime.fromisoformat(sn["publishedAt"].replace("Z", "+00:00")),
                "title": sn.get("title", ""),
                "description": sn.get("description", ""),
                "category": sn.get("categoryId"),
            })
    return out


def _pick(con, channel_id: str, uploads: list, today: date) -> list:
    """Ordered (video, cycle) candidates for today, following the cycle/cooldown rules."""
    rows = con.execute("SELECT source_yt_id, repost_yt_id, cycle, run_date FROM reposts "
                       "WHERE channel_id=? AND repost_yt_id IS NOT NULL", (channel_id,)).fetchall()
    repost_outputs = {r["repost_yt_id"] for r in rows}
    last_repost = {}
    for r in rows:
        d = date.fromisoformat(r["run_date"])
        last_repost[r["source_yt_id"]] = max(d, last_repost.get(r["source_yt_id"], d))
    now = datetime.now(timezone.utc)

    def cooled(v):
        last = last_repost.get(v["id"])
        return last is None or (today - last).days >= COOLDOWN_DAYS

    # Pool = the channel's own originals that are at least a week old.
    aged = [v for v in uploads if v["id"] not in repost_outputs
            and now - v["published"] >= timedelta(days=MIN_AGE_DAYS)]
    top = sorted((v for v in aged if v["views"] >= MIN_VIEWS), key=lambda v: -v["views"])
    cycles = [r["cycle"] for r in rows if r["cycle"] > 0]
    cycle = max(cycles) if cycles else 1
    done = {r["source_yt_id"] for r in rows if r["cycle"] == cycle}

    # 1. 100K+ videos, most viewed first, each once per cycle, 15-day cooldown
    picks = [(v, cycle) for v in top if v["id"] not in done and cooled(v)]
    if not picks and top and all(v["id"] in done for v in top):
        # went all the way down to 100K: the cycle restarts from the most viewed
        picks = [(v, cycle + 1) for v in top if cooled(v)]
        if picks:
            log.info("cycle %d complete — restarting from the most viewed (cycle %d)", cycle, cycle + 1)
    if not picks:
        log.info("no 100K+ video available today (%d in pool) — backing up with the rest", len(top))
    # 2. never skip a day: the best below-100K videos that are off cooldown
    low = sorted((v for v in aged if v["views"] < MIN_VIEWS and cooled(v)), key=lambda v: -v["views"])
    # 3. last resort, everything is cooling down: the video whose last repost is the
    #    oldest (most viewed first among equals), so the channel still posts today
    seen = {v["id"] for v, _ in picks} | {v["id"] for v in low}
    rest = sorted((v for v in aged if v["id"] not in seen),
                  key=lambda v: (last_repost.get(v["id"], date.min), -v["views"]))
    return picks + [(v, 0) for v in low] + [(v, 0) for v in rest]


def _source_url(con, channel: dict, yt_id: str):
    row = con.execute("SELECT tiktok_video_id, tiktok_url FROM posted_videos WHERE youtube_video_id=? "
                      "ORDER BY posted_at LIMIT 1", (yt_id,)).fetchone()
    if row and row["tiktok_video_id"] and not str(row["tiktok_video_id"]).startswith("repost:"):
        url = row["tiktok_url"] or (f"https://www.tiktok.com/@{channel['tiktok_username']}"
                                    f"/video/{row['tiktok_video_id']}")
        return url, row["tiktok_video_id"]
    return None, None


def _download(con, channel: dict, yt_id: str, out: Path):
    url, tid = _source_url(con, channel, yt_id)
    if url:
        try:
            from src.tiktok_downloader import download_video
            f = download_video(url, tid, out)
            if f and Path(f).exists():
                log.info("downloaded from the original TikTok %s", tid)
                return Path(f)
        except Exception as exc:  # noqa: BLE001
            log.warning("TikTok download failed for %s: %s", tid, exc)
    # Fallback: our own upload on YouTube.
    try:
        import yt_dlp
        tmpl = str(out / f"yt_{yt_id}.%(ext)s")
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "outtmpl": tmpl,
                               "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
                               "merge_output_format": "mp4"}) as y:
            y.extract_info(f"https://www.youtube.com/shorts/{yt_id}", download=True)
        files = sorted(out.glob(f"yt_{yt_id}.*"))
        if files:
            log.info("downloaded from YouTube %s", yt_id)
            return files[0]
    except Exception as exc:  # noqa: BLE001
        log.warning("YouTube download failed for %s: %s", yt_id, str(exc)[:200])
    return None


def _youtube_client(channel: dict):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    tok = json.loads((ROOT / channel["oauth_token_file"]).read_text(encoding="utf-8"))
    cid, csec = tok.get("client_id"), tok.get("client_secret")
    if not (cid and csec):
        sec = json.loads((ROOT / channel["google_credentials_file"]).read_text(encoding="utf-8"))
        block = sec.get("installed") or sec.get("web") or {}
        cid, csec = block.get("client_id"), block.get("client_secret")
    creds = Credentials(token=tok.get("token"), refresh_token=tok.get("refresh_token"),
                        token_uri=tok.get("token_uri", "https://oauth2.googleapis.com/token"),
                        client_id=cid, client_secret=csec,
                        scopes=tok.get("scopes") or ["https://www.googleapis.com/auth/youtube.upload"])
    if not creds.valid:
        creds.refresh(Request())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _upload(channel: dict, video: dict, path: Path) -> str:
    from googleapiclient.http import MediaFileUpload
    yt = _youtube_client(channel)
    body = {
        "snippet": {"title": (video["title"] or channel.get("youtube_channel_name", ""))[:100],
                    "description": video["description"][:4900],
                    "tags": list(channel.get("default_tags") or [])[:30],
                    "categoryId": video["category"] or str(channel.get("youtube_category_id", "22"))},
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(path), mimetype="video/mp4",
                                                        resumable=True, chunksize=5 * 1024 * 1024))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["id"]


# ── main ───────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", type=int, required=True)
    ap.add_argument("--channel", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    dry = a.dry_run or _env_dry_run()

    cfg = yaml.safe_load((ROOT / "channels.yaml").read_text(encoding="utf-8-sig"))
    channel = next((c for c in cfg.get("channels", []) if c.get("id") == a.channel), None)
    if not channel or not channel.get("enabled", True):
        log.info("%s not configured/enabled — nothing to do", a.channel)
        return 0
    if not os.environ.get("YT_API_KEY") or not os.environ.get("YT_CHANNEL_ID"):
        log.warning("YT_API_KEY / YT_CHANNEL_ID missing — repost fallback disabled")
        return 0

    con = _db()
    today = date.today()
    if con.execute("SELECT 1 FROM runs WHERE channel_id=? AND run_date=? AND slot=? AND status='success'",
                   (a.channel, today.isoformat(), a.slot)).fetchone():
        log.info("slot %d already posted today — no repost needed", a.slot)
        return 0
    if con.execute("SELECT 1 FROM reposts WHERE channel_id=? AND run_date=? AND repost_yt_id IS NOT NULL",
                   (a.channel, today.isoformat())).fetchone():
        log.info("today's one repost is already done — slot %d stays empty", a.slot)
        return 0

    uploads = _channel_uploads(os.environ["YT_CHANNEL_ID"])
    cands = _pick(con, a.channel, uploads, today)
    log.info("%d uploads on the channel, %d with 100K+ views, %d candidates today",
             len(uploads), sum(v["views"] >= MIN_VIEWS for v in uploads), len(cands))
    if not cands:
        log.error("no repost candidate at all (channel too new / everything cooling down)")
        return 1

    out = ROOT / "downloads" / "repost"
    out.mkdir(parents=True, exist_ok=True)
    for video, cycle in cands[:MAX_ATTEMPTS]:
        log.info("candidate %s — %s views — cycle %s — '%s'", video["id"], f"{video['views']:,}",
                 cycle or "fallback<100K", video["title"][:60])
        if dry:
            log.info("[DRY RUN] would repost %s", video["id"])
            return 0
        path = _download(con, channel, video["id"], out)
        if not path:
            continue
        try:
            new_id = _upload(channel, video, path)
        except Exception as exc:  # noqa: BLE001
            log.error("upload failed: %s", exc)
            return 1
        finally:
            path.unlink(missing_ok=True)
        now = datetime.utcnow().isoformat()
        with con:
            con.execute("INSERT INTO reposts (channel_id, source_yt_id, repost_yt_id, cycle, views_at_pick,"
                        " run_date, slot, reposted_at) VALUES (?,?,?,?,?,?,?,?)",
                        (a.channel, video["id"], new_id, cycle, video["views"], today.isoformat(), a.slot, now))
            # Count it as this slot's upload so the day guard, the retry job and the
            # portal all see the slot as posted.
            con.execute("INSERT OR REPLACE INTO posted_videos (channel_id, tiktok_video_id, format_type,"
                        " tiktok_title, youtube_video_id, posted_at, status) VALUES (?,?,?,?,?,?,'uploaded')",
                        (a.channel, f"repost:{video['id']}:{today.isoformat()}", "repost",
                         video["title"], new_id, now))
            con.execute("INSERT INTO runs (channel_id, run_date, slot, status, videos_uploaded, error_message,"
                        " started_at, completed_at) VALUES (?,?,?,'success',1,?,?,?)",
                        (a.channel, today.isoformat(), a.slot, f"repost of {video['id']}", now, now))
        log.info("REPOSTED %s -> https://www.youtube.com/watch?v=%s", video["id"], new_id)
        return 0
    log.error("every candidate failed to download")
    return 1


if __name__ == "__main__":
    sys.exit(main())
