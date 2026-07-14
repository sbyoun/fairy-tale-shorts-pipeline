#!/usr/bin/env python3
"""Generate per-dialogue shot images with the Gemini image API.

Reads episodes/<id>/image-prompts.json (written by make_episode.py
--write-prompts-only) and writes images/<shot_id>.png. An approved
representative concept image can be attached to every request as a
style/character reference.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "gemini-3.1-flash-image"
MAX_ATTEMPTS = 4

REFERENCE_INSTRUCTION = (
    "Use the attached reference image as the definitive guide for the art style, "
    "material language, and character designs. Keep the same needle-felted plush "
    "figurine world, the same character appearances, and the same color palette. "
    "Render the new shot described below in that exact style."
)


def load_project_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def encode_reference(path: Path) -> dict:
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    return {
        "inline_data": {
            "mime_type": mime,
            "data": base64.b64encode(path.read_bytes()).decode("ascii"),
        }
    }


def is_valid_image(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        with Image.open(path) as img:
            img.load()
            width, height = img.size
    except Exception:
        return False
    return width >= 512 and height > width


def generate_one(
    *,
    api_key: str,
    model: str,
    prompt: str,
    reference_part: dict | None,
    out_path: Path,
) -> None:
    parts: list[dict] = []
    if reference_part is not None:
        parts.append(reference_part)
        parts.append({"text": REFERENCE_INSTRUCTION + "\n\n" + prompt})
    else:
        parts.append({"text": prompt})

    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {"aspectRatio": "9:16", "imageSize": "1K"},
        },
    }
    request = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=json.dumps(body).encode("utf-8"),
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        method="POST",
    )

    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                payload = json.load(response)
            candidates = payload.get("candidates") or []
            image_bytes: bytes | None = None
            for part in ((candidates[0].get("content") or {}).get("parts") or []) if candidates else []:
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    image_bytes = base64.b64decode(inline["data"])
                    break
            if image_bytes is None:
                finish = candidates[0].get("finishReason") if candidates else "NO_CANDIDATES"
                raise RuntimeError(f"No image in response (finishReason={finish})")
            with Image.open(io.BytesIO(image_bytes)) as img:
                img.load()
                if img.size[1] <= img.size[0]:
                    raise RuntimeError(f"Not portrait: {img.size}")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(image_bytes)
            return
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            last_error = RuntimeError(f"HTTP {exc.code}: {detail}")
            if exc.code not in (429, 500, 502, 503, 504):
                raise last_error
        except (urllib.error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
            last_error = exc
        if attempt < MAX_ATTEMPTS:
            wait = 2.0 * (2 ** (attempt - 1))
            print(f"  retry {attempt}/{MAX_ATTEMPTS - 1} in {wait:.0f}s: {last_error}", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"Failed after {MAX_ATTEMPTS} attempts: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate dialogue shot images with Gemini.")
    parser.add_argument("--episode", type=Path, required=True, help="Path to episode.json")
    parser.add_argument("--reference", type=Path, default=None, help="Approved concept image used as style reference.")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--only", default=None, help="Comma-separated shot_id list to (re)generate.")
    parser.add_argument("--force", action="store_true", help="Regenerate even if a valid image exists.")
    args = parser.parse_args()

    load_project_env()
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY is required (put it in the project .env).")

    episode_dir = args.episode.resolve().parent
    prompts_path = episode_dir / "image-prompts.json"
    if not prompts_path.exists():
        raise SystemExit(f"Missing {prompts_path}. Run make_episode.py --write-prompts-only first.")
    prompts = json.loads(prompts_path.read_text(encoding="utf-8"))

    reference_part = None
    if args.reference:
        reference_path = args.reference.resolve()
        if not reference_path.exists():
            raise SystemExit(f"Reference image not found: {reference_path}")
        reference_part = encode_reference(reference_path)

    only = {s.strip() for s in args.only.split(",")} if args.only else None
    image_dir = episode_dir / "images"
    done = skipped = failed = 0
    failures: list[str] = []

    for entry in prompts:
        shot_id = entry["shot_id"]
        if only and shot_id not in only:
            continue
        out_path = image_dir / f"{shot_id}.png"
        if not args.force and is_valid_image(out_path):
            skipped += 1
            continue
        print(f"[{shot_id}] generating...", flush=True)
        try:
            generate_one(
                api_key=api_key,
                model=args.model,
                prompt=entry["prompt"],
                reference_part=reference_part,
                out_path=out_path,
            )
            done += 1
            print(f"[{shot_id}] ok", flush=True)
        except Exception as exc:
            failed += 1
            failures.append(shot_id)
            print(f"[{shot_id}] FAILED: {exc}", flush=True)

    print(f"done={done} skipped={skipped} failed={failed}")
    if failures:
        print("failed shots: " + ",".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
