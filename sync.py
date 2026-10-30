import os
import sys
import time
from datetime import datetime, timezone

import requests

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
DATABASE_ID = os.environ["DATABASE_ID"]
ANILIST_TOKEN = os.environ["ANILIST_TOKEN"]
CALENDAR_DB_ID = os.environ["CALENDAR_DB_ID"]

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}
ANILIST_URL = "https://graphql.anilist.co"
ANILIST_HEADERS = {
    "Authorization": f"Bearer {ANILIST_TOKEN}",
    "Content-Type": "application/json",
}

STATUTS = ["CURRENT", "COMPLETED", "PAUSED", "DROPPED", "PLANNING", "REPEATING"]


# ------------------------------------------------------------ utilitaires

def request_with_retry(method, url, **kwargs):
    for _ in range(6):
        r = requests.request(method, url, timeout=30, **kwargs)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", 30)) + 1)
            continue
        if r.status_code >= 500:
            time.sleep(3)
            continue
        r.raise_for_status()
        return r
    raise RuntimeError(f"Trop de tentatives sur {url}")


def fuzzy_to_iso(d):
    if not d or not d.get("year"):
        return None
    return f"{d['year']:04d}-{(d.get('month') or 1):02d}-{(d.get('day') or 1):02d}"


def iso_to_fuzzy(s):
    if not s:
        return {"year": None, "month": None, "day": None}
    y, m, d = s[:10].split("-")
    return {"year": int(y), "month": int(m), "day": int(d)}


# ---------------------------------------------------------------- AniList

def anilist(query, variables=None):
    r = request_with_retry(
        "POST",
        ANILIST_URL,
        json={"query": query, "variables": variables or {}},
        headers=ANILIST_HEADERS,
    )
    data = r.json()
    if "errors" in data:
        raise RuntimeError(data["errors"])
    return data["data"]


def get_viewer_id():
    return anilist("query { Viewer { id } }")["Viewer"]["id"]


LIST_QUERY = """
query ($userId: Int) {
  MediaListCollection(userId: $userId, type: ANIME) {
    lists {
      entries {
        mediaId
        status
        score(format: POINT_10)
        progress
        updatedAt
        startedAt { year month day }
        completedAt { year month day }
        media {
          title { romaji english }
          nextAiringEpisode { airingAt episode }
        }
      }
    }
  }
}
"""


def get_anilist_list(user_id):
    data = anilist(LIST_QUERY, {"userId": user_id})
    entries = {}
    for lst in data["MediaListCollection"]["lists"]:
        for e in lst["entries"]:
            entries[e["mediaId"]] = e
    return entries


SAVE_MUTATION = """
mutation ($mediaId: Int, $status: MediaListStatus, $score: Float,
          $progress: Int, $start: FuzzyDateInput, $end: FuzzyDateInput) {
  SaveMediaListEntry(mediaId: $mediaId, status: $status, scoreRaw: $score,
                     progress: $progress, startedAt: $start, completedAt: $end) {
    id
  }
}
"""


def save_anilist(media_id, status, score, progress, start, end):
    anilist(
        SAVE_MUTATION,
        {
            "mediaId": media_id,
            "status": status,
            "score": float((score or 0) * 10),
            "progress": int(progress or 0),
            "start": start,
            "end": end,
        },
    )


# ----------------------------------------------------------------- Notion

def notion_query_all():
    pages, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = request_with_retry(
            "POST",
            f"https://api.notion.com/v1/databases/{DATABASE_ID}/query",
            headers=NOTION_HEADERS,
            json=body,
        )
        data = r.json()
        pages.extend(data["results"])
        if not data.get("has_more"):
            return pages
        cursor = data["next_cursor"]


def notion_update(page_id, props):
    request_with_retry(
        "PATCH",
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=NOTION_HEADERS,
        json={"properties": props},
    )


def notion_create(props):
    request_with_retry(
        "POST",
        "https://api.notion.com/v1/pages",
        headers=NOTION_HEADERS,
        json={"parent": {"database_id": DATABASE_ID}, "properties": props},
    )


# --------------------------------------------------------------- Calendrier

def calendar_existing_names():
    names, cursor = set(), None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = request_with_retry(
            "POST",
            f"https://api.notion.com/v1/databases/{CALENDAR_DB_ID}/query",
            headers=NOTION_HEADERS,
            json=body,
        )
        data = r.json()
        for p in data["results"]:
            t = p["properties"].get("Nom", {}).get("title") or []
            names.add("".join(x["plain_text"] for x in t))
        if not data.get("has_more"):
            return names
        cursor = data["next_cursor"]


def calendar_log(page_id, title, episode, when_iso, existing):
    name = f"{title} – Ép. {episode}"
    if name in existing:
        return False
    request_with_retry(
        "POST",
        "https://api.notion.com/v1/pages",
        headers=NOTION_HEADERS,
        json={
            "parent": {"database_id": CALENDAR_DB_ID},
            "properties": {
                "Nom": {"title": [{"text": {"content": name}}]},
                "Date": {"date": {"start": when_iso}},
                "Animes trackers": {"relation": [{"id": page_id}]},
            },
        },
    )
    existing.add(name)
    time.sleep(0.35)
    return True


# ------------------------------------------------------------ lecture / écriture

def read_page(page):
    p = page["properties"]

    def text(name):
        arr = (p.get(name) or {}).get("title") or (p.get(name) or {}).get("rich_text") or []
        return "".join(t["plain_text"] for t in arr)

    def number(name):
        return (p.get(name) or {}).get("number")

    def select(name):
        s = (p.get(name) or {}).get("select") or (p.get(name) or {}).get("status")
        return s["name"] if s else None

    def date(name):
        d = (p.get(name) or {}).get("date")
        return d["start"][:10] if d and d.get("start") else None

    return {
        "id": page["id"],
        "edited": datetime.fromisoformat(page["last_edited_time"].replace("Z", "+00:00")),
        "title": text("Titre"),
        "anilist_id": number("AniList ID"),
        "status": select("Statut"),
        "score": number("Score"),
        "episode": number("Épisode"),
        "start": date("Date de début"),
        "end": date("Date de fin"),
        "has_next": date("Prochaine sortie") is not None,
    }


def airing_props(media):
    nxt = (media or {}).get("nextAiringEpisode")
    if not nxt:
        return {
            "Prochaine sortie": {"date": None},
            "Temps restant": {"rich_text": []},
        }
    ts = nxt["airingAt"]
    secs = max(0, ts - int(time.time()))
    d, h, m = secs // 86400, (secs % 86400) // 3600, (secs % 3600) // 60
    if d:
        txt = f"Ép. {nxt['episode']} dans {d}j {h}h {m}min"
    else:
        txt = f"Ép. {nxt['episode']} dans {h}h {m}min"
    iso = datetime.fromtimestamp(ts, timezone.utc).isoformat()
    return {
        "Prochaine sortie": {"date": {"start": iso}},
        "Temps restant": {"rich_text": [{"text": {"content": txt}}]},
    }


def props_from_anilist(e, with_title=False):
    props = {
        "AniList ID": {"number": e["mediaId"]},
        "Statut": {"select": {"name": e["status"]}},
        "Score": {"number": e["score"] or None},
        "Épisode": {"number": e["progress"] or 0},
        "Date de début": {"date": {"start": fuzzy_to_iso(e["startedAt"])} if fuzzy_to_iso(e["startedAt"]) else None},
        "Date de fin": {"date": {"start": fuzzy_to_iso(e["completedAt"])} if fuzzy_to_iso(e["completedAt"]) else None},
        "Dernier visionnage": {
            "date": {"start": datetime.fromtimestamp(e["updatedAt"], timezone.utc).isoformat()}
            if e.get("updatedAt") else None
        },
    }
    props.update(airing_props(e.get("media")))
    if with_title:
        t = (e.get("media") or {}).get("title") or {}
        name = t.get("english") or t.get("romaji") or str(e["mediaId"])
        props["Titre"] = {"title": [{"text": {"content": name}}]}
    return props


def needs_update(pg, e):
    nxt = (e.get("media") or {}).get("nextAiringEpisode")
    return (
        pg["status"] != e["status"]
        or (pg["episode"] or 0) != (e["progress"] or 0)
        or (pg["score"] or 0) != (e["score"] or 0)
        or pg["start"] != fuzzy_to_iso(e["startedAt"])
        or pg["end"] != fuzzy_to_iso(e["completedAt"])
        or bool(nxt) != pg["has_next"]
        or bool(nxt)  # les animes en cours de diffusion sont toujours rafraîchis
    )


# ------------------------------------------------------------------- main

def main():
    user_id = get_viewer_id()
    al = get_anilist_list(user_id)
    pages = [read_page(p) for p in notion_query_all()]
    by_id = {p["anilist_id"]: p for p in pages if p["anilist_id"]}
    print(f"AniList : {len(al)} entrées | Notion : {len(pages)} lignes")

    existing = calendar_existing_names()
    errors = updated = created = pushed = logged = 0

    for mid, entry in al.items():
        label = ((entry.get("media") or {}).get("title") or {}).get("romaji", mid)
        try:
            pg = by_id.get(mid)

            # Absent de Notion : création
            if not pg:
                notion_create(props_from_anilist(entry, with_title=True))
                created += 1
                time.sleep(0.35)
                continue

            al_updated = datetime.fromtimestamp(entry["updatedAt"] or 0, timezone.utc)
            notion_diff = (
                pg["status"] != entry["status"]
                or (pg["episode"] or 0) != (entry["progress"] or 0)
                or (pg["score"] or 0) != (entry["score"] or 0)
                or pg["start"] != fuzzy_to_iso(entry["startedAt"])
                or pg["end"] != fuzzy_to_iso(entry["completedAt"])
            )

            # Notion plus récent qu'AniList et différent : Notion -> AniList
            if notion_diff and pg["edited"] > al_updated and pg["status"] in STATUTS:
                save_anilist(
                    mid,
                    pg["status"],
                    pg["score"],
                    int(pg["episode"] or 0),
                    iso_to_fuzzy(pg["start"]),
                    iso_to_fuzzy(pg["end"]),
                )
                pushed += 1

                # Épisode vu augmenté depuis Notion : entrée Calendrier (AVANT la mise à jour locale)
                if int(pg["episode"] or 0) > int(entry["progress"] or 0):
                    if calendar_log(
                        pg["id"],
                        pg["title"],
                        int(pg["episode"]),
                        datetime.now(timezone.utc).isoformat(),
                        existing,
                    ):
                        logged += 1

                # Mise à jour locale (pas de relecture complète de la liste)
                entry["status"] = pg["status"]
                entry["progress"] = int(pg["episode"] or 0)
                entry["score"] = pg["score"] or 0
                entry["startedAt"] = iso_to_fuzzy(pg["start"])
                entry["completedAt"] = iso_to_fuzzy(pg["end"])
                time.sleep(0.7)

            # Épisode vu augmenté depuis AniList : entrée Calendrier
            if (entry["progress"] or 0) > (pg["episode"] or 0) and entry.get("updatedAt"):
                when = datetime.fromtimestamp(entry["updatedAt"], timezone.utc).isoformat()
                if calendar_log(pg["id"], pg["title"], int(entry["progress"]), when, existing):
                    logged += 1

            # AniList -> Notion, seulement si nécessaire
            if needs_update(pg, entry):
                notion_update(pg["id"], props_from_anilist(entry))
                updated += 1
                time.sleep(0.35)
        except Exception as ex:
            errors += 1
            print(f"  ! Erreur sur « {label} » : {ex}")

    print(
        f"Créées : {created} | Mises à jour Notion : {updated} | "
        f"Envoyées à AniList : {pushed} | Calendrier : {logged} | Erreurs : {errors}"
    )
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
