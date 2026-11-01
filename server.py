"""
Minimal Flask server for the coach progression visualization.
Reads from data/coaches.db and serves the app + JSON API.

Install: pip install flask
Run:     python server.py
"""

import sqlite3
import os
from flask import Flask, jsonify, request, send_file

app = Flask(__name__, static_folder="static", static_url_path="/static")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "coaches.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@app.route("/")
def index():
    return send_file("app.html")


@app.route("/api/competitions")
def competitions():
    db = get_db()
    rows = db.execute(
        "SELECT DISTINCT competition FROM team_progression ORDER BY competition"
    ).fetchall()
    db.close()
    return jsonify([r["competition"] for r in rows])


@app.route("/api/seasons")
def seasons():
    competition = request.args.get("competition", "")
    db = get_db()
    rows = db.execute(
        "SELECT DISTINCT season FROM team_progression WHERE competition = ? ORDER BY season DESC",
        (competition,),
    ).fetchall()
    db.close()
    return jsonify([r["season"] for r in rows])


@app.route("/api/progression")
def progression():
    competition = request.args.get("competition", "")
    season = request.args.get("season", "")
    db = get_db()
    rows = db.execute(
        """SELECT competition, season, team_id, team_name, matchday, match_date,
                  coach_id, coach_name, result, goals_for, goals_against,
                  cumulative_points, cumulative_matches, points_pct, coach_change
           FROM team_progression
           WHERE competition = ? AND season = ?
           ORDER BY team_name, matchday""",
        (competition, season),
    ).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


LEAGUE_ZONES = {
    "La Liga": {
        "champions_league": 4,
        "europa_league": 5,
        "conference_league": 6,
        "relegation": 18,
    },
    "Premier League": {
        "champions_league": 4,
        "europa_league": 5,
        "conference_league": 6,
        "relegation": 18,
    },
    "Serie A": {
        "champions_league": 4,
        "europa_league": 5,
        "conference_league": 6,
        "relegation": 18,
    },
    "Ligue 1": {
        "champions_league": 3,
        "europa_league": 4,
        "conference_league": 5,
        "relegation": 16,
    },
    "Bundesliga": {
        "champions_league": 4,
        "europa_league": 5,
        "conference_league": 6,
        "relegation": 16,
    },
    "Championship": {
        "champions_league": 2,
        "europa_league": 2,
        "conference_league": 6,
        "relegation": 22,
    },
    "LaLiga 2": {
        "champions_league": 2,
        "europa_league": 2,
        "conference_league": 6,
        "relegation": 19,
    },
    "Ligue 2": {
        "champions_league": 2,
        "europa_league": 2,
        "conference_league": 3,
        "relegation": 17,
    },
    "2. Bundesliga": {
        "champions_league": 2,
        "europa_league": 2,
        "conference_league": 3,
        "relegation": 16,
    },
    "Serie B": {
        "champions_league": 2,
        "europa_league": 2,
        "conference_league": 8,
        "relegation": 17,
    },
    "Primeira Liga": {
        "champions_league": 2,
        "europa_league": 3,
        "conference_league": 4,
        "relegation": 16,
    },
    "Eredivisie": {
        "champions_league": 2,
        "europa_league": 3,
        "conference_league": 5,
        "relegation": 16,
    },
    "Brasileirão": {
        "champions_league": 6,
        "europa_league": 6,
        "conference_league": 12,
        "relegation": 17,
    },
    "Liga Profesional Argentina": {
        "champions_league": 4,
        "europa_league": 4,
        "conference_league": 4,
        "relegation": 25,
    },
    "Pro League": {
        "champions_league": 2,
        "europa_league": 3,
        "conference_league": 4,
        "relegation": 16,
    },
    "Chinese Super League": {
        "champions_league": 3,
        "europa_league": 3,
        "conference_league": 3,
        "relegation": 15,
    },
}


@app.route("/api/zones")
def zones():
    competition = request.args.get("competition", "")
    season = request.args.get("season", "")
    db = get_db()

    max_md = db.execute(
        "SELECT MAX(matchday) as m FROM team_progression WHERE competition = ? AND season = ?",
        (competition, season),
    ).fetchone()["m"]

    empty = {"champion": 0, "champions_league": 0, "europa_league": 0, "conference_league": 0, "relegation": 0}
    if max_md is None:
        db.close()
        return jsonify(empty)

    # Final standings by total season points (not per-coach pct)
    standings = db.execute(
        """SELECT team_name, result
           FROM team_progression
           WHERE competition = ? AND season = ?
           ORDER BY team_name, matchday""",
        (competition, season),
    ).fetchall()

    # Compute total points per team
    team_pts = {}
    team_matches = {}
    for r in standings:
        tn = r["team_name"]
        team_pts[tn] = team_pts.get(tn, 0) + (3 if r["result"] == "W" else 1 if r["result"] == "D" else 0)
        team_matches[tn] = team_matches.get(tn, 0) + 1

    # Sort by points descending
    ranked = sorted(team_pts.items(), key=lambda x: x[1], reverse=True)
    n = len(ranked)
    if n == 0:
        db.close()
        return jsonify(empty)

    # Compute pct for each position
    def pct_at(pos):
        if pos > n:
            return 0
        name, pts = ranked[pos - 1]
        matches = team_matches[name]
        return round((pts / (matches * 3)) * 100, 2) if matches > 0 else 0

    zones_cfg = LEAGUE_ZONES.get(competition, LEAGUE_ZONES["La Liga"])

    db.close()
    return jsonify({
        "champion": pct_at(1),
        "champions_league": pct_at(zones_cfg["champions_league"]),
        "europa_league": pct_at(zones_cfg["europa_league"]),
        "conference_league": pct_at(zones_cfg["conference_league"]),
        "relegation": pct_at(zones_cfg["relegation"]),
    })


@app.route("/api/coach_changes")
def coach_changes():
    competition = request.args.get("competition", "")
    season = request.args.get("season", "")
    db = get_db()

    rows = db.execute(
        """SELECT team_id, team_name, coach_name, matchday, points_pct
           FROM team_progression
           WHERE competition = ? AND season = ?
           ORDER BY team_name, matchday""",
        (competition, season),
    ).fetchall()
    db.close()

    # Group by team, then by coach stints
    teams = {}
    team_ids = {}
    for r in rows:
        tn = r["team_name"]
        if tn not in teams:
            teams[tn] = []
            team_ids[tn] = r["team_id"]
        teams[tn].append(dict(r))

    result = []
    for team_name, matches in teams.items():
        coaches = []
        current_coach = None
        stint_start = None
        for m in matches:
            if m["coach_name"] != current_coach:
                if current_coach is not None:
                    coaches[-1]["to_matchday"] = m["matchday"] - 1
                current_coach = m["coach_name"]
                stint_start = m["matchday"]
                coaches.append({
                    "coach_name": current_coach,
                    "from_matchday": stint_start,
                    "to_matchday": m["matchday"],
                    "points_pct": m["points_pct"],
                })
            else:
                coaches[-1]["to_matchday"] = m["matchday"]
                coaches[-1]["points_pct"] = m["points_pct"]

        # Calculate points_pct per stint (use the pct at last matchday of stint)
        result.append({
            "team_id": team_ids.get(team_name, ""),
            "team_name": team_name,
            "total_coaches": len(coaches),
            "coaches": coaches,
        })

    # Sort by number of coach changes descending
    result.sort(key=lambda x: x["total_coaches"], reverse=True)
    return jsonify(result)


@app.route("/api/coach_stats")
def coach_stats():
    competition = request.args.get("competition", "")
    season = request.args.get("season", "")
    team_id = request.args.get("team_id", "")
    coach_id = request.args.get("coach_id", "")
    db = get_db()

    # Get progression rows for this coach stint
    prog_rows = db.execute(
        """SELECT matchday, result, goals_for, goals_against, points_pct,
                  cumulative_points, cumulative_matches
           FROM team_progression
           WHERE competition = ? AND season = ? AND team_id = ? AND coach_id = ?
           ORDER BY matchday""",
        (competition, season, team_id, coach_id),
    ).fetchall()

    if not prog_rows:
        db.close()
        return jsonify({})

    matches = len(prog_rows)
    wins = sum(1 for r in prog_rows if r["result"] == "W")
    draws = sum(1 for r in prog_rows if r["result"] == "D")
    losses = sum(1 for r in prog_rows if r["result"] == "L")
    gf = sum(r["goals_for"] or 0 for r in prog_rows)
    ga = sum(r["goals_against"] or 0 for r in prog_rows)
    pts = wins * 3 + draws
    pct = round((pts / (matches * 3)) * 100, 2) if matches > 0 else 0

    # Streaks
    results_seq = [r["result"] for r in prog_rows]
    win_streak = _longest_streak(results_seq, {"W"})
    unbeaten_streak = _longest_streak(results_seq, {"W", "D"})
    winless_streak = _longest_streak(results_seq, {"D", "L"})

    # Possession & formation from matches table
    # We need to find which matches correspond to this coach for this team
    match_rows = db.execute(
        """SELECT home_team_id, home_possession, away_possession,
                  home_formation, away_formation
           FROM matches m
           JOIN team_progression tp ON (
               m.competition = tp.competition AND m.season = tp.season
               AND m.match_date = tp.match_date
               AND (m.home_team_id = tp.team_id OR m.away_team_id = tp.team_id)
           )
           WHERE tp.competition = ? AND tp.season = ? AND tp.team_id = ?
                 AND tp.coach_id = ?""",
        (competition, season, team_id, coach_id),
    ).fetchall()

    possessions = []
    formations = {}
    for mr in match_rows:
        if mr["home_team_id"] == team_id:
            if mr["home_possession"] is not None:
                possessions.append(mr["home_possession"])
            f = mr["home_formation"]
        else:
            if mr["away_possession"] is not None:
                possessions.append(mr["away_possession"])
            f = mr["away_formation"]
        if f:
            formations[f] = formations.get(f, 0) + 1

    avg_possession = round(sum(possessions) / len(possessions), 1) if possessions else None
    top_formation = max(formations, key=formations.get) if formations else None

    db.close()
    return jsonify({
        "matches": matches,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "goals_for": gf,
        "goals_against": ga,
        "goal_diff": gf - ga,
        "points": pts,
        "points_pct": pct,
        "win_streak": win_streak,
        "unbeaten_streak": unbeaten_streak,
        "winless_streak": winless_streak,
        "avg_possession": avg_possession,
        "top_formation": top_formation,
    })


@app.route("/api/coach_history")
def coach_history():
    coach_id = request.args.get("coach_id", "")
    db = get_db()

    rows = db.execute(
        """SELECT competition, season, team_id, team_name, matchday,
                  result, goals_for, goals_against, points_pct, coach_change
           FROM team_progression
           WHERE coach_id = ?
           ORDER BY season, competition, team_name, matchday""",
        (coach_id,),
    ).fetchall()
    db.close()

    # Group by (competition, season, team)
    stints = []
    current = None
    for r in rows:
        key = (r["competition"], r["season"], r["team_id"])
        if current is None or current["_key"] != key:
            if current:
                stints.append(current)
            wins = 1 if r["result"] == "W" else 0
            draws = 1 if r["result"] == "D" else 0
            losses = 1 if r["result"] == "L" else 0
            current = {
                "_key": key,
                "competition": r["competition"],
                "season": r["season"],
                "team_id": r["team_id"],
                "team_name": r["team_name"],
                "matches": 1,
                "wins": wins,
                "draws": draws,
                "losses": losses,
                "gf": r["goals_for"] or 0,
                "ga": r["goals_against"] or 0,
                "final_pct": r["points_pct"],
            }
        else:
            current["matches"] += 1
            if r["result"] == "W": current["wins"] += 1
            elif r["result"] == "D": current["draws"] += 1
            else: current["losses"] += 1
            current["gf"] += r["goals_for"] or 0
            current["ga"] += r["goals_against"] or 0
            current["final_pct"] = r["points_pct"]

    if current:
        stints.append(current)

    result = []
    for s in stints:
        pts = s["wins"] * 3 + s["draws"]
        result.append({
            "competition": s["competition"],
            "season": s["season"],
            "team_id": s["team_id"],
            "team_name": s["team_name"],
            "matches": s["matches"],
            "wins": s["wins"],
            "draws": s["draws"],
            "losses": s["losses"],
            "gf": s["gf"],
            "ga": s["ga"],
            "points": pts,
            "points_pct": s["final_pct"],
        })

    return jsonify(result)


def _longest_streak(results, target_set):
    best = 0
    current = 0
    for r in results:
        if r in target_set:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


@app.route("/api/team_history")
def team_history():
    team_id = request.args.get("team_id", "")
    db = get_db()

    rows = db.execute(
        """SELECT competition, season, team_name, matchday, match_date,
                  coach_id, coach_name, result, goals_for, goals_against,
                  cumulative_points, cumulative_matches, points_pct, coach_change
           FROM team_progression
           WHERE team_id = ?
           ORDER BY season DESC, competition, matchday""",
        (team_id,),
    ).fetchall()
    db.close()

    # Group into coach stints
    stints = []
    current = None
    for r in rows:
        key = (r["competition"], r["season"], r["coach_id"])
        if current is None or current["_key"] != key:
            if current:
                pts = current["wins"] * 3 + current["draws"]
                current["points"] = pts
                current["points_pct"] = round((pts / (current["matches"] * 3)) * 100, 2) if current["matches"] else 0
                stints.append(current)
            current = {
                "_key": key,
                "competition": r["competition"],
                "season": r["season"],
                "team_name": r["team_name"],
                "coach_id": r["coach_id"],
                "coach_name": r["coach_name"],
                "from_matchday": r["matchday"],
                "to_matchday": r["matchday"],
                "matches": 1,
                "wins": 1 if r["result"] == "W" else 0,
                "draws": 1 if r["result"] == "D" else 0,
                "losses": 1 if r["result"] == "L" else 0,
                "gf": r["goals_for"] or 0,
                "ga": r["goals_against"] or 0,
                "sacked": False,
            }
        else:
            current["to_matchday"] = r["matchday"]
            current["matches"] += 1
            if r["result"] == "W": current["wins"] += 1
            elif r["result"] == "D": current["draws"] += 1
            else: current["losses"] += 1
            current["gf"] += r["goals_for"] or 0
            current["ga"] += r["goals_against"] or 0

    if current:
        pts = current["wins"] * 3 + current["draws"]
        current["points"] = pts
        current["points_pct"] = round((pts / (current["matches"] * 3)) * 100, 2) if current["matches"] else 0
        stints.append(current)

    # Mark sacked coaches (not the last stint per season/competition)
    by_season = {}
    for s in stints:
        sk = (s["competition"], s["season"])
        if sk not in by_season:
            by_season[sk] = []
        by_season[sk].append(s)
    for group in by_season.values():
        for s in group[:-1]:
            s["sacked"] = True

    result = []
    for s in stints:
        del s["_key"]
        result.append(s)

    return jsonify(result)


@app.route("/api/search_teams")
def search_teams():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    db = get_db()
    rows = db.execute(
        """SELECT DISTINCT team_id, team_name
           FROM team_progression
           WHERE team_name LIKE ?
           ORDER BY team_name
           LIMIT 20""",
        (f"%{q}%",),
    ).fetchall()
    db.close()
    return jsonify([{"team_id": r["team_id"], "team_name": r["team_name"]} for r in rows])


@app.route("/api/search_coaches")
def search_coaches():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    db = get_db()
    rows = db.execute(
        """SELECT DISTINCT coach_id, coach_name
           FROM team_progression
           WHERE coach_name LIKE ?
           ORDER BY coach_name
           LIMIT 20""",
        (f"%{q}%",),
    ).fetchall()
    db.close()
    return jsonify([{"coach_id": r["coach_id"], "name": r["coach_name"]} for r in rows])


@app.route("/api/coach_photos")
def coach_photos():
    photos_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "coaches")
    if not os.path.isdir(photos_dir):
        return jsonify({})
    result = {}
    for f in os.listdir(photos_dir):
        name, ext = os.path.splitext(f)
        if ext.lower() in (".png", ".jpg", ".jpeg", ".webp"):
            result[name] = f"/static/coaches/{f}"
    return jsonify(result)


@app.route("/api/refresh", methods=["POST"])
def refresh():
    import subprocess
    competition = request.json.get("competition", "") if request.json else ""
    season = request.json.get("season", "") if request.json else ""

    cmd = ["python3", "fetch_data.py"]
    if competition:
        cmd += ["--competition", competition]
    if season:
        cmd += ["--season", season]

    try:
        result = subprocess.run(
            cmd,
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True,
            text=True,
            timeout=600,
        )
        return jsonify({"ok": True, "output": result.stdout[-500:] if result.stdout else "", "error": result.stderr[-500:] if result.stderr else ""})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Timeout (10 min)"}), 504
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    print(f"Serving on http://localhost:5050")
    print(f"Database: {DB_PATH}")
    app.run(host="0.0.0.0", port=5050, debug=False)
