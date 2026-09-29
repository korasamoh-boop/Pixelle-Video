"""Free daily AI video maker (runs inside GitHub Actions).

Script writing : GitHub Models (free, uses the built-in GITHUB_TOKEN)
Voice          : edge-tts (free), falls back to gTTS
Images         : Pollinations (free)
Video          : ffmpeg (zoom animation + captions)
"""
import asyncio
import datetime
import json
import math
import os
import pathlib
import random
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
os.chdir(ROOT)
OUT = ROOT / "out"
WORK = ROOT / "work"
OUT.mkdir(exist_ok=True)
WORK.mkdir(exist_ok=True)
HISTORY = ROOT / "scripts" / "history.txt"

LLM_BASE = os.environ.get("LLM_BASE_URL") or "https://models.github.ai/inference"
LLM_MODEL = os.environ.get("LLM_MODEL") or "openai/gpt-4.1-mini"
LLM_KEY = os.environ.get("LLM_API_KEY") or os.environ.get("GITHUB_TOKEN") or ""
VOICE = os.environ.get("VOICE") or "en-US-GuyNeural"
STYLE = os.environ.get("IMAGE_STYLE") or (
    "cinematic digital illustration, rich colors, dramatic lighting, highly detailed, "
    "no text, no watermark, no logos, no real people"
)
BGM_FILE = os.environ.get("BGM_FILE") or ""   # اختياري: مسار ملف موسيقى مسموح باستخدامه
N_SHORT = int(os.environ.get("SHORTS") or 2)
N_LONG = int(os.environ.get("LONGS") or 1)

SHORT_LIMIT = 29.0     # ثانية (الحد المطلوب 30)
LONG_LIMIT = 175.0     # ثانية (الحد المطلوب 3 دقائق)
SHORT_WORDS = 60
LONG_WORDS = 330
FPS = 25

BANNED = re.compile(
    r"\b(news|breaking|election|elections|president|politic\w*|government|war|trump|biden|"
    r"putin|gaza|israel|ukraine|congress|senate|vote|voting|protest|riot|scandal|arrest|lawsuit)\b",
    re.I,
)


# ------------------------------------------------------------ helpers
def run(cmd, cwd=None):
    p = subprocess.run(cmd, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        print("COMMAND FAILED:", " ".join(str(c) for c in cmd))
        print(p.stderr[-1500:])
        raise RuntimeError("ffmpeg failed")


def probe(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)]
    )
    return float(out.decode().strip())


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "video"


# ------------------------------------------------------------ LLM + trends
def llm(prompt, temperature=0.9):
    body = json.dumps({
        "model": LLM_MODEL,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": "You are a careful assistant. Follow the requested output format exactly."},
            {"role": "user", "content": prompt},
        ],
    }).encode()
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(
                LLM_BASE.rstrip("/") + "/chat/completions",
                data=body,
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + LLM_KEY},
            )
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r)["choices"][0]["message"]["content"].strip()
        except Exception as e:
            last = e
            detail = ""
            if hasattr(e, "read"):
                try:
                    detail = e.read().decode("utf-8", "ignore")[:300]
                except Exception:
                    pass
            print(f"LLM error (attempt {attempt + 1}): {e} {detail}")
            time.sleep(15 * (attempt + 1))
    raise RuntimeError(f"LLM failed: {last}")


def parse_json(text):
    m = re.search(r"\{.*\}|\[.*\]", text, re.S)
    return json.loads(m.group(0))


def suggestions(seed):
    url = ("https://suggestqueries.google.com/complete/search?client=firefox&ds=yt&hl=en&q="
           + urllib.parse.quote(seed))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))[1]
    except Exception as e:
        print("suggest failed:", e)
        return []


def read_history():
    if HISTORY.exists():
        return HISTORY.read_text(encoding="utf-8").splitlines()[-300:]
    return []


def pick_topic(niche, used):
    cands = []
    for seed in niche["seeds"]:
        cands += suggestions(seed)
        for letter in random.sample("abcdefghijklmnoprstw", 3):
            cands += suggestions(f"{seed} {letter}")
    cands = [c for c in dict.fromkeys(cands) if not BANNED.search(c)]
    print(f"[{niche['name']}] {len(cands)} trending searches found")
    prompt = (
        f"Niche: {niche['name']}.\n"
        f"Currently popular YouTube searches: {json.dumps(cands[:60])}\n"
        f"Topics already used recently (do NOT repeat): {json.dumps(used[-60:])}\n\n"
        "Pick ONE specific, interesting, evergreen-friendly video topic inspired by these searches. "
        "It must NOT be news, politics, current events, medical/financial advice, or about real private people. "
        'Return only JSON: {"topic": "..."}'
    )
    return parse_json(llm(prompt))["topic"]


def write_script(topic, niche, short):
    words = SHORT_WORDS if short else LONG_WORDS
    scenes = 4 if short else 8
    kind = "a YouTube Short (under 30 seconds)" if short else "a YouTube video (about 2 minutes)"
    prompt = (
        f"Write narration for {kind} about: {topic}\n"
        f"Niche: {niche['name']}. Language: English. Total about {words} words, exactly {scenes} scenes.\n"
        "Rules: strong hook in the first sentence, simple spoken language, only well-established facts "
        "(no invented statistics, no fake quotes), no news, no politics, no medical or financial advice, "
        "no stage directions, no emojis, no scene numbers.\n"
        "For each scene also write an image prompt (English, 15-30 words) describing one vivid visual that "
        "matches the narration. No text in images, no real people, no logos.\n"
        'Return only JSON: {"title": "max 70 chars, curiosity-driven", "description": "2 short sentences", '
        '"tags": ["5 to 8 tags"], "scenes": [{"text": "narration", "image_prompt": "visual description"}]}'
    )
    info = parse_json(llm(prompt))
    info["scenes"] = [
        {"text": s["text"].strip(), "image_prompt": s["image_prompt"].strip()}
        for s in info["scenes"] if s.get("text", "").strip()
    ]
    return info


def shorten(texts, ratio):
    prompt = (
        f"Rewrite these narration scenes to about {int(ratio * 100)}% of their current length. "
        f"Keep the same number of scenes ({len(texts)}) and keep the hook.\n"
        f"Return only a JSON list of strings.\n{json.dumps(texts)}"
    )
    return parse_json(llm(prompt))


# ------------------------------------------------------------ voice
async def tts(text, path):
    try:
        import edge_tts

        await edge_tts.Communicate(text, VOICE).save(str(path))
        if path.exists() and path.stat().st_size > 1000:
            return
    except Exception as e:
        print("edge-tts failed, using gTTS:", e)
    from gtts import gTTS

    gTTS(text, lang="en").save(str(path))


# ------------------------------------------------------------ images
_last_image_time = 0.0


def fetch_image(prompt, w, h, path):
    """Returns True if a real AI image was saved."""
    global _last_image_time
    key = os.environ.get("POLLINATIONS_KEY")
    q = urllib.parse.quote(prompt[:450])
    for attempt in range(3):
        if not key:  # الطبقة المجانية بدون مفتاح: طلب كل 15 ثانية تقريبًا
            wait = 16 - (time.time() - _last_image_time)
            if wait > 0:
                time.sleep(wait)
        seed = random.randint(1, 10**6)
        if key:
            url = (f"https://gen.pollinations.ai/image/{q}?width={w}&height={h}&model=flux"
                   f"&seed={seed}&nologo=true&key={key}")
        else:
            url = (f"https://image.pollinations.ai/prompt/{q}?width={w}&height={h}&model=flux"
                   f"&seed={seed}&nologo=true")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=180) as r:
                data = r.read()
            _last_image_time = time.time()
            if len(data) > 5000:
                path.write_bytes(data)
                return True
            print("image too small, retrying")
        except Exception as e:
            _last_image_time = time.time()
            print(f"image error (attempt {attempt + 1}): {e}")
            time.sleep(10)
    return False


def fallback_image(path, w, h):
    color = random.choice(["0x1b2a49", "0x2d1b49", "0x0f3d3e", "0x3d1f1f", "0x1f3d2a"])
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:s={w}x{h}", "-frames:v", "1", str(path)])


# ------------------------------------------------------------ video
def make_clip(img, audio, dur, w, h, out, zoom_in):
    frames = int(math.ceil(dur * FPS)) + 5
    bw, bh = int(w * 1.5) // 2 * 2, int(h * 1.5) // 2 * 2
    z = "min(1+0.0007*on,1.3)" if zoom_in else "max(1.3-0.0007*on,1.0)"
    vf = (f"scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
          f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={w}x{h}:fps={FPS},"
          "format=yuv420p")
    run(["ffmpeg", "-y", "-i", str(img), "-i", str(audio), "-vf", vf, "-af", "apad=pad_dur=0.4",
         "-t", f"{dur:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
         "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2", str(out)])


def ass_time(t):
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def build_ass(texts, durs, w, h, path):
    vertical = h > w
    size = 78 if vertical else 54
    margin = 260 if vertical else 80
    step = 4 if vertical else 6
    head = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {w}\nPlayResY: {h}\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,DejaVu Sans,{size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,"
        f"100,100,0,0,1,5,2,2,60,60,{margin},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = []
    t0 = 0.0
    for text, d in zip(texts, durs):
        words = text.replace("{", "").replace("}", "").split()
        chunks = [" ".join(words[i:i + step]) for i in range(0, len(words), step)] or [""]
        total = sum(len(c) for c in chunks) or 1
        t = t0
        for c in chunks:
            cd = d * len(c) / total
            lines.append(f"Dialogue: 0,{ass_time(t)},{ass_time(t + cd)},Default,,0,0,0,,{c}")
            t += cd
        t0 += d
    path.write_text(head + "\n".join(lines) + "\n", encoding="utf-8")


def finalize(work, out):
    bgm = BGM_FILE if BGM_FILE and pathlib.Path(BGM_FILE).exists() else ""
    if bgm:
        cmd = ["ffmpeg", "-y", "-i", "joined.mp4", "-stream_loop", "-1", "-i", str(pathlib.Path(bgm).resolve()),
               "-filter_complex",
               "[0:v]ass=caps.ass[v];[1:a]volume=0.10[b];[0:a][b]amix=inputs=2:duration=first:dropout_transition=2,volume=2[a]",
               "-map", "[v]", "-map", "[a]"]
    else:
        cmd = ["ffmpeg", "-y", "-i", "joined.mp4", "-vf", "ass=caps.ass", "-map", "0:v", "-map", "0:a"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-c:a", "aac", "-b:a", "192k",
            "-shortest", "-movflags", "+faststart", str(out)]
    run(cmd, cwd=work)


async def make_video(info, short, idx):
    w, h = (1080, 1920) if short else (1920, 1080)
    limit = SHORT_LIMIT if short else LONG_LIMIT
    work = WORK / f"{'s' if short else 'l'}{idx}_{int(time.time())}"
    work.mkdir(parents=True)
    scenes = info["scenes"]

    durs = []
    for attempt in range(3):
        durs = []
        for i, s in enumerate(scenes):
            p = work / f"a{i}.mp3"
            await tts(s["text"], p)
            durs.append(probe(p) + 0.4)
        total = sum(durs)
        print(f"attempt {attempt + 1}: voice length {total:.1f}s (limit {limit:.0f}s)")
        if total <= limit:
            break
        new = shorten([s["text"] for s in scenes], ratio=(limit / total) * 0.92)
        for s, t in zip(scenes, new):
            s["text"] = str(t).strip()
    else:
        print("Too long even after shortening; skipping this video.")
        return False

    real_images = 0
    for i, s in enumerate(scenes):
        img = work / f"i{i}.jpg"
        if fetch_image(f"{s['image_prompt']}, {STYLE}", w, h, img):
            real_images += 1
        else:
            fallback_image(img, w, h)
    if real_images == 0:
        print("No AI image could be generated (service unavailable). Skipping.")
        return False

    clips = []
    for i, s in enumerate(scenes):
        clip = work / f"c{i}.mp4"
        make_clip(work / f"i{i}.jpg", work / f"a{i}.mp3", durs[i], w, h, clip, zoom_in=(i % 2 == 0))
        clips.append(clip)

    (work / "list.txt").write_text("".join(f"file '{c.name}'\n" for c in clips), encoding="utf-8")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", "joined.mp4"], cwd=work)

    real_durs = [probe(c) for c in clips]
    build_ass([s["text"] for s in scenes], real_durs, w, h, work / "caps.ass")

    date = datetime.date.today().isoformat()
    base = f"{date}_{'short' if short else 'long'}{idx + 1}_{slug(info['title'])}"
    finalize(work, OUT / f"{base}.mp4")

    desc = info.get("description", "")
    if short:
        desc += "\n\n#Shorts"
    meta = {"title": info["title"], "description": desc, "tags": info.get("tags", [])}
    (OUT / f"{base}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved {base} ({probe(OUT / (base + '.mp4')):.1f}s, {real_images}/{len(scenes)} AI images)")
    shutil.rmtree(work, ignore_errors=True)
    return True


async def main():
    if not LLM_KEY:
        print("No LLM key found (GITHUB_TOKEN missing).")
        sys.exit(1)
    niches = json.loads((ROOT / "scripts" / "niches.json").read_text(encoding="utf-8"))
    used = read_history()
    doy = datetime.date.today().timetuple().tm_yday
    jobs = [(True, i) for i in range(N_SHORT)] + [(False, i) for i in range(N_LONG)]
    made = 0
    for k, (short, i) in enumerate(jobs):
        niche = niches[(doy + k) % len(niches)]
        try:
            topic = pick_topic(niche, used)
            print("TOPIC:", topic)
            info = write_script(topic, niche, short)
            if await make_video(info, short, i):
                made += 1
                used.append(topic)
                with HISTORY.open("a", encoding="utf-8") as f:
                    f.write(topic + "\n")
        except Exception:
            print("FAILED job", k)
            traceback.print_exc()
    print(f"Done: {made}/{len(jobs)} videos created")
    if made == 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
