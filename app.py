from flask import Flask, render_template
from datetime import datetime, timedelta
import requests
import os

app = Flask(__name__)

API_KEY = os.getenv("RIOT_API_KEY")
REGION = "na1"

HEADERS = {"X-Riot-Token": API_KEY}


def fetch_league(url):
    res = requests.get(url, headers=HEADERS)
    return res.json().get("entries", [])


def get_top_300():
    challenger = fetch_league(f"https://{REGION}.api.riotgames.com/lol/league/v4/challengerleagues/by-queue/RANKED_SOLO_5x5")
    grandmaster = fetch_league(f"https://{REGION}.api.riotgames.com/lol/league/v4/grandmasterleagues/by-queue/RANKED_SOLO_5x5")
    master = fetch_league(f"https://{REGION}.api.riotgames.com/lol/league/v4/masterleagues/by-queue/RANKED_SOLO_5x5")

    all_players = challenger + grandmaster + master
    all_players.sort(key=lambda x: x.get("leaguePoints", 0), reverse=True)

    return all_players[:300]


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/top300")
def top300():
    players = get_top_300()
    return render_template("top300.html", players=players)


@app.route("/health")
def health():
    return {"status": "ok"}, 200


def get_weekly_runs(region, realm, name):
    url = "https://raider.io/api/v1/characters/profile"

    params = {
        "region": region,
        "realm": realm,
        "name": name,
        "fields": "mythic_plus_recent_runs"
    }

    res = requests.get(url, params=params)
    data = res.json()

    runs = data.get("mythic_plus_recent_runs", [])

    one_week_ago = datetime.utcnow() - timedelta(days=7)
    filtered = []

    for r in runs:
        # RaiderIO format example: "2024-01-01T12:34:56Z"
        ts = r.get("completed_at") or r.get("timestamp")

        if ts:
            run_time = datetime.fromisoformat(ts.replace("Z", ""))

            if run_time > one_week_ago:
                filtered.append({
                    "dungeon": r.get("dungeon"),
                    "level": r.get("mythic_level"),
                    "time": ts,
                    "score": r.get("score", 0)
                })

    return filtered


@app.route("/player/<region>/<realm>/<name>")
def player(region, realm, name):
    runs = get_weekly_runs(region, realm, name)

    return render_template(
        "player.html",
        name=name,
        realm=realm,
        region=region,
        runs=runs
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)