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
from PIL import Image, ImageDraw, ImageFilter, ImageFont

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
  clearly visible, a simple uncluttered background that matches the real location, bright
  saturated light, high contrast, shallow depth of field. Leave the OTHER side calmer for
  the text we add later. Say clearly: no text, no letters, no logos, no watermark.
- text_side: which side ("left"/"right") stays free for the text.
- accent_color: one bold hex colour for the text outline/box that suits the image."""


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


def build_base(p, fr, tries: int = 2):
    """Always an AI image (owner rule). Retry once if the face clearly drifted; if neither
    attempt passes, keep the best-scoring one."""
    face_refs = [fr[i][3] for i in p["face_frames"]][:3] or [fr[p["hero_frame"]][3]]
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
        verdict = same_person(cand, face_refs)
        print(f"identity check {n}: score {verdict['score']}/10 same={verdict['same_person']} - {verdict['differences']}")
        if best is None or verdict["score"] > best[1]:
            best = (cand, verdict["score"])
        if verdict["same_person"]:
            return cand, f"ai (check {verdict['score']}/10)"
        feedback = ("Previous attempt changed the person: " + verdict["differences"]
                    + ". Copy the face from the reference frames exactly.")
    return best[0], f"ai, best of {tries} (score {best[1]}/10)"

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
            if draw.textbbox((0, 0), ln, font=f, stroke_width=size // 9)[2] <= box_w:
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
