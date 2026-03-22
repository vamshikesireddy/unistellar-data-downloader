#!/usr/bin/env python3
"""
Unistellar Telescope Data Downloader
Downloads FITS observation data from the telescope's local WiFi API.

Key discovery: the telescope's backend (evsoft) requires an active event
poller on /api/event BEFORE it will stream data from the zip endpoint.
Without concurrent event polling, the zip request hangs and returns 502.

Prerequisites:
    pip install requests

Usage:
    1. Connect laptop WiFi to your Unistellar telescope
    2. python unistellar_download.py
    3. Select observations and format, let it run
"""

import requests
import time
import argparse
import json
import sys
import threading
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

BASE_URL = "http://192.168.100.1"
API_BASE = f"{BASE_URL}/api"

CHUNK_SIZE = 256 * 1024   # 256 KB chunks
MAX_RETRIES = 10
RETRY_DELAY = 5           # seconds between retries
CONNECT_TIMEOUT = 15
READ_TIMEOUT = 300        # 5 min per chunk read
LIST_TIMEOUT = 120        # 2 min for observation list
EVENT_POLL_TIMEOUT = 30   # event long-poll timeout

ALLOWED_FORMATS = ("fits", "tiff", "png")


def _safe_str(val: object, max_len: int = 50) -> str:
    """Sanitize a value for terminal display — strip non-printable/non-ASCII chars."""
    s = str(val) if val is not None else ""
    s = "".join(c for c in s if c.isprintable() and ord(c) < 128)
    return s[:max_len]


def _safe_int(val: object, default: int = 0) -> int:
    """Safely coerce a value to int."""
    if isinstance(val, (int, float)):
        return int(val)
    return default


class EventPoller:
    """Background thread that long-polls /api/event.
    The telescope backend requires this to be active for downloads to work."""

    def __init__(self):
        self.running = False
        self.thread = None
        self.progress = 0
        self.nb_frames = 0
        self.status = ""
        self.error = None
        self._session = None

    def start(self):
        """Start polling in background thread."""
        self.running = True
        self.progress = 0
        self.nb_frames = 0
        self.status = ""
        self.error = None
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0",
            "Accept": "*/*",
            "Connection": "keep-alive",
        })
        self.thread = threading.Thread(target=self._poll_loop, daemon=True)
        self.thread.start()

    def stop(self):
        """Stop polling."""
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)

    def cancel_download(self):
        """Send cancel command to telescope."""
        try:
            self._session.post(
                f"{API_BASE}/event",
                json={"cmd": "cancelDownload"},
                timeout=(5, 10),
            )
        except Exception:
            pass

    def _poll_loop(self):
        """Continuously long-poll /api/event."""
        while self.running:
            try:
                resp = self._session.get(
                    f"{API_BASE}/event",
                    timeout=(CONNECT_TIMEOUT, EVENT_POLL_TIMEOUT),
                )
                if not self.running:
                    break

                # Limit response size to prevent memory exhaustion
                text = resp.text[:8192]
                try:
                    data = json.loads(text)
                except (json.JSONDecodeError, ValueError):
                    text = text.strip()
                    if text.startswith("{"):
                        try:
                            data = json.loads(text)
                        except (json.JSONDecodeError, ValueError):
                            continue
                    else:
                        continue

                cmd = data.get("cmd", "")
                if cmd == "download":
                    self.status = str(data.get("status", ""))
                    self.progress = _safe_int(data.get("progress", 0))
                    self.nb_frames = _safe_int(data.get("nb_frames", 0))
                elif cmd == "obslist":
                    pass  # Observation list update, ignore

            except requests.exceptions.ReadTimeout:
                # Normal — long poll timed out, retry immediately
                pass
            except requests.exceptions.ConnectionError:
                if self.running:
                    time.sleep(2)
            except (json.JSONDecodeError, ValueError, KeyError, TypeError):
                if self.running:
                    time.sleep(2)


def check_connection() -> bool:
    """Verify telescope is reachable."""
    print(f"Connecting to telescope at {BASE_URL} ...")
    try:
        resp = requests.get(BASE_URL, timeout=(5, 15))
        if resp.status_code == 200:
            print("  Connected!")
            return True
        print(f"  Unexpected status: {resp.status_code}")
        return False
    except requests.exceptions.ConnectionError:
        print("  FAILED: Cannot connect. Are you on the telescope WiFi?")
        return False
    except requests.exceptions.Timeout:
        print("  FAILED: Connection timed out.")
        return False


def list_observations(session: requests.Session) -> list[dict]:
    """Fetch list of all observations from telescope."""
    print("\nFetching observation list (this can take a minute)...")

    for attempt in range(1, 4):
        try:
            resp = session.get(
                f"{API_BASE}/observations/list",
                timeout=(CONNECT_TIMEOUT, LIST_TIMEOUT),
            )
            resp.raise_for_status()

            ct = resp.headers.get("Content-Type", "")
            if "html" in ct and "<html" in resp.text.lower():
                print("  ERROR: Got HTML instead of JSON.")
                return []

            text = resp.text.replace("NaN", "null")
            data = json.loads(text)

            if isinstance(data, list):
                print(f"  Found {len(data)} observation(s)")
                return data
            elif isinstance(data, dict):
                for key in ("observations", "obs", "data", "list", "items"):
                    if key in data and isinstance(data[key], list):
                        result = data[key]
                        print(f"  Found {len(result)} observation(s)")
                        return result
                if "vpath" in data:
                    return [data]
            return []

        except requests.exceptions.ReadTimeout:
            print(f"  Attempt {attempt}/3 timed out")
            if attempt < 3:
                time.sleep(RETRY_DELAY)
        except requests.exceptions.ConnectionError:
            print(f"  Attempt {attempt}/3 connection error")
            if attempt < 3:
                time.sleep(RETRY_DELAY)
        except json.JSONDecodeError:
            print(f"  JSON parse error. Raw: {resp.text[:300]}")
            return []

    return []


def format_timestamp(ms: int) -> str:
    """Convert millisecond timestamp to readable date."""
    if not isinstance(ms, (int, float)) or ms <= 0:
        return "?"
    try:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except (OSError, ValueError):
        return "?"


def display_observations(observations: list[dict]) -> None:
    """Display observations in a numbered list."""
    print(f"\n{'#':>3}  {'Date':<22}  {'Type':<22}  {'Frames':>7}  {'Tag'}")
    print("-" * 80)
    for i, obs in enumerate(observations, 1):
        obs_start = obs.get("obs_start", 0)
        date_str = format_timestamp(obs_start)
        purpose = _safe_str(obs.get("purpose", ""))
        pmode = _safe_str(obs.get("pmode", ""))
        obs_type = f"{pmode} {purpose}".strip() if purpose else pmode
        frames = _safe_int(obs.get("nb_frames", 0))
        tag = _safe_str(obs.get("obs_attr", {}).get("tag_sc", ""))
        print(f"{i:>3}  {date_str:<22}  {obs_type:<22}  {frames:>7}  {tag}")


def select_observations(observations: list[dict]) -> list[dict]:
    """Let user select which observations to download."""
    print(f"\nSelect Observation(s) to Download:")
    print("  'all'   - download everything")
    print("  '1,2'   - specific numbers")
    print("  '1-3'   - range")
    print("  'q'     - quit")

    while True:
        choice = input("\n> ").strip().lower()
        if choice == "q":
            return []
        if choice == "all":
            return observations

        try:
            indices = set()
            for part in choice.split(","):
                part = part.strip()
                if "-" in part:
                    start, end = part.split("-", 1)
                    for i in range(int(start), int(end) + 1):
                        indices.add(i)
                else:
                    indices.add(int(part))

            selected = []
            for idx in sorted(indices):
                if 1 <= idx <= len(observations):
                    selected.append(observations[idx - 1])
            if selected:
                return selected
            print("  No valid selections.")
        except ValueError:
            print("  Invalid input. Examples: 'all', '1,2', '1-3'")


def select_format() -> str:
    """Let user choose download format."""
    print("\nChoose Format:")
    print("  [1] FITS  - raw science data (for analysis)")
    print("  [2] TIFF  - lossless image")
    print("  [3] PNG   - compressed image")
    choice = input("Format (default: 1): ").strip()
    formats = {"1": "fits", "2": "tiff", "3": "png", "": "fits"}
    fmt = formats.get(choice, "fits")
    print(f"  Using: {fmt.upper()}")
    return fmt


def download_observation(
    session: requests.Session,
    obs: dict,
    fmt: str,
    output_dir: Path,
    index: int,
    total: int,
) -> bool:
    """Download a single observation with event polling + retry."""
    vpath = obs.get("vpath", "")
    name = _safe_str(obs.get("name", "unknown"))
    purpose = _safe_str(obs.get("purpose", ""))
    frames = _safe_int(obs.get("nb_frames", 0))
    tag = _safe_str(obs.get("obs_attr", {}).get("tag_sc", ""))

    # Build sanitized filename — only allow alphanumeric + safe chars
    parts = [p for p in [purpose.lower(), tag, name] if p]
    raw_name = "_".join(parts)
    safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in raw_name)
    fallback = "".join(c if c.isalnum() or c in "-_." else "_" for c in vpath.replace("/", "_"))
    safe_name = safe_name.strip("_") or fallback.strip("_") or "observation"
    dest = (output_dir / f"{safe_name}.zip").resolve()

    # Guard against path traversal — dest must be inside output_dir
    if not str(dest).startswith(str(output_dir.resolve())):
        print(f"\n[{index}/{total}] ERROR: path traversal detected, skipping")
        return False

    # Skip if already downloaded (verify it's a real zip, not a saved error page)
    if dest.exists() and dest.stat().st_size > 1024:
        with open(dest, "rb") as f:
            magic = f.read(2)
        if magic == b"PK":
            print(f"\n[{index}/{total}] {purpose} {tag} — already exists "
                  f"({dest.stat().st_size / 1024 / 1024:.1f} MB), skipping")
            return True
        else:
            print(f"\n[{index}/{total}] {purpose} {tag} — removing invalid file "
                  f"({dest.stat().st_size} bytes, not a zip)")
            dest.unlink()

    # Validate format before URL construction
    if fmt not in ALLOWED_FORMATS:
        print(f"\n[{index}/{total}] ERROR: invalid format '{fmt}'")
        return False

    # URL-encode vpath to prevent path injection
    vpath_encoded = urllib.parse.quote(vpath, safe="/")
    download_url = f"{API_BASE}/observations/zip/{fmt}/0x0/{vpath_encoded}"

    print(f"\n[{index}/{total}] {purpose} {tag or name}")
    print(f"  {frames} frames")

    for attempt in range(1, MAX_RETRIES + 1):
        # Cancel any stuck download from a previous run before starting
        if attempt == 1:
            try:
                requests.post(
                    f"{API_BASE}/event",
                    json={"cmd": "cancelDownload"},
                    timeout=(3, 5),
                )
                time.sleep(1)
            except Exception:
                pass

        # Start event poller — REQUIRED for the backend to stream the zip
        poller = EventPoller()
        poller.start()
        # Give poller a moment to establish connection
        time.sleep(1)

        try:
            print(f"  Attempt {attempt}/{MAX_RETRIES}...", flush=True)

            resp = session.get(
                download_url,
                stream=True,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )

            ct = resp.headers.get("Content-Type", "")
            if "html" in ct:
                print("  Got HTML — endpoint not found")
                poller.stop()
                return False

            if resp.status_code == 502:
                print("  502 Bad Gateway — backend not ready, retrying...")
                poller.stop()
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
                continue

            resp.raise_for_status()

            total_size = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            start_time = time.time()
            last_progress_print = 0

            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)

                        now = time.time()
                        # Update progress every 0.5s to avoid flooding
                        if now - last_progress_print >= 0.5:
                            last_progress_print = now
                            elapsed = now - start_time
                            speed = downloaded / elapsed if elapsed > 0 else 0
                            if speed > 1024 * 1024:
                                speed_str = f"{speed / 1024 / 1024:.1f} MB/s"
                            else:
                                speed_str = f"{speed / 1024:.0f} KB/s"

                            mb = downloaded / 1024 / 1024

                            # Frame progress from event poller
                            frame_str = ""
                            if poller.nb_frames > 0:
                                frame_pct = poller.progress / poller.nb_frames * 100
                                frame_str = f" | frames: {poller.progress}/{poller.nb_frames} ({frame_pct:.0f}%)"

                            if total_size > 0:
                                pct = downloaded / total_size * 100
                                mb_total = total_size / 1024 / 1024
                                remaining = (total_size - downloaded) / speed if speed > 0 else 0
                                eta = f"{remaining / 60:.0f}m" if remaining > 60 else f"{remaining:.0f}s"
                                print(
                                    f"\r  {mb:.1f}/{mb_total:.1f} MB ({pct:.0f}%) "
                                    f"[{speed_str}, ETA {eta}]{frame_str}      ",
                                    end="", flush=True,
                                )
                            else:
                                print(
                                    f"\r  {mb:.1f} MB [{speed_str}]{frame_str}      ",
                                    end="", flush=True,
                                )

            poller.stop()

            final_size = dest.stat().st_size
            elapsed = time.time() - start_time

            if final_size == 0:
                print("\n  WARNING: Downloaded 0 bytes")
                dest.unlink(missing_ok=True)
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
                    continue
                return False

            avg_speed = final_size / elapsed if elapsed > 0 else 0
            if avg_speed > 1024 * 1024:
                avg_str = f"{avg_speed / 1024 / 1024:.1f} MB/s"
            else:
                avg_str = f"{avg_speed / 1024:.0f} KB/s"

            print(f"\n  Saved: {dest.name}")
            print(f"  {final_size / 1024 / 1024:.1f} MB in {elapsed:.0f}s ({avg_str} avg)")
            return True

        except (
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ConnectionError,
            requests.exceptions.ReadTimeout,
        ) as e:
            poller.cancel_download()
            poller.stop()
            print(f"\n  Connection dropped: {type(e).__name__}")
            dest.unlink(missing_ok=True)
            if attempt < MAX_RETRIES:
                print(f"  Waiting {RETRY_DELAY}s before retry...")
                time.sleep(RETRY_DELAY)
            else:
                print(f"  FAILED after {MAX_RETRIES} attempts.")
                return False

        except requests.exceptions.HTTPError as e:
            poller.cancel_download()
            poller.stop()
            print(f"\n  HTTP error: {e}")
            dest.unlink(missing_ok=True)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
                continue
            return False

        except KeyboardInterrupt:
            print("\n\n  Interrupted! Sending cancel to telescope...")
            poller.cancel_download()
            poller.stop()
            dest.unlink(missing_ok=True)
            sys.exit(1)

    return False


def main():
    parser = argparse.ArgumentParser(
        description="Download Unistellar telescope observations"
    )
    parser.add_argument(
        "--output", default="./unistellar_data",
        help="Output directory (default: ./unistellar_data)",
    )
    parser.add_argument(
        "--format", choices=["fits", "tiff", "png"], default=None,
        help="Image format (prompted if not specified)",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Download all observations without prompting",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Just list observations and exit",
    )
    parser.add_argument(
        "--cancel", action="store_true",
        help="Cancel any in-progress download on the telescope",
    )
    args = parser.parse_args()

    output_dir = Path(args.output).expanduser().resolve()

    if not check_connection():
        sys.exit(1)

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0",
        "Accept": "*/*",
        "Connection": "keep-alive",
    })

    # Cancel mode
    if args.cancel:
        print("\nSending cancel command...")
        try:
            resp = session.post(
                f"{API_BASE}/event",
                json={"cmd": "cancelDownload"},
                timeout=(5, 10),
            )
            print(f"  Response: {_safe_str(resp.text, 200)}")
        except Exception as e:
            print(f"  {e}")
        sys.exit(0)

    # List observations
    observations = list_observations(session)
    if not observations:
        print("\nNo observations found.")
        sys.exit(1)

    display_observations(observations)

    if args.check:
        print("\n(--check mode)")
        sys.exit(0)

    # Select format
    fmt = args.format or select_format()

    # Select observations
    if getattr(args, "all"):
        selected = observations
        print(f"\nDownloading all {len(selected)} observations")
    else:
        selected = select_observations(observations)
        if not selected:
            print("Nothing selected.")
            sys.exit(0)

    # Download one at a time
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"\nOutput: {output_dir}")
    print(f"Downloading {len(selected)} observation(s) as {fmt.upper()}")
    print("Event polling active — this is what makes it work!\n")

    succeeded = 0
    failed = []
    for i, obs in enumerate(selected, 1):
        ok = download_observation(session, obs, fmt, output_dir, i, len(selected))
        if ok:
            succeeded += 1
        else:
            failed.append(_safe_str(
                obs.get("obs_attr", {}).get("tag_sc", obs.get("name", "?"))
            ))

    print("\n" + "=" * 50)
    print(f"DONE: {succeeded}/{len(selected)} downloaded")
    if failed:
        print(f"FAILED ({len(failed)}):")
        for name in failed:
            print(f"  - {name}")
    print(f"Files in: {output_dir}")


if __name__ == "__main__":
    main()
