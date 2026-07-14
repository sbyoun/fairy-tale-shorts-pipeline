#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def start(args: argparse.Namespace) -> int:
    if not args.command:
        raise SystemExit("Missing command after --")

    pid_path = args.pid.resolve()
    status_path = args.status.resolve()
    log_path = args.log.resolve()
    cwd = args.cwd.resolve()

    if pid_path.exists() and not args.replace:
        raw_pid = pid_path.read_text(encoding="utf-8").strip()
        if raw_pid.isdigit() and is_running(int(raw_pid)):
            print(f"Already running: pid={raw_pid}")
            return 2

    runner = [
        sys.executable,
        str(Path(__file__).resolve()),
        "run",
        "--name",
        args.name,
        "--cwd",
        str(cwd),
        "--log",
        str(log_path),
        "--status",
        str(status_path),
        "--",
        *args.command,
    ]
    process = subprocess.Popen(
        runner,
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(f"{process.pid}\n", encoding="utf-8")
    write_json(
        status_path,
        {
            "name": args.name,
            "state": "running",
            "pid": process.pid,
            "started_at": utc_now(),
            "cwd": str(cwd),
            "command": args.command,
            "log": str(log_path),
            "pid_file": str(pid_path),
        },
    )
    print(f"Started {args.name}: pid={process.pid}")
    print(f"Log: {log_path}")
    print(f"Status: {status_path}")
    return 0


def run_job(args: argparse.Namespace) -> int:
    status_path = args.status.resolve()
    log_path = args.log.resolve()
    cwd = args.cwd.resolve()
    if not args.command:
        raise SystemExit("Missing command after --")

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab", buffering=0) as log_handle:
        header = (
            f"\n[{utc_now()}] START {args.name}\n"
            f"cwd={cwd}\n"
            f"cmd={shlex.join(args.command)}\n\n"
        )
        log_handle.write(header.encode("utf-8"))
        process = subprocess.Popen(
            args.command,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        return_code = process.wait()
        footer = f"\n[{utc_now()}] END {args.name} return_code={return_code}\n"
        log_handle.write(footer.encode("utf-8"))

    payload = read_json(status_path)
    payload.update(
        {
            "state": "complete" if return_code == 0 else "failed",
            "return_code": return_code,
            "finished_at": utc_now(),
        }
    )
    write_json(status_path, payload)
    return return_code


def status(args: argparse.Namespace) -> int:
    status_path = args.status.resolve()
    payload = read_json(status_path)
    pid = payload.get("pid")
    running = bool(isinstance(pid, int) and is_running(pid))
    if payload:
        recorded_state = str(payload.get("state") or "")
        state = "running" if running else recorded_state or "not_running"
        print(f"{payload.get('name', status_path.name)}: {state}")
        print(f"pid={pid}")
        print(f"log={payload.get('log', '')}")
    else:
        print(f"No status file: {status_path}")
        return 1
    return 0 if running else 3


def stop(args: argparse.Namespace) -> int:
    payload = read_json(args.status.resolve())
    pid = payload.get("pid")
    if not isinstance(pid, int) or not is_running(pid):
        print("No running process.")
        return 0
    try:
        os.killpg(pid, signal.SIGTERM)
        print(f"Sent SIGTERM to process group pid={pid}")
    except ProcessLookupError:
        print("No running process.")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start and track a long-running project command in the background.")
    subparsers = parser.add_subparsers(dest="command_name", required=True)

    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("--name", required=True)
    start_parser.add_argument("--cwd", type=Path, default=ROOT)
    start_parser.add_argument("--log", type=Path, required=True)
    start_parser.add_argument("--pid", type=Path, required=True)
    start_parser.add_argument("--status", type=Path, required=True)
    start_parser.add_argument("--replace", action="store_true", help="Allow replacing a stale pid file.")
    start_parser.add_argument("command", nargs=argparse.REMAINDER)
    start_parser.set_defaults(func=start)

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--status", type=Path, required=True)
    status_parser.set_defaults(func=status)

    stop_parser = subparsers.add_parser("stop")
    stop_parser.add_argument("--status", type=Path, required=True)
    stop_parser.set_defaults(func=stop)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--name", required=True)
    run_parser.add_argument("--cwd", type=Path, default=ROOT)
    run_parser.add_argument("--log", type=Path, required=True)
    run_parser.add_argument("--status", type=Path, required=True)
    run_parser.add_argument("command", nargs=argparse.REMAINDER)
    run_parser.set_defaults(func=run_job)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if getattr(args, "command", None) and args.command[0:1] == ["--"]:
        args.command = args.command[1:]
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
