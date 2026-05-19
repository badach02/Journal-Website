from flask import Flask, render_template, request, jsonify, redirect, url_for
from datetime import datetime, timedelta
import requests
import os
import time
import logging
from dotenv import load_dotenv

# Load environment variables from a local .env file if present (helps local dev)
load_dotenv()

app = Flask(__name__)

# Configure basic logging so the server console shows API diagnostics
logging.basicConfig(level=logging.INFO)
app.logger.setLevel(logging.INFO)

API_KEY = os.getenv("RIOT_API_KEY")
REGION = "na1"

HEADERS = {"X-Riot-Token": API_KEY} if API_KEY else {}

DEFAULT_TIMEOUT = 7
CACHE_TTL_SECONDS = 120
CACHE = {}

session = requests.Session()

def fetch_league(url):
    data = fetch_json(url, headers=HEADERS)
    entries = data.get("entries", []) if isinstance(data, dict) else []
    tier = data.get("tier")

    for entry in entries:
        if tier and not entry.get("tier"):
            entry["tier"] = tier
        if not entry.get("summonerName"):
            # Riot's response can vary depending on queue/team entries — try several fallbacks
            entry["summonerName"] = (
                entry.get("playerOrTeamName")
                or entry.get("name")
                or entry.get("displayName")
                or entry.get("summonerId")
                or entry.get("playerOrTeamId")
            )

    return entries


def get_top_300(region=REGION):
    if not API_KEY:
        raise ValueError("RIOT_API_KEY is not configured")

    challenger = fetch_league(f"https://{region}.api.riotgames.com/lol/league/v4/challengerleagues/by-queue/RANKED_SOLO_5x5")
    grandmaster = fetch_league(f"https://{region}.api.riotgames.com/lol/league/v4/grandmasterleagues/by-queue/RANKED_SOLO_5x5")
    master = fetch_league(f"https://{region}.api.riotgames.com/lol/league/v4/masterleagues/by-queue/RANKED_SOLO_5x5")

    all_players = challenger + grandmaster + master
    all_players.sort(key=lambda x: x.get("leaguePoints", 0), reverse=True)

    return all_players[:300]


def make_cache_key(url, params=None):
    if not params:
        return url
    items = sorted(params.items())
    return f"{url}?{'&'.join([f'{k}={v}' for k, v in items])}"


def fetch_json(url, params=None, headers=None):
    cache_key = make_cache_key(url, params)
    cached = CACHE.get(cache_key)
    if cached and time.time() < cached['expires']:
        return cached['value']

    data = None
    for attempt in range(2):
        try:
            app.logger.info("External API request start: url=%s params=%s attempt=%d", url, params, attempt + 1)
            start = time.time()
            response = session.get(url, params=params, headers=headers or {}, timeout=DEFAULT_TIMEOUT)
            elapsed = time.time() - start
            status = getattr(response, 'status_code', None)
            app.logger.info("External API response: url=%s status=%s elapsed=%.3fs", url, status, elapsed)

            if status == 429:
                retry_after = response.headers.get('Retry-After')
                app.logger.warning("Rate limited by Riot API (429) for %s; Retry-After=%s", url, retry_after)

            response.raise_for_status()
            data = response.json()
            break
        except requests.RequestException as exc:
            app.logger.warning("External API request failed (attempt %s) for url=%s params=%s: %s", attempt + 1, url, params, exc)
            if attempt == 1:
                raise
            time.sleep(0.35)
        except ValueError as exc:
            app.logger.warning("Invalid JSON response from: %s", url)
            raise

    if data is None:
        raise requests.RequestException("Unable to fetch JSON data")

    CACHE[cache_key] = {'value': data, 'expires': time.time() + CACHE_TTL_SECONDS}
    return data


def format_time(timestamp):
    if not timestamp:
        return "Unknown"

    try:
        if timestamp.endswith("Z"):
            timestamp = timestamp.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(timestamp)
        return parsed.strftime("%b %d, %Y %H:%M UTC")
    except ValueError:
        return timestamp


def normalize_value(value):
    if not isinstance(value, str):
        return ""
    return value.strip()


def not_found_error(message="Resource not found"):
    return render_template("error.html", title="Not Found", message=message), 404


def bad_request_error(message="Invalid request"):
    return render_template("error.html", title="Invalid Request", message=message), 400


def render_api_error(message, status=400):
    return jsonify({"error": message}), status


@app.route("/")
def index():
    return render_template(
        "index.html",
        default_region="us",
        default_realm="Stormrage",
        default_name="Owlpacíno",
    )


@app.route("/top300")
def top300():
    region = normalize_value(request.args.get("region", REGION)) or REGION
    try:
        players = get_top_300(region)
    except ValueError as exc:
        return bad_request_error(str(exc))
    except requests.RequestException:
        return bad_request_error("Unable to fetch top 300 players right now. Try again later.")

    return render_template("top300.html", players=players, region=region)


@app.route("/api/top300")
def api_top300():
    region = normalize_value(request.args.get("region", REGION)) or REGION
    try:
        players = get_top_300(region)
    except ValueError as exc:
        return render_api_error(str(exc), 500)
    except requests.RequestException:
        return render_api_error("Unable to fetch player data", 502)

    return jsonify({"region": region, "players": players})


@app.route("/player")
def player_search():
    region = normalize_value(request.args.get("region", "us"))
    realm = normalize_value(request.args.get("realm", "Stormrage"))
    name = normalize_value(request.args.get("name", "Owlpacíno"))

    if not region or not realm or not name:
        return bad_request_error("Please provide region, realm, and character name.")

    return redirect(url_for("player", region=region, realm=realm, name=name))


@app.route("/health")
def health():
    status = {"status": "ok", "riot_api_key_configured": bool(API_KEY)}
    return jsonify(status), 200


def get_weekly_runs(region, realm, name):
    url = "https://raider.io/api/v1/characters/profile"

    params = {
        "region": region,
        "realm": realm,
        "name": name,
        "fields": "mythic_plus_recent_runs,mythic_plus_scores,mythic_plus_scores_by_season:season-mn-1,mythic_plus_best_runs,mythic_plus_current_week_runs,mythic_plus_highest_level"
    }

    data = fetch_json(url, params=params)

    runs = data.get("mythic_plus_recent_runs", [])
    profile = {
        "name": data.get("name", name),
        "realm": data.get("realm", realm),
        "region": data.get("region", region),
        "class": data.get("class"),
        "active_spec_name": data.get("active_spec_name"),
        "score": None,
        "thumbnail_url": data.get("thumbnail_url"),
    }

    def extract_score(source):
        if source is None:
            return None
        if isinstance(source, dict):
            if "score" in source and source.get("score") is not None:
                return source.get("score")
            if "scores" in source and isinstance(source.get("scores"), dict):
                all_score = source["scores"].get("all")
                if all_score is not None:
                    return all_score
            for value in source.values():
                if isinstance(value, (dict, list)):
                    score = extract_score(value)
                    if score is not None:
                        return score
            return None
        elif isinstance(source, list):
            for item in source:
                score = extract_score(item)
                if score is not None:
                    return score
        return None

    score_data = data.get("mythic_plus_scores") or {}
    season_data = data.get("mythic_plus_scores_by_season") or {}
    profile["score"] = (
        extract_score(score_data)
        or extract_score(season_data)
        or extract_score(data.get("mythic_plus_scores_by_season", {}))
        or data.get("mythic_plus_score")
        or data.get("mythic_plus_current_week_score")
        or data.get("mythic_plus_season_score")
    )

    one_week_ago = datetime.utcnow() - timedelta(days=7)
    filtered = []

    for r in runs:
        # RaiderIO format example: "2024-01-01T12:34:56Z"
        ts = r.get("completed_at") or r.get("timestamp")

        if ts:
            try:
                run_time = datetime.fromisoformat(ts.replace("Z", ""))
            except ValueError:
                run_time = None

            if run_time and run_time > one_week_ago:
                filtered.append({
                    "dungeon": r.get("dungeon"),
                    "level": r.get("mythic_level"),
                    "time": format_time(ts),
                    "raw_time": ts,
                    "score": r.get("score", 0),
                })

    return profile, filtered


@app.route("/player/<region>/<realm>/<name>")
def player(region, realm, name):
    region = normalize_value(region)
    realm = normalize_value(realm)
    name = normalize_value(name)

    try:
        profile, runs = get_weekly_runs(region, realm, name)
    except requests.RequestException:
        return bad_request_error("Unable to fetch player data at this time.")
    except ValueError as exc:
        return bad_request_error(str(exc))

    return render_template(
        "player.html",
        profile=profile,
        name=name,
        realm=realm,
        region=region,
        runs=runs,
    )


@app.route("/api/player/<region>/<realm>/<name>")
def api_player(region, realm, name):
    try:
        profile, runs = get_weekly_runs(region, realm, name)
    except requests.RequestException:
        return render_api_error("Unable to fetch player data", 502)

    return jsonify({"profile": profile, "runs": runs})


@app.errorhandler(404)
def page_not_found(error):
    return render_template("error.html", title="Page Not Found", message="The requested page does not exist."), 404


@app.errorhandler(500)
def internal_server_error(error):
    return render_template("error.html", title="Server Error", message="An unexpected error occurred."), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)