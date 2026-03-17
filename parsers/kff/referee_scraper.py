import requests
from bs4 import BeautifulSoup


def scrape_referees(match_id):

    url = f"https://kffleague.kz/match/{match_id}"

    r = requests.get(url)

    soup = BeautifulSoup(r.text, "lxml")

    referees = []

    rows = soup.select(".match-referees li")

    for row in rows:

        name = row.text.strip()

        parts = name.split(" ")

        referees.append({
            "first_name": parts[0],
            "last_name": " ".join(parts[1:]),
            "role": "main"
        })

    return referees