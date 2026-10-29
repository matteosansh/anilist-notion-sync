"""Synchronisation bidirectionnelle AniList <-> Notion.

Variables d'environnement requises :
  NOTION_TOKEN, DATABASE_ID, ANILIST_TOKEN, ANILIST_USERNAME

Colonnes Notion requises :
  Titre (Titre), Statut (Sélection), Score (Nombre), Épisode (Nombre),
  AniList ID (Nombre), Date de début (Date), Date de fin (Date),
  Dernier visionnage (Date)
"""
import os
import sys
import time
from datetime import datetime, timezone

import requests

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
DATABASE_ID = os.environ["DATABASE_ID"]
ANILIST_TOKEN = os.environ["ANILIST_TOKEN"]
ANILIST_USERNAME = os.environ["ANILIST_USERNAME"]

ANILIST_URL = "https://graphql.anilist.co"
AL_HEADERS = {
    "Authorization": f"Bearer {ANILIST_TOKEN}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}
DEDUPE = os.environ.get("DEDUPE_ARCHIVE") == "1"  # 1 = mettre les doublons à la corbeille
VALID_STATUS = {"CURRENT", "COMPLETED", "PAUSED", "DROPPED", "PLANNING", "REPEATING"}


# ---------- Helpers dates ----------
def now_iso():
    return datetime.now(timezone.utc).isoformat()


def al_date_to_iso(d):
    """{'year','month','day'} AniList -> 'YYYY-MM-DD' ou None si incomplet."""
    if d and d.get("year") and d.get("month") and d.get("day"):
        return f"{d['year']}-{d['month']:02d}-{d['day']:02d}"
    return None


def iso_to_al_date(s):
    """'YYYY-MM-DD...' -> dict pour AniList ; dict à None pour effacer la date."""
    if not s:
        return {"year": None, "month": None, "day": None}
    y, m, d = s[:10].split("-")
    return {"year": int(y), "month": int(m), "day": int(d)}


# ---------- Helpers API ----------
def anilist(query, variables=None):
    for _ in range(3):
        r = requests.post(
            ANILIST_URL,
            json={"query": query, "variables": variables or {}},
            headers=AL_HEADERS,
            timeout=30,
        )
        if r.status_code == 429:  # limite de requêtes
            time.sleep(int(r.headers.get("Retry-After", 60)))
            continue
        break
    data = r.json()
    if "errors" in data:
        raise RuntimeError(data["errors"])
    time.sleep(0.7)
    return data["data"]


def notion(method, path, **kwargs):
    for _ in range(3):
        r = requests.request(
            method, f"https://api.notion.com/v1{path}",
            headers=NOTION_HEADERS, timeout=30, **kwargs,
        )
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", 5)))
            continue
        break
    time.sleep(0.2)
    if not r.ok:
        raise RuntimeError(f"Notion {r.status_code}: {r.text}")
    return r.json()


# ---------- Lecture ----------
def fetch_anilist():
    query = """
    query ($userName: String) {
      MediaListCollection(userName: $userName, type: ANIME) {
        lists { entries {
          mediaId status score progress updatedAt
          startedAt { year month day }
          completedAt { year month day }
          media { title { romaji english } coverImage { extraLarge } }
        } }
      }
    }"""
    data = anilist(query, {"userName": ANILIST_USERNAME})
    entries = {}
    for lst in data["MediaListCollection"]["lists"]:
        for e in lst["entries"]:
            t = e["media"]["title"]
            entries[e["mediaId"]] = {
                "id": e["mediaId"],
                "title": t["romaji"] or t["english"] or "Sans titre",
                "status": e["status"],
                "score": e["score"] or 0,
                "progress": e["progress"] or 0,
                "updatedAt": e["updatedAt"] or 0,
                "started": al_date_to_iso(e["startedAt"]),
                "completed": al_date_to_iso(e["completedAt"]),
                "image": e["media"]["coverImage"]["extraLarge"],
            }
    return entries


def fetch_notion_pages():
    pages, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        res = notion("POST", f"/databases/{DATABASE_ID}/query", json=body)
        pages.extend(res["results"])
        if not res["has_more"]:
            return pages
        cursor = res["next_cursor"]


def date_prop(props, name):
    """Lit une colonne Date Notion -> 'YYYY-MM-DD' ou None."""
    d = (props.get(name) or {}).get("date")
    return d["start"][:10] if d and d.get("start") else None


def parse_page(page):
    props = page["properties"]
    ep = props.get("Épisode", {}).get("number")
    return {
        "page_id": page["id"],
        "title": "".join(t["plain_text"] for t in props["Titre"]["title"]).strip(),
        "al_id": props["AniList ID"]["number"],
        "status": (props["Statut"]["select"] or {}).get("name"),
        "score": props["Score"]["number"] or 0,
        "progress": int(ep) if ep is not None else None,  # None = colonne vide
        "started": date_prop(props, "Date de début"),
        "completed": date_prop(props, "Date de fin"),
        "edited": datetime.fromisoformat(
            page["last_edited_time"].replace("Z", "+00:00")
        ).timestamp(),
    }


# ---------- Recherche ----------
def search_media(title):
    try:
        data = anilist(
            "query ($s: String) { Media(search: $s, type: ANIME) { id } }", {"s": title}
        )
        return data["Media"]["id"]
    except RuntimeError:
        return None


# ---------- Écriture ----------
def push_to_anilist(media_id, p, entry=None):
    """Envoie l'état Notion vers AniList. Les champs vides de Notion
    (épisode, dates) reprennent la valeur AniList pour ne rien effacer."""
    status = p["status"] if p["status"] in VALID_STATUS else "PLANNING"
    progress = p["progress"] if p["progress"] is not None else (entry or {}).get("progress", 0)
    started = p["started"] or (entry or {}).get("started")
    completed = p["completed"] or (entry or {}).get("completed")
    anilist(
        """mutation ($id: Int, $status: MediaListStatus, $score: Float, $progress: Int,
                     $startedAt: FuzzyDateInput, $completedAt: FuzzyDateInput) {
             SaveMediaListEntry(mediaId: $id, status: $status, score: $score,
                                progress: $progress, startedAt: $startedAt,
                                completedAt: $completedAt) { id }
           }""",
        {
            "id": media_id,
            "status": status,
            "score": float(p["score"] or 0),
            "progress": int(progress or 0),
            "startedAt": iso_to_al_date(started),
            "completedAt": iso_to_al_date(completed),
        },
    )
    print(f"  -> AniList mis à jour (id {media_id})")


def pull_to_notion(page_id, entry, progress_changed=False):
    props = {
        "Statut": {"select": {"name": entry["status"]}},
        "Score": {"number": entry["score"]},
        "Épisode": {"number": entry["progress"]},
    }
    if entry["started"]:
        props["Date de début"] = {"date": {"start": entry["started"]}}
    if entry["completed"]:
        props["Date de fin"] = {"date": {"start": entry["completed"]}}
    if progress_changed:
        props["Dernier visionnage"] = {"date": {"start": now_iso()}}
    notion("PATCH", f"/pages/{page_id}", json={"properties": props})
    print(f"  -> Notion mis à jour : {entry['title']}")


def create_in_notion(entry):
    props = {
        "Titre": {"title": [{"text": {"content": entry["title"]}}]},
        "Score": {"number": entry["score"]},
        "Épisode": {"number": entry["progress"]},
        "Statut": {"select": {"name": entry["status"]}},
        "AniList ID": {"number": entry["id"]},
        "Dernier visionnage": {"date": {"start": datetime.fromtimestamp(
            entry["updatedAt"], tz=timezone.utc).isoformat()}},
    }
    if entry["started"]:
        props["Date de début"] = {"date": {"start": entry["started"]}}
    if entry["completed"]:
        props["Date de fin"] = {"date": {"start": entry["completed"]}}
    image = {"type": "external", "external": {"url": entry["image"]}}
    notion("POST", "/pages", json={
        "parent": {"database_id": DATABASE_ID},
        "properties": props, "icon": image, "cover": image,
    })
    print(f"  -> Ajouté dans Notion : {entry['title']}")


# ---------- Synchronisation ----------
def same(p, entry):
    """Vrai si Notion et AniList sont déjà alignés (champs vides Notion ignorés)."""
    return (
        p["status"] == entry["status"]
        and p["score"] == entry["score"]
        and (p["progress"] is None or p["progress"] == entry["progress"])
        and (p["started"] is None or p["started"] == entry["started"])
        and (p["completed"] is None or p["completed"] == entry["completed"])
    )


def has_gaps(p, entry):
    """Vrai si Notion a des champs vides qu'AniList peut remplir."""
    return (
        (p["progress"] is None)
        or (p["started"] is None and entry["started"])
        or (p["completed"] is None and entry["completed"])
    )


def main():
    errors = 0
    al = fetch_anilist()
    by_title = {e["title"].lower(): e for e in al.values()}
    raw = fetch_notion_pages()
    pages = [parse_page(p) for p in raw
             if not (p.get("archived") or p.get("in_trash"))]
    print(f"AniList : {len(al)} animés | Notion : {len(pages)} lignes actives")

    # 1. Lignes Notion sans AniList ID : on retrouve l'ID via le titre
    for p in pages:
        if p["al_id"] is not None or not p["title"]:
            continue
        try:
            entry = by_title.get(p["title"].lower())
            media_id = entry["id"] if entry else search_media(p["title"])
            if not media_id:
                print(f"  ! Introuvable sur AniList : {p['title']}")
                continue
            notion("PATCH", f"/pages/{p['page_id']}",
                   json={"properties": {"AniList ID": {"number": media_id}}})
            p["al_id"] = media_id
            print(f"  ID trouvé : {p['title']} -> {media_id}")
        except Exception as e:
            errors += 1
            print(f"  ! Erreur sur « {p['title']} » : {e}")

    # 2. Comparaison ligne par ligne (les doublons sont ignorés)
    seen = set()
    duplicates = 0
    for p in pages:
        mid = p["al_id"]
        if mid is None:
            continue
        if mid in seen:
            duplicates += 1
            if DEDUPE:
                try:
                    notion("PATCH", f"/pages/{p['page_id']}", json={"archived": True})
                except Exception as e:
                    errors += 1
                    print(f"  ! Doublon non supprimé : {e}")
            continue
        seen.add(mid)
        try:
            entry = al.get(mid)
            if entry is None:
                push_to_anilist(mid, p)
                notion("PATCH", f"/pages/{p['page_id']}", json={"properties": {
                    "Dernier visionnage": {"date": {"start": now_iso()}}
                }})
                continue

            if same(p, entry):
                # Alignés : on comble seulement les champs vides côté Notion
                if has_gaps(p, entry):
                    pull_to_notion(p["page_id"], entry)
                continue

            # Le plus récent gagne ; un statut Notion vide n'écrase jamais AniList.
            if not p["status"] or entry["updatedAt"] > p["edited"]:
                progress_changed = p["progress"] != entry["progress"]
                pull_to_notion(p["page_id"], entry, progress_changed=progress_changed)
            else:
                push_to_anilist(mid, p, entry)
                # Progression modifiée dans Notion : on horodate la dernière activité
                if p["progress"] is not None and p["progress"] != entry["progress"]:
                    notion("PATCH", f"/pages/{p['page_id']}", json={"properties": {
                        "Dernier visionnage": {"date": {"start": now_iso()}}
                    }})
        except Exception as e:
            errors += 1
            print(f"  ! Erreur sur « {p['title']} » : {e}")

    if duplicates:
        action = "mis à la corbeille" if DEDUPE else "ignorés (DEDUPE_ARCHIVE=1 pour les supprimer)"
        print(f"{duplicates} doublons {action}")

    # 3. Animés présents sur AniList mais pas dans Notion
    for mid, entry in al.items():
        if mid not in seen:
            try:
                create_in_notion(entry)
            except Exception as e:
                errors += 1
                print(f"  ! Création impossible « {entry['title']} » : {e}")

    print(f"Synchronisation terminée ({errors} erreur(s)).")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    interval = int(os.environ.get("LOOP_SECONDS", "0"))
    max_runtime = int(os.environ.get("MAX_RUNTIME_SECONDS", "0"))  # 0 = sans limite
    if interval:  # mode continu : relance la synchro toutes les N secondes
        t0 = time.time()
        while True:
            try:
                main()
            except SystemExit:
                pass
            except Exception as e:
                print(f"Erreur : {e}")
            if max_runtime and time.time() - t0 + interval > max_runtime:
                break
            time.sleep(interval)
    else:
        main()
