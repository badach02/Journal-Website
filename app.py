from flask import Flask, render_template
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
