import asyncio
import logging
import random
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
# CACHE
# ============================================================

popular_cache = {
    "videos": [],
    "created_at": 0,
}

search_cache = {}

popular_refresh_lock = asyncio.Lock()
search_locks = {}


# ============================================================
# YT-DLP OPTIONS
# ============================================================

def get_ydl_options():
    return {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "ignoreerrors": True,
    }


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
        "channel": entry.get("channel"),
        "channel_id": entry.get("channel_id"),
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
            {
                "success": False,
                "error": "Missing YouTube URL",
            },
            status=400,
        )

    try:
        ydl_opts = {
            **get_ydl_options(),
            "extract_flat": False,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(
                url,
                download=False,
            )

        return web.json_response(
            {
                "success": True,
                "id": info.get("id"),
                "title": info.get("title"),
                "description": info.get("description"),
                "thumbnail": (
                    info.get("thumbnail")
                    or (
                        f"https://i.ytimg.com/vi/"
                        f"{info.get('id')}/hqdefault.jpg"
                    )
                ),
                "duration": info.get("duration"),
                "channel": info.get("channel"),
                "channel_id": info.get("channel_id"),
                "webpage_url": info.get("webpage_url"),
            }
        )

    except Exception as e:
        log.exception("YouTube API error")

        return web.json_response(
            {
                "success": False,
                "error": str(e),
            },
            status=500,
        )


# ============================================================
# SEARCH API
# ============================================================

async def youtube_search(request):
    query = request.query.get("q")

    if not query:
        return web.json_response(
            {
                "success": False,
                "error": "Missing search query",
            },
            status=400,
        )

    query = query.strip()

    if not query:
        return web.json_response(
            {
                "success": False,
                "error": "Search query cannot be empty",
            },
            status=400,
        )

    try:
        limit = int(
            request.query.get(
                "limit",
                20,
            )
        )
    except ValueError:
        limit = 20

    limit = max(
        1,
        min(limit, 50),
    )

    cache_key = f"{query.lower()}:{limit}"

    cached = search_cache.get(cache_key)

    if cached:
        age = (
            time.monotonic()
            - cached["created_at"]
        )

        if age < SEARCH_CACHE_TTL:
            response = dict(cached["response"])
            response["cached"] = True

            return web.json_response(response)

    lock = search_locks.setdefault(
        cache_key,
        asyncio.Lock(),
    )

    async with lock:

        cached = search_cache.get(cache_key)

        if cached:
            age = (
                time.monotonic()
                - cached["created_at"]
            )

            if age < SEARCH_CACHE_TTL:
                response = dict(cached["response"])
                response["cached"] = True

                return web.json_response(response)

        try:
            search_query = (
                f"ytsearch{limit}:{query} shorts"
            )

            log.info(
                "YouTube search: %s",
                search_query,
            )

            with yt_dlp.YoutubeDL(
                get_ydl_options()
            ) as ydl:

                result = ydl.extract_info(
                    search_query,
                    download=False,
                )

            videos = []

            for entry in result.get(
                "entries",
                [],
            ):
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
            log.exception(
                "YouTube search API error"
            )

            return web.json_response(
                {
                    "success": False,
                    "error": str(e),
                },
                status=500,
            )


# ============================================================
# COLLECT POPULAR SHORTS
# ============================================================

def load_popular_shorts():

    videos_by_id = {}

    queries = [
        "viral shorts",
        "trending shorts",
        "new shorts",
        "latest shorts",
        "popular shorts",
        "funny shorts",
        "comedy shorts",
        "music shorts",
        "dance shorts",
        "gaming shorts",
        "entertainment shorts",
        "cricket shorts",
        "football shorts",
        "sports shorts",
        "memes shorts",
        "india shorts",
        "hindi shorts",
        "bollywood shorts",
        "viral india shorts",
        "funny india shorts",
        "dance india shorts",
        "music india shorts",
        "comedy india shorts",
        "gaming india shorts",
        "entertainment india shorts",
        "tech shorts",
        "technology shorts",
        "movie shorts",
        "facts shorts",
        "motivation shorts",
        "inspiration shorts",
        "animals shorts",
        "cute animals shorts",
        "food shorts",
        "cooking shorts",
        "travel shorts",
        "nature shorts",
        "car shorts",
        "bike shorts",
        "fitness shorts",
        "workout shorts",
        "news shorts",
        "science shorts",
        "education shorts",
        "life hacks shorts",
        "prank shorts",
        "reaction shorts",
        "story shorts",
        "viral videos shorts",
        "best shorts",
    ]

    # Search up to 100 candidates for each query.
    # Maximum possible candidates = 50 x 100 = 5000.
    per_query = 100

    ydl_opts = get_ydl_options()

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:

        for query_index, query in enumerate(queries, start=1):

            # Stop immediately once we have 100 valid Shorts.
            if len(videos_by_id) >= POPULAR_CACHE_SIZE:
                log.info(
                    "Popular Shorts target reached: %d",
                    len(videos_by_id),
                )
                break

            search_query = f"ytsearch{per_query}:{query}"

            try:

                log.info(
                    "Popular Shorts search [%d/%d]: %s",
                    query_index,
                    len(queries),
                    search_query,
                )

                result = ydl.extract_info(
                    search_query,
                    download=False,
                )

                if not result:
                    continue

                entries = result.get("entries", [])

                for entry in entries:

                    if not entry:
                        continue

                    video_id = entry.get("id")

                    if not video_id:
                        continue

                    # Remove duplicate videos.
                    if video_id in videos_by_id:
                        continue

                    duration = entry.get("duration")

                    # Duration must be available.
                    if duration is None:
                        continue

                    # Ignore invalid duration.
                    if duration <= 0:
                        continue

                    # Shorts only.
                    if duration > 60:
                        continue

                    video = extract_video_data(entry)

                    if not video:
                        continue

                    videos_by_id[video_id] = video

                    log.info(
                        "Short accepted [%d/%d]: %s (%ss)",
                        len(videos_by_id),
                        POPULAR_CACHE_SIZE,
                        video_id,
                        duration,
                    )

                    # Target reached.
                    if (
                        len(videos_by_id)
                        >= POPULAR_CACHE_SIZE
                    ):
                        break

            except Exception:

                log.exception(
                    "Popular Shorts search failed: %s",
                    query,
                )

    videos = list(videos_by_id.values())

    # Randomize the complete cached pool.
    random.shuffle(videos)

    log.info(
        "Popular Shorts collection complete: %d videos",
        len(videos),
    )

    return videos

# ============================================================
# REFRESH POPULAR CACHE
# ============================================================

async def refresh_popular_cache(force=False):

    global popular_cache

    now = time.monotonic()

    if not force and popular_cache["videos"]:

        age = (
            now
            - popular_cache["created_at"]
        )

        if age < POPULAR_CACHE_TTL:
            return popular_cache["videos"]

    async with popular_refresh_lock:

        if not force and popular_cache["videos"]:

            age = (
                time.monotonic()
                - popular_cache["created_at"]
            )

            if age < POPULAR_CACHE_TTL:
                return popular_cache["videos"]

        log.info(
            "Refreshing popular Shorts cache..."
        )

        videos = await asyncio.to_thread(
            load_popular_shorts
        )

        if videos:

            popular_cache = {
                "videos": videos[
                    :POPULAR_CACHE_SIZE
                ],
                "created_at": time.monotonic(),
            }

            log.info(
                "Popular cache refreshed: %d videos",
                len(videos),
            )

        return popular_cache["videos"]


# ============================================================
# POPULAR SHORTS API
# ============================================================

async def youtube_popular(request):

    try:

        try:
            limit = int(
                request.query.get(
                    "limit",
                    POPULAR_RETURN_SIZE,
                )
            )
        except ValueError:
            limit = POPULAR_RETURN_SIZE

        limit = max(
            1,
            min(limit, 50),
        )

        videos = await refresh_popular_cache()

        if not videos:

            return web.json_response(
                {
                    "success": False,
                    "error": (
                        "Could not load "
                        "popular Shorts"
                    ),
                },
                status=503,
            )

        selected = random.sample(
            videos,
            min(
                limit,
                len(videos),
            ),
        )

        cache_age = (
            time.monotonic()
            - popular_cache["created_at"]
        )

        return web.json_response(
            {
                "success": True,
                "type": "popular_latest_shorts",
                "collected": len(videos),
                "returned": len(selected),
                "cache_ttl_seconds": (
                    POPULAR_CACHE_TTL
                ),
                "cache_age_seconds": int(
                    cache_age
                ),
                "cached": True,
                "videos": selected,
            }
        )

    except Exception as e:

        log.exception(
            "YouTube popular API error"
        )

        return web.json_response(
            {
                "success": False,
                "error": str(e),
            },
            status=500,
        )


# ============================================================
# CLEAR CACHE
# ============================================================

async def youtube_clear_cache(request):

    global popular_cache

    popular_cache = {
        "videos": [],
        "created_at": 0,
    }

    search_cache.clear()
    search_locks.clear()

    log.info(
        "YouTube API cache cleared"
    )

    return web.json_response(
        {
            "success": True,
            "message": "YouTube API cache cleared",
        }
    )