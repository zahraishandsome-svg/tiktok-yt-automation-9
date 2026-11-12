"""Video catalog: what every YouTube upload is made of (owner 2026-11-12).

Titles are now written by Claude, so a YouTube title no longer tells us which TikTok clip(s)
a video came from. Every upload therefore gets rows here, one per part:

  youtube_video_id, part_no, channel_id, kind (short / longform / merge / single / repost),
  youtube_title (what we put on YouTube), tiktok_video_id, tiktok_caption (the creator's
  original caption = the old title), tiktok_url, seconds, recorded_at

A merged video has one row per clip, in playing order. Reposts copy the parts of their
source video. The table is filled from posted_videos / ai_metadata / merges the first time
it is created, so older uploads are in it too. Recording never breaks an upload.
"""
import json
import logging
import sqlite3
from datetime import datetime

log = logging.getLogger(__name__)


def _has(con, table):
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def ensure(con):
    con.execute("""CREATE TABLE IF NOT EXISTS video_catalog (
        youtube_video_id TEXT, part_no INTEGER, channel_id TEXT, kind TEXT, youtube_title TEXT,
        tiktok_video_id TEXT, tiktok_caption TEXT, tiktok_url TEXT, seconds REAL, recorded_at TEXT,
        PRIMARY KEY (youtube_video_id, part_no))""")
    if not con.execute("SELECT 1 FROM video_catalog LIMIT 1").fetchone():
        _backfill(con)
    con.commit()


def _backfill(con):
    ai = {}
    if _has(con, "ai_metadata"):
        ai = {(c, t): ti for c, t, ti in con.execute("SELECT channel_id, tiktok_video_id, title FROM ai_metadata")}
    caption = {}
    n = 0
    for ch, tid, fmt, cap, url, yt, at in con.execute(
            "SELECT channel_id, tiktok_video_id, format_type, tiktok_title, tiktok_url, youtube_video_id, posted_at "
            "FROM posted_videos WHERE status='uploaded' AND youtube_video_id IS NOT NULL "
            "AND youtube_video_id NOT LIKE 'already%' ORDER BY posted_at"):
        caption[str(tid)] = (cap, url)
        if fmt == "merge":
            continue            # merged videos come from the merges table below
        kind = "repost" if str(tid).startswith("repost:") else fmt
        con.execute("INSERT OR IGNORE INTO video_catalog VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (yt, 1, ch, kind, ai.get((ch, str(tid))) or cap, str(tid), cap, url, None, at))
        n += 1
    if _has(con, "merges"):
        for yt, ch, parts, title, at in con.execute("SELECT youtube_video_id, channel_id, parts, title, made_at FROM merges"):
            ids = json.loads(parts or "[]")
            for k, tid in enumerate(ids, 1):
                cap, url = caption.get(str(tid), (None, None))
                con.execute("INSERT OR REPLACE INTO video_catalog VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (yt, k, ch, "merge" if len(ids) > 1 else "single", title, str(tid), cap, url, None, at))
                n += 1
    log.info("video catalog created from history: %d rows", n)


def record(db_path, youtube_id, channel_id, kind, youtube_title, parts):
    """parts: [{"id", "caption", "url", "seconds"}] in playing order."""
    try:
        con = sqlite3.connect(str(db_path))
        ensure(con)
        now = datetime.utcnow().isoformat()
        with con:
            for k, p in enumerate(parts, 1):
                con.execute("INSERT OR REPLACE INTO video_catalog VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (youtube_id, k, channel_id, kind, youtube_title, str(p.get("id") or ""),
                             p.get("caption"), p.get("url"), p.get("seconds"), now))
        con.close()
    except Exception as exc:  # noqa: BLE001 - never break an upload over the catalog
        log.warning("video catalog: could not record %s: %s", youtube_id, exc)


def parts_of(db_path, youtube_id):
    """Rows (tiktok_video_id, tiktok_url, kind, part_no) of an upload, [] if unknown."""
    try:
        con = sqlite3.connect(str(db_path))
        ensure(con)
        rows = con.execute("SELECT tiktok_video_id, tiktok_url, kind, part_no FROM video_catalog "
                           "WHERE youtube_video_id=? ORDER BY part_no", (youtube_id,)).fetchall()
        con.close()
        return rows
    except Exception:  # noqa: BLE001
        return []
