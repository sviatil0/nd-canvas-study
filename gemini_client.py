"""Shared Vertex AI Gemini text-generation backend.

Reads project + locations + model from env so callers don't repeat config.

Env:
    GCP_PROJECT             (required)
    GCP_LOCATIONS           comma-separated; default "us-central1,us-east5,us-west4"
    GEMINI_MODEL            default "gemini-2.5-pro"
"""
from __future__ import annotations

import hashlib
import json as _json
import os
import threading
import time
from pathlib import Path as _Path
from typing import Iterable

DEFAULT_LOCATIONS = "us-central1,us-east5,us-west4"
DEFAULT_MODEL = "gemini-2.5-pro"
DEFAULT_RPM_PER_REGION = 5

CACHE_DIR = _Path(os.environ.get("LLM_CACHE_DIR", _Path(__file__).parent / ".llm_cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_key(payload: dict) -> str:
    blob = _json.dumps(payload, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:24]


def _cache_get(key: str) -> str | None:
    f = CACHE_DIR / f"{key}.txt"
    if f.exists():
        return f.read_text()
    return None


def _cache_put(key: str, value: str) -> None:
    (CACHE_DIR / f"{key}.txt").write_text(value)

_init_lock = threading.Lock()
_models: dict[str, object] = {}  # location -> GenerativeModel
_limiters: dict[str, "RateLimiter"] = {}
_rotation_idx = 0
_rotation_lock = threading.Lock()


class RateLimiter:
    def __init__(self, rpm: int):
        self.min_interval = 60.0 / max(rpm, 1)
        self.lock = threading.Lock()
        self.next_allowed = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.time()
            sleep_for = self.next_allowed - now
            if sleep_for > 0:
                time.sleep(sleep_for)
                now = time.time()
            self.next_allowed = max(self.next_allowed, now) + self.min_interval


def _ensure_initialized() -> list[str]:
    global _models, _limiters
    if _models:
        return list(_models.keys())
    with _init_lock:
        if _models:
            return list(_models.keys())
        project = os.environ.get("GCP_PROJECT")
        if not project:
            raise RuntimeError("Set GCP_PROJECT env var (your GCP project id).")
        locations = [
            l.strip()
            for l in os.environ.get("GCP_LOCATIONS", DEFAULT_LOCATIONS).split(",")
            if l.strip()
        ]
        rpm = int(os.environ.get("GCP_RPM_PER_REGION", DEFAULT_RPM_PER_REGION))
        import vertexai
        from vertexai.generative_models import GenerativeModel
        model_name = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
        for loc in locations:
            vertexai.init(project=project, location=loc)
            _models[loc] = GenerativeModel(model_name)
            _limiters[loc] = RateLimiter(rpm)
        return locations


def _next_region() -> str:
    global _rotation_idx
    locs = _ensure_initialized()
    with _rotation_lock:
        loc = locs[_rotation_idx % len(locs)]
        _rotation_idx += 1
    return loc


def generate(prompt: str, system: str | None = None,
             max_output_tokens: int = 8192, temperature: float = 0.3,
             retries: int = 3, use_cache: bool = True) -> str:
    """Single-text-prompt → string. Round-robins across configured regions."""
    from vertexai.generative_models import GenerationConfig
    model_name = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    cache_payload = {"m": model_name, "p": prompt, "s": system or "",
                     "max": max_output_tokens, "t": temperature, "kind": "text"}
    key = _cache_key(cache_payload)
    if use_cache:
        hit = _cache_get(key)
        if hit is not None:
            return hit

    last_err = ""
    for attempt in range(retries):
        loc = _next_region()
        model = _models[loc]
        limiter = _limiters[loc]
        full_prompt = (f"{system.strip()}\n\n{prompt}" if system else prompt)
        limiter.wait()
        try:
            resp = model.generate_content(
                full_prompt,
                generation_config=GenerationConfig(
                    max_output_tokens=max_output_tokens,
                    temperature=temperature,
                ),
            )
            text = (resp.text or "").strip()
            if text:
                if use_cache:
                    _cache_put(key, text)
                return text
            last_err = "empty response"
        except Exception as e:
            last_err = str(e)
            if "429" in last_err or "quota" in last_err.lower():
                time.sleep(min(60, 2 ** attempt * 5))
            else:
                time.sleep(2)
    raise RuntimeError(f"Gemini failed after {retries} tries: {last_err[:300]}")


def generate_with_images(prompt: str, image_paths: Iterable[str],
                         system: str | None = None,
                         max_output_tokens: int = 8192,
                         retries: int = 3, use_cache: bool = True) -> str:
    """Multimodal — text prompt + images (file paths)."""
    from vertexai.generative_models import Part, GenerationConfig
    image_paths_list = list(image_paths)

    # Cache by file content hash so identical attempts hit cache
    img_hashes = []
    for p in image_paths_list:
        with open(p, "rb") as f:
            img_hashes.append(hashlib.sha256(f.read()).hexdigest()[:16])
    model_name = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    cache_payload = {"m": model_name, "p": prompt, "s": system or "",
                     "imgs": img_hashes, "max": max_output_tokens, "kind": "multimodal"}
    key = _cache_key(cache_payload)
    if use_cache:
        hit = _cache_get(key)
        if hit is not None:
            return hit

    parts: list = []
    for p in image_paths_list:
        with open(p, "rb") as f:
            parts.append(Part.from_data(data=f.read(), mime_type="image/png"))
    full_prompt = (f"{system.strip()}\n\n{prompt}" if system else prompt)
    parts.append(full_prompt)
    last_err = ""
    for attempt in range(retries):
        loc = _next_region()
        model = _models[loc]
        limiter = _limiters[loc]
        limiter.wait()
        try:
            resp = model.generate_content(
                parts,
                generation_config=GenerationConfig(
                    max_output_tokens=max_output_tokens,
                    temperature=0.3,
                ),
            )
            text = (resp.text or "").strip()
            if text:
                if use_cache:
                    _cache_put(key, text)
                return text
            last_err = "empty response"
        except Exception as e:
            last_err = str(e)
            if "429" in last_err or "quota" in last_err.lower():
                time.sleep(min(60, 2 ** attempt * 5))
            else:
                time.sleep(2)
    raise RuntimeError(f"Gemini failed after {retries} tries: {last_err[:300]}")
