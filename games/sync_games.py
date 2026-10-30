import os
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


def fetch_rawg_image(game_id=None, title=None):
    """Retourne l'URL de l'image RAWG (background_image) pour un jeu."""
    if game_id:
        r = requests.get(
            f"https://api.rawg.io/api/games/{int(game_id)}",
            params={"key": RAWG_API_KEY},
            timeout=15,
        )
        if r.ok:
            return r.json().get("background_image")
        return None

    if title:
        r = requests.get(
            "https://api.rawg.io/api/games",
            params={"key": RAWG_API_KEY, "search": title, "page_size": 1},
            timeout=15,
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
    total = updated = 0
    for page in get_all_pages():
        total += 1
        if sync_page(page):
            updated += 1
    print(f"Jeux : {total} | Images/couvertures mises à jour : {updated}")


if __name__ == "__main__":
    main()
