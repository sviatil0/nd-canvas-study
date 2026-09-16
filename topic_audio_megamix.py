"""Build single combined exam-review podcast MP3.

Strategy:
1. Score each topic by exam likelihood (exam guidelines mentions, mistake count,
   formula sheet hits, # of practice questions).
2. Order topics: high-priority first → low-priority last (best for runs;
   if you stop early, you've covered the most exam-critical material).
3. Use already-generated `<topic>.script.txt` files (no Gemini calls).
4. Add 1-line audio transition between topics ("Next: ...").
5. Synth via TTS, concat all MP3 frames into single file.

Usage:
    python topic_audio_megamix.py --course-dir downloads/129492_porgramming-paradigms
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from topic_audio import synth_chunk, _split_chunks, DEFAULT_VOICE


# TTS-friendly substitutions for symbols/operators that don't speak well.
# Spoken form should explain INTENT, not literal characters.
_TTS_SUBS = [
    # Operators (most exam-relevant)
    (r"===", " strict equality "),
    (r"!==", " strict inequality "),
    (r"==", " loose equality "),
    (r"!=", " loose inequality "),
    (r"=>", " arrow function "),
    (r"->", " arrow "),
    (r"<=", " less than or equal to "),
    (r">=", " greater than or equal to "),
    (r"<<", " left shift "),
    (r">>", " right shift "),
    (r"&&", " logical and "),
    (r"\|\|", " logical or "),
    (r"\+\+", " plus plus "),
    (r"--", " minus minus "),
    (r"\+=", " plus equals "),
    (r"-=", " minus equals "),
    # Single chars in math/programming context
    (r"≤", " less than or equal to "),
    (r"≥", " greater than or equal to "),
    (r"≠", " not equal "),
    (r"→", " maps to "),
    (r"←", " from "),
    (r"⇒", " implies "),
    (r"∀", " for all "),
    (r"∃", " there exists "),
    (r"∈", " in "),
    (r"∉", " not in "),
    (r"∪", " union "),
    (r"∩", " intersection "),
    # Markdown remnants
    (r"```\w*", " "),
    (r"`([^`]+)`", r" \1 "),     # inline code: drop backticks
    (r"\*\*([^*]+)\*\*", r"\1"), # bold
    (r"\*([^*]+)\*", r"\1"),     # italic
    (r"^#+\s*", "", True),       # headers
    (r"\|", " "),                # table pipes
    (r"^[-*]\s+", "", True),     # bullet markers
    # Symbols read poorly
    (r"&", " and "),
    (r"@", " at "),
    (r"%", " percent"),
    # Multi-space cleanup
    (r"[ \t]+", " "),
    (r"\n{3,}", "\n\n"),
]


def _sanitize_for_tts(text: str) -> str:
    """Apply TTS-friendly substitutions."""
    for sub in _TTS_SUBS:
        if len(sub) == 3 and sub[2]:
            text = re.sub(sub[0], sub[1], text, flags=re.M)
        else:
            text = re.sub(sub[0], sub[1], text)
    return text.strip()


def score_topic(course_dir: Path, topic: str, label: str) -> tuple[float, dict]:
    """Heuristic exam-priority score (higher = study earlier in run)."""
    score = 0.0
    breakdown = {}

    # Mistakes you've made on this topic (from problems.json)
    pfile = course_dir / "bundles" / "problems.json"
    if pfile.exists():
        import json
        data = json.loads(pfile.read_text())
        ps = data.get(topic, [])
        wrong = sum(1 for p in ps if p.get("you_got_wrong") or p.get("synthetic"))
        score += wrong * 4
        breakdown["wrong_or_synth"] = wrong
        score += len(ps) * 0.5  # general coverage in problems set
        breakdown["n_problems"] = len(ps)

    # Final exam guidelines / review mentions (paradigms doc OR comp arch review slides)
    guideline_sources = [
        course_dir / "_external/google_drive_remote/document/16w-Wm7rCsRCH6QUiSdMwRirSe-wM71-BOpmjORygakg.txt",
        course_dir / "_ocr" / "_external__google_drive_local__slides__CSE 30321 SP26 Computer Architecture - 29 - Final Exam Review (marked).pdf.txt",
    ]
    words = [w for w in re.split(r"\W+", label.lower()) if len(w) > 3]
    total_hits = 0
    for g in guideline_sources:
        if g.exists():
            total_hits += sum(g.read_text(errors="ignore").lower().count(w) for w in words)
    if total_hits:
        score += total_hits * 2
        breakdown["guidelines_hits"] = total_hits

    # Mistake explanations from /mistakes (Gradescope wrongs)
    progress = course_dir / "bundles" / "progress.json"
    if progress.exists():
        import json
        try:
            d = json.loads(progress.read_text())
            mistakes = d.get("mistake_explanations", {})
            # Heuristic: mistakes with topic keyword in body
            for v in mistakes.values():
                if isinstance(v, str) and any(w in v.lower() for w in re.split(r"\W+", label.lower()) if len(w) > 4):
                    score += 1
        except Exception:
            pass

    return score, breakdown


def make_megamix(course_dir: Path, voice: str = DEFAULT_VOICE,
                 force: bool = False) -> Path | None:
    """Build single combined MP3 across all topics in priority order."""
    audio_dir = course_dir / "bundles" / "audio"
    if not audio_dir.exists():
        sys.exit("No audio dir — generate per-topic audio first")
    out_file = audio_dir / "_MEGAMIX_exam_review.mp3"
    if out_file.exists() and out_file.stat().st_size > 100000 and not force:
        print(f"cached: {out_file}")
        return out_file

    # Detect course id + graph
    cid = int(course_dir.name.split("_", 1)[0])
    from topic_graph import for_course
    graph = for_course(cid)

    # Score topics
    scored = []
    for topic, info in graph.items():
        script_file = audio_dir / f"{topic}.script.txt"
        if not script_file.exists():
            continue
        score, breakdown = score_topic(course_dir, topic, info["label"])
        scored.append((score, topic, info["label"], script_file, breakdown))
    scored.sort(key=lambda r: -r[0])

    print("Topic priority order (highest first):")
    for s, t, lbl, _, bd in scored:
        print(f"  {s:6.1f}  {t:30}  {bd}")

    # Build full script with smooth transitions.
    lines = []
    n = len(scored)
    intro = (
        f"This is your final exam review podcast. "
        f"You'll hear {n} topics, ordered by exam priority — the material most "
        f"likely to appear first. Listen straight through, or stop early after "
        f"the highest-priority sections. "
        f"Each topic gets four to seven minutes. At one and a half times speed, "
        f"the full review runs about an hour. Let's start."
    )
    lines.append(intro)
    transitions_first = "First up:"
    transitions_mid = [
        "Moving on.", "Next.", "Now.", "Switching gears.",
        "Continuing.", "Up next.", "On to the next topic.",
    ]
    transitions_priority = [
        "This next one is also high-priority.",
        "Another exam-likely topic.",
        "Still in the high-priority zone.",
    ]
    transitions_low = [
        "Dropping into lower-priority territory now — review if you have time.",
        "These remaining topics are less likely on the exam, but worth a quick listen.",
        "Final topics — bonus material.",
    ]
    high_threshold = scored[max(0, n // 3)][0] if n else 0
    low_threshold = scored[min(n - 1, (2 * n) // 3)][0] if n else 0
    low_emitted = False
    for i, (score, topic, label, script_file, _) in enumerate(scored):
        if i == 0:
            t = f"{transitions_first} {label}."
        elif score <= low_threshold and not low_emitted:
            t = f"{transitions_low[0]} {label}."
            low_emitted = True
        elif score >= high_threshold and i < n // 3:
            t = f"{transitions_priority[i % len(transitions_priority)]} {label}."
        else:
            t = f"{transitions_mid[i % len(transitions_mid)]} {label}."
        lines.append(t)
        body = script_file.read_text().strip()
        body = re.sub(r"^(Here is the script[:\.]?\s*)", "", body, flags=re.I)
        body = _sanitize_for_tts(body)
        lines.append(body)
    outro = (
        "That's the full review. Topics covered in priority order based on "
        "exam likelihood. Re-listen to early sections if time allows — those "
        "are the highest-yield. Good luck on the final."
    )
    lines.append(outro)
    full_script = "\n\n".join(lines)
    (audio_dir / "_MEGAMIX_exam_review.script.txt").write_text(full_script)
    print(f"\nMegamix script: {len(full_script):,} chars")

    # Chunk + synth
    chunks = _split_chunks(full_script)
    print(f"chunks: {len(chunks)}")
    audio_parts: list[bytes] = []
    t0 = time.time()
    for i, c in enumerate(chunks):
        try:
            audio_parts.append(synth_chunk(c, voice))
            print(f"  ✓ chunk {i+1}/{len(chunks)} ({len(c)}c)", flush=True)
        except Exception as e:
            print(f"  ✗ chunk {i+1}: {e}")
            return None
        time.sleep(0.15)
    out_file.write_bytes(b"".join(audio_parts))
    elapsed = time.time() - t0
    print(f"\nDone {out_file} ({out_file.stat().st_size//1024//1024} MB) in {elapsed:.1f}s")
    return out_file


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course-dir", required=True)
    ap.add_argument("--voice", default=DEFAULT_VOICE)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    cdir = Path(args.course_dir)
    if not cdir.is_dir():
        sys.exit(f"not a directory: {cdir}")
    make_megamix(cdir, voice=args.voice, force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
