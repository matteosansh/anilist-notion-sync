import os
import requests
from notion_client import Client

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
GAMES_DATABASE_ID = os.environ["GAMES_DATABASE_ID"]

notion = Client(auth=NOTION_TOKEN)


def get_all_pages():
    """Récupère toutes les pages de la base Games (gère la pagination)."""
    kwargs = {"database_id": GAMES_DATABASE_ID, "page_size": 100}
    while True:
        resp = notion.databases.query(**kwargs)
        for page in resp["results"]:
            yield page
        if not resp.get("has_more"):
            break
        kwargs["start_cursor"] = resp["next_cursor"]


def get_cover_url(page):
    """
    Retourne l'URL de l'image de couverture à utiliser.
    Adapte cette fonction à ta source : ici on lit une propriété 'Image' de type URL.
    Si ta source est une API (IGDB, RAWG...), remplace le contenu par l'appel API.
    """
    prop = page["properties"].get("Image", {})
    url = prop.get("url")
    return url if url and url.startswith("http") else None


def update_cover(page):
    """Met à jour la couverture de la page si une image est disponible."""
    cover_url = get_cover_url(page)
    if not cover_url:
        return False

    # Ne rien faire si la couverture est déjà la bonne
    current = page.get("cover") or {}
    current_url = (current.get("external") or {}).get("url")
    if current_url == cover_url:
        return False

    notion.pages.update(
        page_id=page["id"],
        cover={"type": "external", "external": {"url": cover_url}},
    )
    return True


def main():
    total = updated_covers = 0
    for page in get_all_pages():
        total += 1
        if update_cover(page):
            updated_covers += 1
    print(f"Jeux : {total} | Couvertures mises à jour : {updated_covers}")


if __name__ == "__main__":
    main()
