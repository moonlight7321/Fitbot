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

import dataclasses
import os
from collections import defaultdict, deque
from datetime import datetime
from zoneinfo import ZoneInfo

from aiohttp import web

import calories
import diary
import exercises
import planner
import profiles
import progress
import subscriptions as subs
from config import Settings

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
WEBAPP_DIR = BASE_DIR / "webapp"
MAX_AUTH_AGE = 24 * 60 * 60  # initData живёт сутки

# Ограничение частоты: (сколько запросов, за сколько секунд)
RATE_WRITE = (12, 60)
RATE_READ = (90, 60)
_RATE_BUCKETS: dict = defaultdict(deque)
RATE_CLEANUP_EVERY = 500
_rate_calls = 0


def _rate_key(request: web.Request, kind: str):
    user = request.get("tg_user") or {}
    return (int(user.get("id", 0)) or request.remote, kind)


def _check_rate(request: web.Request, kind: str) -> bool:
    global _rate_calls
    limit, window = RATE_WRITE if kind == "write" else RATE_READ
    key = _rate_key(request, kind)
    now = time.monotonic()
    bucket = _RATE_BUCKETS[key]

    while bucket and now - bucket[0] > window:
        bucket.popleft()
    if len(bucket) >= limit:
        return False
    bucket.append(now)

    _rate_calls += 1
    if _rate_calls % RATE_CLEANUP_EVERY == 0:
        for stale in [k for k, v in _RATE_BUCKETS.items() if not v]:
            _RATE_BUCKETS.pop(stale, None)
    return True


@web.middleware
async def no_cache(request: web.Request, handler):
    """Статика мини-аппа не кешируется: иначе Telegram держит старый JS."""
    response = await handler(request)
    if request.path.startswith(("/static/", "/media/")) or request.path == "/":
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@web.middleware
async def rate_limit(request: web.Request, handler):
    """Простой лимит в памяти: 12 записей и 90 чтений в минуту на пользователя."""
    if request.path.startswith(("/static/", "/media/")) or request.path == "/":
        return await handler(request)

    kind = "write" if request.method in ("POST", "PUT", "PATCH", "DELETE") else "read"
    if not _check_rate(request, kind):
        return web.json_response(
            {"error": "too many requests"},
            status=429,
            headers={"Retry-After": "30"},
        )
    return await handler(request)


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


def _adjusted_sets(slot, deload: bool, pain: set) -> int:
    """Подходы с учётом разгрузки и болевых групп."""
    sets = slot.sets
    if deload:
        sets = max(2, sets // 2)
    if slot.exercise.group in pain:
        sets = max(2, sets - 1)
    return sets


async def api_overview(request: web.Request) -> web.Response:
    user_id = _user_id(request)
    assert user_id is not None

    try:
        period = int(request.query.get("period", "30"))
    except ValueError:
        period = 30
    period = max(7, min(180, period))

    profile = await profiles.get(user_id)
    status = await subs.get_status(user_id)
    access = status.active
    rows = await diary.items(user_id) if access else []
    diary_history = await diary.history(user_id, period) if access else []

    payload = {
        "access": access,
        "subscription": {
            "kind": status.kind,
            "active": status.active,
            "days_left": status.days_left,
            "trial_used": status.trial_used,
            "paid_until": status.paid_until.isoformat() if status.paid_until else None,
        },
        "profile": None,
        "plan": None,
        "calories": None,
        "diary": {"totals": diary.totals(rows), "items": rows[-20:]} if access else None,
        "diary_history": diary_history,
        "progress": None,
        "profile_raw": None,
        "options": {
            "goals": profiles.GOALS,
            "experience": profiles.EXPERIENCE,
            "equipment": profiles.EQUIPMENT,
            "activity": profiles.ACTIVITY,
            "limitations": profiles.LIMITATIONS,
            "days": list(profiles.DAYS_OPTIONS),
            "minutes": list(profiles.MINUTES_OPTIONS),
        },
    }

    if profile is not None:
        payload["profile_raw"] = {
            "height": profile.height, "weight": profile.weight,
            "experience": profile.experience, "days": profile.days,
            "minutes": profile.minutes, "goal": profile.goal,
            "equipment": profile.equipment, "activity": profile.activity,
            "sex": profile.sex, "age": profile.age,
            "limitations": sorted(profile.limits),
        }
        payload["reminders"] = {"days": profile.weekdays, "time": profile.workout_time}
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
        # План — платная функция
        if access:
            plan = planner.build(profile)
            week = await progress.week_state(user_id, len(plan.days))
            breakdown = await progress.breakdown(user_id)
            pain = {group for group, reasons in breakdown.items() if reasons.get("pain")}
            feels = await progress.feelings(user_id, week["week"])
            weights = {}
            for day_index, day in enumerate(plan.days):
                allow = (
                    not week["deload"]
                    and not week["after_deload"]
                    and day_index not in week.get("partial", [])
                )
                feel = feels.get(day_index)
                for slot in day.slots:
                    slug = slot.exercise.slug
                    if slug in weights:
                        continue
                    sets = await progress.last_sets(user_id, slug)
                    if not sets:
                        start = planner.start_weight(profile.weight, slot.exercise)
                        if start > 0:
                            label = "≈ " + planner.format_weight(start, slot.exercise.equipment)
                            weights[slug] = {"label": label, "reps": None, "value": start}
                        continue
                    allow_group = allow and not breakdown.get(slot.exercise.group, {}).get("pain")
                    top = progress._top_reps(profile.goal)
                    best = max(int(x["reps"]) for x in sets)
                    last_weight = float(sets[-1]["weight"])
                    rule_sets = sets
                    if feel == "easy":
                        rule_sets = [{"weight": last_weight, "reps": top}]
                    elif feel == "normal":
                        rule_sets = [{"weight": last_weight, "reps": min(top, best + 1)}]
                    elif feel == "hard":
                        allow_group = False
                    step = progress.next_plan(
                        profile.goal, slot.exercise.equipment, rule_sets,
                        allow_increase=allow_group,
                    )
                    weights[slug] = {
                        "label": planner.format_weight(step["weight"], slot.exercise.equipment),
                        "reps": None if step["bodyweight"] else step["reps"],
                        "value": step["weight"],
                    }

            payload["week"] = {
                "number": week["week"],
                "done": week["done"],
                "planned": week["planned"],
                "deload": week["deload"],
                "after_deload": week["after_deload"],
                "partial": week["partial"],
                "reasons": week["reasons"],
            }
            if 0 <= profile.day_cursor < len(plan.days):
                payload["next_day"] = profile.day_cursor
            else:
                payload["next_day"] = await progress.next_day_index(user_id, len(plan.days))
            payload["plan"] = {
                "note": plan.note,
                "deload": week["deload"],
                "days": [
                    {
                        "title": day.title,
                        "slots": [
                            {"slug": s.exercise.slug, "name": s.exercise.name_ru,
                             "sets": _adjusted_sets(s, week["deload"], pain),
                             "reps": (weights.get(s.exercise.slug) or {}).get("reps") or s.reps,
                             "weight": (weights.get(s.exercise.slug) or {}).get("label"),
                             "weight_value": weights.get(s.exercise.slug, {}).get("value", 0)}
                            for s in day.slots
                        ],
                    }
                    for day in plan.days
                ],
            }
            payload["progress"] = await _progress_payload(user_id, period)
            history = await progress.history_weeks(user_id, max(1, period // 7))
            payload["history"] = history
            payload["details"] = await progress.day_details(user_id, 12)
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


async def api_reminders(request: web.Request) -> web.Response:
    user_id = _user_id(request)
    assert user_id is not None
    if not await subs.has_access(user_id):
        return web.json_response({"error": "subscription required"}, status=403)

    data = await request.json()
    raw_days = data.get("days") or []
    if not isinstance(raw_days, list):
        return web.json_response({"error": "bad days"}, status=400)
    days = sorted({int(d) for d in raw_days if str(d).isdigit() and 1 <= int(d) <= 7})
    time_value = str(data.get("time", "")).strip()
    allowed = {"07:00", "09:00", "12:00", "18:00", "19:00", "21:00"}
    if time_value and time_value not in allowed:
        return web.json_response({"error": "bad time"}, status=400)

    profile = await profiles.get(user_id)
    if profile is None:
        return web.json_response({"error": "no profile"}, status=400)

    # Нельзя поставить напоминание на сегодняшнее время, которое уже прошло
    now = datetime.now(ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow")))
    if time_value and now.isoweekday() in days and time_value <= now.strftime("%H:%M"):
        return web.json_response(
            {"error": "time in past", "message": "На сегодня это время уже прошло"},
            status=400,
        )

    profile = dataclasses.replace(
        profile,
        workout_days=",".join(str(d) for d in days),
        workout_time=time_value,
    )
    await profiles.save(profile)
    return web.json_response({"ok": True})


async def api_alternatives(request: web.Request) -> web.Response:
    user_id = _user_id(request)
    assert user_id is not None
    slug = request.match_info["slug"]
    current = exercises.by_slug(slug)
    profile = await profiles.get(user_id)
    if current is None or profile is None:
        return web.json_response({"error": "not found"}, status=404)

    excluded = planner.excluded_slugs(profile.limits)
    items = [
        {"slug": ex.slug, "name": ex.name_ru}
        for ex in exercises.by_group(current.group)
        if ex.slug != slug and planner._allowed(ex, profile.experience, profile.equipment, excluded)
    ][:8]
    return web.json_response({"items": items})


def _weight_cap(exercise) -> float:
    """Реалистичный предел для снаряда: штанга 300, гантель 60, тренажёр 200."""
    equipment = (exercise.equipment or "").lower()
    if "гантел" in equipment:
        return 60.0
    if "тренажёр" in equipment or "тренажер" in equipment or "блок" in equipment:
        return 200.0
    if "штанг" in equipment:
        return 300.0
    return 60.0


async def api_workout_custom(request: web.Request) -> web.Response:
    user_id = _user_id(request)
    assert user_id is not None
    if not await subs.has_access(user_id):
        return web.json_response({"error": "subscription required"}, status=403)

    data = await request.json()
    try:
        day_index = int(data.get("day_index", 0))
    except (TypeError, ValueError):
        return web.json_response({"error": "bad day"}, status=400)
    items = data.get("items") or []
    if not isinstance(items, list) or not items or len(items) > 20:
        return web.json_response({"error": "bad items"}, status=400)

    profile = await profiles.get(user_id)
    if profile is None:
        return web.json_response({"error": "no profile"}, status=400)
    plan = planner.build(profile)
    if day_index < 0 or day_index >= len(plan.days):
        return web.json_response({"error": "bad day"}, status=400)

    records = []
    for item in items:
        slug = str(item.get("slug", ""))
        exercise = exercises.by_slug(slug)
        if exercise is None:
            continue
        original = str(item.get("original", "")).strip()
        if original and original != slug:
            source = exercises.by_slug(original)
            if source is None or source.group != exercise.group:
                return web.json_response(
                    {"error": "bad replacement",
                     "message": "Замена должна быть из той же группы мышц"},
                    status=400,
                )
            excluded = planner.excluded_slugs(profile.limits)
            if not planner._allowed(exercise, profile.experience, profile.equipment, excluded):
                return web.json_response(
                    {"error": "not allowed",
                     "message": f"{exercise.name_ru}: не подходит под инвентарь или ограничения"},
                    status=400,
                )
            await progress.add_replacement(user_id, day_index, original, slug)
        try:
            sets = max(1, min(10, int(item.get("sets", 3))))
            reps = max(1, min(50, int(item.get("reps", 10))))
            weight = max(0.0, float(item.get("weight", 0)))
        except (TypeError, ValueError):
            continue

        # Если вес не указан для снаряда, подставляем стартовый ориентир:
        # иначе ноль в журнале будет читаться как «свой вес» и прогрессия сломается
        if weight <= 0 and "свой вес" not in (exercise.equipment or "").lower():
            weight = planner.start_weight(profile.weight, exercise)

        cap = _weight_cap(exercise)
        if weight > cap:
            return web.json_response(
                {"error": "too heavy",
                 "message": f"{exercise.name_ru}: максимум {cap:g} кг"},
                status=400,
            )
        for _ in range(sets):
            records.append(progress.SetRecord(slug, weight, reps))

    if not records:
        return web.json_response({"error": "nothing to save"}, status=400)

    await progress.add_workout(user_id, day_index, plan.days[day_index].title, records)
    profile = dataclasses.replace(profile, day_cursor=(day_index + 1) % len(plan.days))
    await profiles.save(profile)
    return web.json_response({"ok": True, "records": len(records)})


async def api_trial(request: web.Request) -> web.Response:
    user_id = _user_id(request)
    assert user_id is not None
    try:
        await subs.start_trial(user_id)
    except subs.TrialAlreadyUsed:
        return web.json_response(
            {"error": "trial used", "message": "Пробный период уже был"},
            status=400,
        )
    return web.json_response({"ok": True})


async def api_profile_save(request: web.Request) -> web.Response:
    user_id = _user_id(request)
    assert user_id is not None
    data = await request.json()
    existing = await profiles.get(user_id)

    try:
        height = int(data.get("height", 0))
        weight = int(data.get("weight", 0))
        days = int(data.get("days", 0))
        minutes = int(data.get("minutes", 0))
        age = int(data.get("age", 0))
    except (TypeError, ValueError):
        return web.json_response({"error": "bad numbers"}, status=400)

    if not 100 <= height <= 250:
        return web.json_response({"error": "height"}, status=400)
    if not 30 <= weight <= 250:
        return web.json_response({"error": "weight"}, status=400)
    if days not in profiles.DAYS_OPTIONS:
        return web.json_response({"error": "days"}, status=400)
    if minutes not in profiles.MINUTES_OPTIONS:
        return web.json_response({"error": "minutes"}, status=400)
    if age and not 14 <= age <= 90:
        return web.json_response({"error": "age"}, status=400)

    goal = str(data.get("goal", "tone"))
    experience = str(data.get("experience", "novice"))
    equipment = str(data.get("equipment", "gym"))
    if goal not in profiles.GOALS:
        return web.json_response({"error": "goal"}, status=400)
    if experience not in profiles.EXPERIENCE:
        return web.json_response({"error": "experience"}, status=400)
    if equipment not in profiles.EQUIPMENT:
        return web.json_response({"error": "equipment"}, status=400)

    activity = str(data.get("activity", "")) or (existing.activity if existing else "")
    if activity and activity not in profiles.ACTIVITY:
        return web.json_response({"error": "activity"}, status=400)

    sex = str(data.get("sex", "")) or (existing.sex if existing else "")
    if sex and sex not in ("m", "f"):
        return web.json_response({"error": "sex"}, status=400)

    limits = data.get("limitations")
    if isinstance(limits, list):
        limitations = ",".join(str(x) for x in limits if x in profiles.LIMITATIONS)
    else:
        limitations = existing.limitations if existing else ""

    profile = profiles.Profile(
        user_id=user_id, height=height, weight=weight,
        experience=experience, days=days, minutes=minutes,
        goal=goal, equipment=equipment, limitations=limitations,
        sex=sex, age=age, activity=activity,
        workout_days=existing.workout_days if existing else "",
        workout_time=existing.workout_time if existing else "",
        last_reminder=existing.last_reminder if existing else "",
        day_cursor=0,
    )
    await profiles.save(profile)
    return web.json_response({"ok": True})


async def _progress_payload(user_id: int, period: int = 30) -> dict:
    rows = await progress.history(user_id, 28)
    grouped: dict = {}
    for row in rows:
        grouped.setdefault(row["slug"], []).append(row)

    items = []
    for slug, history in grouped.items():
        ex = exercises.by_slug(slug)
        if ex is None:
            continue
        first = float(history[0]["weight"])
        last = float(history[-1]["weight"])
        items.append({
            "slug": slug, "name": ex.name_ru,
            "first": first, "last": last, "delta": round(last - first, 1),
            "last_date": history[-1]["done_on"],
        })
    items.sort(key=lambda x: x["delta"], reverse=True)

    # Ряды для графиков: по три упражнения с историей
    series = []
    for item in items[:3]:
        if item["last"] <= 0:
            continue
        points = await progress.series(user_id, item["slug"], max(7, period))
        series.append({
            "slug": item["slug"],
            "name": item["name"],
            "points": [
                {"date": p["done_on"], "weight": float(p["weight"])} for p in points
            ],
        })

    weak = await progress.weak_groups(user_id)
    breakdown = await progress.breakdown(user_id)
    history = await progress.history_weeks(user_id, 8)
    return {
        "count": await progress.count(user_id),
        "experience": history["experience"],
        "streak": history["streak"],
        "items": items[:12],
        "series": series,
        "weak_groups": [{"group": group, "count": count} for group, count in weak[:5]],
        "breakdown": breakdown,
    }


async def api_progress(request: web.Request) -> web.Response:
    user_id = _user_id(request)
    assert user_id is not None
    if not await subs.has_access(user_id):
        return web.json_response({"error": "subscription required"}, status=403)
    return web.json_response(await _progress_payload(user_id))


async def api_skip(request: web.Request) -> web.Response:
    user_id = _user_id(request)
    assert user_id is not None
    if not await subs.has_access(user_id):
        return web.json_response({"error": "subscription required"}, status=403)

    data = await request.json()
    try:
        day_index = int(data.get("day_index", 0))
    except (TypeError, ValueError):
        return web.json_response({"error": "bad day"}, status=400)

    profile = await profiles.get(user_id)
    if profile is None:
        return web.json_response({"error": "no profile"}, status=400)
    plan = planner.build(profile)
    if day_index < 0 or day_index >= len(plan.days):
        return web.json_response({"error": "bad day"}, status=400)

    profile = dataclasses.replace(profile, day_cursor=(day_index + 1) % len(plan.days))
    await profiles.save(profile)
    week = await progress.week_index(user_id)
    await progress.set_check(user_id, week, day_index, 0)
    return web.json_response({"ok": True, "next_day": profile.day_cursor})


async def api_feel(request: web.Request) -> web.Response:
    user_id = _user_id(request)
    assert user_id is not None
    if not await subs.has_access(user_id):
        return web.json_response({"error": "subscription required"}, status=403)

    data = await request.json()
    feel = str(data.get("feel", ""))
    if feel not in ("easy", "normal", "hard"):
        return web.json_response({"error": "bad feel"}, status=400)
    try:
        day_index = int(data.get("day_index", 0))
    except (TypeError, ValueError):
        return web.json_response({"error": "bad day"}, status=400)

    profile = await profiles.get(user_id)
    if profile is None:
        return web.json_response({"error": "no profile"}, status=400)
    plan = planner.build(profile)
    if day_index < 0 or day_index >= len(plan.days):
        return web.json_response({"error": "bad day"}, status=400)

    week = await progress.week_index(user_id)
    await progress.set_feel(user_id, week, day_index, feel)
    return web.json_response({"ok": True})


async def api_workout_complete(request: web.Request) -> web.Response:
    """Полное выполнение дня без ввода подходов."""
    user_id = _user_id(request)
    assert user_id is not None
    if not await subs.has_access(user_id):
        return web.json_response({"error": "subscription required"}, status=403)

    data = await request.json()
    try:
        day_index = int(data.get("day_index", 0))
    except (TypeError, ValueError):
        return web.json_response({"error": "bad day"}, status=400)

    profile = await profiles.get(user_id)
    if profile is None:
        return web.json_response({"error": "no profile"}, status=400)
    plan = planner.build(profile)
    if day_index < 0 or day_index >= len(plan.days):
        return web.json_response({"error": "bad day"}, status=400)

    await progress.add_workout(user_id, day_index, plan.days[day_index].title, [])
    profile = dataclasses.replace(profile, day_cursor=(day_index + 1) % len(plan.days))
    await profiles.save(profile)
    state = await progress.week_state(user_id, len(plan.days))
    return web.json_response({
        "ok": True,
        "week": {
            "number": state["week"], "done": state["done"],
            "planned": state["planned"], "deload": state["deload"],
        },
    })


async def api_workout_add(request: web.Request) -> web.Response:
    user_id = _user_id(request)
    assert user_id is not None
    if not await subs.has_access(user_id):
        return web.json_response({"error": "subscription required"}, status=403)

    data = await request.json()
    try:
        day_index = int(data.get("day_index", 0))
    except (TypeError, ValueError):
        return web.json_response({"error": "bad day"}, status=400)
    text = str(data.get("text", ""))
    if len(text) > 2000:
        return web.json_response({"error": "too long"}, status=400)

    profile = await profiles.get(user_id)
    if profile is None:
        return web.json_response({"error": "no profile"}, status=400)

    plan = planner.build(profile)
    if day_index < 0 or day_index >= len(plan.days):
        return web.json_response({"error": "bad day"}, status=400)
    day = plan.days[day_index]
    allowed = {slot.exercise.slug: slot.exercise for slot in day.slots}

    records, errors, touched = [], [], []
    for line in text.splitlines():
        parsed = progress.parse_line(line)
        if parsed is None:
            if line.strip():
                errors.append(f"«{line.strip()[:24]}»: формат «упражнение 60x8»")
            continue
        name, sets = parsed
        ex = exercises.find_by_name_among(name, allowed)
        if ex is None:
            errors.append(f"«{name}» не найден")
            continue
        if ex.slug not in touched:
            touched.append(ex.slug)
        for weight, reps in sets:
            records.append(progress.SetRecord(ex.slug, weight, reps))

    if not records:
        return web.json_response({"added": 0, "errors": errors})

    await progress.add_workout(user_id, day_index, day.title, records)

    week_state = await progress.week_state(user_id, len(plan.days))
    allow_increase = not week_state["deload"] and not week_state["after_deload"]

    advice = []
    for slug in touched:
        ex = exercises.by_slug(slug)
        sets = await progress.last_sets(user_id, slug)
        advice.append({
            "name": ex.name_ru if ex else slug,
            "text": progress.next_advice(
                profile.goal, ex.equipment if ex else "", sets, allow_increase=allow_increase
            ),
        })

    return web.json_response({
        "added": len(records), "errors": errors, "advice": advice,
        "progress": await _progress_payload(user_id),
    })


async def api_check(request: web.Request) -> web.Response:
    user_id = _user_id(request)
    assert user_id is not None
    if not await subs.has_access(user_id):
        return web.json_response({"error": "subscription required"}, status=403)

    data = await request.json()
    try:
        day_index = int(data.get("day_index", 0))
    except (TypeError, ValueError):
        return web.json_response({"error": "bad day"}, status=400)
    status = str(data.get("status", "")).strip()
    if status == "partial":
        value = 2
    elif status == "skip":
        value = 0
    elif status == "done":
        value = 1
    else:
        value = 1 if data.get("done") else 0

    profile = await profiles.get(user_id)
    if profile is None:
        return web.json_response({"error": "no profile"}, status=400)
    plan = planner.build(profile)
    if day_index < 0 or day_index >= len(plan.days):
        return web.json_response({"error": "bad day"}, status=400)

    week = await progress.week_index(user_id)
    await progress.set_check(user_id, week, day_index, value)

    slugs = data.get("slugs") or []
    if isinstance(slugs, list) and len(slugs) > 20:
        slugs = slugs[:20]
    reason = str(data.get("reason", "")).strip()[:32]
    if value == 2 and isinstance(slugs, list) and slugs:
        await progress.add_failures(
            user_id, week, day_index,
            [(str(slug), reason or "unknown") for slug in slugs],
        )
    state = await progress.week_state(user_id, len(plan.days))
    return web.json_response({
        "week": {
            "number": state["week"], "done": state["done"], "planned": state["planned"],
            "deload": state["deload"], "after_deload": state["after_deload"],
            "partial": state["partial"], "reasons": state["reasons"],
        }
    })


async def api_exercises(request: web.Request) -> web.Response:
    user_id = _user_id(request)
    access = bool(user_id) and await subs.has_access(user_id)
    groups = []
    for group in exercises.groups():
        items = exercises.by_group(group)
        groups.append({
            "group": group,
            "items": [
                {"slug": e.slug, "name": e.name_ru, "free": e.free,
                 "level": e.level, "locked": not e.free and not access}
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
    if not await subs.has_access(user_id):
        return web.json_response({"error": "subscription required"}, status=403)
    data = await request.json()
    text = str(data.get("text", "")).strip()
    if not text:
        return web.json_response({"error": "empty"}, status=400)
    if len(text) > 1000:
        return web.json_response({"error": "too long"}, status=400)

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
    if not await subs.has_access(user_id):
        return web.json_response({"error": "subscription required"}, status=403)
    await diary.clear(user_id)
    return web.json_response({"ok": True})


async def index(request: web.Request) -> web.StreamResponse:
    response = web.FileResponse(WEBAPP_DIR / "index.html")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


def create_app(settings: Settings) -> web.Application:
    # telegram_auth первым: лимит считается по пользователю из подписи
    app = web.Application(middlewares=[telegram_auth, rate_limit, no_cache])
    app["bot_token"] = settings.telegram_token
    app.router.add_get("/", index)
    app.router.add_static("/static/", WEBAPP_DIR, show_index=False)
    # ВАЖНО: корень media, а не media/exercises — иначе /media/exercises/<slug>/0.jpg
    # превращается в media/exercises/exercises/...
    app.router.add_static("/media/", exercises.MEDIA_DIR.parent, show_index=False)
    app.router.add_get("/api/overview", api_overview)
    app.router.add_get("/api/exercises", api_exercises)
    app.router.add_get("/api/progress", api_progress)
    app.router.add_post("/api/workouts", api_workout_add)
    app.router.add_post("/api/workouts/complete", api_workout_complete)
    app.router.add_post("/api/feel", api_feel)
    app.router.add_post("/api/skip", api_skip)
    app.router.add_post("/api/profile", api_profile_save)
    app.router.add_post("/api/trial", api_trial)
    app.router.add_post("/api/reminders", api_reminders)
    app.router.add_get("/api/alternatives/{slug}", api_alternatives)
    app.router.add_post("/api/workouts/custom", api_workout_custom)
    app.router.add_post("/api/checks", api_check)
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
