"""Claude Code CLI text-generation backend.

Drop-in sibling of `gemini_client`: same disk cache, same `generate(...)` /
`generate_with_images(...)` signatures, so a view can swap backends by env
without touching prompt code. Inference runs through the local `claude` CLI in
non-interactive mode, so it bills against the Claude Code subscription and
needs no API key.

Beyond the Gemini surface it also exposes `run_with_tools(...)`, which is the
whole reason to prefer Claude Code here: the model can Read course files and
shell out (tshark, python, scapy) to compute an answer from an attachment
instead of guessing at it.

Env:
    CLAUDE_MODEL            default "sonnet"   (fast lane)
    CLAUDE_THINKING_MODEL   default "opus"     (thinking lane)
    CLAUDE_MAX_PROCS        default 4          (concurrent CLI processes)
    CLAUDE_TIMEOUT          default 300        (seconds, text calls)
    LLM_CACHE_DIR           default ./.llm_cache
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Iterable, Sequence

DEFAULT_MODEL = "sonnet"
DEFAULT_THINKING_MODEL = "opus"
DEFAULT_TIMEOUT = 300
TOOL_TIMEOUT = 900

CACHE_DIR = Path(os.environ.get("LLM_CACHE_DIR", Path(__file__).parent / ".llm_cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Text-only calls: deny every tool so the CLI can't wander off into the
# filesystem when all we asked for was prose.
NO_TOOLS = ["Bash", "Edit", "Write", "NotebookEdit", "Read", "Glob", "Grep",
            "Task", "WebFetch", "WebSearch", "TodoWrite"]

# Tool calls: read-only inspection plus the packet/data CLIs a homework
# attachment actually needs. Bash is scoped per-command, never bare.
DEFAULT_TOOLS = [
    "Read", "Glob", "Grep",
    "Bash(tshark:*)", "Bash(capinfos:*)", "Bash(editcap:*)", "Bash(dumpcap:*)",
    "Bash(python3:*)", "Bash(python:*)", "Bash(head:*)", "Bash(tail:*)",
    "Bash(wc:*)", "Bash(file:*)", "Bash(strings:*)", "Bash(xxd:*)",
    "Bash(sort:*)", "Bash(uniq:*)", "Bash(grep:*)", "Bash(awk:*)", "Bash(sed:*)",
]

_sem = threading.Semaphore(int(os.environ.get("CLAUDE_MAX_PROCS", "4")))


class ClaudeError(RuntimeError):
    pass


def cli_path() -> str:
    return shutil.which("claude") or "claude"


def available() -> bool:
    return shutil.which("claude") is not None


def model_for(thinking: bool = False) -> str:
    if thinking:
        return os.environ.get("CLAUDE_THINKING_MODEL", DEFAULT_THINKING_MODEL)
    return os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL)


def _cache_key(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True).encode()
    return "claude_" + hashlib.sha256(blob).hexdigest()[:24]


def _cache_get(key: str) -> dict | None:
    f = CACHE_DIR / f"{key}.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except Exception:
        return None


def _cache_put(key: str, value: dict) -> None:
    (CACHE_DIR / f"{key}.json").write_text(json.dumps(value, indent=1))


def _file_digest(path: str | Path) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


def _invoke(prompt: str, *, system: str | None, model: str,
            allowed_tools: Sequence[str] | None,
            denied_tools: Sequence[str] | None,
            add_dirs: Sequence[str | Path] | None,
            cwd: str | Path | None, timeout: int) -> dict:
    """One `claude -p` process. Returns the parsed result envelope."""
    args = [cli_path(), "-p", "--output-format", "json", "--model", model,
            # No user/project settings, no MCP servers: the study tool's prompts
            # should not inherit whatever is configured for interactive work.
            "--setting-sources", "", "--strict-mcp-config"]
    if system:
        args += ["--system-prompt", system]
    if allowed_tools:
        args += ["--allowedTools", *allowed_tools]
    if denied_tools:
        args += ["--disallowedTools", *denied_tools]
    for d in (add_dirs or []):
        args += ["--add-dir", str(d)]

    with _sem:
        try:
            proc = subprocess.run(
                args,
                # Prompt over stdin so a long prompt is never mistaken for a
                # value of the preceding --flag.
                input=prompt,
                capture_output=True, text=True, timeout=timeout,
                cwd=str(cwd) if cwd else None,
            )
        except FileNotFoundError:
            raise ClaudeError("claude CLI not found on PATH")
        except subprocess.TimeoutExpired:
            raise ClaudeError(f"claude timed out after {timeout}s")

    raw = (proc.stdout or "").strip()
    if not raw:
        raise ClaudeError(f"claude produced no output: {(proc.stderr or '')[:300]}")
    try:
        env = json.loads(raw)
    except json.JSONDecodeError:
        # --output-format json should always hold, but never lose a usable answer.
        return {"text": raw, "is_error": proc.returncode != 0, "cost_usd": 0.0,
                "model": model, "turns": 1, "session_id": ""}

    text = (env.get("result") or "").strip()
    if env.get("is_error") or not text:
        raise ClaudeError(text or env.get("api_error_status") or "claude returned an error")
    return {
        "text": text,
        "is_error": False,
        "cost_usd": env.get("total_cost_usd") or 0.0,
        "model": model,
        "turns": env.get("num_turns") or 1,
        "session_id": env.get("session_id") or "",
        "denials": env.get("permission_denials") or [],
    }


def generate(prompt: str, system: str | None = None,
             max_output_tokens: int | None = None, temperature: float | None = None,
             retries: int = 2, use_cache: bool = True,
             model: str | None = None, thinking: bool = False) -> str:
    """Single-text-prompt -> string.

    `max_output_tokens` / `temperature` are accepted for signature parity with
    gemini_client and ignored: the CLI exposes neither.
    """
    model_name = model or model_for(thinking)
    key = _cache_key({"p": prompt, "s": system or "", "m": model_name, "kind": "text"})
    if use_cache:
        hit = _cache_get(key)
        if hit:
            return hit["text"]

    last = ""
    for _ in range(max(1, retries)):
        try:
            out = _invoke(prompt, system=system, model=model_name,
                          allowed_tools=None, denied_tools=NO_TOOLS,
                          add_dirs=None, cwd=None,
                          timeout=int(os.environ.get("CLAUDE_TIMEOUT", DEFAULT_TIMEOUT)))
        except ClaudeError as e:
            last = str(e)
            continue
        if use_cache:
            _cache_put(key, out)
        return out["text"]
    raise ClaudeError(f"claude failed after {retries} tries: {last[:300]}")


def generate_with_images(prompt: str, image_paths: Iterable[str],
                         system: str | None = None,
                         max_output_tokens: int | None = None,
                         retries: int = 2, use_cache: bool = True,
                         model: str | None = None, thinking: bool = False) -> str:
    """Text prompt + local images, read off disk by the Read tool."""
    paths = [str(Path(p).resolve()) for p in image_paths]
    model_name = model or model_for(thinking)
    key = _cache_key({"p": prompt, "s": system or "", "m": model_name,
                      "imgs": [_file_digest(p) for p in paths], "kind": "images"})
    if use_cache:
        hit = _cache_get(key)
        if hit:
            return hit["text"]

    full = (prompt + "\n\nRead these image files before answering:\n"
            + "\n".join(f"- {p}" for p in paths))
    dirs = sorted({str(Path(p).parent) for p in paths})
    last = ""
    for _ in range(max(1, retries)):
        try:
            out = _invoke(full, system=system, model=model_name,
                          allowed_tools=["Read"], denied_tools=None,
                          add_dirs=dirs, cwd=None, timeout=TOOL_TIMEOUT)
        except ClaudeError as e:
            last = str(e)
            continue
        if use_cache:
            _cache_put(key, out)
        return out["text"]
    raise ClaudeError(f"claude failed after {retries} tries: {last[:300]}")


def run_with_tools(prompt: str, *, system: str | None = None,
                   files: Sequence[str | Path] = (),
                   allowed_tools: Sequence[str] | None = None,
                   cwd: str | Path | None = None,
                   model: str | None = None, thinking: bool = False,
                   timeout: int = TOOL_TIMEOUT, use_cache: bool = True) -> dict:
    """Let Claude Code actually inspect `files` with real tools.

    Returns the full envelope (text, cost_usd, turns, denials) because the
    caller wants to show what it cost and whether a tool was refused.
    """
    paths = [str(Path(f).resolve()) for f in files]
    model_name = model or model_for(thinking)
    digests = []
    for p in paths:
        try:
            digests.append(_file_digest(p))
        except OSError:
            digests.append("missing")
    key = _cache_key({"p": prompt, "s": system or "", "m": model_name,
                      "files": digests, "kind": "tools"})
    if use_cache:
        hit = _cache_get(key)
        if hit:
            return {**hit, "cached": True}

    full = prompt
    if paths:
        full += "\n\nFiles you may inspect:\n" + "\n".join(f"- {p}" for p in paths)
    dirs = sorted({str(Path(p).parent) for p in paths})
    if cwd:
        dirs = sorted(set(dirs) | {str(Path(cwd).resolve())})
    out = _invoke(full, system=system, model=model_name,
                  allowed_tools=list(allowed_tools or DEFAULT_TOOLS),
                  denied_tools=None, add_dirs=dirs, cwd=cwd, timeout=timeout)
    if use_cache:
        _cache_put(key, out)
    return {**out, "cached": False}
