"""Daily AI video maker. Runs inside GitHub Actions.

MODE=check : prints the project's workflows, templates and example config (free, no videos).
MODE=make  : picks trending topics, writes scripts, renders videos into ./out
"""
import asyncio
import datetime
import json
import os
import pathlib
import random
import re
import shutil
import sys
import traceback
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
os.chdir(ROOT)
OUT = ROOT / "out"
OUT.mkdir(exist_ok=True)
HISTORY = ROOT / "scripts" / "history.txt"

MODE = os.environ.get("MODE", "make")
N_LONG = int(os.environ.get("VIDEOS_LONG") or 1)
N_SHORT = int(os.environ.get("VIDEOS_SHORT") or 2)

SHORT_MAX_SEC = 30      # حد الشورتس
LONG_MAX_SEC = 180      # حد الفيديو الطويل (3 دقائق)
SHORT_WORDS = 60        # عدد الكلمات المستهدف للشورتس
LONG_WORDS = 330        # عدد الكلمات المستهدف للفيديو الطويل

LLM_BASE = os.environ.get("LLM_BASE_URL") or "https://api.openai.com/v1"
LLM_MODEL = os.environ.get("LLM_MODEL") or "gpt-4o-mini"
VOICE = os.environ.get("VOICE") or "en-US-GuyNeural"
TEMPLATE_SHORT = os.environ.get("SHORT_TEMPLATE") or "1080x1920/image_default.html"
TEMPLATE_LONG = os.environ.get("LONG_TEMPLATE") or "1920x1080/image_default.html"
BGM = "bgm/default.mp3"

BANNED = re.compile(
    r"\b(news|breaking|election|elections|president|politic\w*|government|war|trump|biden|"
    r"putin|gaza|israel|ukraine|congress|senate|vote|voting|protest|riot|scandal|arrest|lawsuit)\b",
    re.I,
)


# ---------------------------------------------------------------- check mode
def check():
    print("=== WORKFLOWS ===")
    wf = ROOT / "workflows"
    for p in sorted(wf.rglob("*.json")):
        print(p.relative_to(wf).as_posix())
    print("=== TEMPLATES ===")
    tp = ROOT / "templates"
    for p in sorted(tp.rglob("*.html")):
        print(p.relative_to(tp).as_posix())
    print("=== config.example.yaml ===")
    print((ROOT / "config.example.yaml").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- config
def deep_merge(a, b):
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            deep_merge(a[k], v)
        else:
            a[k] = v
    return a


def find_dict(cfg, key):
    if isinstance(cfg.get(key), dict):
        return cfg[key]
    for v in cfg.values():
        if isinstance(v, dict) and isinstance(v.get(key), dict):
            return v[key]
    return None


def prepare_config():
    import yaml

    cfg = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8")) or {}
    llm_cfg = cfg.setdefault("llm", {})
    llm_cfg["api_key"] = os.environ["LLM_API_KEY"]
    llm_cfg["base_url"] = LLM_BASE
    llm_cfg["model"] = LLM_MODEL

    wf = os.environ.get("IMAGE_WORKFLOW")
    if wf:
        d = find_dict(cfg, "image")
        if d is not None:
            d["default_workflow"] = wf
        else:
            print("WARNING: no image section found in config; IMAGE_WORKFLOW ignored")

    extra = os.environ.get("EXTRA_CONFIG_YAML")
    if extra:
        deep_merge(cfg, yaml.safe_load(extra) or {})

    (ROOT / "config.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")


# ---------------------------------------------------------------- LLM + trends
def llm(prompt, system="You are a careful assistant.", temperature=0.9):
    body = json.dumps({
        "model": LLM_MODEL,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }).encode()
    req = urllib.request.Request(
        LLM_BASE.rstrip("/") + "/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + os.environ["LLM_API_KEY"],
        },
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)["choices"][0]["message"]["content"].strip()


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
        'Return only JSON: {"title": "max 70 chars, curiosity-driven", '
        '"description": "2 short sentences", "tags": ["5 to 8 tags"], "scenes": ["narration 1", "..."]}'
    )
    info = parse_json(llm(prompt))
    info["scenes"] = [s.strip() for s in info["scenes"] if s.strip()]
    return info


def shorten(scenes, ratio):
    prompt = (
        f"Rewrite these narration scenes to about {int(ratio * 100)}% of their current length. "
        f"Keep the same number of scenes ({len(scenes)}), keep the hook.\n"
        f"Return only a JSON list of strings.\n{json.dumps(scenes)}"
    )
    return parse_json(llm(prompt))


# ---------------------------------------------------------------- rendering
def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "video"


async def make_video(core, info, short, idx):
    limit = SHORT_MAX_SEC if short else LONG_MAX_SEC
    scenes = info["scenes"]
    result = None
    for attempt in (1, 2):
        script = "\n\n".join(scenes)
        result = await core.generate_video(
            text=script,
            pipeline="standard",
            mode="fixed",
            tts_inference_mode="local",
            tts_voice=VOICE,
            frame_template=TEMPLATE_SHORT if short else TEMPLATE_LONG,
            bgm_path=BGM,
        )
        print(f"attempt {attempt}: duration {result.duration:.1f}s (limit {limit}s)")
        if result.duration <= limit:
            break
        scenes = shorten(scenes, ratio=(limit / result.duration) * 0.9)
    else:
        print("Still too long after retry, skipping this video.")
        return False

    date = datetime.date.today().isoformat()
    kind = "short" if short else "long"
    base = f"{date}_{kind}{idx + 1}_{slug(info['title'])}"
    shutil.copy(result.video_path, OUT / f"{base}.mp4")
    desc = info.get("description", "")
    if short:
        desc += "\n\n#Shorts"
    meta = {"title": info["title"], "description": desc, "tags": info.get("tags", [])}
    (OUT / f"{base}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved", base)
    return True


async def main():
    if MODE == "check":
        check()
        return

    prepare_config()
    niches = json.loads((ROOT / "scripts" / "niches.json").read_text(encoding="utf-8"))

    from pixelle_video import pixelle_video as core

    await core.initialize()

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
            if await make_video(core, info, short, i):
                made += 1
                used.append(topic)
                with HISTORY.open("a", encoding="utf-8") as f:
                    f.write(topic + "\n")
        except Exception:
            print("FAILED job", k)
            traceback.print_exc()

    await core.cleanup()
    print(f"Done: {made}/{len(jobs)} videos created")
    if made == 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
