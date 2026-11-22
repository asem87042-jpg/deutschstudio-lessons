#!/usr/bin/env python3
"""
Deutschstudio lesson-library generator.

Generates ready-made lesson days as JSON files in exactly the format the
Deutschstudio app already parses (the same shape as the bundled lessons.json),
using the same prompt the app uses and the same validation rules.

Usage:
    export GEMINI_API_KEY="your-free-aistudio-key"
    python3 generate_lessons.py --level A1.1 --start 1 --count 3

Pacing (free-tier friendly):
    One API call per lesson day. The script sleeps between calls and stops
    after --count days, so run it a few times a day (e.g. --count 5) and it
    resumes where it left off: existing days/<id>.json files are skipped.

Output:
    days/<id>.json      the lesson, ready for the app
    review/<id>.txt     plain-text summary to skim before publishing
    manifest.json       rebuilt after every run (index the app downloads)
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.5-flash-lite"]
BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# A1.1 topic track, mirrors CurriculumLedger.blueprints["A1.1"] suggestedTopics in the app.
TOPICS_A11 = [
    ("Begrüßung & sein", "sein/heißen present tense, Sie vs. du"),
    ("Im Café & möchten", "möchten, bestellen, Zahlen bitte"),
    ("Zahlen, Preise & Supermarkt", "numbers 0-100, prices, articles der/die/das"),
    ("Uhrzeit & Tagesablauf", "telling time, daily routine, separable verbs"),
    ("Wegbeschreibung & U-Bahn", "directions, imperative du/Sie, prepositions zu/nach"),
    ("Familie & Wohnen", "possessive articles, haben, family vocabulary"),
    ("Wochenende & Hobbys", "gern/lieber, free-time verbs, negation nicht/kein"),
    ("Büro & Vorstellung", "jobs, formal introductions, modal verb können"),
    ("Einkaufen & Farben", "clothes, colors, accusative case"),
    ("Essen, Trinken & mögen", "food vocabulary, mögen/essen/trinken, dative basics"),
    ("Arzttermin & Körper", "body parts, dative verbs, weh tun"),
    ("Wetter & Kleidung", "weather phrases, present tense review, tragen"),
]

CEFR_A11 = (
    "Beginner band: very short, simple sentences; high-frequency everyday vocabulary; "
    "present tense only (plus sein/haben perfect is NOT yet used); every grammar point "
    "explained with one tiny example; no subordinate clauses."
)

PROMPT_TEMPLATE = """You are Deutschstudio, an expert German curriculum architect (A1-B2).
Generate the complete Day {day_id} German lesson in valid JSON, strictly calibrated to CEFR level **{level}** ({level} beginner band).

CEFR Level Calibration ({level}):
- {cefr}
- Requested Topic: "{topic}"
- Requested Grammar Focus: "{focus}"
- Learner style & themes: style=balanced, themes=daily life in Germany
- Explanation & Coaching Language: explain in clear, simple English (Arabic glosses welcome in vocabulary meanings).

ANTI-DUPLICATION & SPACED REPETITION RULES (CRITICAL):
1. Previously completed lessons: {covered}
2. DO NOT select any of these already-taught words as the 5 new target vocabulary items: {taught}.
3. Choose 5 brand-new, high-utility German vocabulary items appropriate for **{level}** and "{topic}".
4. Naturally recycle 2-3 of these due/weak review words inside the reading story, dialogues, and `review` quiz: {recycle}.

Return ONLY a valid JSON object matching the exact Deutschstudio lesson schema with keys:
level ("{level}"), title, subtitle, description, focus, objectives (5 items),
vocabulary (5 arrays of [word, meaning, example]),
dialogues (5 objects {{title, body}}), reading ({{title, text}}), culture ({{title, body}}),
grammar ({{title, body, quizzes}}), translations (2 quizzes), listening ({{title, steps, script, quiz}}),
writing, model, checkpoint, vocabQuiz, comprehension, trueFalse, review.
"""


def discover_models(api_key):
    """Same approach as the app: list models the key can use, prefer the known-good ones."""
    req = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/models?pageSize=100&key=" + api_key)
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    skip = ("embedding", "tts", "image", "aqa", "robotics", "computer-use")
    names = []
    for m in payload.get("models", []):
        n = str(m.get("name", "")).replace("models/", "")
        if (n.startswith("gemini-")
                and "generateContent" in m.get("supportedGenerationMethods", [])
                and not any(s in n for s in skip)):
            names.append(n)
    ordered = [p for p in GEMINI_MODELS if p in names] + [n for n in names if n not in GEMINI_MODELS]
    print("Available models:", ", ".join(ordered[:5]) if ordered else "none")
    return ordered or GEMINI_MODELS


def call_gemini(api_key, prompt, models, retries=3):
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.9, "maxOutputTokens": 8192,
                             "responseMimeType": "application/json"},
    }).encode()
    last_err = None
    for attempt in range(retries):
        for model in models:
            req = urllib.request.Request(
                BASE_URL.format(model=model) + "?key=" + api_key,
                data=body, headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    payload = json.load(resp)
                return payload["candidates"][0]["content"]["parts"][0]["text"], model
            except urllib.error.HTTPError as e:
                last_err = "%s: HTTP %s" % (model, e.code)
                if e.code == 429:
                    print("  quota hit on %s, waiting 60s..." % model)
                    time.sleep(60)
                continue
            except Exception as e:  # noqa: BLE001
                last_err = "%s: %s" % (model, e)
                continue
        time.sleep(20 * (attempt + 1))
    raise SystemExit("Gemini failed after retries: %s" % last_err)


def extract_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def validate(lesson):
    """Same acceptance rules as the app's LessonNormalizer."""
    problems = []
    vocab = lesson.get("vocabulary") or []
    dialogues = lesson.get("dialogues") or []
    if len(vocab) < 5:
        problems.append("vocabulary has %d items (<5)" % len(vocab))
    if len(dialogues) < 5:
        problems.append("dialogues has %d items (<5)" % len(dialogues))
    examples = {v[2] for v in vocab if isinstance(v, list) and len(v) > 2}
    if len(examples) < 4:
        problems.append("only %d distinct vocab examples (<4)" % len(examples))
    for key in ("title", "subtitle", "description", "focus", "objectives",
                "reading", "culture", "grammar", "review"):
        if key not in lesson:
            problems.append("missing key: %s" % key)
    return problems


def covered_summary(days_dir):
    parts = []
    for path in sorted(days_dir.glob("*.json"), key=lambda p: int(p.stem)):
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            parts.append("Day %s (%s): %s [%s]" % (
                d.get("id", path.stem), d.get("level", "?"),
                d.get("title", "?"), d.get("focus", "?")))
        except Exception:  # noqa: BLE001
            continue
    return " | ".join(parts[-60:]) or "none yet"


def taught_words(days_dir):
    words = []
    for path in sorted(days_dir.glob("*.json")):
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            for v in d.get("vocabulary") or []:
                if isinstance(v, list) and v:
                    words.append(str(v[0]).split(",")[0].strip())
        except Exception:  # noqa: BLE001
            continue
    return ", ".join(list(dict.fromkeys(words))[-160:]) if words else "none"


def write_summary(lesson, path, model):
    lines = [
        "Day %s - %s" % (lesson.get("id"), lesson.get("title")),
        "Level: %s | Focus: %s | Model: %s" % (lesson.get("level"), lesson.get("focus"), model),
        "",
        "Objectives:",
    ]
    lines += ["  - %s" % o for o in lesson.get("objectives", [])]
    lines.append("")
    lines.append("Vocabulary:")
    for v in lesson.get("vocabulary", []):
        lines.append("  - %s | %s | %s" % tuple((list(v) + ["", "", ""])[:3]))
    lines.append("")
    lines.append("Dialogues:")
    for d in lesson.get("dialogues", []):
        body = str(d.get("body", ""))[:200]
        lines.append("  - %s: %s..." % (d.get("title", "?"), body))
    path.write_text("\n".join(lines), encoding="utf-8")


def rebuild_manifest(days_dir, manifest_path):
    days = []
    for path in sorted(days_dir.glob("*.json"), key=lambda p: int(p.stem)):
        raw = path.read_bytes()
        d = json.loads(raw.decode("utf-8"))
        days.append({
            "id": d.get("id", int(path.stem)),
            "date": d.get("date", ""),
            "level": d.get("level", ""),
            "title": d.get("title", ""),
            "file": "days/%s.json" % path.stem,
            "sha256": hashlib.sha256(raw).hexdigest(),
        })
    version = int(manifest_path.exists() and json.loads(
        manifest_path.read_text()).get("version", 0)) + 1
    manifest_path.write_text(json.dumps(
        {"version": version, "days": days}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print("manifest.json rebuilt: version %d, %d days" % (version, len(days)))


def main():
    from pathlib import Path
    ap = argparse.ArgumentParser()
    ap.add_argument("--level", default="A1.1")
    ap.add_argument("--start", type=int, default=1, help="first day id")
    ap.add_argument("--count", type=int, default=3, help="days to generate this run")
    ap.add_argument("--delay", type=int, default=20, help="seconds between calls")
    args = ap.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("Set GEMINI_API_KEY first (free key from aistudio.google.com).")

    models = discover_models(api_key)

    root = Path(__file__).resolve().parent
    days_dir = root / "days"
    review_dir = root / "review"
    days_dir.mkdir(exist_ok=True)
    review_dir.mkdir(exist_ok=True)

    if args.level != "A1.1":
        print("Note: only the A1.1 topic track is embedded; add more tracks to TOPICS_A11.")

    made = 0
    for day_id in range(args.start, args.start + args.count * 4):
        if made >= args.count:
            break
        target = days_dir / ("%d.json" % day_id)
        if target.exists():
            print("Day %d exists, skipping." % day_id)
            continue
        topic, focus = TOPICS_A11[(day_id - 1) % len(TOPICS_A11)]
        prompt = PROMPT_TEMPLATE.format(
            day_id=day_id, level=args.level, cefr=CEFR_A11,
            topic=topic, focus=focus,
            covered=covered_summary(days_dir),
            taught=taught_words(days_dir),
            recycle="sein, heißen, wohnen",
        )
        print("Generating day %d (%s)..." % (day_id, topic))
        raw, model = call_gemini(api_key, prompt, models)
        try:
            lesson = extract_json(raw)
        except Exception as e:  # noqa: BLE001
            print("  invalid JSON from model, skipping day %d (%s)" % (day_id, e))
            continue
        lesson["id"] = day_id
        lesson.setdefault("level", args.level)
        problems = validate(lesson)
        if problems:
            print("  day %d failed validation: %s (not saved)" % (day_id, "; ".join(problems)))
            continue
        target.write_text(json.dumps(lesson, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
        write_summary(lesson, review_dir / ("%d.txt" % day_id), model)
        print("  saved days/%d.json (%s)" % (day_id, model))
        made += 1
        if made < args.count:
            time.sleep(args.delay)

    rebuild_manifest(days_dir, root / "manifest.json")
    print("Done. Generated %d new day(s). Skim review/*.txt, then commit and push." % made)


if __name__ == "__main__":
    main()
