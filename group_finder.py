import asyncio
import os
import random
import threading

import aiohttp
from dotenv import load_dotenv

load_dotenv()


def _emit_event(on_event, outcome, message, group=None, group_id=None, level="info"):
    event = {
        "outcome": outcome,
        "message": message,
        "level": level,
        "group_id": group_id,
        "group": group,
    }
    if on_event is None:
        print(message)
    else:
        on_event(event)


class RequestLimiter:
    def __init__(self, interval):
        self.interval = interval
        self._lock = asyncio.Lock()
        self._next_request_at = 0.0

    async def wait(self, stop_event):
        loop = asyncio.get_running_loop()
        while not stop_event.is_set():
            async with self._lock:
                delay = self._next_request_at - loop.time()
                if delay <= 0:
                    self._next_request_at = loop.time() + self.interval
                    return True
            await asyncio.sleep(min(delay, 0.25))
        return False

    async def defer(self, seconds):
        loop = asyncio.get_running_loop()
        async with self._lock:
            self._next_request_at = max(
                self._next_request_at,
                loop.time() + seconds,
            )


class GroupIdSequence:
    def __init__(self, minimum, maximum):
        self.minimum = minimum
        self.maximum = maximum
        self.next_id = random.randint(minimum, maximum)
        self.remaining = maximum - minimum + 1
        self._lock = asyncio.Lock()

    async def next(self):
        async with self._lock:
            if self.remaining == 0:
                return None
            group_id = self.next_id
            self.remaining -= 1
            self.next_id = self.minimum if group_id == self.maximum else group_id + 1
            return group_id


async def _check_group(
    session,
    webhook_url,
    group_id,
    request_limiter,
    stop_event,
    on_event=None,
):
    url = f"https://groups.roblox.com/v1/groups/{group_id}"
    if not await request_limiter.wait(stop_event):
        return
    async with session.get(url) as response:
        if response.status in (400, 404):
            _emit_event(
                on_event,
                "not_found",
                f"Group {group_id} was not found.",
                group_id=group_id,
            )
            return
        if response.status == 429:
            try:
                retry_after = float(response.headers.get("Retry-After", ""))
            except ValueError:
                retry_after = request_limiter.interval * 2
            retry_after = min(max(retry_after, request_limiter.interval), 300)
            await request_limiter.defer(retry_after)
            _emit_event(
                on_event,
                "rate_limited",
                f"Roblox rate limit; pausing for {retry_after:g}s.",
                group_id=group_id,
                level="warning",
            )
            return
        response.raise_for_status()
        group = await response.json()

    owner = group.get("owner")
    owner = owner if isinstance(owner, dict) else None
    group_info = {
        "id": group_id,
        "name": group.get("name") or f"Group {group_id}",
        "description": group.get("description") or "",
        "created_at": group.get("created"),
        "shout": (group.get("shout") or {}).get("body", "")
        if isinstance(group.get("shout"), dict)
        else "",
        "owner_name": (
            (owner or {}).get("displayName")
            or (owner or {}).get("username")
            or (str((owner or {}).get("id")) if owner else None)
        ),
        "owner_id": (owner or {}).get("userId") or (owner or {}).get("id"),
        "member_count": group.get("memberCount"),
        "is_locked": bool(group.get("isLocked")),
        "public_entry_allowed": bool(group.get("publicEntryAllowed")),
        "is_verified": (
            bool(group.get("hasVerifiedBadge") or group.get("isVerified"))
            if "hasVerifiedBadge" in group or "isVerified" in group
            else None
        ),
    }
    if group_info["is_locked"]:
        group_info["status"] = "locked"
    elif not group_info["public_entry_allowed"]:
        group_info["status"] = "entry_blocked"
    elif owner is not None:
        group_info["status"] = "owned"
    else:
        group_info["status"] = "unowned"

    if group.get("isLocked"):
        _emit_event(
            on_event,
            "locked",
            f"{group_info['name']} is locked.",
            group=group_info,
            group_id=group_id,
        )
        return

    if not group.get("publicEntryAllowed"):
        _emit_event(
            on_event,
            "entry_blocked",
            f"{group_info['name']} does not allow public entry.",
            group=group_info,
            group_id=group_id,
        )
        return

    if group.get("owner") is not None:
        _emit_event(
            on_event,
            "owned",
            f"{group_info['name']} is owned by {group_info['owner_name'] or 'another user'}.",
            group=group_info,
            group_id=group_id,
        )
        return

    embed = {
        "title": "Group Found!",
        "description": f"[Click here to view the group](https://www.roblox.com/groups/group.aspx?gid={group_id})",
        "color": 3066993,
        "footer": {"text": "RoFinder | By: RXNation"},
    }
    _emit_event(
        on_event,
        "unowned",
        f"Found unowned group: {group_info['name']}.",
        group=group_info,
        group_id=group_id,
        level="success",
    )
    async with session.post(webhook_url, json={"embeds": [embed]}) as response:
        response.raise_for_status()


async def _scan_worker(
    session,
    webhook_url,
    request_limiter,
    stop_event,
    worker_id,
    on_event=None,
    group_ids=None,
):
    while not stop_event.is_set():
        group_id = await group_ids.next()
        if group_id is None:
            return
        try:
            await _check_group(
                session,
                webhook_url,
                group_id,
                request_limiter,
                stop_event,
                on_event,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            _emit_event(
                on_event,
                "error",
                f"Worker {worker_id}: request failed ({type(error).__name__}).",
                group_id=group_id,
                level="error",
            )


async def groupfinder(
    webhook_url,
    worker_count=1,
    request_timeout=10,
    request_interval=3,
    stop_event=None,
    on_event=None,
    group_id_min=1_000_000,
    group_id_max=9_999_999,
):
    stop_event = stop_event or threading.Event()
    timeout = aiohttp.ClientTimeout(total=request_timeout)
    connector = aiohttp.TCPConnector(limit=worker_count * 2)
    request_limiter = RequestLimiter(request_interval)
    group_ids = GroupIdSequence(group_id_min, group_id_max)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        workers = [
            asyncio.create_task(
                _scan_worker(
                    session,
                    webhook_url,
                    request_limiter,
                    stop_event,
                    worker_id,
                    on_event,
                    group_ids,
                )
            )
            for worker_id in range(1, worker_count + 1)
        ]
        await asyncio.gather(*workers)
    if not stop_event.is_set() and group_ids.remaining == 0 and on_event is not None:
        on_event({
            "outcome": "scan_complete",
            "level": "success",
            "message": f"Finished scanning group IDs {group_id_min:,}–{group_id_max:,}; no IDs were repeated.",
        })


if __name__ == '__main__':
    webhook_url = os.getenv('DISCORD_WEBHOOK')
    if webhook_url:
        asyncio.run(groupfinder(webhook_url))
    else:
        print("Set DISCORD_WEBHOOK in your .env file before starting the scanner.")
