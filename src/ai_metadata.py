"""AI title + description for long-form uploads (owner rule 2026-11-12: long videos only).

Claude looks at a few small frames of the clip and the TikTok caption, then writes a
YouTube-style title and description in the channel's language. Kept cheap: 8 frames at
384 px, Sonnet, medium effort, short output (~$0.01 per video). Any failure returns None and
the upload falls back to the TikTok caption, so an upload is never blocked.

Channel config: ai_longform_metadata: Japanese / English   (language; unset = off)
"""
import base64
import json
import logging
import os
import sqlite3
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-5-5"
N_FRAMES = 8

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["what_happens", "title", "description"],
    "properties": {
        "what_happens": {"type": "string"},
        "title": {"type": "string"},
        "description": {"type": "string"},
    },
}

SYSTEM = """You write YouTube titles and descriptions for long-form videos. Each video is a real
clip reposted from the creator's TikTok (any niche: family, food, crafts, pets, comedy, ...);
you see frames from it in order plus the creator's original caption.

First work out what actually happens in the clip (who, what, the funny / sweet / surprising /
satisfying moment). The caption is written by the creator and is the TRUTH about the story:
who made or did what, who is who, how they feel. Keep the caption's meaning (e.g. if the
caption says someone else drew the picture, never say the child drew it). Use a person's name
only if it is written in the caption; otherwise use plain roles (son, mom, the chef ...;
Japanese: 息子 / 娘 / パパ / ママ / 赤ちゃん). Strip @handles.
Then write, in the requested language, the way a good native YouTuber of this genre writes:

TITLE: natural and clickable, built on the real moment of THIS clip, makes people curious
without lying. Japanese: 20-45 characters, may use 【】 when natural. English and other
languages: 40-70 characters, normal sentence or title case. At most one fitting emoji. No
hashtags in the title, no clickbait that the clip does not deliver, no ALL CAPS. Never copy
the caption word for word. Must be clearly different from the channel's earlier titles.

DESCRIPTION: 2-4 short lines that describe the moment warmly in the same language (what
happens, the feeling), one gentle line inviting viewers to subscribe / comment, then a last
line with 3-5 relevant hashtags. No links, no timestamps, no made-up facts (names, ages,
places, prices) that the caption does not state - this also applies to the hashtags (no
#3歳 / #3yearsold etc. unless the caption says the age). Under 600 characters."""


def _frames(video: Path, outdir: Path) -> List[Path]:
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "csv=p=0", str(video)], capture_output=True, text=True).stdout)
    out = []
    for i in range(N_FRAMES):
        f = outdir / f"m{i:02d}.jpg"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{dur * (i + 0.5) / N_FRAMES:.2f}",
                        "-i", str(video), "-frames:v", "1", "-vf", "scale=384:-2", "-q:v", "4", str(f)],
                       check=True)
        out.append(f)
    return out


def _table(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.execute("""CREATE TABLE IF NOT EXISTS ai_metadata (
        tiktok_video_id TEXT, channel_id TEXT, title TEXT, description TEXT, made_at TEXT,
        PRIMARY KEY (channel_id, tiktok_video_id))""")
    return con


def recent_titles(db_path: Path, channel_id: str, n: int = 40) -> List[str]:
    try:
        con = _table(db_path)
        rows = con.execute("SELECT title FROM ai_metadata WHERE channel_id=? ORDER BY made_at DESC LIMIT ?",
                           (channel_id, n)).fetchall()
        con.close()
        return [r[0] for r in rows if r[0]]
    except Exception:  # noqa: BLE001
        return []


def title_for(db_path: Path, channel_id: str, tiktok_video_id: str) -> Optional[str]:
    try:
        con = _table(db_path)
        r = con.execute("SELECT title FROM ai_metadata WHERE channel_id=? AND tiktok_video_id=?",
                        (channel_id, tiktok_video_id)).fetchone()
        con.close()
        return r[0] if r else None
    except Exception:  # noqa: BLE001
        return None


def write(video_path: Path, caption: str, lang: str, channel_id: str, tiktok_video_id: str,
          db_path: Path) -> Optional[dict]:
    """Return {"title", "description"} or None (caller keeps the old caption-based text)."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.info("[%s] ai_metadata: no ANTHROPIC_API_KEY - using caption", channel_id)
        return None
    try:
        import anthropic
        with tempfile.TemporaryDirectory(prefix="meta_") as tmp:
            content = []
            for i, f in enumerate(_frames(Path(video_path), Path(tmp))):
                content.append({"type": "text", "text": f"frame {i + 1}"})
                content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                            "data": base64.b64encode(f.read_bytes()).decode()}})
        content.append({"type": "text", "text": f"Original caption: {caption or '(none)'}\nLanguage: {lang}"})
        used = recent_titles(db_path, channel_id)
        if used:
            content.append({"type": "text", "text": "Titles this channel already used (write a clearly "
                            "different one): " + " / ".join(used)})
        resp = anthropic.Anthropic().messages.create(
            model=MODEL, max_tokens=4000, system=SYSTEM,
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": content}],
        )
        if resp.stop_reason != "end_turn":
            raise RuntimeError(f"stop_reason={resp.stop_reason}")
        d = json.loads(next(b.text for b in resp.content if b.type == "text"))
        title, desc = d["title"].strip(), d["description"].strip()
        if len(title) < 5:
            raise RuntimeError("title too short")
        u = resp.usage
        logger.info("[%s] ai_metadata: '%s' (%d in / %d out tokens)", channel_id, title,
                    u.input_tokens, u.output_tokens)
        con = _table(db_path)
        with con:
            con.execute("INSERT OR REPLACE INTO ai_metadata VALUES (?,?,?,?,?)",
                        (tiktok_video_id, channel_id, title, desc, datetime.utcnow().isoformat()))
        con.close()
        return {"title": title, "description": desc}
    except Exception as exc:  # noqa: BLE001 - never block an upload
        logger.warning("[%s] ai_metadata failed, using caption: %s", channel_id, str(exc)[:200])
        return None
