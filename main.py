import ctypes
import asyncio
from collections import deque
from datetime import datetime
import getpass
import math
import os
import socket
import threading
import time
from urllib.parse import urlsplit

import aiohttp
from flask import Flask, jsonify, render_template, request
from colorama import Fore, Style, init as init_colors
from werkzeug.serving import WSGIRequestHandler, make_server

from config import (
    get_console_progress,
    get_group_id_range,
    get_request_interval,
    get_request_timeout,
    get_webhook_url,
    get_worker_count,
    save_setting,
)
from group_finder import groupfinder

app = Flask(__name__, template_folder="dashboard")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
scanner_status = {"state": "idle", "workers": 0, "error": None}
scan_metrics = {
    "checks": 0,
    "unowned": 0,
    "owned": 0,
    "locked": 0,
    "entry_blocked": 0,
    "not_found": 0,
    "rate_limited": 0,
    "errors": 0,
}
scan_logs = deque(maxlen=250)
group_history = deque(maxlen=100)
thumbnail_cache = {}
state_lock = threading.RLock()
next_log_id = 1
scanner_thread = None
scanner_stop_event = None
http_server = None
http_server_thread = None
dashboard_port = 8080


class QuietRequestHandler(WSGIRequestHandler):
    def log(self, request_type, message, *args):
        del request_type, message, args


def record_scan_event(event):
    global next_log_id
    outcome = event["outcome"]
    with state_lock:
        if outcome in {"unowned", "owned", "locked", "entry_blocked", "not_found", "rate_limited"}:
            scan_metrics["checks"] += 1
        metric_name = "errors" if outcome == "error" else outcome
        if metric_name in scan_metrics:
            scan_metrics[metric_name] += 1

        log_entry = {
            "id": next_log_id,
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "outcome": outcome,
            "level": event.get("level", "info"),
            "message": event.get("message", ""),
            "group_id": event.get("group_id"),
        }
        next_log_id += 1
        scan_logs.appendleft(log_entry)

        group = event.get("group")
        if group:
            existing = [item for item in group_history if item["id"] != group["id"]]
            group_history.clear()
            group_history.extend([group, *existing[:99]])

    if get_console_progress():
        print(f"[{outcome.upper()}] {event.get('message', '')}")


def _settings_snapshot():
    webhook_url = os.getenv("DISCORD_WEBHOOK", "").strip()
    webhook_is_configured = bool(webhook_url) and "paste_" not in webhook_url.lower()
    return {
        "webhook_configured": webhook_is_configured,
        "worker_count": get_worker_count(),
        "request_interval": get_request_interval(),
        "request_timeout": get_request_timeout(),
        "console_progress": get_console_progress(),
        "group_id_min": get_group_id_range()[0],
        "group_id_max": get_group_id_range()[1],
    }


def _valid_webhook_url(value):
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and parsed.hostname in {"discord.com", "ptb.discord.com"}
        and parsed.path.startswith("/api/webhooks/")
    )


def _is_local_request():
    allowed_hosts = {"127.0.0.1", "localhost"}
    if request.host.split(":", 1)[0] not in allowed_hosts:
        return False
    origin = request.headers.get("Origin")
    if not origin:
        return True
    parsed_origin = urlsplit(origin)
    return (
        parsed_origin.scheme == "http"
        and parsed_origin.hostname in allowed_hosts
        and parsed_origin.port == dashboard_port
    )


def start_services():
    global scanner_thread, scanner_stop_event

    if scanner_thread is not None and scanner_thread.is_alive():
        return True, "Scanner is already running."

    try:
        webhook_url = get_webhook_url()
        worker_count = get_worker_count()
        request_timeout = get_request_timeout()
        request_interval = get_request_interval()
        group_id_min, group_id_max = get_group_id_range()
    except (SystemExit, OSError) as error:
        return False, str(error)

    scanner_stop_event = threading.Event()
    stop_event = scanner_stop_event
    with state_lock:
        scanner_status.update(state="starting", workers=worker_count, error=None)

    def run_scanner():
        with state_lock:
            scanner_status.update(state="running", workers=worker_count, error=None)
        try:
            asyncio.run(
                groupfinder(
                    webhook_url,
                    worker_count,
                    request_timeout,
                    request_interval,
                    stop_event,
                    record_scan_event,
                    group_id_min,
                    group_id_max,
                )
            )
        except Exception as error:
            record_scan_event({
                "outcome": "error",
                "level": "error",
                "message": f"Scanner stopped after an unexpected error ({type(error).__name__}).",
            })
            with state_lock:
                scanner_status.update(state="error", error=type(error).__name__, workers=0)
        else:
            with state_lock:
                scanner_status.update(state="idle", workers=0)

    scanner_thread = threading.Thread(target=run_scanner, name="group-finder", daemon=True)
    scanner_thread.start()
    return True, "Scanner started."


def stop_services(quiet=False):
    global scanner_thread, scanner_stop_event
    if scanner_thread is None and scanner_stop_event is None:
        if not quiet:
            return True, "Scanner is not running."
        return True, ""

    if scanner_stop_event is not None:
        scanner_stop_event.set()
    if scanner_thread is not None and scanner_thread.is_alive():
        scanner_thread.join(timeout=get_request_timeout() + 2)
        if scanner_thread.is_alive():
            return False, "Scanner is finishing an in-flight request. Wait before starting again."

    scanner_thread = None
    scanner_stop_event = None
    with state_lock:
        scanner_status.update(state="idle", workers=0)
    return True, "Scanner stopped."


def start_http_server():
    global http_server, http_server_thread, dashboard_port
    if http_server_thread is not None and http_server_thread.is_alive():
        return True
    for candidate_port in range(8080, 8091):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if probe.connect_ex(("127.0.0.1", candidate_port)) == 0:
                continue
        try:
            http_server = make_server(
                "127.0.0.1",
                candidate_port,
                app,
                request_handler=QuietRequestHandler,
            )
        except OSError:
            continue
        dashboard_port = candidate_port
        break
    else:
        print("Local dashboard could not start: ports 8080-8090 are already in use.")
        return False
    http_server_thread = threading.Thread(
        target=http_server.serve_forever,
        name="local-dashboard",
        daemon=True,
    )
    http_server_thread.start()
    return True


def stop_http_server():
    global http_server, http_server_thread
    if http_server is not None:
        if http_server_thread is not None and http_server_thread.is_alive():
            http_server.shutdown()
            http_server_thread.join(timeout=2)
        http_server.server_close()
    http_server = None
    http_server_thread = None


def _masked_webhook():
    webhook_url = os.getenv("DISCORD_WEBHOOK", "").strip()
    return "[configured]" if webhook_url and "paste_" not in webhook_url.lower() else "[not set]"


def _edit_number(label, environment_name, current_value, minimum, maximum, cast):
    raw_value = input(f"{label} [{current_value}] (Enter keeps current): ").strip()
    if not raw_value:
        return
    try:
        value = cast(raw_value)
    except ValueError:
        print("That value is not a valid number; leaving it unchanged.")
        return
    if not minimum <= value <= maximum:
        print(f"Choose a value from {minimum} to {maximum}; leaving it unchanged.")
        return
    save_setting(environment_name, value)


def edit_settings():
    print("\nSettings (webhook input is hidden)")
    print(f"Webhook: {_masked_webhook()}")
    new_webhook = getpass.getpass("New Discord webhook (Enter keeps current): ").strip()
    if new_webhook:
        parsed_webhook = urlsplit(new_webhook)
        if (
            parsed_webhook.scheme == "https"
            and parsed_webhook.hostname in {"discord.com", "ptb.discord.com"}
            and parsed_webhook.path.startswith("/api/webhooks/")
        ):
            save_setting("DISCORD_WEBHOOK", new_webhook)
        else:
            print("That does not look like a Discord webhook URL; it was not saved.")

    _edit_number("Workers (1-5)", "ROFINDER_WORKERS", get_worker_count(), 1, 5, int)
    _edit_number(
        "Seconds between requests (1-60)",
        "ROFINDER_REQUEST_INTERVAL",
        get_request_interval(),
        1,
        60,
        float,
    )
    _edit_number(
        "Request timeout in seconds (1-30)",
        "ROFINDER_REQUEST_TIMEOUT",
        get_request_timeout(),
        1,
        30,
        int,
    )
    group_id_min, group_id_max = get_group_id_range()
    _edit_number(
        "Minimum group ID (1-2147483647)",
        "ROFINDER_GROUP_ID_MIN",
        group_id_min,
        1,
        2_147_483_647,
        int,
    )
    _edit_number(
        "Maximum group ID (1-2147483647)",
        "ROFINDER_GROUP_ID_MAX",
        group_id_max,
        1,
        2_147_483_647,
        int,
    )
    current_progress = get_console_progress()
    progress_choice = input(
        f"Show scan progress in CMD [{ 'on' if current_progress else 'off' }] (y/n/Enter): "
    ).strip().lower()
    if progress_choice in {"y", "yes"}:
        save_setting("ROFINDER_CONSOLE_PROGRESS", "true")
    elif progress_choice in {"n", "no"}:
        save_setting("ROFINDER_CONSOLE_PROGRESS", "false")
    print("Scan parameters apply next start; CMD scan-log display changes immediately.")


@app.route('/')
def index():
    return render_template("index.html")


@app.route('/health')
def health():
    with state_lock:
        return jsonify({"scanner": dict(scanner_status), "metrics": dict(scan_metrics)})


@app.get("/api/state")
def api_state():
    with state_lock:
        return jsonify({
            "scanner": dict(scanner_status),
            "metrics": dict(scan_metrics),
            "logs": list(scan_logs),
            "groups": list(group_history),
            "settings": _settings_snapshot(),
        })


@app.get("/api/thumbnails")
def api_thumbnails():
    if request.host.split(":", 1)[0] not in {"127.0.0.1", "localhost"}:
        return jsonify({"error": "Local requests only."}), 403

    raw_ids = request.args.get("group_ids", "")
    try:
        group_ids = list(dict.fromkeys(int(value) for value in raw_ids.split(",") if value))
    except ValueError:
        return jsonify({"error": "Group IDs must be integers."}), 400
    if not group_ids or len(group_ids) > 100 or any(group_id <= 0 for group_id in group_ids):
        return jsonify({"error": "Provide between 1 and 100 valid group IDs."}), 400

    current_time = time.monotonic()
    with state_lock:
        image_urls = {
            group_id: cached[1]
            for group_id in group_ids
            if (cached := thumbnail_cache.get(group_id)) and cached[0] > current_time
        }
    missing_ids = [group_id for group_id in group_ids if group_id not in image_urls]
    if missing_ids:
        try:
            fetched = asyncio.run(_fetch_group_thumbnails(missing_ids))
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            fetched = {}
        with state_lock:
            for group_id in missing_ids:
                image_url = fetched.get(group_id, "")
                thumbnail_cache[group_id] = (
                    current_time + (3600 if image_url else 60),
                    image_url,
                )
                if image_url:
                    image_urls[group_id] = image_url
    return jsonify({str(group_id): image_url for group_id, image_url in image_urls.items()})


async def _fetch_group_thumbnails(group_ids):
    url = "https://thumbnails.roblox.com/v1/groups/icons"
    params = {
        "groupIds": ",".join(map(str, group_ids)),
        "size": "150x150",
        "format": "Png",
        "isCircular": "false",
    }
    timeout = aiohttp.ClientTimeout(total=5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, params=params) as response:
            response.raise_for_status()
            payload = await response.json()
    return {
        int(item["targetId"]): item["imageUrl"]
        for item in payload.get("data", [])
        if item.get("targetId") and item.get("imageUrl")
    }


@app.post("/api/control/<action>")
def api_control(action):
    if not _is_local_request():
        return jsonify({"error": "Local requests only."}), 403
    if action == "start":
        success, message = start_services()
    elif action == "stop":
        success, message = stop_services()
    else:
        return jsonify({"error": "Unknown control action."}), 404
    return jsonify({"success": success, "message": message}), (200 if success else 400)


@app.post("/api/settings")
def api_settings():
    if not _is_local_request():
        return jsonify({"error": "Local requests only."}), 403
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Expected a JSON settings object."}), 400

    current = _settings_snapshot()
    try:
        workers = _parse_setting(data, "worker_count", current["worker_count"], int, 1, 5)
        interval = _parse_setting(
            data, "request_interval", current["request_interval"], float, 1, 60
        )
        timeout = _parse_setting(
            data, "request_timeout", current["request_timeout"], int, 1, 30
        )
        group_id_min = _parse_setting(
            data, "group_id_min", current["group_id_min"], int, 1, 2_147_483_647
        )
        group_id_max = _parse_setting(
            data, "group_id_max", current["group_id_max"], int, 1, 2_147_483_647
        )
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    if group_id_min > group_id_max:
        return jsonify({"error": "Minimum group ID cannot exceed maximum group ID."}), 400

    progress = data.get("console_progress", current["console_progress"])
    if not isinstance(progress, bool):
        return jsonify({"error": "CMD scan-log display must be true or false."}), 400

    updates = {
        "ROFINDER_WORKERS": workers,
        "ROFINDER_REQUEST_INTERVAL": interval,
        "ROFINDER_REQUEST_TIMEOUT": timeout,
        "ROFINDER_GROUP_ID_MIN": group_id_min,
        "ROFINDER_GROUP_ID_MAX": group_id_max,
        "ROFINDER_CONSOLE_PROGRESS": str(progress).lower(),
    }
    webhook = data.get("webhook", "")
    if webhook:
        if not isinstance(webhook, str) or not _valid_webhook_url(webhook.strip()):
            return jsonify({"error": "Enter a valid Discord webhook URL."}), 400
        updates["DISCORD_WEBHOOK"] = webhook.strip()

    try:
        for name, value in updates.items():
            save_setting(name, value)
    except OSError:
        return jsonify({"error": "Could not save settings to .env."}), 500
    return jsonify({"success": True, "settings": _settings_snapshot()})


def _parse_setting(data, name, current, cast, minimum, maximum):
    value = data.get(name, current)
    if isinstance(value, bool):
        raise ValueError(f"{name.replace('_', ' ').title()} must be a number.")
    try:
        parsed = cast(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name.replace('_', ' ').title()} must be a number.") from None
    if isinstance(parsed, float) and not math.isfinite(parsed):
        raise ValueError(f"{name.replace('_', ' ').title()} must be finite.")
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name.replace('_', ' ').title()} must be from {minimum} to {maximum}.")
    return parsed


def set_console_title(title):
    if os.name == "nt":
        ctypes.windll.kernel32.SetConsoleTitleW(title)
    else:
        print(f"\033]0;{title}\007", end="", flush=True)


if __name__ == '__main__':
    init_colors(autoreset=True)
    try:
        set_console_title("RoGroup Finder")
    except Exception as e:
        print(f"Error setting console title: {e}")

    print(Fore.CYAN + """
     ____  _____  ___  ____  _____  __  __  ____    ____  ____  _  _  ____  ____  ____ 
    (  _ \(  _  )/ __)(  _ \(  _  )(  )(  )(  _ \  ( ___)(_  _)( \( )(  _ \( ___)(  _ \\
     )   / )(_)(( (_-. )   / )(_)(  )(__)(  )___/   )__)  _)(_  )  (  )(_) ))__)  )   /
    (_)\_)(_____)\___/(_)\_)(_____)(______)(__)    (__)  (____)(_)\_)(____/(____)(_)\_)

                 By RXNation.dev
    """)

    dashboard_started = start_http_server()
    if dashboard_started:
        print(f"Local dashboard: http://127.0.0.1:{dashboard_port}")

    try:
        while True:
            state = scanner_status["state"]
            state_color = Fore.GREEN if state == "running" else Fore.YELLOW
            if state == "error":
                state_color = Fore.RED
            print(Fore.CYAN + "+--------------------------------------+")
            print(Fore.CYAN + "|" + Style.BRIGHT + "          RoFinder Control Panel       " + Fore.CYAN + "|")
            print(Fore.CYAN + "+--------------------------------------+")
            print(f"  Status: {state_color}{state.upper()}")
            print(f"  {Fore.GREEN}[1]{Style.RESET_ALL} Start scanning")
            print(f"  {Fore.GREEN}[2]{Style.RESET_ALL} Edit settings")
            print(f"  {Fore.GREEN}[3]{Style.RESET_ALL} Stop scanning")
            print(f"  {Fore.GREEN}[4]{Style.RESET_ALL} Exit")
            print(f"  {Fore.GREEN}[5]{Style.RESET_ALL} Toggle scan logs in CMD")
            choice = input("Select an option: ").strip()

            if choice == "1":
                _, message = start_services()
                print(message)
            elif choice == "2":
                edit_settings()
            elif choice == "3":
                _, message = stop_services()
                print(message)
            elif choice == "4":
                break
            elif choice == "5":
                next_value = not get_console_progress()
                save_setting("ROFINDER_CONSOLE_PROGRESS", str(next_value).lower())
                print(f"CMD scan logs {'enabled' if next_value else 'disabled'}.")
            else:
                print("Choose 1, 2, 3, 4, or 5.")
    except (EOFError, KeyboardInterrupt):
        print("\nShutting down.")
    finally:
        stop_services(quiet=True)
        stop_http_server()
