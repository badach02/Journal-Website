from flask import Flask, render_template, request, jsonify, redirect, url_for
from datetime import datetime, timedelta
import requests
import os
import time
import logging
from dotenv import load_dotenv
import concurrent.futures

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
ROLE_CACHE_TTL_SECONDS = 60 * 60 * 24  # cache inferred roles for 24 hours
ROLE_INFER_LIMIT = 100  # number of top players to infer roles for by default

session = requests.Session()

# Mapping from Riot region to platform routing for Match-V5
PLATFORM_ROUTING = {
    "na1": "americas",
    "br1": "americas",
    "la1": "americas",
    "la2": "americas",
    "oc1": "americas",
    "kr": "asia",
    "jp1": "asia",
    "euw1": "europe",
    "eun1": "europe",
    "ru": "europe",
}

def platform_host_for_region(region):
    return PLATFORM_ROUTING.get(region, "americas")

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

    top = all_players[:300]

    # Infer roles for the top N players (to show role distribution)
    try:
        # collect puuids for players missing a role
        puuids = [p.get("puuid") for p in top[:ROLE_INFER_LIMIT] if p.get("puuid")]
        if puuids:
            roles_map = infer_roles_for_puuids(puuids, region=region, batch_size=10, pause=0.12)
            for p in top:
                puuid = p.get("puuid")
                if puuid and roles_map.get(puuid):
                    p["role"] = roles_map.get(puuid)
                else:
                    p["role"] = None
    except Exception as exc:
        app.logger.warning("Role inference failed: %s", exc)

    return top


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


def fetch_match_ids_by_puuid(puuid, region=REGION, count=5):
    """Return recent match ids for a puuid using Match-V5 (platform routing)."""
    platform = platform_host_for_region(region)
    url = f"https://{platform}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids"
    params = {"start": 0, "count": count}
    try:
        data = fetch_json(url, params=params, headers=HEADERS)
        return data if isinstance(data, list) else []
    except requests.RequestException:
        return []


def fetch_match_by_id(match_id, region=REGION):
    platform = platform_host_for_region(region)
    url = f"https://{platform}.api.riotgames.com/lol/match/v5/matches/{match_id}"
    try:
        return fetch_json(url, headers=HEADERS)
    except requests.RequestException:
        return None


def infer_role_from_participant(part):
    """Infer simple role string from a match participant object.

    Returns one of: 'Top','Jungle','Mid','ADC','Support' or None.
    """
    # Prefer teamPosition if present (newer schema)
    tp = part.get("teamPosition") or part.get("teamPosition", None)
    if tp:
        mapping = {
            "TOP": "Top",
            "JUNGLE": "Jungle",
            "MIDDLE": "Mid",
            "MID": "Mid",
            "BOTTOM": "ADC",
            "UTILITY": "Support",
        }
        role = mapping.get(tp.upper()) if isinstance(tp, str) else None
        if role:
            return role

    # Fallback to lane/role fields
    lane = part.get("lane") or part.get("timeline", {}).get("lane")
    role_field = part.get("role")
    if lane:
        l = lane.upper()
        if l in ("TOP",):
            return "Top"
        if l in ("JUNGLE", "JNG"):
            return "Jungle"
        if l in ("MID", "MIDDLE"):
            return "Mid"
        if l in ("BOTTOM", "BOT"):
            # distinguish adc/support by role_field if available
            if role_field == "DUO_SUPPORT":
                return "Support"
            return "ADC"

    if role_field:
        rf = role_field.upper()
        if rf == "DUO_SUPPORT":
            return "Support"
        if rf == "DUO_CARRY":
            return "ADC"
        if rf == "SOLO":
            # Solo depends on lane, skip
            return None

    return None


def infer_role_for_puuid(puuid, region=REGION, max_matches=5):
    """Infer a player's primary role by scanning recent matches until we can determine a role."""
    if not puuid:
        return None

    # Check cache first
    url = f"role://{puuid}"
    cached = CACHE.get(url)
    if cached and time.time() < cached['expires']:
        return cached['value']

    match_ids = fetch_match_ids_by_puuid(puuid, region=region, count=max_matches)
    for mid in match_ids:
        match = fetch_match_by_id(mid, region=region)
        if not match:
            continue
        # find participant matching puuid
        info = match.get("info") or {}
        participants = info.get("participants") or []
        for part in participants:
            if part.get("puuid") == puuid:
                role = infer_role_from_participant(part)
                if role:
                    CACHE[url] = {'value': role, 'expires': time.time() + ROLE_CACHE_TTL_SECONDS}
                    return role

    # nothing found
    CACHE[url] = {'value': None, 'expires': time.time() + ROLE_CACHE_TTL_SECONDS}
    return None


def infer_roles_for_puuids(puuids, region=REGION, batch_size=10, pause=0.12):
    """Batch infer roles for multiple puuids concurrently; returns dict puuid->role."""
    results = {}
    remaining = [p for p in puuids if p]
    for i in range(0, len(remaining), batch_size):
        chunk = remaining[i : i + batch_size]
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(chunk)) as ex:
            future_map = {ex.submit(infer_role_for_puuid, puuid, region): puuid for puuid in chunk}
            for fut in concurrent.futures.as_completed(future_map):
                puuid = future_map[fut]
                try:
                    results[puuid] = fut.result()
                except Exception as exc:
                    app.logger.warning("Role inference failed for %s: %s", puuid, exc)
                    results[puuid] = None
        if i + batch_size < len(remaining):
            time.sleep(pause)
    return results


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

    # Compute role distribution counts for display
    role_keys = ["Top", "Jungle", "Mid", "ADC", "Support"]
    role_counts = {k: 0 for k in role_keys}
    unknown = 0
    for p in players:
        r = p.get("role")
        if r in role_counts:
            role_counts[r] += 1
        else:
            unknown += 1

    return render_template("top300.html", players=players, region=region, role_counts=role_counts, role_unknown=unknown)


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