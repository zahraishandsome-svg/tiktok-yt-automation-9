"""AI thumbnail for one long-form video.

1. 12 frames evenly across the video.
2. Claude (Opus 5.5) watches the frames + caption, picks the main character's clearest
   face frames and the strongest moment, writes a short hook in the audience's language
   and an image brief.
3. Gemini image model on Vertex AI composes a 16:9 scene from the real frames (character
   kept identical), with NO text.
4. We draw the hook ourselves with Noto Sans JP Black, so every character is exact.

Used by thumbnails.py (daily + backfill). Standalone: python -m src.ai_thumbnail <video.mp4> <caption> <lang> <out.jpg>
Env:   ANTHROPIC_API_KEY, VERTEX_API_KEY, VERTEX_PROJECT
"""
import base64
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import anthropic
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageStat

HERE = Path(__file__).parent
FONT = Path(os.environ.get("THUMB_FONT", HERE.parent / "fonts" / "NotoSansJP.ttf"))
N_FRAMES = 12

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["what_happens", "main_character", "face_frames", "hero_frame",
                 "hook_text", "hook_lines", "hook_meaning_en", "image_brief", "text_side", "accent_color"],
    "properties": {
        "what_happens": {"type": "string"},
        "main_character": {"type": "string"},
        "face_frames": {"type": "array", "items": {"type": "integer"}},
        "hero_frame": {"type": "integer"},
        "hook_text": {"type": "string"},
        "hook_lines": {"type": "array", "items": {"type": "string"}},
        "hook_meaning_en": {"type": "string"},
        "image_brief": {"type": "string"},
        "text_side": {"type": "string", "enum": ["left", "right"]},
        "accent_color": {"type": "string"},
    },
}

SYSTEM = """You design YouTube thumbnails for long-form videos made from short vertical clips.
You get frames sampled evenly through one video (labelled f00..f11 with their time) and the
video's caption. Work out what actually happens and who the main character is.

Return:
- what_happens: one or two plain sentences.
- main_character: who they are and how they look (age, hair, clothes), as seen in the frames.
- face_frames: 1-3 frame numbers where the main character's FACE is clearest and most
  expressive (eyes visible, not blurred, not turned away).
- hero_frame: the single frame with the strongest, most clickable moment of the video.
- hook_text: the text that goes on the thumbnail, in the requested language, 2-7 characters
  for Japanese (or 1-4 words for English). Punchy, emotional, curiosity-raising, and TRUE to
  the video (never promise something that does not happen). No hashtags, no emoji.
- hook_lines: the same hook stacked into 1-3 SHORT lines (Japanese: ideally 2-4 characters
  per line) so the text can be printed very large. Break only at natural boundaries, never
  inside a word, e.g. 「急に」/「赤ちゃん」/「返り」.
- hook_meaning_en: the hook translated to English.
IDENTITY RULE (most important): the main character is the face of this channel. In the
thumbnail they must be unmistakably the SAME person as in the frames: identical facial
features (eye shape and size, eyebrows, nose, mouth, face shape, cheeks, ears), same age,
same hair (colour, length, style, fringe), same skin tone, same body size, same clothes.
Never beautify, idealise, age up/down, slim, change ethnicity or 'improve' the face.
Only the expression, pose, light and background may be chosen for the thumbnail.

- image_brief: an English instruction for an image model that will receive the hero frame
  and the face frames as reference images. Describe a 16:9 thumbnail scene: the same main
  character (identical face, hair, clothes, age - copied from the references) large and
  sharp on one side with a strong, readable expression, the key object/action of the video
  clearly visible and big, shallow depth of field. If the person's face never appears in
  the frames (a hands-only video), do not invent a face: the hands and the key object are
  the hero.
  COLOUR RULE (owner 2026-11-12, a white, washed-out thumbnail was rejected as "too white
  and basic"): the image must be RICH and WARM - deep, saturated colours, mid-tones,
  cinematic warm light (lamp light, golden evening light) with real depth and contrast.
  The background is the same kind of place as the real video but colourful and darker
  than the subject: coloured walls, wood, fabrics, shelves or props that suit the topic,
  softly blurred. Spell out in the brief: NEVER a white, off-white, light-grey or pastel
  background, never a plain empty wall, never overexposed, no blown-out highlights, no
  white table or white floor filling the frame. If the real room is white or plain,
  re-dress and re-light it warmly instead of copying the white.
  The OTHER side (where we add the text later) is a softer, out-of-focus, darker part of
  the SAME colourful scene - never an empty panel, blank block, flat colour area, border
  or split screen; the scene fills the whole frame edge to edge.
  Say clearly: no text, no letters, no logos, no watermark.
- text_side: which side ("left"/"right") stays free for the text.
- accent_color: one bold, saturated hex colour for the text outline that suits the image
  (never white, pastel or light pink)."""

# Tone gate (owner 2026-11-12: AYA's first thumbnail was "too white aur basic"). Measured
# on the base image before the text goes on. Rejected: AYA kitty bag (luminance 184,
# saturation 51, 52% white, blank pale panel); kept ch8 thumbnails sit at luminance 97-141,
# saturation 89-132, white 6-11%.
LUM_MIN, LUM_MAX, SAT_MIN, WHITE_MAX, BLANK_MAX = 55, 150, 70, 0.15, 0.30


def frames(video: Path, outdir: Path):
    outdir.mkdir(parents=True, exist_ok=True)
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "csv=p=0", str(video)], capture_output=True, text=True).stdout)
    out = []
    for i in range(N_FRAMES):
        t = dur * (i + 0.5) / N_FRAMES
        small, full = outdir / f"f{i:02d}.jpg", outdir / f"full_f{i:02d}.jpg"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(video),
                        "-frames:v", "1", "-vf", "scale=540:-2", "-q:v", "3", str(small)], check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(video),
                        "-frames:v", "1", "-q:v", "2", str(full)], check=True)
        out.append((i, round(t, 1), small, full))
    return out


def plan(fr, caption: str, lang: str, avoid=None) -> dict:
    client = anthropic.Anthropic()
    content = []
    for i, t, small, _ in fr:
        content.append({"type": "text", "text": f"f{i:02d} at {t}s"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.b64encode(small.read_bytes()).decode()}})
    content.append({"type": "text", "text": f"Caption: {caption}\nThumbnail text language: {lang}"})
    if avoid:
        content.append({"type": "text", "text": "Hooks this channel already used (write a DIFFERENT one, "
                        "never repeat or closely copy these): " + " / ".join(avoid)})
    resp = client.messages.create(
        model="claude-opus-5-5",
        max_tokens=16000,
        system=SYSTEM,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": content}],
    )
    if resp.stop_reason == "refusal":
        raise SystemExit("Claude declined this video")
    text = next(b.text for b in resp.content if b.type == "text")
    u = resp.usage
    cost = u.input_tokens * 4e-6 + u.output_tokens * 20e-6
    print(f"claude: {u.input_tokens} in / {u.output_tokens} out tokens ~ ${cost:.3f}")
    return json.loads(text)


def image(p: dict, fr, feedback: str = "") -> Image.Image:
    refs = [fr[p["hero_frame"]][3]] + [fr[i][3] for i in p["face_frames"] if i != p["hero_frame"]][:2]
    parts = [{"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(r.read_bytes()).decode()}}
             for r in refs]
    parts.append({"text": "The attached images are real frames from the video; the main character "
                          "must look exactly like them. IDENTITY LOCK: keep the main character's "
                          "facial features identical to the reference frames - eye shape and size, "
                          "eyebrows, nose, mouth, face shape, cheeks, ears, skin tone, age, hair "
                          "colour/length/style and clothes. Do not beautify, idealise, age, slim or "
                          "otherwise change the face; it must be recognisably the same real person. "
                          + p["image_brief"] + (" " + feedback if feedback else "")})
    body = {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": "16:9"}}}
    url = (f"https://aiplatform.googleapis.com/v1/projects/{os.environ['VERTEX_PROJECT']}/locations/global"
           f"/publishers/google/models/gemini-3.1-flash-image:generateContent")
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "x-goog-api-key": os.environ["VERTEX_API_KEY"]})
    # Vertex answers 429 when a new trial project goes over its per-minute image quota;
    # wait and retry instead of failing the video
    for attempt in range(8):
        try:
            out = json.load(urllib.request.urlopen(req, timeout=240))
            break
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 503) or attempt == 7:
                raise
            wait = 20 * (attempt + 1)
            print(f"vertex {exc.code} - waiting {wait}s (attempt {attempt + 1})", flush=True)
            time.sleep(wait)
    cand = (out.get("candidates") or [{}])[0]
    data = next((x["inlineData"]["data"] for x in (cand.get("content") or {}).get("parts", [])
                 if "inlineData" in x), None)
    if data is None:
        # no image came back (e.g. the model's own safety filter): say why and let the
        # caller try again
        raise RuntimeError(f"no image returned (finishReason={cand.get('finishReason')}, "
                           f"block={out.get('promptFeedback', {}).get('blockReason')})")
    return Image.open(io.BytesIO(base64.b64decode(data))).convert("RGB").resize((1280, 720), Image.LANCZOS)


CHECK_SCHEMA = {"type": "object", "additionalProperties": False,
                "required": ["same_person", "score", "differences"],
                "properties": {"same_person": {"type": "boolean"},
                               "score": {"type": "integer"},
                               "differences": {"type": "string"}}}


def same_person(candidate: Image.Image, face_refs) -> dict:
    """Claude compares the generated thumbnail with the real face frames."""
    buf = io.BytesIO(); candidate.save(buf, "JPEG", quality=88)
    content = [{"type": "text", "text": "REAL frames from the video (the true main character):"}]
    for r in face_refs:
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.b64encode(r.read_bytes()).decode()}})
    content += [{"type": "text", "text": "GENERATED thumbnail:"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": base64.b64encode(buf.getvalue()).decode()}},
                {"type": "text", "text": "Is the main character in the generated thumbnail clearly the SAME "
                 "real person as in the real frames? Judge facial features (eyes, eyebrows, nose, mouth, "
                 "face shape), age, hair, skin tone and clothes; ignore expression, pose, light and "
                 "background. Small differences are fine (owner, 2026-11-12: minor mistakes are OK, it only "
                 "has to clearly read as the same person). score 1-10 (10 = indistinguishable). same_person "
                 "if score >= 6. "
                 "differences: list what is off, or 'none'."}]
    resp = anthropic.Anthropic().messages.create(
        model="claude-opus-5-5", max_tokens=4000,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": CHECK_SCHEMA}},
        messages=[{"role": "user", "content": content}])
    return json.loads(next(b.text for b in resp.content if b.type == "text"))


def tone(img: Image.Image, side: str = "left") -> dict:
    """Is the base image rich and warm, or white / washed out / half empty?"""
    im = img.convert("RGB").resize((320, 180))
    L, S = im.convert("L"), im.convert("HSV").split()[1]
    lum, sat = ImageStat.Stat(L).mean[0], ImageStat.Stat(S).mean[0]
    wm = ImageChops.multiply(L.point(lambda v: 255 if v > 225 else 0), S.point(lambda v: 255 if v < 40 else 0))
    white = ImageStat.Stat(wm).mean[0] / 255
    x0 = 0 if side == "left" else 160
    blank = n = 0
    for y in range(0, 180, 20):                 # bright, featureless blocks on the text side
        for x in range(x0, x0 + 160, 20):
            st = ImageStat.Stat(L.crop((x, y, x + 20, y + 20)))
            n += 1
            blank += st.stddev[0] < 7 and st.mean[0] > 170
    blank /= n
    why = []
    if lum > LUM_MAX:
        why.append(f"too bright (mean luminance {lum:.0f}, max {LUM_MAX})")
    if lum < LUM_MIN:
        why.append(f"too dark (mean luminance {lum:.0f}, min {LUM_MIN})")
    if sat < SAT_MIN:
        why.append(f"washed-out colour (saturation {sat:.0f}, min {SAT_MIN})")
    if white > WHITE_MAX:
        why.append(f"{white:.0%} of the frame is white or near-white (max {WHITE_MAX:.0%})")
    if blank > BLANK_MAX:
        why.append(f"{blank:.0%} of the text side is a blank pale area (max {BLANK_MAX:.0%})")
    return {"ok": not why, "lum": round(lum), "sat": round(sat), "white": round(white, 2),
            "blank": round(blank, 2), "why": "; ".join(why)}


def grade(img: Image.Image, side: str = "left") -> Image.Image:
    """Last resort when no attempt passed the tone gate: pull the highlights down and
    lift the colour step by step until it passes (at most 4 steps)."""
    for _ in range(4):
        t = tone(img, side)
        if t["ok"]:
            break
        if t["lum"] > LUM_MAX or t["white"] > WHITE_MAX or t["blank"] > BLANK_MAX:
            img = img.point(lambda v: int(232 * (v / 255) ** 1.25))     # darker mids, whites pulled down
            img = ImageEnhance.Contrast(img).enhance(1.08)
        if t["sat"] < SAT_MIN or t["lum"] > LUM_MAX:
            img = ImageEnhance.Color(img).enhance(1.25)
    return img


def build_base(p, fr, tries: int = 3):
    """Always an AI image (owner rule). An attempt is kept only when the face matches the
    video AND the colour passes the tone gate (no white / washed-out / half-empty images).
    Otherwise retry with the reason as feedback; after the last try keep the best one and
    grade it if its colour still fails."""
    face_refs = [fr[i][3] for i in p["face_frames"]][:3] or [fr[p["hero_frame"]][3]]
    side = p.get("text_side", "left")
    feedback, best = "", None
    for n in range(1, tries + 1):
        try:
            cand = image(p, fr, feedback)
        except RuntimeError as exc:          # empty answer from the image model
            print(f"image attempt {n}: {exc}")
            if n == tries and best is None:
                cand = image(p, fr, "Keep it wholesome and family-friendly.")   # last try
            else:
                feedback = "Keep it wholesome and family-friendly."
                continue
        t = tone(cand, side)
        verdict = same_person(cand, face_refs)
        print(f"identity check {n}: score {verdict['score']}/10 same={verdict['same_person']} - {verdict['differences']}")
        print(f"tone check {n}: {'ok' if t['ok'] else t['why']} (lum {t['lum']}, sat {t['sat']}, "
              f"white {t['white']:.0%}, blank {t['blank']:.0%})")
        rank = (verdict["same_person"], t["ok"], verdict["score"])
        if best is None or rank > best[1]:
            best = (cand, rank, t)
        if verdict["same_person"] and t["ok"]:
            return cand, f"ai (check {verdict['score']}/10, tone ok)"
        fixes = []
        if not verdict["same_person"]:
            fixes.append("Previous attempt changed the person: " + verdict["differences"]
                         + ". Copy the face from the reference frames exactly.")
        if not t["ok"]:
            fixes.append("Previous attempt was rejected as too white and plain: " + t["why"] + ". "
                         "Make it rich and warm: deep saturated colours, warm cinematic light, a colourful "
                         "darker background of the same kind of place, no white or pastel walls, floors or "
                         "tables, no blown-out highlights, no empty area - the scene fills the whole frame.")
        feedback = " ".join(fixes)
    img, (same, tone_ok, score), t = best
    how = f"ai, best of {tries} (score {score}/10)"
    if not tone_ok:
        img = grade(img, side)
        g = tone(img, side)
        print(f"tone graded: {'ok' if g['ok'] else g['why']} (lum {g['lum']}, sat {g['sat']}, white {g['white']:.0%})")
        how += ", colour graded"
    return img, how

def _font(size):
    f = ImageFont.truetype(str(FONT), size)
    try:
        f.set_variation_by_axes([900])  # Black weight
    except Exception:
        pass
    return f


def add_text(img: Image.Image, lines: list, side: str, accent: str) -> Image.Image:
    """Each line gets its own size: as large as fits the free half (short lines grow
    bigger), then the block is scaled down together if it is too tall."""
    W, H = img.size
    draw = ImageDraw.Draw(img)
    lines = [ln for ln in lines if ln.strip()][:3]
    box_w, max_h = int(W * 0.47), int(H * 0.84)

    def fit(ln):
        size = 300
        while size > 50:
            f = _font(size)
            if draw.textbbox((0, 0), ln, font=f, stroke_width=size // 9 + size // 24)[2] <= box_w:
                return size
            size -= 6
        return size

    sizes = [fit(ln) for ln in lines]
    # one size for every line (the largest that fits the widest line) reads as one bold
    # block; short stacked lines are what make that size big
    sizes = [min(sizes)] * len(sizes)
    total = sum(int(s * 1.12) for s in sizes)
    if total > max_h:
        sizes = [int(s * max_h / total) for s in sizes]
        total = sum(int(s * 1.12) for s in sizes)
    x0 = int(W * 0.03) if side == "left" else W - box_w - int(W * 0.03)
    y = (H - total) // 2
    for ln, size in zip(lines, sizes):
        f = _font(size)
        sw = max(6, size // 9)
        w = draw.textbbox((0, 0), ln, font=f, stroke_width=sw)[2]
        x = x0 + (box_w - w) // 2
        shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(shadow).text((x + 8, y + 10), ln, font=f, fill=(0, 0, 0, 170),
                                    stroke_width=sw, stroke_fill=(0, 0, 0, 170))
        blur = shadow.filter(ImageFilter.GaussianBlur(6))
        img.paste(blur, (0, 0), blur)
        draw = ImageDraw.Draw(img)
        # thin dark edge outside the colour outline, so the text pops on any background
        draw.text((x, y), ln, font=f, fill="#111111", stroke_width=sw + max(4, size // 24), stroke_fill="#111111")
        draw.text((x, y), ln, font=f, fill="white", stroke_width=sw, stroke_fill=accent)
        y += int(size * 1.12)
    return img

if __name__ == "__main__":
    video, caption, lang, out = Path(sys.argv[1]), sys.argv[2], sys.argv[3], Path(sys.argv[4])
    fr = frames(video, out.parent / "frames")
    p = plan(fr, caption, lang)
    print(json.dumps(p, ensure_ascii=False, indent=1))
    base, how = build_base(p, fr)
    print("base image:", how)
    base.save(out.with_name(out.stem + "_base.jpg"), quality=92)
    final = add_text(base.copy(), p.get("hook_lines") or [p["hook_text"]], p["text_side"], p["accent_color"])
    final.save(out, quality=90)
    print("saved", out, out.stat().st_size, "bytes")
