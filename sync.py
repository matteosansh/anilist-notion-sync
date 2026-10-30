import os
import sys
import time
from datetime import datetime, timezone

import requests

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
DATABASE_ID = os.environ["DATABASE_ID"]
ANILIST_TOKEN = os.environ["ANILIST_TOKEN"]

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

# ---------------------------------------------------------------- AniList

def anilist(query, variables=None):
    r = requests.post(
        ANILIST_URL,
        json={"query": query, "variables": variables or {}},
        headers=ANILIST_HEADERS,
        timeout=30,
    )
    if r.status_code == 429:
        time.sleep(int(r.headers.get("Retry-After", 60)))
        return anilist(query, variables)
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(data["errors"])
    return data["data"]


def get_viewer_id():
    return anilist("query { Viewer { id } }")["Viewer"]["id"]


def get_anilist_list(user_id):
    query = """
    query ($userId: Int) {
      MediaListCollection(userId: $userId, type: ANIME) {
        lists {
          entries {
            mediaId
            status
            score(format: POINT_10)
            progress
            startedAt { year month day }
            completedAt { year month day }
            updatedAt
            media {
              title { romaji english }
              nextAiringEpisode { airingAt episode }
            }
          }
        }
      }
    }
    """
    data = anilist(query, {"userId": user_id})
    entries = {}
    for lst in data["MediaListCollection"]["lists"]:
        for e in lst["entries"]:
            entries[e["mediaId"]] = e
    return entries


def save_anilist(media_id, status, score, progress, started, completed):
    mutation = """
    mutation ($mediaId: Int, $status: MediaListStatus, $score: Float,
              $progress: Int, $started: FuzzyDateInput, $completed: FuzzyDateInput) {
      SaveMediaListEntry(mediaId: $mediaId, status: $status, score: $score,
                         progress: $progress, startedAt: $started, completedAt: $completed) {
        id
      }
    }
    """
    anilist(mutation, {
        "mediaId": media_id,
        "status": status,
        "score": score or 0,
        "progress": progress or 0,
        "started": started,
        "completed": completed,
    })


# ------------------------------------------------------------------ Dates

def fuzzy_to_iso(d):
    if not d or not d.get("year"):
        return None
    return f"{d['year']:04d}-{d.get('month') or 1:02d}-{d.get('day') or 1:02d}"


def iso_to_fuzzy(s):
    if not s:
        return {"year": None, "month": None, "day": None}
    dt = datetime.fromisoformat(s[:10])
    return {"year": dt.year, "month": dt.month, "day": dt.day}


# ----------------------------------------------------------------- Notion

def notion_query_all():
    pages, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(
            f"https://api.notion.com/v1/databases/{DATABASE_ID}/query",
            headers=NOTION_HEADERS, json=body, timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        pages += data["results"]
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    return pages


def notion_update(page_id, props):
    r = requests.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=NOTION_HEADERS, json={"properties": props}, timeout=30,
    )
    r.raise_for_status()


def notion_create(props):
    r = requests.post(
        "https://api.notion.com/v1/pages",
        headers=NOTION_HEADERS,
        json={"parent": {"database_id": DATABASE_ID}, "properties": props},
        timeout=30,
    )
    r.raise_for_status()


def read_page(page):
    p = page["properties"]

    def num(name):
        return (p.get(name) or {}).get("number")

    def date(name):
        d = (p.get(name) or {}).get("date")
        return d["start"] if d else None

    def select(name):
        s = (p.get(name) or {}).get("select")
        return s["name"] if s else None

    title = "".join(t["plain_text"] for t in p["Titre"]["title"]) if "Titre" in p else ""
    return {
        "id": page["id"],
        "title": title,
        "anilist_id": num("AniList ID"),
        "status": select("Statut"),
        "score": num("Score"),
        "episode": num("Épisode"),
        "start": date("Date de début"),
        "end": date("Date de fin"),
        "last_watch": date("Dernier visionnage"),
        "edited": datetime.fromisoformat(page["last_edited_time"].replace("Z", "+00:00")),
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


def props_from_anilist(e):
    props = {
        "Statut": {"select": {"name": e["status"]}},
        "Score": {"number": e["score"] or None},
        "Épisode": {"number": e["progress"]},
        "Date de début": {"date": {"start": fuzzy_to_iso(e["startedAt"])} if fuzzy_to_iso(e["startedAt"]) else None},
        "Date de fin": {"date": {"start": fuzzy_to_iso(e["completedAt"])} if fuzzy_to_iso(e["completedAt"]) else None},
    }
    if e.get("updatedAt"):
        iso = datetime.fromtimestamp(e["updatedAt"], timezone.utc).isoformat()
        props["Dernier visionnage"] = {"date": {"start": iso}}
    props.update(airing_props(e.get("media")))
    return props


# ------------------------------------------------------------------- Main

def main():
    user_id = get_viewer_id()
    al = get_anilist_list(user_id)
    pages = [read_page(p) for p in notion_query_all()]
    errors = 0
    seen = set()

    for pg in pages:
        label = pg["title"] or pg["id"]
        try:
            mid = int(pg["anilist_id"]) if pg["anilist_id"] else None
            if not mid:
                continue
            seen.add(mid)
            entry = al.get(mid)

            # Entrée absente d'AniList : on la crée depuis Notion
            if entry is None:
                if pg["status"] in STATUTS:
                    save_anilist(
                        mid, pg["status"], pg["score"], int(pg["episode"] or 0),
                        iso_to_fuzzy(pg["start"]), iso_to_fuzzy(pg["end"]),
                    )
                continue

            al_updated = datetime.fromtimestamp(entry["updatedAt"] or 0, timezone.utc)
            notion_diff = (
                pg["status"] != entry["status"]
                or (pg["episode"] or 0) != (entry["progress"] or 0)
                or (pg["score"] or 0) != (entry["score"] or 0)
            )

            # Notion plus récent que AniList et différent : Notion -> AniList
            if notion_diff and pg["edited"] > al_updated and pg["status"] in STATUTS:
                save_anilist(
                    mid, pg["status"], pg["score"], int(pg["episode"] or 0),
                    iso_to_fuzzy(pg["start"]), iso_to_fuzzy(pg["end"]),
                )
                # Relire pour renvoyer les dates normalisées par AniList
                entry = get_anilist_list(user_id).get(mid, entry)

            # AniList -> Notion (toujours, pour rafraîchir aussi le temps restant)
            notion_update(pg["id"], props_from_anilist(entry))
            time.sleep(0.35)
        except Exception as ex:
            errors += 1
            print(f"  ! Erreur sur « {label} » : {ex}")

    print(f"Synchronisation terminée ({errors} erreur(s))")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
