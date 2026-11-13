import os
import sys
import time
import requests
from notion_client import Client

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
GAMES_DATABASE_ID = os.environ["GAMES_DATABASE_ID"]
RAWG_API_KEY = os.environ["RAWG_API_KEY"]

notion = Client(auth=NOTION_TOKEN)

TITLE_PROP = "Nom"
RAWG_ID_PROP = "ID RAWG"
IMAGE_PROP = "Image"


def get_all_pages():
    """Récupère toutes les pages de la base Games tracker (gère la pagination)."""
    kwargs = {"database_id": GAMES_DATABASE_ID, "page_size": 100}
    while True:
        resp = notion.databases.query(**kwargs)
        for page in resp["results"]:
            yield page
        if not resp.get("has_more"):
            break
        kwargs["start_cursor"] = resp["next_cursor"]


def get_title(prop):
    """Extrait le texte d'une propriété title."""
    if not prop:
        return ""
    return "".join(i.get("plain_text", "") for i in prop.get("title", [])).strip()


def get_rawg_id(prop):
    """Extrait l'ID RAWG (propriété number)."""
    if not prop:
        return None
    return prop.get("number")


def rawg_get(url, params):
    """GET RAWG avec nouvelles tentatives (timeouts / erreurs 5xx / 429)."""
    last = None
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                last = RuntimeError(f"RAWG {r.status_code}")
            else:
                return r
        except requests.RequestException as ex:
            last = ex
        time.sleep(2 ** attempt * 2)
    raise last


def fetch_rawg_image(game_id=None, title=None):
    """Retourne l'URL de l'image RAWG (background_image) pour un jeu."""
    if game_id:
        r = rawg_get(
            f"https://api.rawg.io/api/games/{int(game_id)}",
            {"key": RAWG_API_KEY},
        )
        if r.ok:
            return r.json().get("background_image")
        return None

    if title:
        r = rawg_get(
            "https://api.rawg.io/api/games",
            {"key": RAWG_API_KEY, "search": title, "page_size": 1},
        )
        if r.ok:
            results = r.json().get("results") or []
            if results:
                return results[0].get("background_image")
    return None


def sync_page(page):
    """Remplit la colonne Image depuis RAWG et met à jour la couverture de la page."""
    props = page["properties"]
    rawg_id = get_rawg_id(props.get(RAWG_ID_PROP))
    title = get_title(props.get(TITLE_PROP))

    # Déjà rempli : on ne rappelle pas RAWG ni Notion
    if (props.get(IMAGE_PROP) or {}).get("url") and page.get("cover"):
        return False

    image_url = fetch_rawg_image(game_id=rawg_id, title=None if rawg_id else title)
    if not image_url:
        return False

    notion.pages.update(
        page_id=page["id"],
        properties={IMAGE_PROP: {"url": image_url}},
        cover={"type": "external", "external": {"url": image_url}},
    )
    return True


def main():
    total = updated = errors = 0
    for page in get_all_pages():
        total += 1
        try:
            if sync_page(page):
                updated += 1
                time.sleep(0.35)
        except Exception as ex:
            errors += 1
            title = get_title(page["properties"].get(TITLE_PROP))
            print(f"  ! Erreur sur « {title} » : {ex}")
    print(f"Jeux : {total} | Images/couvertures mises à jour : {updated} | Erreurs : {errors}")
    # Échec du workflow seulement si tout a échoué (une panne RAWG ponctuelle n'est pas bloquante)
    if total and errors == total:
        sys.exit(1)


if __name__ == "__main__":
    main()
