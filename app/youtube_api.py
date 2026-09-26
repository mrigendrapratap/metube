import asyncio
import logging
import os
import random
import shutil
import time

import yt_dlp
from aiohttp import web

log = logging.getLogger("youtube_api")

# ============================================================
# CACHE SETTINGS
# ============================================================

POPULAR_CACHE_TTL = 15 * 60       # 15 minutes
SEARCH_CACHE_TTL = 5 * 60         # 5 minutes

POPULAR_CACHE_SIZE = 100
POPULAR_RETURN_SIZE = 20

# ============================================================
# CACHE & LOCKS
# ============================================================

popular_cache = {
    "videos": [],
    "created_at": 0,
}

search_cache = {}

popular_refresh_lock = asyncio.Lock()
search_locks = {}


# ============================================================
# COOKIES HELPER (Avoids [Errno 30] Read-Only FS on Render)
# ============================================================

def get_writable_cookiefile():
    secret_path = "/etc/secrets/cookies.txt"
    writable_path = "/tmp/yt_cookies.txt"

    # Also check if MeTube UI uploaded cookies exist
    state_cookies = os.path.join(".", "cookies.txt")

    source = None
    if os.path.exists(secret_path):
        source = secret_path
    elif os.path.exists(state_cookies):
        source = state_cookies

    if source:
        try:
            # Sync to writable /tmp directory if source changed or doesn't exist
            if not os.path.exists(writable_path) or os.path.getmtime(source) > os.path.getmtime(writable_path):
                shutil.copyfile(source, writable_path)
            return writable_path
        except Exception as e:
            log.warning("Failed to prepare writable cookies file: %s", e)
            return None
    return None


# ============================================================
# YT-DLP OPTIONS
# ============================================================

def get_ydl_options():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "ignoreerrors": True,
    }
    cookiefile = get_writable_cookiefile()
    if cookiefile:
        opts["cookiefile"] = cookiefile
    return opts


def _run_extract_info(ydl_opts, target):
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(target, download=False)


# ============================================================
# VIDEO DATA
# ============================================================

def extract_video_data(entry):
    video_id = entry.get("id")
    if not video_id:
        return None

    thumbnail = (
        entry.get("thumbnail")
        or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    )

    return {
        "id": video_id,
        "title": entry.get("title"),
        "thumbnail": thumbnail,
        "duration": entry.get("duration"),
        "channel": entry.get("channel") or entry.get("uploader"),
        "channel_id": entry.get("channel_id") or entry.get("uploader_id"),
        "webpage_url": (
            entry.get("webpage_url")
            or f"https://www.youtube.com/watch?v={video_id}"
        ),
    }


# ============================================================
# SINGLE VIDEO API
# ============================================================

async def youtube_info(request):
    url = request.query.get("url")
    if not url:
        return web.json_response(
            {"success": False, "error": "Missing YouTube URL"},
            status=400,
        )

    try:
        ydl_opts = {
            **get_ydl_options(),
            "extract_flat": False,
        }

        info = await asyncio.to_thread(_run_extract_info, ydl_opts, url)
        if not info:
            return web.json_response(
                {"success": False, "error": "Video not found or extraction failed"},
                status=404,
            )

        video_id = info.get("id")
        return web.json_response(
            {
                "success": True,
                "id": video_id,
                "title": info.get("title"),
                "description": info.get("description"),
                "thumbnail": (
                    info.get("thumbnail")
                    or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
                ),
                "duration": info.get("duration"),
                "channel": info.get("channel") or info.get("uploader"),
                "channel_id": info.get("channel_id") or info.get("uploader_id"),
                "webpage_url": info.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}",
            }
        )

    except Exception as e:
        log.exception("YouTube API error")
        return web.json_response(
            {"success": False, "error": str(e)},
            status=500,
        )


# ============================================================
# SEARCH API
# ============================================================

async def youtube_search(request):
    query = request.query.get("q", "").strip()
    if not query:
        return web.json_response(
            {"success": False, "error": "Missing or empty search query"},
            status=400,
        )

    try:
        limit = int(request.query.get("limit", 20))
    except ValueError:
        limit = 20

    limit = max(1, min(limit, 50))
    cache_key = f"{query.lower()}:{limit}"

    cached = search_cache.get(cache_key)
    if cached and (time.monotonic() - cached["created_at"] < SEARCH_CACHE_TTL):
        response = dict(cached["response"])
        response["cached"] = True
        return web.json_response(response)

    lock = search_locks.setdefault(cache_key, asyncio.Lock())
    async with lock:
        cached = search_cache.get(cache_key)
        if cached and (time.monotonic() - cached["created_at"] < SEARCH_CACHE_TTL):
            response = dict(cached["response"])
            response["cached"] = True
            return web.json_response(response)

        try:
            search_query = f"ytsearch{limit}:{query} shorts"
            log.info("YouTube search: %s", search_query)

            result = await asyncio.to_thread(_run_extract_info, get_ydl_options(), search_query)

            videos = []
            for entry in (result.get("entries") or []):
                if not entry:
                    continue
                video = extract_video_data(entry)
                if video:
                    videos.append(video)

            response = {
                "success": True,
                "query": query,
                "count": len(videos),
                "cached": False,
                "videos": videos,
            }

            search_cache[cache_key] = {
                "created_at": time.monotonic(),
                "response": response,
            }
            return web.json_response(response)

        except Exception as e:
            log.exception("YouTube search API error")
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500,
            )
        finally:
            if not lock.locked():
                search_locks.pop(cache_key, None)


# ============================================================
# COLLECT POPULAR SHORTS
# ============================================================

def load_popular_shorts():
    videos_by_id = {}
    queries = [
        "viral shorts", "trending shorts", "new shorts", "latest shorts",
        "popular shorts", "funny shorts", "comedy shorts", "music shorts",
        "dance shorts", "gaming shorts", "entertainment shorts", "cricket shorts",
        "sports shorts", "memes shorts", "india shorts", "hindi shorts",
        "tech shorts", "motivation shorts", "facts shorts"
    ]

    per_query = 50
    ydl_opts = get_ydl_options()

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        for query_index, query in enumerate(queries, start=1):
            if len(videos_by_id) >= POPULAR_CACHE_SIZE:
                break

            search_query = f"ytsearch{per_query}:{query}"
            try:
                result = ydl.extract_info(search_query, download=False)
                if not result:
                    continue

                for entry in result.get("entries", []):
                    if not entry:
                        continue
                    video_id = entry.get("id")
                    if not video_id or video_id in videos_by_id:
                        continue

                    duration = entry.get("duration")
                    if duration and (duration <= 0 or duration > 60):
                        continue

                    video = extract_video_data(entry)
                    if video:
                        videos_by_id[video_id] = video

                    if len(videos_by_id) >= POPULAR_CACHE_SIZE:
                        break
            except Exception:
                log.exception("Popular Shorts search failed for query: %s", query)

    videos = list(videos_by_id.values())
    random.shuffle(videos)
    return videos


# ============================================================
# REFRESH POPULAR CACHE
# ============================================================

async def refresh_popular_cache(force=False):
    global popular_cache
    now = time.monotonic()

    if not force and popular_cache["videos"]:
        if (now - popular_cache["created_at"]) < POPULAR_CACHE_TTL:
            return popular_cache["videos"]

    async with popular_refresh_lock:
        if not force and popular_cache["videos"]:
            if (now - popular_cache["created_at"]) < POPULAR_CACHE_TTL:
                return popular_cache["videos"]

        log.info("Refreshing popular Shorts cache...")
        videos = await asyncio.to_thread(load_popular_shorts)

        if videos:
            popular_cache = {
                "videos": videos[:POPULAR_CACHE_SIZE],
                "created_at": time.monotonic(),
            }
            log.info("Popular cache refreshed: %d videos", len(videos))

        return popular_cache["videos"]


# ============================================================
# POPULAR SHORTS API
# ============================================================

async def youtube_popular(request):
    try:
        try:
            limit = int(request.query.get("limit", POPULAR_RETURN_SIZE))
        except ValueError:
            limit = POPULAR_RETURN_SIZE

        limit = max(1, min(limit, 50))
        videos = await refresh_popular_cache()

        if not videos:
            return web.json_response(
                {"success": False, "error": "Could not load popular Shorts"},
                status=503,
            )

        selected = random.sample(videos, min(limit, len(videos)))
        cache_age = time.monotonic() - popular_cache["created_at"]

        return web.json_response(
            {
                "success": True,
                "type": "popular_latest_shorts",
                "collected": len(videos),
                "returned": len(selected),
                "cache_ttl_seconds": POPULAR_CACHE_TTL,
                "cache_age_seconds": int(cache_age),
                "cached": True,
                "videos": selected,
            }
        )

    except Exception as e:
        log.exception("YouTube popular API error")
        return web.json_response({"success": False, "error": str(e)}, status=500)


# ============================================================
# CLEAR CACHE
# ============================================================

async def youtube_clear_cache(request):
    global popular_cache
    popular_cache = {"videos": [], "created_at": 0}
    search_cache.clear()
    search_locks.clear()
    log.info("YouTube API cache cleared")
    return web.json_response({"success": True, "message": "YouTube API cache cleared"})