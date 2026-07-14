#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import make_episode


ROOT = Path(__file__).resolve().parents[1]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def update_status(episode_dir: Path, **values: Any) -> None:
    status_path = episode_dir / "automation" / "status.json"
    payload: dict[str, Any] = {}
    if status_path.exists():
        payload = read_json(status_path)
    payload.update(values)
    payload["updated_at"] = utc_now()
    write_json(status_path, payload)


def run(cmd: list[str], *, log_path: Path, cwd: Path = ROOT) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n[{utc_now()}] $ {' '.join(cmd)}\n")
        log.flush()
        subprocess.run(cmd, cwd=str(cwd), stdout=log, stderr=subprocess.STDOUT, check=True)


def ffprobe_duration(path: Path) -> float:
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


def ffprobe_video(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,avg_frame_rate",
            "-show_entries",
            "format=duration,size",
            "-of",
            "json",
            str(path),
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def generate_music(episode: dict[str, Any], episode_dir: Path, *, log_path: Path) -> tuple[Path, Path]:
    music_dir = episode_dir / "music"
    plan_path = music_dir / "composition-plan.json"
    if not plan_path.exists():
        raise RuntimeError(f"Missing Music v2 plan: {plan_path}")

    make_episode.load_project_env()
    api_key = os.environ.get("ELEVENLABS_API_KEY") or os.environ.get("ELEVEN_LABS_API_KEY")
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY is required for Music v2 generation.")

    music_dir.mkdir(parents=True, exist_ok=True)
    request_path = music_dir / "music-request.json"
    source_path = music_dir / f"{episode['id']}-elevenlabs-music-v2.mp3"
    headers_path = music_dir / "generation-headers.txt"
    steady_path = music_dir / f"{episode['id']}-elevenlabs-music-v2-steady.wav"

    payload = {
        "model_id": "music_v2",
        "composition_plan": read_json(plan_path),
    }
    write_json(request_path, payload)

    if not source_path.exists() or source_path.stat().st_size == 0:
        request = urllib.request.Request(
            "https://api.elevenlabs.io/v1/music",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=900) as response:
                source_path.write_bytes(response.read())
                safe_headers = []
                for key, value in response.headers.items():
                    safe_headers.append(f"{key}: {value}")
                headers_path.write_text(
                    f"HTTP {response.status}\n" + "\n".join(safe_headers) + "\n",
                    encoding="utf-8",
                )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"\n[{utc_now()}] ElevenLabs Music v2 failed: {detail}\n")
            raise

    loudnorm_cfg = (episode.get("audio") or {}).get("music_loudnorm") or {}
    ln_i = loudnorm_cfg.get("i", -27)
    ln_tp = loudnorm_cfg.get("tp", -3)
    ln_lra = loudnorm_cfg.get("lra", 5)
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source_path),
            "-af",
            f"loudnorm=I={ln_i}:TP={ln_tp}:LRA={ln_lra}",
            "-ar",
            "44100",
            "-ac",
            "2",
            str(steady_path),
        ],
        log_path=log_path,
    )
    return source_path, steady_path


def finalize(args: argparse.Namespace) -> int:
    episode_path = args.episode.resolve()
    episode_dir = episode_path.parent
    episode = read_json(episode_path)
    out_dir = args.out_dir.resolve() if args.out_dir else episode_dir / "build-final"
    log_path = args.log.resolve() if args.log else episode_dir / "automation" / "finalize.log"
    final_path = out_dir / f"{episode['id']}-elevenlabs-music-v2-steady.mp4"

    update_status(episode_dir, stage="finalize_running", final_job_started_at=utc_now())
    run(
        [
            sys.executable,
            str(ROOT / "scripts" / "make_episode.py"),
            "--episode",
            str(episode_path),
            "--out-dir",
            str(out_dir),
        ],
        log_path=log_path,
    )

    voice_sfx_path = out_dir / f"{episode['id']}-voice-sfx.mp4"
    if not voice_sfx_path.exists():
        raise RuntimeError(f"Missing voice/SFX render: {voice_sfx_path}")

    source_music, steady_music = generate_music(episode, episode_dir, log_path=log_path)
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(voice_sfx_path),
            "-i",
            str(steady_music),
            "-filter_complex",
            "[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,alimiter=limit=0.95[outa]",
            "-map",
            "0:v:0",
            "-map",
            "[outa]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            "-shortest",
            str(final_path),
        ],
        log_path=log_path,
    )

    video_info = ffprobe_video(final_path)
    duration = ffprobe_duration(final_path)
    stream = (video_info.get("streams") or [{}])[0]
    update_status(
        episode_dir,
        stage="ready_for_review",
        voice_sfx_video=str(voice_sfx_path.relative_to(episode_dir)),
        music_source=str(source_music.relative_to(episode_dir)),
        music_steady=str(steady_music.relative_to(episode_dir)),
        final_video=str(final_path.relative_to(episode_dir)),
        duration_seconds=round(duration, 3),
        width=stream.get("width"),
        height=stream.get("height"),
        fps=stream.get("avg_frame_rate"),
        final_job_finished_at=utc_now(),
    )
    print(final_path)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render voice/SFX, generate ElevenLabs Music v2, and create a final fixed-volume MP4.")
    parser.add_argument("--episode", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--log", type=Path, default=None)
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(finalize(parse_args()))
