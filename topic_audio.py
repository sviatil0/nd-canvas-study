"""Generate podcast-style audio per topic summary.

For each `bundles/topics/<topic>.md`:
  1. Strip Markdown to plain text suitable for TTS
  2. Convert formulas/code to spoken descriptions (Gemini reformat pass)
  3. Synthesize MP3 via Cloud TTS Chirp3-HD voice
  4. Save to `bundles/audio/<topic>.mp3`

Long summaries chunked at ~4500 chars (TTS API limit), MP3s concatenated.

Usage:
    python topic_audio.py --course-dir downloads/129492_porgramming-paradigms
    python topic_audio.py --course-dir <dir> --topic javascript_closures
    python topic_audio.py --course-dir <dir> --voice en-US-Chirp3-HD-Aoede
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

DEFAULT_VOICE = "en-US-Chirp3-HD-Aoede"  # best female: warm, natural, holds up at 1.5x
TTS_CHUNK_LIMIT = 4500  # Cloud TTS hard limit ~5000 bytes; leave margin

REFORMAT_PROMPT = """Convert this topic summary into a TIGHT exam-prep audio
script for the topic: {label}.

CONSTRAINTS:
- 600-1000 words MAX (≈ 4-7 min audio at 1x speed). Brutal trimming required.
- Every sentence must teach exam-relevant content. NO filler, NO tangents,
  NO motivational asides ("this is important", "let's dive in"), NO
  recapping what you just said.
- Stay STRICTLY on this topic. Don't drift into related topics — they have
  their own audio.

STYLE RULES:
1. Drop Markdown (##, **, |, ```).
2. Convert code into spoken English: "the function takes a callback and
   returns a closure capturing the outer x". Don't read literal syntax
   character-by-character.
3. Convert tables into 1-line comparisons: "static typing checks at compile
   time; dynamic typing checks at run time".
4. Convert symbols (≤, →, ∀) to spoken words.
5. Use minimal transitions ("Next.", "Key point.", "Trap to avoid:").
6. Direct, instructional tone — like a tutor giving a 5-minute targeted
   review the night before the exam.

PRIORITIZE in this order:
1. Definitions + key invariants
2. Common exam pitfalls + gotchas
3. Distinguishing pairs (== vs ===, var vs let, etc.)
4. One worked example only if essential
5. Skip background/motivation entirely

NO meta commentary. Return ONLY the script body, no headers.

Input:
---
{md}
---
"""


def _reformat_to_speech(md: str, label: str = "this topic") -> str:
    """Use Gemini to convert Markdown into TIGHT exam-prep spoken script."""
    sys.path.insert(0, str(Path(__file__).parent))
    from gemini_client import generate
    prompt = REFORMAT_PROMPT.format(md=md[:60000], label=label)
    return generate(prompt, max_output_tokens=4000, temperature=0.3)


def _split_chunks(text: str, limit: int = TTS_CHUNK_LIMIT) -> list[str]:
    """Split on paragraph boundaries, then sentence boundaries, ≤ limit chars."""
    paras = re.split(r"\n\n+", text)
    chunks: list[str] = []
    cur = ""
    for p in paras:
        if len(cur) + len(p) + 2 <= limit:
            cur += ("\n\n" if cur else "") + p
        else:
            if cur:
                chunks.append(cur)
            if len(p) <= limit:
                cur = p
            else:
                # paragraph too long → split sentences
                sentences = re.split(r"(?<=[.!?])\s+", p)
                cur = ""
                for s in sentences:
                    if len(cur) + len(s) + 1 <= limit:
                        cur += (" " if cur else "") + s
                    else:
                        if cur:
                            chunks.append(cur)
                        cur = s
    if cur:
        chunks.append(cur)
    return chunks


def synth_chunk(text: str, voice: str) -> bytes:
    """Synth one chunk → MP3 bytes."""
    from google.cloud import texttospeech
    client = texttospeech.TextToSpeechClient()
    synthesis_input = texttospeech.SynthesisInput(text=text)
    voice_params = texttospeech.VoiceSelectionParams(
        language_code="en-US",
        name=voice,
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        speaking_rate=1.0,
    )
    resp = client.synthesize_speech(
        input=synthesis_input,
        voice=voice_params,
        audio_config=audio_config,
    )
    return resp.audio_content


def synth_topic(course_dir: Path, topic: str, voice: str = DEFAULT_VOICE,
                force: bool = False, skip_reformat: bool = False) -> tuple[str, Path | None, str | None]:
    md_file = course_dir / "bundles" / "topics" / f"{topic}.md"
    if not md_file.exists():
        return (topic, None, "no summary md")
    out_dir = course_dir / "bundles" / "audio"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{topic}.mp3"
    script_file = out_dir / f"{topic}.script.txt"
    if out_file.exists() and out_file.stat().st_size > 5000 and not force:
        return (topic, out_file, "cached")

    md = md_file.read_text()

    # Lookup topic label for context-aware reformat
    label = topic
    try:
        cid = int(course_dir.name.split("_", 1)[0])
        from topic_graph import for_course
        g = for_course(cid)
        label = g.get(topic, {}).get("label", topic)
    except Exception:
        pass

    # Reformat to spoken script
    if skip_reformat or script_file.exists():
        script = script_file.read_text() if script_file.exists() else md
    else:
        try:
            script = _reformat_to_speech(md, label=label)
            script_file.write_text(script)
        except Exception as e:
            return (topic, None, f"reformat failed: {e}")

    # Sanitize for TTS (operators, symbols, markdown remnants)
    try:
        from topic_audio_megamix import _sanitize_for_tts
        script = _sanitize_for_tts(script)
    except Exception:
        pass

    # Split + synth
    chunks = _split_chunks(script)
    if not chunks:
        return (topic, None, "empty script")
    audio_parts: list[bytes] = []
    try:
        for i, c in enumerate(chunks):
            audio_parts.append(synth_chunk(c, voice))
            time.sleep(0.2)
    except Exception as e:
        return (topic, None, f"tts failed: {e}")

    # Concatenate raw MP3 frames (works for same encoding/voice)
    out_file.write_bytes(b"".join(audio_parts))
    return (topic, out_file, None)


def build_all(course_dir: Path, voice: str = DEFAULT_VOICE,
              workers: int = 3, only: list[str] | None = None,
              skip_existing: bool = True,
              skip_reformat: bool = False) -> None:
    topics_dir = course_dir / "bundles" / "topics"
    if not topics_dir.exists():
        sys.exit("No topic summaries — generate them first")
    targets = only or [p.stem for p in sorted(topics_dir.glob("*.md"))]
    out_dir = course_dir / "bundles" / "audio"
    if skip_existing:
        targets = [t for t in targets
                   if not ((out_dir / f"{t}.mp3").exists() and
                           (out_dir / f"{t}.mp3").stat().st_size > 5000)]
    print(f"Generating {len(targets)} audio files with voice {voice}, {workers} workers")
    t0 = time.time()
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(synth_topic, course_dir, t, voice, False, skip_reformat): t
                   for t in targets}
        for fut in as_completed(futures):
            topic = futures[fut]
            t_, path, err = fut.result()
            if err and err != "cached":
                print(f"  ✗ {topic}: {err}")
                fail += 1
            else:
                size = path.stat().st_size if path else 0
                tag = "[cached]" if err == "cached" else "✓"
                print(f"  {tag} {topic}: {size//1024} KB", flush=True)
                ok += 1
    print(f"Done {ok}/{ok+fail} in {time.time()-t0:.1f}s → {out_dir}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course-dir", required=True)
    ap.add_argument("--topic", action="append", default=[])
    ap.add_argument("--voice", default=DEFAULT_VOICE)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--skip-reformat", action="store_true",
                    help="skip Gemini reformat (use raw markdown — saves time)")
    args = ap.parse_args()
    cdir = Path(args.course_dir)
    if not cdir.is_dir():
        sys.exit(f"not a directory: {cdir}")
    build_all(cdir, voice=args.voice, workers=args.workers,
              only=args.topic or None,
              skip_existing=not args.rebuild,
              skip_reformat=args.skip_reformat)
    return 0


if __name__ == "__main__":
    sys.exit(main())
