#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import random
import re
import struct
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
CANVAS_W = 1080
CANVAS_H = 1920
FPS = 30
SAMPLE_RATE = 44100

FONT_REGULAR_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
FONT_BOLD_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


CACHE_MANIFEST_NAME = "cache-manifest.json"


def input_hash(payload: Any) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_cache_manifest(out_dir: Path) -> dict[str, str]:
    path = out_dir / CACHE_MANIFEST_NAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items()}


def cached_output_is_fresh(out_dir: Path, path: Path, digest: str) -> bool:
    if not (path.exists() and path.stat().st_size > 0):
        return False
    key = str(path.relative_to(out_dir))
    return load_cache_manifest(out_dir).get(key) == digest


def record_cache_entry(out_dir: Path, path: Path, digest: str) -> None:
    manifest = load_cache_manifest(out_dir)
    manifest[str(path.relative_to(out_dir))] = digest
    write_json(out_dir / CACHE_MANIFEST_NAME, manifest)


def safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


def load_project_env() -> None:
    load_env_file(ROOT / ".env")
    load_env_file(Path.home() / ".env")


def pick_font(candidates: list[str], size: int) -> ImageFont.ImageFont:
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default(size=size)


def text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def wrap_text(text: str, draw: ImageDraw.ImageDraw, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        return []
    has_spaces = " " in text
    tokens = text.split(" ") if has_spaces else list(text)
    lines: list[str] = []
    current = ""
    for token in tokens:
        candidate = f"{current} {token}".strip() if has_spaces else current + token
        if current and text_width(draw, candidate, font) > max_width:
            lines.append(current)
            current = token
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def draw_multiline(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    lines: list[str],
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int],
    spacing: int,
) -> int:
    x, y = xy
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        box = draw.textbbox((x, y), line, font=font)
        y = box[3] + spacing
    return y


def fit_cover(img: Image.Image) -> Image.Image:
    img = img.convert("RGB")
    scale = max(CANVAS_W / img.width, CANVAS_H / img.height)
    resized = img.resize((math.ceil(img.width * scale), math.ceil(img.height * scale)), Image.Resampling.LANCZOS)
    left = (resized.width - CANVAS_W) // 2
    top = (resized.height - CANVAS_H) // 2
    return resized.crop((left, top, left + CANVAS_W, top + CANVAS_H))


def scene_image_path(episode_dir: Path, scene_id: str) -> Path | None:
    image_dir = episode_dir / "images"
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        path = image_dir / f"{scene_id}{ext}"
        if path.exists():
            return path
    return None


def frame_image_path(episode_dir: Path, frame: dict[str, Any]) -> Path | None:
    image_id = str(frame.get("id") or "")
    if not image_id:
        return None
    return scene_image_path(episode_dir, image_id)


def speaker_focus(speaker: str, episode: dict[str, Any] | None = None) -> str:
    profile = ((episode or {}).get("voice_profiles") or {}).get(speaker) or {}
    if isinstance(profile, dict) and str(profile.get("image_focus") or "").strip():
        return str(profile["image_focus"]).strip()

    focus_by_speaker = {
        "narrator": "a wide storybook view of the moment, showing the main characters and setting clearly",
        "ella": "Ella speaking or reacting kindly in the center of the frame",
        "stepmother": "Ella's stepmother speaking warmly, pointing at the shared plan or invitation",
        "stepsister_1": "Ella's first older stepsister speaking, a friendly older sister in a pink dress",
        "stepsister_2": "Ella's second older stepsister speaking, a friendly older sister in a green dress",
        "fairy_teacher": "the fairy teacher speaking gently with a calm teacher-like gesture",
        "mouse": "a cute mouse in the foreground reacting with tiny expressive paws",
        "prince": "the young prince speaking politely and kindly",
        "clock": "a large friendly clock showing 9 o'clock as the main focus",
    }
    if speaker in focus_by_speaker:
        return focus_by_speaker[speaker]

    image_cfg = (episode or {}).get("image_generation") or {}
    if image_cfg.get("shot_focus") == "speaker_closeup" and speaker != "narrator":
        label = str(profile.get("character_name") or speaker).replace("_", " ")
        return f"a medium close-up of {label}, face and gesture centered, reacting clearly to the dialogue"
    return f"the speaker {speaker} reacting clearly in the scene"


def build_line_visual(
    scene: dict[str, Any],
    line: dict[str, Any],
    line_index: int,
    line_count: int,
    episode: dict[str, Any] | None = None,
) -> str:
    speaker = str(line.get("speaker") or "narrator")
    base_visual = str(scene.get("visual") or "").strip()
    beat = str(scene.get("beat") or "").strip()
    image_cfg = (episode or {}).get("image_generation") or {}
    continuity_rule = str(
        image_cfg.get("continuity_rule")
        or "Preserve the same pastel children's book world and character continuity."
    ).strip()
    character_rule = str(
        image_cfg.get("character_rule")
        or "Character rule: Cinderella/Ella lives with her stepmother and two older stepsisters; do not show a mother, father, dog, pet, extra siblings, or extra children."
    ).strip()
    return (
        f"{base_visual}. "
        f"Shot {line_index} of {line_count} for the beat '{beat}'. "
        f"Focus on {speaker_focus(speaker, episode)}. "
        f"{continuity_rule} "
        f"{character_rule} "
        "No written text in the image."
    )


def build_shots(episode: dict[str, Any]) -> list[dict[str, Any]]:
    shots: list[dict[str, Any]] = []
    for scene_number, scene in enumerate(episode.get("scenes") or [], start=1):
        scene_id = str(scene.get("id") or f"scene_{scene_number:02d}")
        if scene.get("type") == "title":
            lines = scene.get("lines") or []
            text = str((lines[0] or {}).get("text") if lines else scene.get("title") or episode.get("title") or "").strip()
            title_shot = dict(scene)
            title_shot.update(
                {
                    "id": scene_id,
                    "parent_scene_id": scene_id,
                    "shot_number": len(shots) + 1,
                    "scene_number": scene_number,
                    "line_number": 1,
                    "speaker": str((lines[0] or {}).get("speaker") if lines else "narrator"),
                    "text": text,
                }
            )
            shots.append(title_shot)
            continue

        explicit_shots = scene.get("shots")
        if isinstance(explicit_shots, list):
            for line_number, raw_shot in enumerate(explicit_shots, start=1):
                shot = dict(raw_shot)
                shot.setdefault("speaker", "narrator")
                shot.setdefault("text", "")
                shot.setdefault("visual", scene.get("visual", ""))
                shot.update(
                    {
                        "id": str(shot.get("id") or f"{scene_id}_shot_{line_number:02d}"),
                        "parent_scene_id": scene_id,
                        "beat": scene.get("beat"),
                        "shot_number": len(shots) + 1,
                        "scene_number": scene_number,
                        "line_number": line_number,
                    }
                )
                shots.append(shot)
            continue

        lines = scene.get("lines")
        if not isinstance(lines, list):
            text = str(scene.get("narration", "")).strip()
            lines = [{"speaker": "narrator", "text": text}] if text else []
        for line_number, line in enumerate(lines, start=1):
            text = str(line.get("text") or "").strip()
            if not text:
                raise RuntimeError(f"Scene {scene_id} line {line_number} has empty text.")
            shot = {
                "id": f"{scene_id}_shot_{line_number:02d}",
                "parent_scene_id": scene_id,
                "type": "dialogue",
                "beat": scene.get("beat"),
                "speaker": str(line.get("speaker") or "narrator"),
                "text": text,
                "visual": str(line.get("visual") or build_line_visual(scene, line, line_number, len(lines), episode)),
                "shot_number": len(shots) + 1,
                "scene_number": scene_number,
                "line_number": line_number,
            }
            if "sfx" in line:
                shot["sfx"] = line["sfx"]
            shots.append(shot)
    return shots


def render_placeholder(path: Path, *, episode: dict[str, Any], scene: dict[str, Any], index: int) -> None:
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), (255, 246, 237))
    draw = ImageDraw.Draw(img)

    palette = [
        (255, 221, 225),
        (232, 244, 255),
        (234, 247, 224),
        (255, 241, 203),
        (239, 230, 255),
        (225, 247, 242),
    ]
    bg = palette[(index - 1) % len(palette)]
    draw.rectangle((0, 0, CANVAS_W, CANVAS_H), fill=bg)
    for y in range(0, CANVAS_H, 18):
        shade = tuple(max(0, min(255, c - (y // 18) % 8)) for c in bg)
        draw.line((0, y, CANVAS_W, y), fill=shade)

    # Simple storybook shapes for fallback frames. Text is added later as an overlay.
    center_x = CANVAS_W // 2
    ground_y = 1260
    draw.ellipse((130, ground_y - 30, 950, ground_y + 82), fill=(255, 255, 252), outline=(225, 198, 188), width=5)

    # Ella.
    draw.ellipse((center_x - 128, 480, center_x + 128, 736), fill=(255, 224, 194), outline=(74, 51, 42), width=5)
    draw.arc((center_x - 112, 570, center_x - 22, 650), 0, 180, fill=(74, 51, 42), width=4)
    draw.arc((center_x + 22, 570, center_x + 112, 650), 0, 180, fill=(74, 51, 42), width=4)
    draw.arc((center_x - 54, 636, center_x + 54, 706), 15, 165, fill=(74, 51, 42), width=5)
    draw.polygon([(center_x, 775), (center_x - 215, 1180), (center_x + 215, 1180)], fill=(153, 190, 230), outline=(74, 51, 42))
    draw.line((center_x - 118, 895, center_x - 270, 1080), fill=(74, 51, 42), width=8)
    draw.line((center_x + 118, 895, center_x + 270, 1080), fill=(74, 51, 42), width=8)

    accent = (166, 105, 91)
    if index == 1:
        draw.rounded_rectangle((120, 350, 380, 610), radius=26, fill=(255, 255, 252), outline=accent, width=6)
        for i in range(4):
            y = 395 + i * 48
            draw.line((160, y, 340, y), fill=accent, width=5)
            draw.ellipse((138, y - 8, 154, y + 8), fill=(153, 190, 230))
    elif index == 2:
        draw.ellipse((155, 960, 295, 1100), fill=(255, 185, 92), outline=accent, width=6)
        draw.ellipse((780, 990, 840, 1050), fill=(180, 154, 129), outline=accent, width=4)
        draw.ellipse((860, 990, 920, 1050), fill=(180, 154, 129), outline=accent, width=4)
    elif index == 3:
        draw.rounded_rectangle((260, 1150, 470, 1245), radius=40, fill=(255, 118, 136), outline=accent, width=6)
        draw.rounded_rectangle((610, 1150, 820, 1245), radius=40, fill=(255, 118, 136), outline=accent, width=6)
    elif index == 4:
        for x, y, color in [(220, 1010, (255, 221, 92)), (300, 930, (143, 207, 180)), (795, 1010, (255, 221, 92)), (710, 930, (143, 207, 180))]:
            draw.rounded_rectangle((x, y, x + 95, y + 95), radius=16, fill=color, outline=accent, width=5)
    elif index == 5:
        draw.ellipse((720, 360, 980, 620), fill=(255, 255, 252), outline=accent, width=8)
        draw.line((850, 490, 850, 405), fill=accent, width=7)
        draw.line((850, 490, 925, 520), fill=accent, width=7)
    else:
        draw.rounded_rectangle((690, 880, 930, 990), radius=18, fill=(255, 255, 252), outline=accent, width=6)
        draw.polygon([(250, 1180), (520, 980), (790, 1180)], fill=(255, 221, 92), outline=accent)

    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, quality=95)


def render_title_card(path: Path, *, scene: dict[str, Any]) -> None:
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), (255, 241, 232))
    draw = ImageDraw.Draw(img)
    for y in range(0, CANVAS_H, 10):
        shade = int(246 - (y / CANVAS_H) * 8)
        draw.rectangle((0, y, CANVAS_W, y + 9), fill=(255, shade, shade - 10))

    ink = (58, 43, 39)
    accent = (168, 96, 86)
    pale = (255, 255, 250)
    font_series = pick_font(FONT_BOLD_CANDIDATES, 56)
    font_title = pick_font(FONT_BOLD_CANDIDATES, 126)
    font_subtitle = pick_font(FONT_BOLD_CANDIDATES, 66)
    font_small = pick_font(FONT_REGULAR_CANDIDATES, 32)

    draw.rounded_rectangle((92, 178, CANVAS_W - 92, CANVAS_H - 178), radius=44, fill=pale, outline=accent, width=8)
    draw.rounded_rectangle((132, 224, CANVAS_W - 132, CANVAS_H - 224), radius=34, outline=(236, 195, 181), width=3)

    # Decorative storybook icons.
    draw.ellipse((160, 430, 330, 600), fill=(255, 221, 92), outline=accent, width=5)
    draw.polygon([(845, 430), (930, 590), (760, 590)], fill=(153, 190, 230), outline=accent)
    draw.rounded_rectangle((166, 1285, 340, 1385), radius=38, fill=(153, 190, 230), outline=accent, width=5)
    draw.rounded_rectangle((740, 1285, 914, 1385), radius=38, fill=(255, 118, 136), outline=accent, width=5)

    lines = scene.get("title_lines") or []
    series = str(lines[0] if len(lines) > 0 else "바른 생활 대안 동화")
    title = str(lines[1] if len(lines) > 1 else "신데렐라")
    subtitle = str(lines[2] if len(lines) > 2 else "")

    series_lines = wrap_text(series, draw, font_series, CANVAS_W - 270)
    y = 650
    for line in series_lines[:2]:
        line_w = text_width(draw, line, font_series)
        draw.text(((CANVAS_W - line_w) // 2, y), line, font=font_series, fill=accent)
        y += 72

    title_lines = wrap_text(title, draw, font_title, CANVAS_W - 230)
    y = 810
    for line in title_lines[:2]:
        line_w = text_width(draw, line, font_title)
        draw.text(((CANVAS_W - line_w) // 2, y), line, font=font_title, fill=ink)
        y += 145

    subtitle_lines = wrap_text(subtitle, draw, font_subtitle, CANVAS_W - 240)
    y += 35
    for line in subtitle_lines[:3]:
        line_w = text_width(draw, line, font_subtitle)
        draw.text(((CANVAS_W - line_w) // 2, y), line, font=font_subtitle, fill=ink)
        y += 82

    footer = "mock-educational fairy tale satire"
    footer_w = text_width(draw, footer, font_small)
    draw.text(((CANVAS_W - footer_w) // 2, CANVAS_H - 330), footer, font=font_small, fill=(119, 91, 84))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, quality=95)


def render_title_image_cover(path: Path, *, scene: dict[str, Any], image_path: Path) -> None:
    img = fit_cover(Image.open(image_path)).convert("RGBA")
    overlay = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    for i in range(620):
        y = CANVAS_H - 620 + i
        alpha = int(18 + (i / 620) * 162)
        draw.line((0, y, CANVAS_W, y), fill=(38, 28, 24, alpha))

    lines = scene.get("title_lines") or []
    series = str(lines[0] if len(lines) > 0 else "바른 생활 대안 동화")
    title = str(lines[1] if len(lines) > 1 else "신데렐라")
    subtitle = str(lines[2] if len(lines) > 2 else "")

    font_series = pick_font(FONT_BOLD_CANDIDATES, 46)
    font_title = pick_font(FONT_BOLD_CANDIDATES, 126)
    font_subtitle = pick_font(FONT_BOLD_CANDIDATES, 58)
    y = CANVAS_H - 520
    for text, font, step in ((series, font_series, 64), (title, font_title, 142), (subtitle, font_subtitle, 74)):
        for line in wrap_text(text, draw, font, CANVAS_W - 150)[:2]:
            line_w = text_width(draw, line, font)
            x = (CANVAS_W - line_w) // 2
            draw.text((x + 3, y + 3), line, font=font, fill=(44, 30, 26, 190))
            draw.text((x, y), line, font=font, fill=(255, 252, 244, 245))
            y += step
        y += 8

    composed = Image.alpha_composite(img, overlay).convert("RGB")
    path.parent.mkdir(parents=True, exist_ok=True)
    composed.save(path, quality=95)


def render_slide(
    out_path: Path,
    *,
    episode: dict[str, Any],
    scene: dict[str, Any],
    episode_dir: Path,
    index: int,
    total: int,
    allow_placeholder: bool = False,
) -> None:
    if scene.get("type") == "title":
        existing = frame_image_path(episode_dir, scene)
        if existing:
            render_title_image_cover(out_path, scene=scene, image_path=existing)
            return
        render_title_card(out_path, scene=scene)
        return

    existing = frame_image_path(episode_dir, scene)
    if existing:
        img = fit_cover(Image.open(existing))
    elif not allow_placeholder:
        expected = episode_dir / "images" / f"{scene.get('id')}.png"
        raise RuntimeError(
            f"Missing generated shot image for {scene.get('id')}. "
            f"Create {expected} or rerun with --allow-placeholders for a rough pipeline test."
        )
    else:
        render_placeholder(out_path, episode=episode, scene=scene, index=index)
        img = Image.open(out_path).convert("RGB")

    if not episode.get("show_overlay", True):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path, quality=95)
        return

    overlay = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font_caption = pick_font(FONT_BOLD_CANDIDATES, 78)
    font_meta = pick_font(FONT_BOLD_CANDIDATES, 30)
    font_small = pick_font(FONT_REGULAR_CANDIDATES, 28)

    draw.rounded_rectangle((60, 64, CANVAS_W - 60, 132), radius=18, fill=(255, 255, 252, 222))
    draw.text((90, 83), str(episode.get("series_title", "대안 동화")), font=font_meta, fill=(55, 42, 38, 255))
    draw.text((CANVAS_W - 170, 83), f"{index}/{total}", font=font_meta, fill=(55, 42, 38, 255))

    caption = str(scene.get("caption", "")).strip()
    if caption:
        lines = wrap_text(caption, draw, font_caption, CANVAS_W - 160)
        panel_h = 132 if len(lines) <= 1 else 228
        panel_top = CANVAS_H - panel_h - 112
        draw.rounded_rectangle((60, panel_top, CANVAS_W - 60, panel_top + panel_h), radius=30, fill=(35, 31, 30, 226))
        y = panel_top + 28
        for line in lines[:2]:
            line_w = text_width(draw, line, font_caption)
            draw.text(((CANVAS_W - line_w) // 2, y), line, font=font_caption, fill=(255, 255, 252, 255))
            y += 90
    draw.text((82, CANVAS_H - 72), str(episode.get("title", ""))[:42], font=font_small, fill=(255, 255, 252, 210))

    composed = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    composed.save(out_path, quality=95)


def concat_audio_parts(part_paths: list[Path], out_path: Path, *, silence_seconds: float = 0.12) -> None:
    if not part_paths:
        raise RuntimeError("No audio parts to concatenate.")
    inputs: list[str] = []
    filter_parts: list[str] = []
    concat_labels: list[str] = []
    input_index = 0
    for index, part_path in enumerate(part_paths):
        inputs.extend(["-i", str(part_path)])
        label = f"a{index}"
        filter_parts.append(f"[{input_index}:a]aresample=44100,aformat=channel_layouts=stereo[{label}]")
        concat_labels.append(f"[{label}]")
        input_index += 1
        if index != len(part_paths) - 1:
            silence_label = f"s{index}"
            inputs.extend(["-f", "lavfi", "-t", f"{silence_seconds:.3f}", "-i", "anullsrc=r=44100:cl=stereo"])
            filter_parts.append(f"[{input_index}:a]aformat=sample_rates=44100:channel_layouts=stereo[{silence_label}]")
            concat_labels.append(f"[{silence_label}]")
            input_index += 1
    filter_complex = ";".join(filter_parts) + ";" + "".join(concat_labels) + f"concat=n={len(concat_labels)}:v=0:a=1[a]"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            *inputs,
            "-filter_complex",
            filter_complex,
            "-map",
            "[a]",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "44100",
            "-ac",
            "2",
            str(out_path),
        ],
        check=True,
    )


def write_mono_wav(path: Path, samples: list[float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        frames = bytearray()
        for sample in samples:
            clipped = max(-1.0, min(1.0, sample))
            frames.extend(struct.pack("<h", int(clipped * 32767)))
        wav.writeframes(bytes(frames))


def render_music_box_bgm(path: Path, *, duration: float, volume: float, start_delay: float = 0.0, fade_in: float = 0.0) -> None:
    total = max(duration, 1.0)
    total_samples = int(total * SAMPLE_RATE)
    beat = 0.42
    notes = [523.25, 659.25, 783.99, 659.25, 587.33, 698.46, 783.99, 659.25]
    samples: list[float] = []
    for i in range(total_samples):
        t = i / SAMPLE_RATE
        if t < start_delay:
            samples.append(0.0)
            continue
        bgm_t = t - start_delay
        note_index = int(bgm_t / beat) % len(notes)
        local = bgm_t % beat
        if local > beat * 0.78:
            samples.append(0.0)
            continue
        freq = notes[note_index]
        attack = min(1.0, local / 0.012)
        decay = math.exp(-local * 5.2)
        fade = min(1.0, bgm_t / fade_in) if fade_in > 0 else 1.0
        tone = math.sin(2 * math.pi * freq * local) + 0.38 * math.sin(2 * math.pi * freq * 2 * local)
        samples.append(volume * fade * attack * decay * tone)
    write_mono_wav(path, samples)


def render_sfx(path: Path, *, preset: str, volume: float) -> None:
    preset = preset.lower().strip()
    duration_by_preset = {
        "chime": 0.72,
        "sparkle": 0.82,
        "clock": 1.35,
        "squeak": 0.34,
        "velcro": 0.42,
        "drop": 0.36,
        "pop": 0.22,
        "whoosh": 0.58,
        "footsteps": 0.92,
        "soft_footsteps": 1.25,
        "rustle": 0.72,
        "snore": 1.2,
        "gulp": 0.46,
        "applause": 1.4,
        "whistle": 0.42,
        "flag_snap": 0.28,
    }
    duration = duration_by_preset.get(preset, 0.45)
    total_samples = int(duration * SAMPLE_RATE)
    rng = random.Random(f"{preset}:classic-fairy-tale")
    samples: list[float] = []
    for i in range(total_samples):
        t = i / SAMPLE_RATE
        sample = 0.0
        if preset == "chime":
            for start, freq in [(0.00, 880.0), (0.18, 1320.0)]:
                if t >= start:
                    local = t - start
                    sample += math.exp(-local * 6.5) * math.sin(2 * math.pi * freq * local)
        elif preset == "sparkle":
            for start, freq in [(0.00, 1046.5), (0.11, 1318.5), (0.23, 1568.0), (0.37, 2093.0)]:
                if t >= start:
                    local = t - start
                    sample += 0.8 * math.exp(-local * 10.0) * math.sin(2 * math.pi * freq * local)
        elif preset == "clock":
            for start in (0.00, 0.42, 0.84):
                if t >= start:
                    local = t - start
                    sample += math.exp(-local * 5.5) * (
                        math.sin(2 * math.pi * 392.0 * local) + 0.45 * math.sin(2 * math.pi * 784.0 * local)
                    )
        elif preset == "squeak":
            freq = 1750.0 + 900.0 * min(t / max(duration, 0.001), 1.0)
            sample = math.sin(2 * math.pi * freq * t) * math.exp(-t * 5.0)
        elif preset == "velcro":
            env = math.exp(-t * 7.0)
            sample = env * rng.uniform(-1.0, 1.0)
        elif preset == "drop":
            sample = math.exp(-t * 12.0) * (math.sin(2 * math.pi * 120.0 * t) + 0.2 * rng.uniform(-1.0, 1.0))
        elif preset == "whoosh":
            progress = min(t / max(duration, 0.001), 1.0)
            env = math.sin(math.pi * progress) ** 0.8
            tone = math.sin(2 * math.pi * (220.0 + 720.0 * progress) * t)
            sample = env * (0.5 * tone + 0.5 * rng.uniform(-1.0, 1.0))
        elif preset in {"footsteps", "soft_footsteps"}:
            step_starts = [0.04, 0.26, 0.49, 0.73] if preset == "footsteps" else [0.08, 0.38, 0.70, 1.02]
            for start in step_starts:
                if t >= start:
                    local = t - start
                    env = math.exp(-local * (24.0 if preset == "footsteps" else 18.0))
                    thump = math.sin(2 * math.pi * (135.0 if preset == "footsteps" else 95.0) * local)
                    fiber = rng.uniform(-1.0, 1.0) * math.exp(-local * 34.0)
                    sample += env * (0.7 * thump + 0.18 * fiber)
        elif preset == "rustle":
            env = math.exp(-t * 3.3) * (0.55 + 0.45 * math.sin(2 * math.pi * 7.0 * t))
            sample = env * rng.uniform(-1.0, 1.0)
        elif preset == "snore":
            env = math.sin(math.pi * min(t / max(duration, 0.001), 1.0)) ** 0.7
            wobble = 75.0 + 18.0 * math.sin(2 * math.pi * 1.15 * t)
            sample = env * (0.75 * math.sin(2 * math.pi * wobble * t) + 0.12 * math.sin(2 * math.pi * wobble * 2 * t))
        elif preset == "gulp":
            env = math.exp(-t * 9.0)
            bend = 230.0 - 120.0 * min(t / max(duration, 0.001), 1.0)
            sample = env * (math.sin(2 * math.pi * bend * t) + 0.16 * rng.uniform(-1.0, 1.0))
        elif preset == "applause":
            for start in [0.02, 0.18, 0.31, 0.47, 0.62, 0.78, 0.96, 1.15]:
                if t >= start:
                    local = t - start
                    env = math.exp(-local * 18.0)
                    sample += env * rng.uniform(-1.0, 1.0)
        elif preset == "whistle":
            freq = 1760.0 + 70.0 * math.sin(2 * math.pi * 8.0 * t)
            env = min(1.0, t / 0.035) * min(1.0, (duration - t) / 0.08)
            sample = env * math.sin(2 * math.pi * freq * t)
        elif preset == "flag_snap":
            env = math.exp(-t * 18.0)
            sample = env * (0.55 * rng.uniform(-1.0, 1.0) + 0.45 * math.sin(2 * math.pi * 520.0 * t))
        else:
            sample = math.exp(-t * 16.0) * math.sin(2 * math.pi * 660.0 * t)
        samples.append(volume * sample)
    write_mono_wav(path, samples)


def infer_sfx_specs(episode: dict[str, Any], shot: dict[str, Any]) -> list[dict[str, Any]]:
    audio_cfg = episode.get("audio") or {}
    if not audio_cfg.get("auto_sfx", False):
        return []
    text = str(shot.get("text") or "")
    beat = str(shot.get("beat") or "")
    speaker = str(shot.get("speaker") or "")
    haystack = f"{text} {beat}"
    specs: list[dict[str, Any]] = []
    if shot.get("type") == "title":
        return []
    elif speaker == "clock" or "땡" in text or "아홉 시" in text:
        specs.append({"preset": "clock", "at": 0.0, "volume": 0.34})
    elif "찍찍" in text or speaker == "mouse":
        specs.append({"preset": "squeak", "at": 0.02, "volume": 0.24})
    elif "운동화" in haystack or "신고" in text or "벗" in text:
        specs.append({"preset": "velcro", "at": 0.05, "volume": 0.16})
    elif "떨어" in text:
        specs.append({"preset": "drop", "at": 0.05, "volume": 0.25})
    return specs


def shot_sfx_specs(episode: dict[str, Any], shot: dict[str, Any]) -> list[dict[str, Any]]:
    raw_specs = shot.get("sfx")
    if raw_specs is None:
        raw_specs = infer_sfx_specs(episode, shot)
    if isinstance(raw_specs, str):
        raw_specs = [{"preset": raw_specs}]
    if isinstance(raw_specs, dict):
        raw_specs = [raw_specs]
    if not isinstance(raw_specs, list):
        return []
    specs: list[dict[str, Any]] = []
    default_volume = safe_float((episode.get("audio") or {}).get("sfx_volume"), 0.25)
    for raw in raw_specs:
        if isinstance(raw, str):
            raw = {"preset": raw}
        if not isinstance(raw, dict):
            continue
        preset = str(raw.get("preset") or raw.get("name") or "").strip()
        if not preset:
            continue
        specs.append(
            {
                "preset": preset,
                "at": max(0.0, safe_float(raw.get("at"), 0.0)),
                "volume": max(0.0, safe_float(raw.get("volume"), default_volume)),
                "prompt": str(raw.get("prompt") or "").strip(),
                "duration_seconds": max(0.0, safe_float(raw.get("duration_seconds"), 0.0)),
                "prompt_influence": max(0.0, min(1.0, safe_float(raw.get("prompt_influence"), 0.45))),
                "loop": bool(raw.get("loop", False)),
                "model_id": str(raw.get("model_id") or "").strip(),
            }
        )
    return specs


def sfx_provider(episode: dict[str, Any]) -> str:
    audio_cfg = episode.get("audio") or {}
    return str(audio_cfg.get("sfx_provider") or "synth").strip().lower()


def generate_elevenlabs_sfx(episode: dict[str, Any], spec: dict[str, Any], path: Path, out_dir: Path) -> None:
    prompt = str(spec.get("prompt") or "").strip()
    if not prompt:
        raise RuntimeError(f"ElevenLabs SFX preset '{spec.get('preset')}' requires a prompt.")

    audio_cfg = episode.get("audio") or {}
    model_id = str(
        spec.get("model_id") or audio_cfg.get("elevenlabs_sfx_model") or "eleven_text_to_sound_v2"
    ).strip()
    output_format = str(audio_cfg.get("elevenlabs_sfx_output_format") or "mp3_44100_128").strip()
    payload: dict[str, Any] = {
        "text": prompt,
        "model_id": model_id,
        "loop": bool(spec.get("loop", False)),
        "prompt_influence": max(0.0, min(1.0, safe_float(spec.get("prompt_influence"), 0.45))),
    }
    duration_seconds = safe_float(spec.get("duration_seconds"), 0.0)
    if duration_seconds > 0:
        payload["duration_seconds"] = max(0.5, min(30.0, duration_seconds))

    digest = input_hash({"kind": "elevenlabs_sfx", "payload": payload, "output_format": output_format})
    if cached_output_is_fresh(out_dir, path, digest):
        return

    load_project_env()
    api_key = os.environ.get("ELEVENLABS_API_KEY") or os.environ.get("ELEVEN_LABS_API_KEY")
    if not api_key:
        raise RuntimeError(
            "audio.sfx_provider is 'elevenlabs', but ELEVENLABS_API_KEY is not set. "
            "Put it in classic-fairy-tale-pc-shorts/.env or export it before rendering."
        )

    query = urllib.parse.urlencode({"output_format": output_format})
    request = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/sound-generation?{query}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            path.write_bytes(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ElevenLabs SFX failed for preset '{spec.get('preset')}': {detail}") from exc
    record_cache_entry(out_dir, path, digest)


def mix_shot_sfx(episode: dict[str, Any], shot: dict[str, Any], audio_path: Path, out_dir: Path, index: int) -> Path:
    specs = shot_sfx_specs(episode, shot)
    if not specs:
        return audio_path
    sfx_dir = out_dir / "sfx"
    sfx_dir.mkdir(parents=True, exist_ok=True)
    inputs = ["-i", str(audio_path)]
    filter_parts = ["[0:a]aresample=44100,aformat=channel_layouts=stereo[a0]"]
    labels = ["[a0]"]
    provider = sfx_provider(episode)
    for spec_index, spec in enumerate(specs, start=1):
        extension = ".mp3" if provider == "elevenlabs" else ".wav"
        sfx_path = sfx_dir / f"{index:03d}_{shot['id']}_{spec_index}_{spec['preset']}{extension}"
        if provider == "elevenlabs":
            generate_elevenlabs_sfx(episode, spec, sfx_path, out_dir)
        elif provider == "synth":
            render_sfx(sfx_path, preset=str(spec["preset"]), volume=1.0)
        else:
            raise RuntimeError(f"Unsupported audio.sfx_provider: {provider}")
        inputs.extend(["-i", str(sfx_path)])
        delay_ms = int(safe_float(spec.get("at"), 0.0) * 1000)
        volume = safe_float(spec.get("volume"), 0.25)
        label = f"s{spec_index}"
        filter_parts.append(
            f"[{spec_index}:a]aresample=44100,aformat=channel_layouts=stereo,"
            f"volume={volume:.4f},adelay={delay_ms}|{delay_ms}[{label}]"
        )
        labels.append(f"[{label}]")
    output_path = out_dir / f"audio_{index:03d}_{shot['id']}_mixed.m4a"
    filter_complex = ";".join(filter_parts) + ";" + "".join(labels) + f"amix=inputs={len(labels)}:duration=longest:normalize=0[a]"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            *inputs,
            "-filter_complex",
            filter_complex,
            "-map",
            "[a]",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "44100",
            "-ac",
            "2",
            str(output_path),
        ],
        check=True,
    )
    return output_path


def speaker_profile(episode: dict[str, Any], speaker: str) -> dict[str, str]:
    profiles = episode.get("voice_profiles") or {}
    default_profile = {
        "voice": episode.get("voice") or "ko-KR-SunHiNeural",
        "openai_voice": episode.get("openai_voice") or "marin",
        "elevenlabs_voice_id": episode.get("elevenlabs_voice_id") or "Xb7hH8MSUJpSbSDYk0k2",
        "elevenlabs_voice_name": episode.get("elevenlabs_voice_name") or "Alice",
        "elevenlabs_tags": "",
        "instructions": "",
        "stability": 0.45,
        "similarity_boost": 0.78,
        "style": 0.35,
        "use_speaker_boost": True,
        "rate": episode.get("voice_rate") or "+0%",
        "pitch": episode.get("voice_pitch") or "+0Hz",
    }
    profile = profiles.get(speaker) or profiles.get("narrator") or default_profile
    return {
        "voice": str(profile.get("voice") or default_profile["voice"]),
        "openai_voice": str(profile.get("openai_voice") or default_profile["openai_voice"]),
        "elevenlabs_voice_id": str(profile.get("elevenlabs_voice_id") or default_profile["elevenlabs_voice_id"]),
        "elevenlabs_voice_name": str(profile.get("elevenlabs_voice_name") or default_profile["elevenlabs_voice_name"]),
        "elevenlabs_tags": str(profile.get("elevenlabs_tags") or default_profile["elevenlabs_tags"]),
        "instructions": str(profile.get("instructions") or default_profile["instructions"]),
        "stability": safe_float(profile.get("stability"), safe_float(default_profile["stability"], 0.45)),
        "similarity_boost": safe_float(
            profile.get("similarity_boost"),
            safe_float(default_profile["similarity_boost"], 0.78),
        ),
        "style": safe_float(profile.get("style"), safe_float(default_profile["style"], 0.35)),
        "use_speaker_boost": bool(profile.get("use_speaker_boost", default_profile["use_speaker_boost"])),
        "rate": str(profile.get("rate") or default_profile["rate"]),
        "pitch": str(profile.get("pitch") or default_profile["pitch"]),
    }


def tts_provider(episode: dict[str, Any]) -> str:
    audio_cfg = episode.get("audio") or {}
    return str(audio_cfg.get("tts_provider") or episode.get("tts_provider") or "edge").strip().lower()


async def generate_edge_tts(episode: dict[str, Any], shots: list[dict[str, Any]], out_dir: Path) -> list[Path]:
    import edge_tts

    paths: list[Path] = []
    for idx, shot in enumerate(shots, start=1):
        speaker = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(shot.get("speaker") or "narrator")).strip("_") or "narrator"
        text = str(shot.get("text") or "").strip()
        if not text:
            raise RuntimeError(f"Shot {shot.get('id')} has empty text.")
        profile = speaker_profile(episode, speaker)
        path = out_dir / f"audio_{idx:03d}_{shot['id']}_{speaker}.mp3"
        communicate = edge_tts.Communicate(
            text,
            voice=profile["voice"],
            rate=profile["rate"],
            volume="+0%",
            pitch=profile["pitch"],
        )
        await communicate.save(str(path))
        paths.append(path)
    return paths


def generate_openai_tts(episode: dict[str, Any], shots: list[dict[str, Any]], out_dir: Path) -> list[Path]:
    load_project_env()
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError(
            "audio.tts_provider is 'openai', but OPENAI_API_KEY is not set. "
            "Put it in classic-fairy-tale-pc-shorts/.env or export it before rendering."
        )

    from openai import OpenAI

    audio_cfg = episode.get("audio") or {}
    model = str(audio_cfg.get("tts_model") or "gpt-4o-mini-tts")
    response_format = str(audio_cfg.get("tts_response_format") or "mp3")
    extension_by_format = {
        "mp3": "mp3",
        "opus": "opus",
        "aac": "aac",
        "flac": "flac",
        "wav": "wav",
        "pcm": "pcm",
    }
    extension = extension_by_format.get(response_format, "mp3")
    client = OpenAI()
    paths: list[Path] = []

    for idx, shot in enumerate(shots, start=1):
        speaker = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(shot.get("speaker") or "narrator")).strip("_") or "narrator"
        text = str(shot.get("text") or "").strip()
        if not text:
            raise RuntimeError(f"Shot {shot.get('id')} has empty text.")
        profile = speaker_profile(episode, speaker)
        path = out_dir / f"audio_{idx:03d}_{shot['id']}_{speaker}.{extension}"
        kwargs: dict[str, Any] = {
            "model": model,
            "voice": profile["openai_voice"],
            "input": text,
            "response_format": response_format,
        }
        if profile["instructions"]:
            kwargs["instructions"] = profile["instructions"]
        with client.audio.speech.with_streaming_response.create(**kwargs) as response:
            response.stream_to_file(path)
        paths.append(path)
    return paths


def generate_elevenlabs_tts(episode: dict[str, Any], shots: list[dict[str, Any]], out_dir: Path) -> list[Path]:
    load_project_env()
    api_key = os.environ.get("ELEVENLABS_API_KEY") or os.environ.get("ELEVEN_LABS_API_KEY")
    if not api_key:
        raise RuntimeError(
            "audio.tts_provider is 'elevenlabs', but ELEVENLABS_API_KEY is not set. "
            "Put it in classic-fairy-tale-pc-shorts/.env or export it before rendering."
        )

    audio_cfg = episode.get("audio") or {}
    model = str(audio_cfg.get("elevenlabs_model") or audio_cfg.get("tts_model") or "eleven_multilingual_v2")
    output_format = str(audio_cfg.get("elevenlabs_output_format") or "mp3_44100_128")
    endpoint_base = "https://api.elevenlabs.io/v1/text-to-speech"
    paths: list[Path] = []

    for idx, shot in enumerate(shots, start=1):
        speaker = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(shot.get("speaker") or "narrator")).strip("_") or "narrator"
        text = str(shot.get("text") or "").strip()
        if not text:
            raise RuntimeError(f"Shot {shot.get('id')} has empty text.")
        profile = speaker_profile(episode, speaker)
        elevenlabs_tags = str(profile.get("elevenlabs_tags") or "").strip()
        directed_text = f"{elevenlabs_tags} {text}".strip() if elevenlabs_tags else text
        path = out_dir / f"audio_{idx:03d}_{shot['id']}_{speaker}.mp3"
        payload = {
            "text": directed_text,
            "model_id": model,
            "voice_settings": {
                "stability": max(0.0, min(1.0, safe_float(profile["stability"], 0.45))),
                "similarity_boost": max(0.0, min(1.0, safe_float(profile["similarity_boost"], 0.78))),
                "style": max(0.0, min(1.0, safe_float(profile["style"], 0.35))),
                "use_speaker_boost": bool(profile["use_speaker_boost"]),
            },
        }
        digest = input_hash(
            {
                "kind": "elevenlabs_tts",
                "voice_id": profile["elevenlabs_voice_id"],
                "payload": payload,
                "output_format": output_format,
            }
        )
        if cached_output_is_fresh(out_dir, path, digest):
            paths.append(path)
            continue
        request = urllib.request.Request(
            f"{endpoint_base}/{profile['elevenlabs_voice_id']}?output_format={output_format}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                path.write_bytes(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"ElevenLabs TTS failed for {shot.get('id')} ({speaker}): {detail}") from exc
        record_cache_entry(out_dir, path, digest)
        paths.append(path)
    return paths


def generate_tts(episode: dict[str, Any], shots: list[dict[str, Any]], out_dir: Path) -> list[Path]:
    provider = tts_provider(episode)
    if provider == "edge":
        return asyncio.run(generate_edge_tts(episode, shots, out_dir))
    if provider == "openai":
        return generate_openai_tts(episode, shots, out_dir)
    if provider == "elevenlabs":
        return generate_elevenlabs_tts(episode, shots, out_dir)
    raise RuntimeError(f"Unsupported audio.tts_provider: {provider}")


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
    return max(float(result.stdout.strip()), 1.0)


def make_segment_video(image_path: Path, audio_path: Path, out_path: Path) -> None:
    duration = ffprobe_duration(audio_path)
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-loop",
            "1",
            "-i",
            str(image_path),
            "-i",
            str(audio_path),
            "-t",
            f"{duration + 0.25:.3f}",
            "-vf",
            (
                f"scale={CANVAS_W}:{CANVAS_H},"
                "zoompan=z='min(zoom+0.00045,1.035)':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                f"s={CANVAS_W}x{CANVAS_H}:fps={FPS},format=yuv420p"
            ),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            str(out_path),
        ],
        check=True,
    )


def concat_videos(segment_paths: list[Path], out_path: Path) -> None:
    if not segment_paths:
        raise RuntimeError("No video segments to concatenate.")
    inputs: list[str] = []
    filter_parts: list[str] = []
    concat_labels: list[str] = []
    for idx, path in enumerate(segment_paths):
        inputs.extend(["-i", str(path)])
        filter_parts.append(f"[{idx}:v]setsar=1,fps={FPS},format=yuv420p[v{idx}]")
        filter_parts.append(f"[{idx}:a]aresample=44100,aformat=sample_rates=44100:channel_layouts=stereo[a{idx}]")
        concat_labels.append(f"[v{idx}][a{idx}]")
    filter_complex = (
        ";".join(filter_parts)
        + ";"
        + "".join(concat_labels)
        + f"concat=n={len(segment_paths)}:v=1:a=1[v][a]"
    )
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            *inputs,
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(out_path),
        ],
        check=True,
    )


def bgm_config(episode: dict[str, Any]) -> dict[str, Any] | None:
    audio_cfg = episode.get("audio") or {}
    bgm = audio_cfg.get("bgm") or {}
    if not isinstance(bgm, dict) or not bgm.get("enabled", False):
        return None
    return bgm


def mix_background_music(video_path: Path, episode: dict[str, Any], out_path: Path) -> Path:
    bgm = bgm_config(episode)
    if not bgm:
        return video_path
    duration = ffprobe_duration(video_path)
    preset = str(bgm.get("preset") or "music_box")
    volume = max(0.0, safe_float(bgm.get("volume"), 0.055))
    start_delay = max(0.0, safe_float(bgm.get("start_delay"), 0.0))
    fade_in = max(0.0, safe_float(bgm.get("fade_in"), 0.0))
    bgm_path = out_path.parent / f"{video_path.stem}-{preset}-bgm.wav"
    if preset != "music_box":
        preset = "music_box"
    render_music_box_bgm(bgm_path, duration=duration + 0.5, volume=volume, start_delay=start_delay, fade_in=fade_in)
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video_path),
            "-i",
            str(bgm_path),
            "-filter_complex",
            "[0:a]volume=1.0[voice];[1:a]volume=1.0[bgm];[voice][bgm]amix=inputs=2:duration=first:normalize=0[a]",
            "-map",
            "0:v:0",
            "-map",
            "[a]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(out_path),
        ],
        check=True,
    )
    return out_path


def build_image_prompts(episode: dict[str, Any], shots: list[dict[str, Any]]) -> list[dict[str, str]]:
    image_cfg = episode.get("image_generation") or {}
    style_prompt = str(image_cfg.get("style_prompt") or episode.get("style_prompt", "")).strip()
    character_bible = str(episode.get("character_bible", "")).strip()
    composition = str(
        image_cfg.get("composition")
        or "9:16 vertical storybook shot, main speaking character or action centered, no caption safe-space requirement"
    ).strip()
    lighting = str(image_cfg.get("lighting") or "bright, gentle, cute preschool animation mood").strip()
    constraints = str(
        image_cfg.get("constraints")
        or "no text, no captions, no speech bubbles, no logos, no watermark, no scary content, no realistic child likeness, no mother, no father, no parents, no dog, no pets, no extra children"
    ).strip()
    prompts: list[dict[str, str]] = []
    for shot in shots:
        if shot.get("type") == "title":
            continue
        visual = str(shot.get("visual", "")).strip()
        prompt = (
            "Use case: illustration-story\n"
            "Asset type: YouTube Shorts dialogue shot image\n"
            f"Primary request: {visual}\n"
            f"Character continuity: {character_bible}\n"
            f"Style/medium: {style_prompt}\n"
            f"Composition/framing: {composition}\n"
            f"Lighting/mood: {lighting}\n"
            f"Constraints: {constraints}\n"
        )
        prompts.append(
            {
                "shot_id": str(shot["id"]),
                "parent_scene_id": str(shot.get("parent_scene_id") or ""),
                "speaker": str(shot.get("speaker") or ""),
                "text": str(shot.get("text") or ""),
                "prompt": prompt,
            }
        )
    return prompts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build one fairy-tale satire Shorts episode.")
    parser.add_argument("--episode", type=Path, required=True, help="Path to episode.json")
    parser.add_argument("--out-dir", type=Path, default=None, help="Output directory. Defaults to episodes/<id>/build")
    parser.add_argument("--write-prompts-only", action="store_true", help="Write image prompts and exit.")
    parser.add_argument(
        "--allow-placeholders",
        action="store_true",
        help="Render rough fallback graphics when generated shot images are missing.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    episode_path = args.episode.resolve()
    episode = read_json(episode_path)
    episode_dir = episode_path.parent
    out_dir = args.out_dir or episode_dir / "build"
    out_dir.mkdir(parents=True, exist_ok=True)

    shots = build_shots(episode)
    write_json(out_dir / "shot-manifest.json", shots)

    prompts = build_image_prompts(episode, shots)
    write_json(episode_dir / "image-prompts.json", prompts)
    if args.write_prompts_only:
        print(f"Wrote {episode_dir / 'image-prompts.json'}")
        return 0

    slide_paths: list[Path] = []
    for idx, shot in enumerate(shots, start=1):
        slide_path = out_dir / f"slide_{idx:03d}_{shot['id']}.jpg"
        render_slide(
            slide_path,
            episode=episode,
            scene=shot,
            episode_dir=episode_dir,
            index=idx,
            total=len(shots),
            allow_placeholder=args.allow_placeholders,
        )
        slide_paths.append(slide_path)

    raw_audio_paths = generate_tts(episode, shots, out_dir)
    audio_paths = [
        mix_shot_sfx(episode, shot, audio_path, out_dir, idx)
        for idx, (shot, audio_path) in enumerate(zip(shots, raw_audio_paths, strict=True), start=1)
    ]

    segment_paths: list[Path] = []
    for idx, (slide_path, audio_path) in enumerate(zip(slide_paths, audio_paths, strict=True), start=1):
        segment_path = out_dir / f"segment_{idx:02d}.mp4"
        make_segment_video(slide_path, audio_path, segment_path)
        segment_paths.append(segment_path)

    final_path = out_dir / f"{episode['id']}.mp4"
    voice_only_path = out_dir / f"{episode['id']}-voice-sfx.mp4"
    concat_videos(segment_paths, voice_only_path)
    if bgm_config(episode):
        mix_background_music(voice_only_path, episode, final_path)
    else:
        shutil.copyfile(voice_only_path, final_path)
    write_json(
        out_dir / "manifest.json",
        {
            "episode": str(episode_path),
            "final_video": str(final_path),
            "slides": [str(path) for path in slide_paths],
            "raw_audio": [str(path) for path in raw_audio_paths],
            "audio": [str(path) for path in audio_paths],
            "segments": [str(path) for path in segment_paths],
            "shots": [str(shot.get("id")) for shot in shots],
        },
    )
    print(f"Wrote {final_path}")
    print(f"Wrote {out_dir / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
