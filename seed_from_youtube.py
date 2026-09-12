#!/usr/bin/env python3
"""Teach the database what is already on the YouTube channel.

12 Sep 2026: @__muk's channel held 748 uploads while the automation's database
knew about 225 of them. Anything the database has never seen is treated as new,
so its TikTok source gets uploaded again — which is how the same clip appeared
twice in the Shorts tab. The cross-format rule cannot help there: the first copy
was never recorded at all.

This reads the channel's own listing and the TikTok profile, matches them by
title (the uploader uses the TikTok title verbatim) and records the matches as
'skipped', which every picker treats as done.

    python seed_from_youtube.py <channel_id>            # report only
    python seed_from_youtube.py <channel_id> --apply    # write the rows

Run from inside a channel's repo. Needs yt-dlp; no credentials.
"""
import json, os, re, sqlite3, subprocess, sys, unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parent


def norm(title: str) -> str:
    """Compare titles the way a person would: same characters, same words."""
    t = unicodedata.normalize("NFKC", title or "").strip().lower()
    return re.sub(r"\s+", " ", t)


def ytdlp(url: str, template: str) -> list:
    out = subprocess.run(
        ["yt-dlp", "--flat-playlist", "--no-warnings", "--ignore-errors",
         "--print", template, url],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    return [l for l in (out.stdout or "").splitlines() if l.strip()]


def load_channel(channel_id: str) -> dict:
    import yaml
    cfg = yaml.safe_load((REPO / "channels.yaml").read_text(encoding="utf-8"))
    for ch in cfg["channels"]:
        if ch["id"] == channel_id:
            return ch
    raise SystemExit(f"{channel_id} is not in channels.yaml")


def db_path(channel_id: str) -> Path:
    p = REPO / "data" / f"{channel_id}.db"
    return p if p.exists() else REPO / "data" / "automation.db"


def youtube_channel_id(con, channel_id: str) -> str:
    row = con.execute(
        "SELECT youtube_video_id FROM posted_videos WHERE status='uploaded' "
        "AND youtube_video_id IS NOT NULL AND youtube_video_id != 'already_on_yt' "
        "ORDER BY posted_at DESC LIMIT 1").fetchone()
    if not row:
        raise SystemExit("no uploaded video in the database to resolve the channel from")
    line = ytdlp(f"https://youtu.be/{row[0]}", "%(channel_id)s")
    if not line:
        raise SystemExit("could not resolve the YouTube channel from " + row[0])
    return line[0].strip()


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    channel_id = sys.argv[1]
    apply = "--apply" in sys.argv

    ch = load_channel(channel_id)
    dbf = db_path(channel_id)
    con = sqlite3.connect(dbf)
    con.row_factory = sqlite3.Row

    yt_id = youtube_channel_id(con, channel_id)
    print(f"repo      : {REPO.name}")
    print(f"database  : {dbf.name}")
    print(f"youtube   : {yt_id}")
    print(f"tiktok    : @{ch['tiktok_username']}")

    on_channel = {}          # normalised title -> list of youtube ids
    for tab, kind in (("videos", "longform"), ("shorts", "short")):
        for line in ytdlp(f"https://www.youtube.com/channel/{yt_id}/{tab}",
                          "%(id)s|%(title)s"):
            vid, _, title = line.partition("|")
            on_channel.setdefault(norm(title), []).append((vid.strip(), kind))
    total_on_channel = sum(len(v) for v in on_channel.values())
    print(f"\non the channel      : {total_on_channel} uploads, {len(on_channel)} distinct titles")

    tiktoks = []
    for line in ytdlp(f"https://www.tiktok.com/@{ch['tiktok_username']}", "%(id)s|%(title)s"):
        vid, _, title = line.partition("|")
        tiktoks.append({"id": vid.strip(), "title": title, "key": norm(title)})
    print(f"on the TikTok profile: {len(tiktoks)} videos")

    known = {r[0] for r in con.execute(
        "SELECT DISTINCT tiktok_video_id FROM posted_videos")}
    print(f"already in the database: {len(known)} TikTok videos")

    # A title shared by several TikToks cannot be matched safely.
    from collections import Counter
    tk_titles = Counter(t["key"] for t in tiktoks)

    to_mark, ambiguous, unmatched = [], [], 0
    for t in tiktoks:
        if t["id"] in known:
            continue
        hit = on_channel.get(t["key"])
        if not hit:
            unmatched += 1
            continue
        if tk_titles[t["key"]] > 1:
            ambiguous.append(t)
            continue
        yt_vid, kind = hit[0]
        to_mark.append((t, yt_vid, kind))

    print(f"\nalready on YouTube but unknown to the database: {len(to_mark)}")
    print(f"  ambiguous (several TikToks share one title, left alone): {len(ambiguous)}")
    print(f"  genuinely new (not on the channel): {unmatched}")

    for t, yt_vid, kind in to_mark[:6]:
        print(f"    {t['id']}  {kind:<8} youtu.be/{yt_vid}  "
              f"{t['title'][:40].encode('ascii','replace').decode()}")
    if len(to_mark) > 6:
        print(f"    … and {len(to_mark) - 6} more")

    if not apply:
        print("\nreport only — run again with --apply to write these rows")
        return

    with con:
        for t, yt_vid, kind in to_mark:
            con.execute("""
                INSERT OR IGNORE INTO posted_videos
                    (channel_id, tiktok_video_id, format_type, tiktok_url, tiktok_title,
                     youtube_video_id, status, error_message, posted_at)
                VALUES (?, ?, ?, ?, ?, ?, 'skipped',
                        'seeded from the YouTube channel: this clip is already uploaded',
                        CURRENT_TIMESTAMP)
            """, (channel_id, t["id"], kind,
                  f"https://www.tiktok.com/@{ch['tiktok_username']}/video/{t['id']}",
                  t["title"], yt_vid))
    print(f"\nwrote {len(to_mark)} rows — those clips will not be uploaded again")


if __name__ == "__main__":
    main()
