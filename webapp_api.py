"""Мини-апп: aiohttp-сервер с API и статикой.

Аутентификация — по подписи initData от Telegram (HMAC-SHA256 с токеном бота).
Данные берутся из тех же модулей, что и бот: profiles, planner, calories, diary.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs

from aiohttp import web

import calories
import diary
import exercises
import planner
import profiles
import subscriptions as subs
from config import Settings

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
WEBAPP_DIR = BASE_DIR / "webapp"
MAX_AUTH_AGE = 24 * 60 * 60  # initData живёт сутки


def validate_init_data(init_data: str, bot_token: str) -> Optional[dict]:
    """Проверяет подпись initData и возвращает пользователя Telegram."""
    if not init_data:
        return None
    try:
        parsed = parse_qs(init_data, keep_blank_values=True)
    except ValueError:
        return None

    received_hash = (parsed.pop("hash", [None])[0] or "").strip()
    if not received_hash:
        return None

    check_string = "\n".join(f"{key}={values[0]}" for key, values in sorted(parsed.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        logger.warning("initData с неверной подписью")
        return None

    try:
        auth_date = int(parsed.get("auth_date", ["0"])[0])
    except ValueError:
        return None
    if time.time() - auth_date > MAX_AUTH_AGE:
        logger.warning("initData просрочен")
        return None

    raw_user = parsed.get("user", ["{}"])[0]
    try:
        user = json.loads(raw_user)
    except json.JSONDecodeError:
        return None
    if not isinstance(user, dict) or "id" not in user:
        return None
    return user


def _user_id(request: web.Request) -> Optional[int]:
    user = request.get("tg_user")
    return int(user["id"]) if user else None


@web.middleware
async def telegram_auth(request: web.Request, handler):
    """Пропускает только запросы с валидной подписью initData."""
    if request.path.startswith(("/static/", "/media/")) or request.path == "/":
        return await handler(request)

    token = request.app["bot_token"]
    init_data = request.headers.get("X-Init-Data", "")
    user = validate_init_data(init_data, token)
    if user is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    request["tg_user"] = user
    return await handler(request)


async def api_overview(request: web.Request) -> web.Response:
    user_id = _user_id(request)
    assert user_id is not None

    profile = await profiles.get(user_id)
    status = await subs.get_status(user_id)
    rows = await diary.items(user_id)

    payload = {
        "subscription": {
            "kind": status.kind,
            "active": status.active,
            "days_left": status.days_left,
            "paid_until": status.paid_until.isoformat() if status.paid_until else None,
        },
        "profile": None,
        "plan": None,
        "calories": None,
        "diary": {"totals": diary.totals(rows), "items": rows[-20:]},
    }

    if profile is not None:
        payload["profile"] = {
            "height": profile.height,
            "weight": profile.weight,
            "experience": profile.experience_title,
            "days": profile.days,
            "minutes": profile.minutes,
            "goal": profile.goal_title,
            "equipment": profile.equipment_title,
            "activity": profile.activity_title,
            "limitations": profile.limitations_title,
        }
        plan = planner.build(profile)
        payload["plan"] = {
            "note": plan.note,
            "days": [
                {
                    "title": day.title,
                    "slots": [
                        {"slug": s.exercise.slug, "name": s.exercise.name_ru,
                         "sets": s.sets, "reps": s.reps}
                        for s in day.slots
                    ],
                }
                for day in plan.days
            ],
        }
        if profile.has_body_data and profile.activity:
            result = calories.compute(profile)
            payload["calories"] = {
                "bmr": result.bmr,
                "tdee": result.tdee,
                "target": result.target,
                "protein": result.protein_g,
                "fat": result.fat_g,
                "carbs": result.carbs_g,
                "floor_applied": result.floor_applied,
            }

    return web.json_response(payload)


async def api_exercises(request: web.Request) -> web.Response:
    groups = []
    for group in exercises.groups():
        items = exercises.by_group(group)
        groups.append({
            "group": group,
            "items": [
                {"slug": e.slug, "name": e.name_ru, "free": e.free, "level": e.level}
                for e in items
            ],
        })
    return web.json_response({"groups": groups})


async def api_exercise(request: web.Request) -> web.Response:
    slug = request.match_info["slug"]
    ex = exercises.by_slug(slug)
    if ex is None:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response({
        "slug": ex.slug,
        "name": ex.name_ru,
        "group": ex.group,
        "level": ex.level,
        "equipment": ex.equipment,
        "muscles": ex.muscles,
        "cues": ex.cues,
        "mistakes": ex.mistakes,
        "safety": ex.safety,
        "free": ex.free,
        "images": [f"/media/exercises/{ex.slug}/{i}.jpg" for i in range(2)],
    })


async def api_diary_add(request: web.Request) -> web.Response:
    user_id = _user_id(request)
    assert user_id is not None
    data = await request.json()
    text = str(data.get("text", "")).strip()
    if not text:
        return web.json_response({"error": "empty"}, status=400)

    import re

    added, errors = 0, []
    for chunk in re.split(r"[,;\n]", text):
        chunk = chunk.strip()
        if not chunk:
            continue
        match = re.match(r"^(.*?)[\s,]*?(\d{1,4})\s*(?:г|гр|грамм)?\.?$", chunk, re.IGNORECASE)
        if not match:
            errors.append(f"«{chunk}»: формат «продукт граммы»")
            continue
        grams = int(match.group(2))
        if not 5 <= grams <= 2000:
            errors.append(f"«{chunk}»: граммы 5–2000")
            continue
        food = diary.find_food(match.group(1))
        if food is None:
            errors.append(f"«{match.group(1).strip()}» не найден")
            continue
        await diary.add(user_id, food, grams)
        added += 1

    rows = await diary.items(user_id)
    return web.json_response({
        "added": added,
        "errors": errors,
        "diary": {"totals": diary.totals(rows), "items": rows[-20:]},
    })


async def api_diary_clear(request: web.Request) -> web.Response:
    user_id = _user_id(request)
    assert user_id is not None
    await diary.clear(user_id)
    return web.json_response({"ok": True})


async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(WEBAPP_DIR / "index.html")


def create_app(settings: Settings) -> web.Application:
    app = web.Application(middlewares=[telegram_auth])
    app["bot_token"] = settings.telegram_token
    app.router.add_get("/", index)
    app.router.add_static("/static/", WEBAPP_DIR, show_index=False)
    # ВАЖНО: корень media, а не media/exercises — иначе /media/exercises/<slug>/0.jpg
    # превращается в media/exercises/exercises/...
    app.router.add_static("/media/", exercises.MEDIA_DIR.parent, show_index=False)
    app.router.add_get("/api/overview", api_overview)
    app.router.add_get("/api/exercises", api_exercises)
    app.router.add_get("/api/exercise/{slug}", api_exercise)
    app.router.add_post("/api/diary/add", api_diary_add)
    app.router.add_post("/api/diary/clear", api_diary_clear)
    return app


async def start(settings: Settings) -> None:
    """Запускает сервер внутри процесса бота."""
    app = create_app(settings)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=settings.webapp_port)
    await site.start()
    logger.info("Мини-апп слушает порт %s", settings.webapp_port)
