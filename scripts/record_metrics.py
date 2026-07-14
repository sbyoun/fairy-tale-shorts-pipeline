#!/usr/bin/env python3
"""Append a timestamped snapshot of channel/video stats to docs/metrics-log.jsonl.

Uses the YouTube Data API with the stored OAuth token. Safe to run on a schedule;
each run appends one JSON line per public video plus one channel line.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from upload_youtube_short import get_credentials  # noqa: E402
from googleapiclient.discovery import build  # noqa: E402

LOG_PATH = ROOT / "docs" / "metrics-log.jsonl"


def record_analytics(creds, now: str) -> list[dict]:
    """Finalized per-video metrics and traffic sources for the last 3 days.

    YouTube Analytics data lags 1-3 days behind realtime; this captures
    retention/traffic-source data the Data API cannot provide.
    """
    from datetime import date, timedelta

    ya = build("youtubeAnalytics", "v2", credentials=creds)
    end = date.today()
    start = end - timedelta(days=3)
    lines: list[dict] = []

    per_video = ya.reports().query(
        ids="channel==MINE",
        startDate=start.isoformat(),
        endDate=end.isoformat(),
        metrics="views,engagedViews,estimatedMinutesWatched,averageViewDuration,averageViewPercentage",
        dimensions="video",
        sort="-views",
        maxResults=25,
    ).execute()
    for row in per_video.get("rows", []):
        vid, views, engaged, minutes, avg_dur, avg_pct = row
        lines.append(
            {
                "ts": now,
                "kind": "analytics_video",
                "id": vid,
                "range": f"{start}..{end}",
                "views": views,
                "engaged_views": engaged,
                "minutes_watched": minutes,
                "avg_view_duration_s": avg_dur,
                "avg_view_pct": avg_pct,
            }
        )

    traffic = ya.reports().query(
        ids="channel==MINE",
        startDate=start.isoformat(),
        endDate=end.isoformat(),
        metrics="views,engagedViews",
        dimensions="insightTrafficSourceType",
        maxResults=50,
    ).execute()
    for row in traffic.get("rows", []):
        source, views, engaged = row
        lines.append(
            {
                "ts": now,
                "kind": "analytics_traffic",
                "range": f"{start}..{end}",
                "source": source,
                "views": views,
                "engaged_views": engaged,
            }
        )
    return lines


def main() -> int:
    creds = get_credentials(ROOT / ".youtube_token.json")
    yt = build("youtube", "v3", credentials=creds)
    now = datetime.now(timezone.utc).isoformat()

    ch = yt.channels().list(part="statistics,contentDetails", mine=True).execute()["items"][0]
    uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    items = yt.playlistItems().list(part="contentDetails", playlistId=uploads, maxResults=50).execute()["items"]
    ids = [i["contentDetails"]["videoId"] for i in items]
    vids = yt.videos().list(part="statistics,snippet,status", id=",".join(ids)).execute()["items"]

    lines = [
        {
            "ts": now,
            "kind": "channel",
            "views": int(ch["statistics"].get("viewCount", 0)),
            "subscribers": int(ch["statistics"].get("subscriberCount", 0)),
        }
    ]
    for v in vids:
        if v["status"]["privacyStatus"] != "public":
            continue
        s = v["statistics"]
        lines.append(
            {
                "ts": now,
                "kind": "video",
                "id": v["id"],
                "title": v["snippet"]["title"],
                "published_at": v["snippet"]["publishedAt"],
                "made_for_kids": v["status"].get("madeForKids"),
                "views": int(s.get("viewCount", 0)),
                "likes": int(s.get("likeCount", 0)),
                "comments": int(s.get("commentCount", 0)),
            }
        )

    try:
        lines.extend(record_analytics(creds, now))
    except Exception as exc:  # Analytics lags/permissions must not break the counter log
        lines.append({"ts": now, "kind": "analytics_error", "error": str(exc)[:200]})

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    print(f"recorded {len(lines) - 1} rows at {now}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
