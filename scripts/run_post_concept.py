#!/usr/bin/env python3
"""Post-concept full automation: images -> TTS/SFX render -> Music v2 -> final mix.

Runs the Gate B -> Gate C span from docs/POST_CONCEPT_AUTOMATION.md in one
command, designed to run under scripts/background_job.py:

1. make_episode.py --write-prompts-only
2. generate_images_gemini.py (Gemini shot images with a style reference)
3. make_episode.py full render (ElevenLabs TTS + SFX + voice/SFX video)
4. Scale a copy of music/composition-plan.json to the actual render length
   (build-final/composition-plan.scaled.json — the source plan is preserved)
5. finalize_episode.py (Music v2 + loudness-steady final MP4), stops at ready_for_review
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_step(name: str, cmd: list[str]) -> None:
    print(f"\n===== step: {name} =====", flush=True)
    subprocess.run(cmd, cwd=str(ROOT), check=True)


def video_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    return float(result.stdout.strip())


def scale_music_plan(episode_dir: Path, out_dir: Path, target_seconds: float, *, tail_seconds: float = 1.5) -> Path:
    """Scale the composition plan to the render length.

    The hand-written source plan (music/composition-plan.json) is never
    modified; the scaled copy is written next to the render outputs and
    its path is returned.
    """
    plan_path = episode_dir / "music" / "composition-plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    chunks = plan.get("chunks") or []
    if not chunks:
        raise RuntimeError(f"No chunks in {plan_path}")
    current_ms = sum(int(c.get("duration_ms") or 0) for c in chunks)
    target_ms = int((target_seconds + tail_seconds) * 1000)
    if current_ms <= 0:
        raise RuntimeError("composition plan has zero total duration")
    ratio = target_ms / current_ms
    if abs(ratio - 1.0) < 0.02:
        print(f"music plan already matches render ({current_ms}ms vs {target_ms}ms)", flush=True)
    else:
        scaled = 0
        for chunk in chunks[:-1]:
            new_ms = max(3000, int(round(int(chunk["duration_ms"]) * ratio)))
            chunk["duration_ms"] = new_ms
            scaled += new_ms
        chunks[-1]["duration_ms"] = max(3000, target_ms - scaled)
        print(
            f"scaled music plan {current_ms}ms -> {sum(int(c['duration_ms']) for c in chunks)}ms "
            f"(render {target_seconds:.1f}s + tail {tail_seconds:.1f}s)",
            flush=True,
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    scaled_path = out_dir / "composition-plan.scaled.json"
    scaled_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return scaled_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the full post-concept pipeline for one episode.")
    parser.add_argument("--episode", type=Path, required=True, help="Path to episode.json")
    parser.add_argument("--reference", type=Path, default=None, help="Approved concept image for style reference.")
    parser.add_argument("--image-model", default="gemini-3.1-flash-image")
    parser.add_argument("--skip-images", action="store_true")
    args = parser.parse_args()

    episode_path = args.episode.resolve()
    episode_dir = episode_path.parent
    episode = json.loads(episode_path.read_text(encoding="utf-8"))
    out_dir = episode_dir / "build-final"

    run_step(
        "write image prompts",
        [sys.executable, str(ROOT / "scripts" / "make_episode.py"), "--episode", str(episode_path), "--write-prompts-only"],
    )

    if not args.skip_images:
        image_cmd = [
            sys.executable,
            str(ROOT / "scripts" / "generate_images_gemini.py"),
            "--episode",
            str(episode_path),
            "--model",
            args.image_model,
        ]
        if args.reference:
            image_cmd += ["--reference", str(args.reference.resolve())]
        run_step("generate shot images (Gemini)", image_cmd)

    run_step(
        "voice/SFX render",
        [sys.executable, str(ROOT / "scripts" / "make_episode.py"), "--episode", str(episode_path), "--out-dir", str(out_dir)],
    )

    voice_sfx = out_dir / f"{episode['id']}-voice-sfx.mp4"
    if not voice_sfx.exists():
        raise RuntimeError(f"Missing voice/SFX render: {voice_sfx}")
    scaled_plan = scale_music_plan(episode_dir, out_dir, video_duration(voice_sfx))

    run_step(
        "Music v2 + final mix",
        [
            sys.executable,
            str(ROOT / "scripts" / "finalize_episode.py"),
            "--episode",
            str(episode_path),
            "--out-dir",
            str(out_dir),
            "--music-plan",
            str(scaled_plan),
        ],
    )
    print("\npipeline complete: ready_for_review", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
