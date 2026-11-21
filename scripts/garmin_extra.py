#!/usr/bin/env python3
"""
garmin_extra.py — dati che ogni sincronizzazione rilegge interi da Garmin
─────────────────────────────────────────────────────────────────────────
Scarpe e foto (data/gear.json, data/gear/) e salute cifrata
(data/salute.enc.json: sonno, HRV, passi, zone FC, previsioni gara).
Sonno e passi si accumulano: lo storico resta tutto, si riscaricano solo gli
ultimi giorni (e una volta sola tutto da GARMIN_START, se manca).
Se Garmin non risponde restano i dati gia' salvati.
"""

import io
import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from PIL import Image, ImageOps

import gps_vault

log = logging.getLogger(__name__)

SALUTE_RELATIVE_PATH = Path("data") / "salute.enc.json"
GEAR_RELATIVE_PATH = Path("data") / "gear.json"
PHOTOS_RELATIVE_PATH = Path("data") / "gear"
PHOTO_MAX_PX = 720
SLEEP_DAYS = 14   # notti riscaricate a ogni giro (Garmin a volte le corregge)
HISTORY_DAYS = 56  # giorni riscaricati per HRV e passi
GARMIN_START = date(2026, 3, 5)  # primo giorno con l'orologio Garmin


def _profile_pk(client, activities):
    for record in activities:
        if record.get("gear_name"):
            gear = client.get_activity_gear(record["garmin_id"])
            if gear:
                return gear[0]["userProfilePk"]
    raise RuntimeError("nessuna corsa con una scarpa da cui leggere il profilo")


def _save_photo(client, uuid, repo_path):
    """Scarica la foto, toglie i metadati e la salva solo se e' cambiata.

    Gli errori non riportano il testo dell'eccezione: conterrebbe l'URL firmato.
    Ritorna il percorso per la dashboard, o None se la scarpa non ha foto.
    """
    out = Path(repo_path) / PHOTOS_RELATIVE_PATH / f"{uuid[:8]}.jpg"
    try:
        dashed = f"{uuid[:8]}-{uuid[8:12]}-{uuid[12:16]}-{uuid[16:20]}-{uuid[20:]}"
        images = client.connectapi(f"/gear-service/gear/v2/{dashed}").get("images") or []
        url = images[0].get("mediumUrl") or images[0].get("url") if images else None
        if not url and out.exists():
            out.unlink()  # foto tolta su Garmin: si toglie anche qui
            log.info("  Foto scarpa tolta: %s", out.name)
        if url:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            photo = ImageOps.exif_transpose(Image.open(io.BytesIO(response.content))).convert("RGB")
            photo.thumbnail((PHOTO_MAX_PX, PHOTO_MAX_PX))
            buffer = io.BytesIO()
            photo.save(buffer, "JPEG", quality=82, optimize=True)
            if not out.exists() or out.read_bytes() != buffer.getvalue():
                out.parent.mkdir(exist_ok=True)
                out.write_bytes(buffer.getvalue())
                log.info("  Foto scarpa aggiornata: %s", out.name)
    except Exception as e:
        log.warning("  Foto scarpa %s non aggiornata (%s)", uuid[:8], type(e).__name__)
    return f"./{out.relative_to(repo_path).as_posix()}" if out.exists() else None


def refresh_gear(client, data, repo_path):
    """Riscrive gear.json e la scarpa di ogni corsa con i dati attuali di Garmin."""
    repo_path = Path(repo_path)
    activities = data.get("activities", [])
    shoes = []
    by_activity = {}
    gear_items = client.get_gear(_profile_pk(client, activities))
    if not gear_items:
        raise RuntimeError("Garmin non ha restituito scarpe")

    for item in gear_items:
        uuid = item["uuid"]
        name = item.get("customMakeModel") or item.get("displayName") or item.get("gearModelName")
        stats = client.get_gear_stats(uuid)
        runs = sorted(
            (a for a in client.get_gear_activities(uuid, limit=1000) if a.get("startTimeLocal")),
            key=lambda a: a["startTimeLocal"],
        )
        km = 0.0
        for run in runs:
            km += (run.get("distance") or 0) / 1000
            by_activity[run["activityId"]] = (name, round(km, 1))
        shoes.append({
            "name": name,
            "label": item.get("displayName"),
            "km": round((stats.get("totalDistance") or 0) / 1000, 1),
            "activities": stats.get("totalActivities"),
            "max_km": round(item["maximumMeters"] / 1000) if item.get("maximumMeters") else None,
            "first_used": runs[0]["startTimeLocal"][:10] if runs else None,
            "last_used": runs[-1]["startTimeLocal"][:10] if runs else None,
            "photo": _save_photo(client, uuid, repo_path),
            "runs": [{
                "date": run["startTimeLocal"][:10],
                "name": run.get("activityName"),
                "km": round((run.get("distance") or 0) / 1000, 2),
            } for run in reversed(runs)],
            "status": item.get("gearStatusName") or "active",
        })
    shoes.sort(key=lambda s: (s["last_used"] or "", s["km"]), reverse=True)

    # Scarpe cancellate su Garmin: via anche la loro foto dal repo.
    keep = {Path(s["photo"]).name for s in shoes if s["photo"]}
    for old in (repo_path / PHOTOS_RELATIVE_PATH).glob("*.jpg"):
        if old.name not in keep:
            old.unlink()
            log.info("  Foto di una scarpa non piu' su Garmin tolta: %s", old.name)

    for record in activities:
        record["gear_name"], record["gear_km"] = by_activity.get(record.get("garmin_id"), (None, None))

    path = repo_path / GEAR_RELATIVE_PATH
    text = json.dumps(shoes, ensure_ascii=False, indent=2)
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")
    log.info("Scarpe aggiornate da Garmin: %d", len(shoes))
    return by_activity


def all_runs(client, by_activity):
    """Tutte le corse su Garmin, anche quelle piu' vecchie della dashboard,
    con la loro scarpa: servono per far coincidere Strava, km compresi."""
    return [{
        "date": a["startTimeLocal"][:10],
        "start_time": a["startTimeLocal"][:19],
        "gear_name": by_activity.get(a.get("activityId"), (None, None))[0],
        "distance_km": round((a.get("distance") or 0) / 1000, 2),
    } for a in client.get_activities_by_date("2000-01-01", None, "running") if a.get("startTimeLocal")]


def _hours(seconds):
    return round((seconds or 0) / 3600, 2)


def _clock(local_ms):
    # I timestamp "Local" di Garmin contengono gia' l'ora locale.
    return datetime.fromtimestamp(local_ms / 1000, timezone.utc).strftime("%H:%M")


def _fetch_sleep(client, today, since=None):
    nights = []
    days = (today - since).days + 1 if since else SLEEP_DAYS
    for offset in range(days - 1, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        night = client.get_sleep_data(day) or {}
        dto = night.get("dailySleepDTO") or {}
        if not dto.get("sleepTimeSeconds") or not dto.get("sleepStartTimestampLocal"):
            continue
        need = dto.get("sleepNeed") or night.get("sleepNeed") or {}
        nights.append({
            "date": day,
            "bed": _clock(dto["sleepStartTimestampLocal"]),
            "wake": _clock(dto["sleepEndTimestampLocal"]),
            "h": _hours(dto["sleepTimeSeconds"]),
            "deep": _hours(dto.get("deepSleepSeconds")),
            "light": _hours(dto.get("lightSleepSeconds")),
            "rem": _hours(dto.get("remSleepSeconds")),
            "awake": _hours(dto.get("awakeSleepSeconds")),
            "score": ((dto.get("sleepScores") or {}).get("overall") or {}).get("value"),
            "need": round((need.get("actual") or 0) / 60, 2),
        })
    return nights


def _fetch_hrv(client, today):
    start = (today - timedelta(days=HISTORY_DAYS - 1)).isoformat()
    summaries = client.connectapi(f"/hrv-service/hrv/daily/{start}/{today.isoformat()}")["hrvSummaries"]
    rows = [{"date": h["calendarDate"], "hrv": h["lastNightAvg"], "avg7": h["weeklyAvg"]}
            for h in summaries if h.get("lastNightAvg")]
    baseline = summaries[-1].get("baseline") or {}
    hrv_range = [baseline["balancedLow"], baseline["balancedUpper"]] if baseline.get("balancedLow") else None
    return rows, hrv_range


def _fetch_steps(client, today, since=None):
    # Oggi e' ancora in corso: si legge solo fino a ieri.
    end = today - timedelta(days=1)
    start = since or end - timedelta(days=HISTORY_DAYS - 1)
    return [{"date": r["calendarDate"], "steps": r["totalSteps"], "goal": r.get("stepGoal")}
            for r in client.get_daily_steps(start.isoformat(), end.isoformat())
            if r.get("totalSteps") is not None]


def _fetch_zones(client):
    zones = client.connectapi("/biometric-service/heartRateZones")
    running = next(z for z in zones if z.get("sport") == "RUNNING")
    return {
        "fcMax": running["maxHeartRateUsed"],
        "rest": running["restingHeartRateUsed"],
        "floors": [running[f"zone{i}Floor"] for i in range(1, 6)],
    }


def _fetch_race_predictions(client):
    # Tempi previsti da Garmin per 5 km, 10 km, mezza e maratona (secondi).
    data = client.get_race_predictions()
    if isinstance(data, list):
        data = data[-1] if data else {}
    keys = {"5k": "time5K", "10k": "time10K", "21k": "timeHalfMarathon", "42k": "timeMarathon"}
    pred = {k: data.get(v) for k, v in keys.items() if data.get(v)}
    if pred:
        pred["date"] = data.get("calendarDate")
    return pred


def _merge_by_date(old_rows, new_rows, keep=None):
    merged = {row["date"]: row for row in old_rows or []}
    merged.update({row["date"]: row for row in new_rows})
    days = sorted(merged)
    return [merged[d] for d in (days[-keep:] if keep else days)]


def _since(rows):
    # Se lo storico non arriva a GARMIN_START, la prima volta si scarica tutto.
    first = min((r["date"] for r in rows or []), default=None)
    return GARMIN_START if not first or first > GARMIN_START.isoformat() else None


def refresh_salute(client, repo_path, password):
    """Aggiorna data/salute.enc.json. I dati in chiaro restano solo in memoria."""
    path = Path(repo_path) / SALUTE_RELATIVE_PATH
    previous = gps_vault.load_payload(path, password) or {}
    salute = dict(previous)
    today = date.today()

    def update(label, action):
        try:
            action()
        except Exception as e:
            log.warning("%s non aggiornato, restano i dati precedenti: %s", label, e)

    def sleep():
        salute["sleep"] = _merge_by_date(salute.get("sleep"), _fetch_sleep(client, today, _since(salute.get("sleep"))))

    def hrv():
        rows, hrv_range = _fetch_hrv(client, today)
        salute["hrv"] = _merge_by_date(salute.get("hrv"), rows)
        if hrv_range:
            salute["hrvRange"] = hrv_range

    def steps():
        salute["steps"] = _merge_by_date(salute.get("steps"), _fetch_steps(client, today, _since(salute.get("steps"))))

    def zones():
        salute["garminZones"] = _fetch_zones(client)

    def races():
        pred = _fetch_race_predictions(client)
        if pred:
            salute["racePredictions"] = pred

    update("Sonno", sleep)
    update("HRV", hrv)
    update("Passi", steps)
    update("Zone FC", zones)
    update("Previsioni gara", races)

    if salute != previous:
        salute["updated"] = today.isoformat()
        gps_vault.write_payload(path, salute, password)
        log.info("Salute cifrata aggiornata.")
    else:
        log.info("Salute: nessuna novita'.")
