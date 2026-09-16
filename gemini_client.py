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
# Models cached as (location, model_name) -> GenerativeModel so different
# model_names (pro vs flash) coexist with same regional limiters.
_models: dict[tuple[str, str], object] = {}
_limiters: dict[str, "RateLimiter"] = {}
_locations_cache: list[str] = []
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


def _ensure_initialized(model_name: str) -> list[str]:
    """Init regional models for given model_name. Reuses limiters across models."""
    global _models, _limiters, _locations_cache
    if _locations_cache and any(k[1] == model_name for k in _models):
        return _locations_cache
    with _init_lock:
        if _locations_cache and any(k[1] == model_name for k in _models):
            return _locations_cache
        project = os.environ.get("GCP_PROJECT")
        if not project:
            raise RuntimeError("Set GCP_PROJECT env var (your GCP project id).")
        if not _locations_cache:
            _locations_cache = [
                l.strip()
                for l in os.environ.get("GCP_LOCATIONS", DEFAULT_LOCATIONS).split(",")
                if l.strip()
            ]
            rpm = int(os.environ.get("GCP_RPM_PER_REGION", DEFAULT_RPM_PER_REGION))
            for loc in _locations_cache:
                _limiters[loc] = RateLimiter(rpm)
        import vertexai
        from vertexai.generative_models import GenerativeModel
        for loc in _locations_cache:
            if (loc, model_name) in _models:
                continue
            vertexai.init(project=project, location=loc)
            _models[(loc, model_name)] = GenerativeModel(model_name)
        return _locations_cache


def _next_region(model_name: str) -> str:
    global _rotation_idx
    locs = _ensure_initialized(model_name)
    with _rotation_lock:
        loc = locs[_rotation_idx % len(locs)]
        _rotation_idx += 1
    return loc


def generate(prompt: str, system: str | None = None,
             max_output_tokens: int = 8192, temperature: float = 0.3,
             retries: int = 3, use_cache: bool = True,
             model: str | None = None) -> str:
    """Single-text-prompt → string. Round-robins across configured regions."""
    from vertexai.generative_models import GenerationConfig
    model_name = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    cache_payload = {"m": model_name, "p": prompt, "s": system or "",
                     "max": max_output_tokens, "t": temperature, "kind": "text"}
    key = _cache_key(cache_payload)
    if use_cache:
        hit = _cache_get(key)
        if hit is not None:
            return hit

    last_err = ""
    for attempt in range(retries):
        loc = _next_region(model_name)
        model_obj = _models[(loc, model_name)]
        limiter = _limiters[loc]
        full_prompt = (f"{system.strip()}\n\n{prompt}" if system else prompt)
        limiter.wait()
        try:
            resp = model_obj.generate_content(
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
                         retries: int = 3, use_cache: bool = True,
                         model: str | None = None) -> str:
    """Multimodal — text prompt + images (file paths)."""
    from vertexai.generative_models import Part, GenerationConfig
    image_paths_list = list(image_paths)

    # Cache by file content hash so identical attempts hit cache
    img_hashes = []
    for p in image_paths_list:
        with open(p, "rb") as f:
            img_hashes.append(hashlib.sha256(f.read()).hexdigest()[:16])
    model_name = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    cache_payload = {"m": model_name, "p": prompt, "s": system or "",
                     "imgs": img_hashes, "max": max_output_tokens, "kind": "multimodal"}
    key = _cache_key(cache_payload)
    if use_cache:
        hit = _cache_get(key)
        if hit is not None:
            return hit

    import mimetypes
    parts: list = []
    for p in image_paths_list:
        guessed, _ = mimetypes.guess_type(str(p))
        # Default to png for unknown image-like; fall back to pdf header detection
        with open(p, "rb") as f:
            data = f.read()
        if not guessed:
            if data[:4] == b"%PDF":
                guessed = "application/pdf"
            else:
                guessed = "image/png"
        # Vertex supports image/* and application/pdf for inline data
        parts.append(Part.from_data(data=data, mime_type=guessed))
    full_prompt = (f"{system.strip()}\n\n{prompt}" if system else prompt)
    parts.append(full_prompt)
    last_err = ""
    for attempt in range(retries):
        loc = _next_region(model_name)
        model_obj = _models[(loc, model_name)]
        limiter = _limiters[loc]
        limiter.wait()
        try:
            resp = model_obj.generate_content(
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
