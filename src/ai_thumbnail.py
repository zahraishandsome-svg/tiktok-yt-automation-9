"""AI thumbnail for one long-form video.

1. 12 frames evenly across the video.
2. Claude (Opus 5.5) watches the frames + caption, picks the main character's clearest
   face frames and the strongest moment, writes a short hook in the audience's language
   and an image brief.
3. Gemini image model on Vertex AI makes the WHOLE 16:9 thumbnail from the real frames,
   including the hook text (owner 2026-11-12: no manual/code editing of thumbnails at all,
   and nothing in the picture that is not in the video).
4. Claude reviews every image: same person, exact text, nothing invented; a failing image
   is fixed by the image model (AI edit) or made again - never by code.

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
from PIL import Image, ImageChops, ImageStat

HERE = Path(__file__).parent
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
  FIDELITY RULE (owner 2026-11-12, mandatory): only people, objects, food, props and places
  that appear in the frames or the caption. Choose the framing, angle, moment and light,
  but never add props, decorations, furniture, food, packaging, rooms or people that are
  not in the video. Keep the real place (e.g. the real white marble kitchen) - do not swap
  it for a different room.
  COLOUR RULE (owner 2026-11-12, a white, washed-out thumbnail was rejected as "too white
  and basic"): the image must be RICH and WARM - deep, saturated, true colours, mid-tones,
  warm light with depth and contrast. Get the colour from the REAL scene: frame tight on
  the colourful real items (food, goods, packaging, clothes) so they fill the frame and
  plain white walls, floors or tables take very little space; no blown-out highlights.
  ONE continuous photograph from one camera, edge to edge: no seam, no second picture, no
  blurred side panel, no collage, no split screen.
  The headline text is rendered by the image model on the text_side half (we add the exact
  words to the prompt ourselves): keep that half's content secondary (other real items of
  the scene) so the text can sit on it; the hero stays fully on the other half.
  Do not write the headline words into image_brief. Say clearly: no logos, no watermark,
  no other text, captions, stickers or emoji.
- text_side: which side ("left"/"right") carries the text.
- accent_color: one bold, saturated hex colour for the text outline that suits the image
  (never white, pastel or light pink)."""

# Tone gate (owner 2026-11-12: AYA's first thumbnail was "too white aur basic"). Measured
# on the finished image (text included). Rejected: AYA kitty bag (luminance 184,
# saturation 51, 52% white, blank pale panel); kept ch8 thumbnails sit at luminance 97-141,
# saturation 89-132, white 6-11%.
LUM_MIN, LUM_MAX, SAT_MIN, WHITE_MAX, BLANK_MAX = 55, 150, 70, 0.15, 0.30

# Per-niche thumbnail styles, chosen per channel (thumbnails.py / manual runs pass style=...).
# jp_vlog = Japanese mum / housewife daily-life vlogs (AYA). Owner 2026-11-12: AYA CTR < 2%,
# "make it like the other YouTubers in this niche". Study of the 48 most-viewed niche videos
# (aya vlog, mnm.myth's vlog, yuki room, TOMO三児の母, Sena, 投げやり主婦 ...): real-looking home
# photos packed with the video's stuff, white only ~5% of the frame (luminance ~134,
# saturation ~72 - natural, not neon), big heavy gothic hook in white/yellow with a thick
# black outline plus a small identity label (「3児のシングルマザー」「4児シンママ」), numbers.
STYLES = {
    "jp_vlog": {
        "limits": {"SAT_MIN": 50, "LUM_MAX": 155, "WHITE_MAX": 0.18},   # incl. the white headline
        "notes": """
NICHE STYLE - Japanese mum / housewife daily-life vlog (owner 2026-11-12). Copy what the top
videos of this niche do, not a cinematic look (these rules override the general light and
colour wording above where they differ; still never white/pastel empty areas or blank panels):
- image_brief: a REAL-LOOKING bright, colourful photo taken in the creator's real home as
  the frames show it, true-to-life colours, natural daylight plus warm room light. Not a
  movie still, not a studio render, not a dark moody room.
- ABUNDANCE is the hook: the video's stuff fills at least two thirds of the frame, big and
  close - the whole haul spread out, the packed fridge/pantry, the full dinner spread, the
  finished result (e.g. the bag completely covered in goods, the hamburg cut open with the
  egg yolk showing) - only the real items from the video, nothing added. Colourful
  packaging and food, no empty table, floor or wall.
- ONE continuous photo edge to edge (one camera, one moment). The text sits over the busy
  photo, so do NOT reserve a blurred or separate area for it: no second photo, no
  blurred side panel, no collage, no split screen, no seam.
- The hero/result the hook talks about (e.g. the cut-open egg hamburg, the decorated bag)
  sits fully inside the half OPPOSITE text_side, big and sharp; the text side shows
  secondary items of the same scene (other dishes, packages, goods) that the text may
  cover. Write this placement into the image_brief with left/right spelled out.
- Packaging labels stay small, turned away or soft; no big readable brand names or fake
  writing on boxes and bottles.
- Show the creator the way the video shows her: her hands only if her face never appears
  (never invent a face); her face/body only if the video shows it.
- label_text: a small context/identity tag printed in a coloured box above the hook, 4-12
  characters, TRUE to the caption/video, in the niche's words, e.g. 「シンママの夜ご飯」
  「JK娘のスクバ」「週末の補充」. Use the creator's own words (#シングルマザー etc.) only if
  the caption uses them.
- hook_lines: 1-2 big lines, the strongest curiosity/result words; use a true number or
  amount when the video has one (「15点」「1週間分」「卵まるごと」).
- highlight_line: index of the hook line printed in YELLOW (the others are white), or -1.""",
    },
}


def limits(style=None) -> dict:
    lim = {"LUM_MIN": LUM_MIN, "LUM_MAX": LUM_MAX, "SAT_MIN": SAT_MIN, "WHITE_MAX": WHITE_MAX, "BLANK_MAX": BLANK_MAX}
    lim.update(STYLES.get(style, {}).get("limits", {}))
    return lim


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


def plan(fr, caption: str, lang: str, avoid=None, style=None) -> dict:
    client = anthropic.Anthropic()
    system, schema = SYSTEM, SCHEMA
    if style in STYLES:
        system = SYSTEM + "\n" + STYLES[style]["notes"]
        schema = json.loads(json.dumps(SCHEMA))
        schema["required"] += ["label_text", "highlight_line"]
        schema["properties"]["label_text"] = {"type": "string"}
        schema["properties"]["highlight_line"] = {"type": "integer"}
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
        system=system,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": content}],
    )
    if resp.stop_reason == "refusal":
        raise SystemExit("Claude declined this video")
    text = next(b.text for b in resp.content if b.type == "text")
    u = resp.usage
    cost = u.input_tokens * 4e-6 + u.output_tokens * 20e-6
    print(f"claude: {u.input_tokens} in / {u.output_tokens} out tokens ~ ${cost:.3f}")
    p = json.loads(text)
    p["_lang"], p["_style"] = lang, style
    return p


def text_spec(p: dict) -> str:
    """The exact headline the image model must paint (owner: no code-drawn text)."""
    lines = [ln.strip() for ln in (p.get("hook_lines") or [p["hook_text"]]) if ln.strip()]
    side, accent = p.get("text_side", "left"), p.get("accent_color", "#E0102F")
    jp = str(p.get("_lang", "")).lower().startswith("jap")
    font = ("an extra-heavy, very bold Japanese gothic sans-serif (like Noto Sans JP Black)" if jp
            else "an extra-heavy condensed bold sans-serif (like Impact)")
    quoted = ", ".join(f'line {i + 1}: "{ln}"' for i, ln in enumerate(lines))
    if p.get("_style") == "jp_vlog":
        hl = p.get("highlight_line", -1)
        colours = "; ".join(f'line {i + 1} {"bright yellow" if i == hl else "white"}' for i in range(len(lines)))
        label = (p.get("label_text") or "").strip()
        lab = (f'At the top of the text block a small label in a rounded {accent} box with a thin black border, white bold letters: '
               f'"{label}". ' if label else "")
        look = (f"{lab}Below it the headline, {len(lines)} stacked line(s): {quoted}. Huge letters in {font}, with a "
                f"THICK black outline and a soft drop shadow ({colours}).")
    else:
        look = (f"{len(lines)} stacked line(s): {quoted}. Huge white letters in {font}, with a THICK {accent} "
                f"outline, a thin black outer edge and a soft drop shadow.")
    return ("THUMBNAIL TEXT - paint it into the image yourself, EXACTLY these characters, nothing more, nothing "
            f"less, every character complete and correct: {look} Place the text block on the {side} half, "
            "vertically centred, as large as fits that half (it must read instantly on a phone), never over the "
            "hero, and keep the bottom-right corner clear. Apart from this headline there is NO other text, "
            "letters, numbers, logos, watermark, captions, stickers or emoji anywhere.")


def image(p: dict, fr, feedback: str = "", edit: Image.Image = None) -> Image.Image:
    """Make the thumbnail; with edit=<previous image> the model changes only what feedback asks."""
    refs = [fr[p["hero_frame"]][3]] + [fr[i][3] for i in p["face_frames"] if i != p["hero_frame"]][:2]
    parts = [{"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(r.read_bytes()).decode()}}
             for r in refs]
    if edit is not None:
        buf = io.BytesIO()
        edit.save(buf, "JPEG", quality=92)
        parts.append({"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(buf.getvalue()).decode()}})
        parts.append({"text": "The LAST attached image is the current thumbnail; the others are real frames from "
                              "the video. Edit the current thumbnail and change ONLY this: " + feedback + " Keep "
                              "everything else exactly as it is (people, hands, objects, layout, colours). "
                              + text_spec(p) + " 16:9."})
        return _vertex(parts)
    parts.append({"text": "The attached images are real frames from the video; the main character "
                          "must look exactly like them. IDENTITY LOCK: keep the main character's "
                          "facial features identical to the reference frames - eye shape and size, "
                          "eyebrows, nose, mouth, face shape, cheeks, ears, skin tone, age, hair "
                          "colour/length/style and clothes. Do not beautify, idealise, age, slim or "
                          "otherwise change the face; it must be recognisably the same real person. "
                          "The frames may show the creator's own on-screen captions, stickers or emoji: "
                          "never copy them. Show only things that are in the video. "
                          + p["image_brief"] + " " + text_spec(p) + (" " + feedback if feedback else "")})
    return _vertex(parts)


def _vertex(parts) -> Image.Image:
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
        except (TimeoutError, urllib.error.URLError, ConnectionError) as exc:
            # a slow or dropped answer (seen 2026-11-12: read timed out) - try again, not fail
            if attempt >= 2:
                raise RuntimeError(f"vertex unreachable: {exc}")
            print(f"vertex {type(exc).__name__}: {exc} - retrying (attempt {attempt + 1})", flush=True)
            time.sleep(10)
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
                "required": ["same_person", "score", "differences", "text_read", "text_exact", "invented_items"],
                "properties": {"same_person": {"type": "boolean"},
                               "score": {"type": "integer"},
                               "differences": {"type": "string"},
                               "text_read": {"type": "string"},
                               "text_exact": {"type": "boolean"},
                               "invented_items": {"type": "array", "items": {"type": "string"}}}}


def review(candidate: Image.Image, p: dict, fr) -> dict:
    """Claude checks one thumbnail against the whole video: same person, exact text, and
    nothing in it that the video does not show (owner 2026-11-12)."""
    buf = io.BytesIO()
    candidate.save(buf, "JPEG", quality=88)
    content = [{"type": "text", "text": "REAL frames from the video, f00..f11:"}]
    for i, t, small, _ in fr:
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.b64encode(small.read_bytes()).decode()}})
    lines = [ln for ln in (p.get("hook_lines") or [p["hook_text"]]) if ln.strip()]
    expected = ((f'label "{p["label_text"]}" + ' if p.get("label_text") else "")
                + " / ".join(f'"{ln}"' for ln in lines))
    if p.get("face_frames"):
        person = ("1) PERSON: is the main character clearly the SAME real person as in the frames ("
                  + ", ".join(f"f{i:02d}" for i in p["face_frames"]) + ")? Judge face, age, hair, skin tone "
                  "and clothes; ignore expression, pose, light. Small differences are fine. score 1-10, "
                  "same_person if >= 6.")
    else:
        person = ("1) PERSON: the video never shows a face, so set same_person true and score 10, unless the "
                  "thumbnail shows an invented face (then same_person false and say so in differences).")
    content += [{"type": "text", "text": "GENERATED thumbnail:"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": base64.b64encode(buf.getvalue()).decode()}},
                {"type": "text", "text": person
                 + f"\n2) TEXT: the thumbnail must show exactly {expected} and no other text. text_read = every "
                 "piece of text you can read in the thumbnail, exactly. text_exact = true only if the headline "
                 "matches character for character (no missing, extra, wrong or garbled characters) and there is "
                 "no other readable text, logo, caption or emoji (tiny unreadable packaging print is fine).\n"
                 "3) FAKES: invented_items = things in the thumbnail that do NOT appear anywhere in the frames "
                 "or the caption: objects, food, props, decorations, furniture, packaging, a different room or "
                 "place, extra people. Ignore light, colour, angle, blur, crop and the headline itself. The same "
                 "kind of item in a slightly different arrangement is fine. [] if none.\n"
                 f"What happens in the video: {p.get('what_happens', '')}"}]
    resp = anthropic.Anthropic().messages.create(
        model="claude-opus-5-5", max_tokens=4000,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": CHECK_SCHEMA}},
        messages=[{"role": "user", "content": content}])
    return json.loads(next(b.text for b in resp.content if b.type == "text"))


def tone(img: Image.Image, side: str = "left", style=None) -> dict:
    """Is the base image rich and warm, or white / washed out / half empty?"""
    lim = limits(style)
    LUM_MIN, LUM_MAX, SAT_MIN, WHITE_MAX, BLANK_MAX = (lim[k] for k in ("LUM_MIN", "LUM_MAX", "SAT_MIN", "WHITE_MAX", "BLANK_MAX"))
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
    # split screen / pasted panel: one column where nearly every row jumps hard
    G = img.convert("L").resize((640, 360))
    seam = (0.0, 0, 0.0)
    for x in range(8, 632):
        d = ImageChops.difference(G.crop((x - 1, 0, x, 360)), G.crop((x, 0, x + 1, 360)))
        m = ImageStat.Stat(d).mean[0]
        if m > seam[0]:
            seam = (m, x, sum(d.histogram()[13:]) / 360)
    why = []
    if seam[0] >= 27 and seam[2] >= 0.5:
        why.append(f"hard vertical edge at {seam[1] / 640:.0%} of the width - looks like a split screen or pasted panel")
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
            "blank": round(blank, 2), "seam": round(seam[0], 1), "why": "; ".join(why)}


def build_base(p, fr, tries: int = 4, style=None):
    """The finished thumbnail, made 100% by the image model (owner 2026-11-12: no code
    drawing, no colour grading). Kept only when Claude's review passes (same person, exact
    text, nothing invented) and the tone gate passes (not white / washed out / split).
    A failing image is fixed by an AI edit when only the text or invented items are wrong,
    otherwise made again with the reasons; after the last try the best one is kept."""
    style = style or p.get("_style")
    side = p.get("text_side", "left")
    feedback, best, prev, edit_next = "", None, None, False
    for n in range(1, tries + 1):
        try:
            cand = image(p, fr, feedback, edit=prev if edit_next else None)
        except RuntimeError as exc:          # empty answer from the image model
            print(f"image attempt {n}: {exc}")
            feedback, edit_next = "Keep it wholesome and family-friendly.", False
            continue
        t = tone(cand, side, style)
        v = review(cand, p, fr)
        print(f"review {n}: person {v['score']}/10 same={v['same_person']} | text_exact={v['text_exact']} "
              f"read='{v['text_read']}' | invented={v['invented_items']}")
        print(f"tone check {n}: {'ok' if t['ok'] else t['why']} (lum {t['lum']}, sat {t['sat']}, "
              f"white {t['white']:.0%}, blank {t['blank']:.0%})")
        rank = (v["same_person"], v["text_exact"], not v["invented_items"], t["ok"], v["score"])
        if best is None or rank > best[1]:
            best = (cand, rank)
        if all(rank[:4]):
            return cand, f"ai (check {v['score']}/10, text ok, no fakes, tone ok, try {n})"
        fixes = []
        if not v["text_exact"]:
            fixes.append(f"The text is wrong (it reads '{v['text_read']}'); paint the headline exactly as "
                         "specified and remove any other text.")
        if v["invented_items"]:
            fixes.append("Remove these things that are not in the video: " + "; ".join(v["invented_items"])
                         + " - show only the real items from the video frames.")
        if not v["same_person"]:
            fixes.append("The person changed: " + v["differences"] + ". Copy the person from the reference "
                         "frames exactly.")
        if not t["ok"]:
            fixes.append("The picture is too white / plain: " + t["why"] + ". Fill the frame with the video's "
                         "colourful real items, big and close; rich true colour, warm light, no white or pastel "
                         "areas, one continuous photo.")
        feedback = " ".join(fixes)
        # small, local problems -> AI edit of this image; person/colour problems -> make it again
        edit_next = v["same_person"] and t["ok"]
        prev = cand
    if best is None:
        raise RuntimeError("the image model returned no image")
    img, rank = best
    return img, (f"ai, best of {tries} (person {rank[4]}/10, text {'ok' if rank[1] else 'NOT exact'}, "
                 f"{'no fakes' if rank[2] else 'has invented items'}, tone {'ok' if rank[3] else 'off'})")


def add_text(img: Image.Image, *_args, **_kw) -> Image.Image:
    """Kept for old callers: the text is now painted by the image model inside build_base
    (owner 2026-11-12: no manual/code editing of thumbnails), so nothing is drawn here."""
    return img


def compose(base: Image.Image, p: dict = None, style=None) -> Image.Image:
    return base


if __name__ == "__main__":
    # python -m src.ai_thumbnail <video.mp4> <caption> <lang> <out.jpg> [style]
    video, caption, lang, out = Path(sys.argv[1]), sys.argv[2], sys.argv[3], Path(sys.argv[4])
    style = sys.argv[5] if len(sys.argv) > 5 else None
    fr = frames(video, out.parent / "frames")
    p = plan(fr, caption, lang, style=style)
    print(json.dumps(p, ensure_ascii=False, indent=1))
    base, how = build_base(p, fr, style=style)
    print("thumbnail:", how)
    out.with_name(out.stem + "_plan.json").write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")
    base.save(out, quality=92)
    print("saved", out, out.stat().st_size, "bytes")
