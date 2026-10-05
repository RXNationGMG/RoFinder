import os
from pathlib import Path
from dotenv import load_dotenv, set_key

load_dotenv()
ENV_PATH = Path(__file__).with_name(".env")


def save_setting(name, value):
    set_key(str(ENV_PATH), name, str(value), quote_mode="always")
    os.environ[name] = str(value)

def get_webhook_url():
    webhook_url = os.getenv("DISCORD_WEBHOOK", "").strip()
    if not webhook_url or "paste_" in webhook_url.lower():
        raise SystemExit(
            "Set DISCORD_WEBHOOK in .env first. See README.md for setup instructions."
        )
    return webhook_url

def _get_bounded_int(name, default, minimum, maximum):
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        print(f"Warning: {name} must be an integer; using {default}.")
        return default

    if not minimum <= value <= maximum:
        print(f"Warning: {name} must be between {minimum} and {maximum}; using {default}.")
        return default

    return value


def get_worker_count():
    return _get_bounded_int("ROFINDER_WORKERS", 1, 1, 5)


def get_request_timeout():
    return _get_bounded_int("ROFINDER_REQUEST_TIMEOUT", 10, 1, 30)


def get_group_id_range():
    default_minimum = 1_000_000
    default_maximum = 9_999_999
    minimum = _get_bounded_int("ROFINDER_GROUP_ID_MIN", default_minimum, 1, 2_147_483_647)
    maximum = _get_bounded_int("ROFINDER_GROUP_ID_MAX", default_maximum, 1, 2_147_483_647)
    if minimum > maximum:
        print("Warning: group ID minimum exceeds maximum; using default range.")
        return default_minimum, default_maximum
    return minimum, maximum


def get_request_interval():
    try:
        interval = float(os.getenv("ROFINDER_REQUEST_INTERVAL", "3"))
    except ValueError:
        print("Warning: ROFINDER_REQUEST_INTERVAL must be a number; using 3 seconds.")
        return 3

    if not 1 <= interval <= 60:
        print("Warning: ROFINDER_REQUEST_INTERVAL must be between 1 and 60; using 3 seconds.")
        return 3

    return interval


def get_console_progress():
    return os.getenv("ROFINDER_CONSOLE_PROGRESS", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def get_thread_count():
    return get_worker_count()
