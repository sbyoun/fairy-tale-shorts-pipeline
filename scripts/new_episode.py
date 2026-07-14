#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EPISODES_DIR = ROOT / "episodes"
TEMPLATE_DIR = EPISODES_DIR / "_template"


def replace_placeholders(text: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a new episode folder from episodes/_template.")
    parser.add_argument("--id", required=True, help="Episode folder id, for example 004-little-red")
    parser.add_argument("--title", required=True)
    parser.add_argument("--series-title", default="바른 생활 대안 동화")
    parser.add_argument("--source-story", default="고전 동화")
    parser.add_argument("--force", action="store_true", help="Overwrite the episode folder if it already exists.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    episode_id = args.id.strip()
    if not episode_id or "/" in episode_id:
        raise RuntimeError("--id must be a folder-safe episode id, for example 004-little-red")

    episode_dir = EPISODES_DIR / episode_id
    if episode_dir.exists() and not args.force:
        raise RuntimeError(f"{episode_dir} already exists. Pass --force to overwrite.")
    if episode_dir.exists():
        shutil.rmtree(episode_dir)

    shutil.copytree(TEMPLATE_DIR, episode_dir)
    values = {
        "EPISODE_ID": episode_id,
        "TITLE": args.title.strip() or "새 에피소드",
        "SERIES_TITLE": args.series_title.strip() or "바른 생활 대안 동화",
        "SOURCE_STORY": args.source_story.strip() or "고전 동화",
    }
    for path in episode_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".md", ".json", ".txt"}:
            path.write_text(replace_placeholders(path.read_text(encoding="utf-8"), values), encoding="utf-8")

    print(episode_dir)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
