import os
import requests
from notion_client import Client

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
RAWG_API_KEY = os.environ["RAWG_API_KEY"]
DATABASE_ID = os.environ["GAMES_DATABASE_ID"]

notion = Client(auth=NOTION_TOKEN)


def search_rawg(name):
    r = requests.get(
        "https://api.rawg.io/api/games",
        params={"key": RAWG_API_KEY, "search": name, "page_size": 1},
        timeout=15,
    )
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0] if results else None


def get_rawg_details(game_id):
    r = requests.get(
        f"https://api.rawg.io/api/games/{game_id}",
        params={"key": RAWG_API_KEY},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def get_all_pages():
    pages, cursor = [], None
    while True:
        kwargs = {"database_id": DATABASE_ID}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.databases.query(**kwargs)
        pages.extend(resp["results"])
        if not resp.get("has_more"):
            break
        cursor = resp["next_cursor"]
    return pages


def main():
    for page in get_all_pages():
        props = page["properties"]
        title_items = props["Nom"]["title"]
        if not title_items:
            continue
        name = title_items[0]["plain_text"]

        rawg_id = props["ID RAWG"]["number"]

        if rawg_id is None:
            found = search_rawg(name)
            if not found:
                print(f"Pas trouvé sur RAWG : {name}")
                continue
            rawg_id = found["id"]

        details = get_rawg_details(rawg_id)
        genres = [{"name": g["name"]} for g in details.get("genres", [])]

        notion.pages.update(
            page_id=page["id"],
            properties={
                "ID RAWG": {"number": rawg_id},
                "Genres": {"multi_select": genres},
            },
        )
        print(f"OK : {name} ({rawg_id})")


if __name__ == "__main__":
    main()
