"""Synchronisation bidirectionnelle AniList <-> Notion.

Variables d'environnement requises :
  NOTION_TOKEN, DATABASE_ID, ANILIST_TOKEN, ANILIST_USERNAME

Colonnes Notion requises :
  Titre (Titre), Statut (Sélection), Score (Nombre), AniList ID (Nombre),
  Date de début (Date), Dernier visionnage (Date)
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
          mediaId status score updatedAt
          startedAt { year month day }
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
                "updatedAt": e["updatedAt"] or 0,
                "started": e["startedAt"],
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


def parse_page(page):
    props = page["properties"]
    return {
        "page_id": page["id"],
        "title": "".join(t["plain_text"] for t in props["Titre"]["title"]).strip(),
        "al_id": props["AniList ID"]["number"],
        "status": (props["Statut"]["select"] or {}).get("name"),
        "score": props["Score"]["number"] or 0,
        "edited": datetime.fromisoformat(
            page["last_edited_time"].replace("Z", "+00:00")
        ).timestamp(),
    }


# ---------- Écriture ----------
def search_media(title):
    try:
        data = anilist(
            "query ($s: String) { Media(search: $s, type: ANIME) { id } }", {"s": title}
        )
        return data["Media"]["id"]
    except RuntimeError:
        return None


def push_to_anilist(media_id, status, score):
    status = status if status in VALID_STATUS else "PLANNING"
    anilist(
        """mutation ($id: Int, $status: MediaListStatus, $score: Float) {
             SaveMediaListEntry(mediaId: $id, status: $status, score: $score) { id }
           }""",
        {"id": media_id, "status": status, "score": float(score or 0)},
    )
    print(f"  -> AniList mis à jour (id {media_id})")


def pull_to_notion(page_id, entry):
    notion("PATCH", f"/pages/{page_id}", json={"properties": {
        "Statut": {"select": {"name": entry["status"]}},
        "Score": {"number": entry["score"]},
    }})
    print(f"  -> Notion mis à jour : {entry['title']}")


def create_in_notion(entry):
    s = entry["started"] or {}
    props = {
        "Titre": {"title": [{"text": {"content": entry["title"]}}]},
        "Score": {"number": entry["score"]},
        "Statut": {"select": {"name": entry["status"]}},
        "AniList ID": {"number": entry["id"]},
        "Dernier visionnage": {"date": {"start": datetime.fromtimestamp(
            entry["updatedAt"], tz=timezone.utc).isoformat()}},
    }
    if s.get("year") and s.get("month") and s.get("day"):
        props["Date de début"] = {"date": {
            "start": f"{s['year']}-{s['month']:02d}-{s['day']:02d}"}}
    image = {"type": "external", "external": {"url": entry["image"]}}
    notion("POST", "/pages", json={
        "parent": {"database_id": DATABASE_ID},
        "properties": props, "icon": image, "cover": image,
    })
    print(f"  -> Ajouté dans Notion : {entry['title']}")


# ---------- Synchronisation ----------
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
                push_to_anilist(mid, p["status"], p["score"])
                continue
            if p["status"] == entry["status"] and p["score"] == entry["score"]:
                continue
            # Le plus récent gagne ; un statut Notion vide n'écrase jamais AniList
            if not p["status"] or entry["updatedAt"] > p["edited"]:
                pull_to_notion(p["page_id"], entry)
            else:
                push_to_anilist(mid, p["status"], p["score"])
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
    main()
