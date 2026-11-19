"""
strava_sync.py — mette su Strava le stesse scarpe di Garmin
────────────────────────────────────────────────────────────
Garmin e' l'unica fonte. A ogni download:
1. per ogni corsa Garmin trova la stessa corsa su Strava (stessa ora di
   partenza) e, se la scarpa e' diversa, mette quella di Garmin (o nessuna);
2. confronta l'elenco delle scarpe e scrive in data/strava_status.json cosa
   va fatto a mano su Strava: creare o ritirare una scarpa non si puo' fare
   dall'esterno, Strava non lo permette.

Le scarpe si abbinano per nome: su Strava ogni scarpa deve chiamarsi come su
Garmin (il nome grande della dashboard).

Serve un'app Strava con permessi read, activity:read_all, activity:write,
profile:read_all. Variabili: STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET,
STRAVA_REFRESH_TOKEN; con STRAVA_TOKEN_OUT il token rinnovato si salva li'.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

API = "https://www.strava.com/api/v3"
STATUS_RELATIVE_PATH = Path("data") / "strava_status.json"
RUN_TYPES = {"Run", "TrailRun", "VirtualRun"}
MATCH_SECONDS = 120   # tolleranza sull'ora di partenza tra Garmin e Strava
MAX_UPDATES = 80      # Strava concede ~200 richieste ogni 15 minuti: il resto al giro dopo
KM_TOLERANCE = 0.5


def configured():
    # Gira solo dove il token rinnovato viene salvato (l'azione su GitHub):
    # Strava invalida i token vecchi, due copie diverse si rovinerebbero a vicenda.
    keys = ("STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET", "STRAVA_REFRESH_TOKEN", "STRAVA_TOKEN_OUT")
    return all(os.environ.get(k) for k in keys)


def _norm(name):
    return " ".join(str(name or "").split()).casefold()


def _access_token():
    r = requests.post("https://www.strava.com/oauth/token", data={
        "client_id": os.environ["STRAVA_CLIENT_ID"],
        "client_secret": os.environ["STRAVA_CLIENT_SECRET"],
        "refresh_token": os.environ["STRAVA_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }, timeout=30)
    if not r.ok:
        raise RuntimeError(f"login Strava fallito ({r.status_code})")
    data = r.json()
    out = os.environ.get("STRAVA_TOKEN_OUT", "").strip()
    if out and data.get("refresh_token"):
        Path(out).write_text(data["refresh_token"], encoding="utf-8")
    return data["access_token"]


def _parse_local(value):
    # Garmin: "2026-11-17 19:30:54"; Strava start_date_local: "2026-11-17T19:30:54Z"
    return datetime.fromisoformat(value.replace("Z", "").replace("T", " "))


def _strava_runs(session, after):
    runs, page = [], 1
    while True:
        r = session.get(f"{API}/athlete/activities",
                        params={"after": after, "per_page": 200, "page": page}, timeout=30)
        r.raise_for_status()
        batch = r.json()
        runs += [a for a in batch if (a.get("sport_type") or a.get("type")) in RUN_TYPES]
        if len(batch) < 200:
            return runs
        page += 1


def sync(activities, repo_path, dry_run=False):
    """Allinea Strava a Garmin. Ritorna il numero di corse cambiate su Strava.
    Con dry_run non cambia niente e ritorna (cose da fare, corse da cambiare)."""
    repo_path = Path(repo_path)
    shoes = json.loads((repo_path / "data" / "gear.json").read_text(encoding="utf-8"))

    session = requests.Session()
    session.headers["Authorization"] = "Bearer " + _access_token()

    athlete = session.get(f"{API}/athlete", timeout=30)
    athlete.raise_for_status()
    strava_shoes = athlete.json().get("shoes") or []
    by_name = {}
    for s in strava_shoes:
        by_name.setdefault(_norm(s.get("name")), s)

    todo = []

    # 1) Elenco scarpe: cosa manca o e' diverso su Strava
    garmin_to_strava = {}
    matched_ids = set()
    for g in shoes:
        s = by_name.get(_norm(g["name"])) or by_name.get(_norm(g.get("label")))
        if not s:
            extra = " e poi ritirala" if g.get("status") == "retired" else ""
            todo.append(f"Crea una scarpa chiamata «{g['name']}»{extra}")
            continue
        garmin_to_strava[g["name"]] = s
        matched_ids.add(s["id"])
        retired_strava = bool(s.get("retired"))
        if g.get("status") == "retired" and not retired_strava:
            todo.append(f"Ritira «{g['name']}»")
        elif g.get("status") != "retired" and retired_strava:
            todo.append(f"Riattiva «{g['name']}»")
    for s in strava_shoes:
        if s["id"] not in matched_ids and not s.get("retired"):
            todo.append(f"«{s.get('name')}» c'è solo su Strava: rinominala come su Garmin, oppure ritirala")

    # 2) Scarpa di ogni corsa
    garmin_runs = [a for a in activities if a.get("start_time")]
    if not garmin_runs:
        return 0
    # Tutte le corse su Strava, anche piu' vecchie di Garmin: se hanno una
    # scarpa, contano nei km su Strava e vanno segnalate.
    strava_runs = _strava_runs(session, 0)

    used = set()
    to_update, not_on_strava = [], []
    for a in garmin_runs:
        start = _parse_local(a["start_time"])
        best = None
        for s in strava_runs:
            if s["id"] in used:
                continue
            delta = abs((_parse_local(s["start_date_local"]) - start).total_seconds())
            if delta <= MATCH_SECONDS and (best is None or delta < best[0]):
                best = (delta, s)
        if not best:
            not_on_strava.append(a["date"])
            continue
        s = best[1]
        used.add(s["id"])
        if a.get("gear_name"):
            shoe = garmin_to_strava.get(a["gear_name"])
            if not shoe:
                continue  # scarpa non ancora creata su Strava: gia' in "todo"
            wanted = shoe["id"]
        else:
            wanted = None
        if (s.get("gear_id") or None) != wanted:
            to_update.append((s["id"], wanted))

    # Corse solo su Strava ma con una scarpa: Garmin non le conosce, quindi
    # non si toccano, ma fanno salire i km della scarpa su Strava.
    strava_only = [s for s in strava_runs if s["id"] not in used and s.get("gear_id")]
    if strava_only:
        esempi = ", ".join(sorted((s["start_date_local"][:10] for s in strava_only), reverse=True)[:3])
        quante = ("1 corsa c'è solo su Strava ma ha" if len(strava_only) == 1
                  else f"{len(strava_only)} corse ci sono solo su Strava ma hanno")
        todo.append(f"{quante} una scarpa (es. {esempi}): contano nei km su Strava e non su Garmin")

    if dry_run:
        return todo, to_update, not_on_strava, strava_only, strava_runs, garmin_to_strava

    changed = 0
    for strava_id, wanted in to_update[:MAX_UPDATES]:
        r = session.put(f"{API}/activities/{strava_id}", json={"gear_id": wanted or "none"}, timeout=30)
        if r.status_code == 429:
            log.warning("Strava: limite di richieste raggiunto, il resto al prossimo download")
            break
        if not r.ok:
            log.warning("Strava: scarpa non cambiata su una corsa (%s)", r.status_code)
            continue
        changed += 1
    left = len(to_update) - changed
    if left > 0:
        todo.append(f"Restano {left} corse da sistemare: premi di nuovo Scarica da Garmin tra 15 minuti")

    if not_on_strava:
        esempi = ", ".join(sorted(not_on_strava, reverse=True)[:3])
        quante = "1 corsa di Garmin non è" if len(not_on_strava) == 1 else f"{len(not_on_strava)} corse di Garmin non sono"
        todo.append(f"{quante} su Strava (es. {esempi}): i km delle scarpe su Strava saranno più bassi")
    elif not to_update and not strava_only:
        # Tutte le corse coincidono: allora devono coincidere anche i km.
        for g in shoes:
            s = garmin_to_strava.get(g["name"])
            if s and abs((s.get("distance") or 0) / 1000 - g["km"]) > KM_TOLERANCE:
                todo.append(f"«{g['name']}»: Garmin {g['km']:.1f} km, Strava {(s.get('distance') or 0) / 1000:.1f} km. "
                            "Controlla i km iniziali della scarpa su Garmin e su Strava")

    status = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "changed": changed,
        "todo": todo,
    }
    path = repo_path / STATUS_RELATIVE_PATH
    path.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log.info("Strava: %d corse aggiornate, %d cose da fare a mano", changed, len(todo))
    return changed
