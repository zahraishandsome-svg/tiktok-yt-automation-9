"""What is already on the YouTube channel.

The database only knows what this automation uploaded. On 12 Sep 2026 @__muk's
channel held 748 videos while the database knew about 225 — everything else had
arrived some other way, so its TikTok source looked new and went up a second
time. No amount of tightening the database rule can see those.

So before uploading, ask the channel itself. The channel's own listing is
public, so this needs no credentials; yt-dlp is already a dependency.

The listing is cached per channel and refreshed when it is older than
CACHE_TTL_HOURS, which keeps a daily run to one fetch.
"""

import json
import logging
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from typing import Optional, Set

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
CACHE_DIR = PROJECT_ROOT / "data"
CACHE_TTL_HOURS = 6
FETCH_TIMEOUT_S = 900


def normalise(title: str) -> str:
    """Compare titles the way a person would: same characters, same spacing."""
    t = unicodedata.normalize("NFKC", title or "").strip().lower()
    return re.sub(r"\s+", " ", t)


def _run_ytdlp(url: str, template: str) -> list:
    """yt-dlp is a pip dependency, so run it as a module — the console script is
    not guaranteed to be on PATH inside a CI runner."""
    args = ["--flat-playlist", "--no-warnings", "--ignore-errors", "--print", template, url]
    for cmd in ([sys.executable, "-m", "yt_dlp"], ["yt-dlp"]):
        try:
            out = subprocess.run(cmd + args, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace",
                                 timeout=FETCH_TIMEOUT_S)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            logger.warning("yt-dlp failed for %s: %s", url, exc)
            continue
        lines = [line for line in (out.stdout or "").splitlines() if line.strip()]
        if lines:
            return lines
    return []


def _cache_file(channel_id: str) -> Path:
    return CACHE_DIR / f"{channel_id}_yt_titles.json"


def resolve_youtube_channel(any_video_id: str) -> Optional[str]:
    lines = _run_ytdlp(f"https://youtu.be/{any_video_id}", "%(channel_id)s")
    return lines[0].strip() if lines else None


def load_index(channel_id: str, youtube_channel: Optional[str],
               force_refresh: bool = False) -> Optional[Set[str]]:
    """Normalised titles of everything on the channel, or None if unknown.

    None means "could not find out" — the caller decides what to do with that
    rather than being handed an empty set that looks like an empty channel.
    """
    cache = _cache_file(channel_id)
    cached = None
    if cache.exists():
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cached = None

    fresh_enough = (cached and not force_refresh
                    and (time.time() - cached.get("fetched_at", 0)) < CACHE_TTL_HOURS * 3600)
    if fresh_enough:
        return set(cached.get("titles", []))

    if not youtube_channel and cached:
        youtube_channel = cached.get("youtube_channel")
    if not youtube_channel:
        logger.warning("[%s] no YouTube channel id — cannot check the channel", channel_id)
        return set(cached["titles"]) if cached else None

    titles = set()
    for tab in ("videos", "shorts"):
        for line in _run_ytdlp(f"https://www.youtube.com/channel/{youtube_channel}/{tab}",
                               "%(title)s"):
            key = normalise(line)
            if key:
                titles.add(key)

    if not titles:
        # A fetch that returns nothing is a failure, not an empty channel —
        # keep whatever we knew before rather than forgetting it.
        logger.warning("[%s] channel listing came back empty; keeping the cached one", channel_id)
        return set(cached["titles"]) if cached else None

    CACHE_DIR.mkdir(exist_ok=True)
    cache.write_text(json.dumps({
        "youtube_channel": youtube_channel,
        "fetched_at": int(time.time()),
        "titles": sorted(titles),
    }, ensure_ascii=False), encoding="utf-8")
    logger.info("[%s] channel listing: %d titles already uploaded", channel_id, len(titles))
    return titles


def remember(channel_id: str, title: str) -> None:
    """Add a title we have just uploaded, so the same run cannot repeat it."""
    cache = _cache_file(channel_id)
    if not cache.exists():
        return
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    key = normalise(title)
    if key and key not in data.get("titles", []):
        data.setdefault("titles", []).append(key)
        cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
