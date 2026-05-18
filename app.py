from flask import Flask, jsonify
import requests
import os

app = Flask(__name__)

API_KEY = os.getenv("RIOT_API_KEY")

if not API_KEY:
    raise Exception("Missing RIOT_API_KEY environment variable")

REGION = "na1"

HEADERS = {
    "X-Riot-Token": API_KEY
}


def fetch_league(url):
    res = requests.get(url, headers=HEADERS)
    return res.json().get("entries", [])


def get_top_300():
    challenger_url = f"https://{REGION}.api.riotgames.com/lol/league/v4/challengerleagues/by-queue/RANKED_SOLO_5x5"
    grandmaster_url = f"https://{REGION}.api.riotgames.com/lol/league/v4/grandmasterleagues/by-queue/RANKED_SOLO_5x5"
    master_url = f"https://{REGION}.api.riotgames.com/lol/league/v4/masterleagues/by-queue/RANKED_SOLO_5x5"

    challenger = fetch_league(challenger_url)
    grandmaster = fetch_league(grandmaster_url)
    master = fetch_league(master_url)

    all_players = challenger + grandmaster + master

    all_players.sort(key=lambda x: x.get("leaguePoints", 0), reverse=True)

    return all_players[:300]


@app.route("/top300")
def top300():
    players = get_top_300()
    return jsonify({
        "count": len(players),
        "players": players
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
