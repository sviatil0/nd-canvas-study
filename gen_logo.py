"""Generate app logo via Vertex AI image generation (Imagen / Nano Banana).

Usage:
    python gen_logo.py --out ui/static/logo.png
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROMPT_VARIANTS = [
    "Minimalist app icon for a college study tool. Centered: stylized open book "
    "overlapping a connected-nodes brain graph. Muted sapphire #1e3a5f on warm "
    "oatmeal #f8f5f0. Flat geometric, soft shadow, rounded square. No text.",

    "App icon: abstract bell curve / normal distribution shape forming a graduation "
    "cap silhouette. Muted sapphire blue and warm gold accents on oatmeal background. "
    "Minimalist, geometric, rounded square frame. No text.",

    "App icon: stack of books arranged like a knowledge graph with circular nodes "
    "connecting them. Sapphire blue #1e3a5f primary, clay orange #d97757 accent. "
    "Flat design, scholarly calm aesthetic, rounded square. No text.",

    "App icon: stylized lowercase 'nd' monogram woven through a study/lightbulb icon. "
    "Muted sapphire on warm oatmeal cream background. Flat, modern, rounded square. "
    "Scholarly, minimal. No additional text.",

    "App icon: a notebook with a glowing spark on top, integrated with subtle bar-chart "
    "/ statistics curve. Sapphire blue and warm gold. Minimalist flat design, rounded "
    "square frame, soft shadow. No text or letters.",
]
PROMPT = PROMPT_VARIANTS[0]  # legacy single-prompt fallback


def gen_via_imagen(out: Path, project: str, location: str = "us-central1",
                   prompt: str = None) -> bool:
    try:
        from vertexai.preview.vision_models import ImageGenerationModel
        import vertexai
        vertexai.init(project=project, location=location)
        model = ImageGenerationModel.from_pretrained("imagen-3.0-generate-002")
        images = model.generate_images(prompt=prompt or PROMPT,
                                       number_of_images=1, aspect_ratio="1:1")
        if not images:
            return False
        out.parent.mkdir(parents=True, exist_ok=True)
        images[0].save(str(out))
        print(f"wrote {out}")
        return True
    except Exception as e:
        print(f"Imagen failed for {out.name}: {e}")
        return False


def gen_via_gemini_image(out: Path, project: str, location: str = "us-central1") -> bool:
    """Fallback: gemini-2.5-flash-image (Nano Banana)."""
    try:
        from vertexai.generative_models import GenerativeModel, Part
        import vertexai
        vertexai.init(project=project, location=location)
        model = GenerativeModel("gemini-2.5-flash-image-preview")
        resp = model.generate_content([PROMPT])
        for part in resp.candidates[0].content.parts:
            if hasattr(part, "inline_data") and part.inline_data:
                out.parent.mkdir(parents=True, exist_ok=True)
                with open(out, "wb") as f:
                    f.write(part.inline_data.data)
                print(f"wrote {out}")
                return True
        return False
    except Exception as e:
        print(f"Gemini image failed: {e}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="ui/static/logo.png")
    ap.add_argument("--project", default=os.environ.get("GCP_PROJECT", "nd-canvas-ocr-1777818991"))
    ap.add_argument("--variants", action="store_true",
                    help="Generate all PROMPT_VARIANTS as logo_v1..vN.png")
    args = ap.parse_args()
    if args.variants:
        out_dir = Path(args.out).parent
        out_dir.mkdir(parents=True, exist_ok=True)
        any_ok = False
        for i, p in enumerate(PROMPT_VARIANTS, 1):
            dest = out_dir / f"logo_v{i}.png"
            print(f"\n=== variant {i} ===\n{p[:120]}…")
            ok = gen_via_imagen(dest, args.project, prompt=p)
            any_ok = any_ok or ok
        return 0 if any_ok else 1
    out = Path(args.out)
    if gen_via_imagen(out, args.project):
        return 0
    if gen_via_gemini_image(out, args.project):
        return 0
    print("Both image generators failed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
