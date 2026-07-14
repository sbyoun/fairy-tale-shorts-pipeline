#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SESSION = ROOT / ".youtube_oauth_session.json"
DEFAULT_TOKEN = ROOT / ".youtube_token.json"
DEFAULT_CLIENT_SECRET = ROOT / "client_secret.json"
DEFAULT_VIDEO = ROOT / "episodes/001-cinderella/build-final/001-cinderella.mp4"
SCOPES = [
    "openid",
    "email",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
SHORTS_MAX_SECONDS = 180


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_private(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def client_config(client_secret_path: Path) -> dict[str, Any]:
    data = read_json(client_secret_path)
    if "installed" in data:
        return data["installed"]
    if "web" in data:
        return data["web"]
    raise RuntimeError(f"Unsupported OAuth client JSON: {client_secret_path}")


def build_auth_url(*, client_secret_path: Path, session_path: Path, redirect_uri: str) -> str:
    import secrets

    client = client_config(client_secret_path)
    state = secrets.token_urlsafe(32)
    write_json_private(
        session_path,
        {
            "state": state,
            "redirect_uri": redirect_uri,
            "client_secret_path": str(client_secret_path),
            "scopes": SCOPES,
        },
    )
    params = {
        "client_id": client["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return "https://accounts.google.com/o/oauth2/auth?" + urllib.parse.urlencode(params)


def exchange_callback(callback_url: str, *, session_path: Path, token_path: Path) -> dict[str, Any]:
    session = read_json(session_path)
    parsed = urllib.parse.urlparse(callback_url)
    query = urllib.parse.parse_qs(parsed.query)
    state = (query.get("state") or [""])[0]
    code = (query.get("code") or [""])[0]
    if not code:
        raise RuntimeError("Callback URL does not contain an OAuth code.")
    if state != session.get("state"):
        raise RuntimeError("OAuth state mismatch. Generate a fresh auth URL and try again.")

    client_secret_path = Path(session["client_secret_path"])
    client = client_config(client_secret_path)
    body = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": client["client_id"],
            "client_secret": client.get("client_secret", ""),
            "redirect_uri": session["redirect_uri"],
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        client["token_uri"],
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            token_response = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Token exchange failed: {detail}") from exc

    expiry = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=int(token_response.get("expires_in", 3600)))
    token_payload = {
        "token": token_response.get("access_token"),
        "refresh_token": token_response.get("refresh_token"),
        "token_uri": client["token_uri"],
        "client_id": client["client_id"],
        "client_secret": client.get("client_secret", ""),
        "scopes": token_response.get("scope", " ".join(session.get("scopes") or SCOPES)).split(),
        "expiry": expiry.isoformat().replace("+00:00", "Z"),
    }
    if not token_payload["refresh_token"]:
        raise RuntimeError("Google did not return a refresh token. Re-run auth URL generation with prompt=consent.")
    write_json_private(token_path, token_payload)
    return {"ok": True, "token_path": str(token_path), "scopes": token_payload["scopes"]}


def ffprobe_video(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,size",
            "-show_entries",
            "stream=index,codec_type,width,height",
            "-of",
            "json",
            str(path),
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def validate_video_file(path: Path, *, max_seconds: int | None = SHORTS_MAX_SECONDS) -> dict[str, Any]:
    info = ffprobe_video(path)
    duration = float(info.get("format", {}).get("duration") or 0)
    videos = [stream for stream in info.get("streams") or [] if stream.get("codec_type") == "video"]
    if not videos:
        raise RuntimeError("No video stream found.")
    width = int(videos[0].get("width") or 0)
    height = int(videos[0].get("height") or 0)
    if duration <= 0:
        raise RuntimeError("Could not determine video duration.")
    if max_seconds is not None and duration > max_seconds:
        raise RuntimeError(f"Video is {duration:.1f}s; expected <= {max_seconds}s.")
    if height < width:
        raise RuntimeError(f"Video is {width}x{height}; Shorts should be vertical or square.")
    return {"duration": duration, "width": width, "height": height}


def metadata(*, privacy: str, made_for_kids: bool, title: str, description: str, tags: list[str]) -> dict[str, Any]:
    return {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": "1",
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": made_for_kids,
        },
    }


def get_credentials(token_path: Path) -> Credentials:
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        write_json_private(token_path, json.loads(creds.to_json()))
    if not creds.valid:
        raise RuntimeError("Stored YouTube OAuth token is not valid.")
    return creds


def upload_video(
    *,
    video_path: Path,
    token_path: Path,
    privacy: str,
    made_for_kids: bool,
    title: str,
    description: str,
    tags: list[str],
    max_seconds: int | None = SHORTS_MAX_SECONDS,
) -> dict[str, Any]:
    info = validate_video_file(video_path, max_seconds=max_seconds)
    body = metadata(
        privacy=privacy,
        made_for_kids=made_for_kids,
        title=title,
        description=description,
        tags=tags,
    )
    creds = get_credentials(token_path)
    youtube = build("youtube", "v3", credentials=creds)
    media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
        notifySubscribers=False,
    )
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"upload progress: {int(status.progress() * 100)}%", flush=True)
    return {"video_info": info, "youtube_response": response}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload the Cinderella short to YouTube.")
    parser.add_argument("--auth-url", action="store_true", help="Print OAuth authorization URL and save session state.")
    parser.add_argument("--exchange", help="OAuth callback URL to exchange for a token.")
    parser.add_argument("--upload", action="store_true", help="Upload the MP4.")
    parser.add_argument("--file", type=Path, default=DEFAULT_VIDEO)
    parser.add_argument("--token", type=Path, default=DEFAULT_TOKEN)
    parser.add_argument("--session", type=Path, default=DEFAULT_SESSION)
    parser.add_argument("--client-secret", type=Path, default=DEFAULT_CLIENT_SECRET)
    parser.add_argument("--redirect-uri", default="http://localhost:1/")
    parser.add_argument("--privacy", choices=["private", "unlisted", "public"], default="private")
    parser.add_argument("--allow-long-video", action="store_true", help="Do not enforce the Shorts duration limit.")
    parser.add_argument("--made-for-kids", action="store_true", help="Mark the upload as made for kids. Default is not made for kids (channel decision, 2026-07-14).")
    parser.add_argument("--title", default="신데렐라: 찍찍이 운동화와 공룡 그림일기장 #Shorts")
    parser.add_argument(
        "--description",
        default="\n".join(
            [
                "바른 생활 대안 동화 파일럿.",
                "",
                "신데렐라를 공평한 청소 당번표, 찍찍이 운동화, 공룡 그림일기장으로 다시 구성한 짧은 동화입니다.",
                "",
                "#Shorts #신데렐라 #동화 #AI영상",
            ]
        ),
    )
    parser.add_argument(
        "--tags",
        default="Shorts,신데렐라,동화,AI영상,fairy tale,Cinderella",
        help="Comma-separated YouTube tags.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.auth_url:
        print(build_auth_url(client_secret_path=args.client_secret, session_path=args.session, redirect_uri=args.redirect_uri))
        return 0
    if args.exchange:
        result = exchange_callback(args.exchange, session_path=args.session, token_path=args.token)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.upload:
        result = upload_video(
            video_path=args.file,
            token_path=args.token,
            privacy=args.privacy,
            made_for_kids=args.made_for_kids,
            title=args.title,
            description=args.description,
            tags=[tag.strip() for tag in args.tags.split(",") if tag.strip()],
            max_seconds=None if args.allow_long_video else SHORTS_MAX_SECONDS,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        video_id = (result.get("youtube_response") or {}).get("id")
        if video_id:
            print(f"https://www.youtube.com/watch?v={video_id}")
        return 0
    raise RuntimeError("Choose one of --auth-url, --exchange, or --upload.")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HttpError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
