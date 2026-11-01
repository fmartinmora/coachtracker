#!/usr/bin/env python3
"""
Fetch football match data from Scoresway/PerformFeeds API and store in SQLite.

Usage:
    python fetch_data.py                          # fetch all leagues, all seasons
    python fetch_data.py --competition "La Liga"  # fetch only La Liga
    python fetch_data.py --season "2024/2025"     # fetch only one season
    python fetch_data.py --dry-run                # show what would be fetched
    python fetch_data.py --refresh-progression    # recompute progression table only
"""

import sys
import os
import argparse
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from scoresway_client import _api_get, _BASE_API

OUTLET = "ft1tiv1inq7v1sk3y9tv12yh5"

COMPETITIONS = {
    "La Liga": "34pl8szyvrbwcmfkuocjm3r6t",
    "Premier League": "2kwbbcootiqqgmrzs6o5inle5",
    "Ligue 1": "dm5ka0os1e3dxcp3vh05kmp33",
    "Bundesliga": "6by3h89i2eykc341oz7lv1ddd",
    "Serie A": "1r097lpxe0xn03ihb7wi98kao",
    "Championship": "7ntvbsyq31jnzoqoa8850b9b8",
    "LaLiga 2": "3is4bkgf3loxv9qfg3hm8zfqb",
    "Ligue 2": "4w7x0s5gfs5abasphlha5de8k",
    "2. Bundesliga": "722fdbecxzcq9788l6jqclzlw",
    "Serie B": "8ey0ww2zsosdmwr8ehsorh6t7",
    "Primeira Liga": "8yi6ejjd1zudcqtbn07haahg6",
    "Eredivisie": "akmkihra9ruad09ljapsm84b3",
    "Brasileirão": "scf9p4y91yjvqvg5jndxzhxj",
    "Liga Profesional Argentina": "581t4mywybx21wcpmpykhyzr3",
    "Pro League": "4zwgbb66rif2spcoeeol2motx",
    "Chinese Super League": "82jkgccg7phfjpd0mltdl3pat",
}

NUM_SEASONS = 6  # current + 5 past

DB_DIR = Path(__file__).resolve().parent / "data"
DB_PATH = DB_DIR / "coaches.db"


# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------

def init_db(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS matches (
            match_id TEXT PRIMARY KEY,
            competition TEXT NOT NULL,
            season TEXT NOT NULL,
            match_date TEXT NOT NULL,
            matchday INTEGER,
            home_team_id TEXT NOT NULL,
            home_team_name TEXT NOT NULL,
            away_team_id TEXT NOT NULL,
            away_team_name TEXT NOT NULL,
            home_goals INTEGER,
            away_goals INTEGER,
            home_coach_id TEXT,
            home_coach_name TEXT,
            away_coach_id TEXT,
            away_coach_name TEXT,
            home_possession REAL,
            away_possession REAL,
            home_formation TEXT,
            away_formation TEXT
        );

        CREATE TABLE IF NOT EXISTS team_progression (
            competition TEXT NOT NULL,
            season TEXT NOT NULL,
            team_id TEXT NOT NULL,
            team_name TEXT NOT NULL,
            matchday INTEGER NOT NULL,
            match_date TEXT,
            coach_id TEXT,
            coach_name TEXT,
            result TEXT,
            goals_for INTEGER,
            goals_against INTEGER,
            cumulative_points INTEGER,
            cumulative_matches INTEGER,
            points_pct REAL,
            coach_change INTEGER DEFAULT 0,
            PRIMARY KEY (competition, season, team_id, matchday)
        );
    """)


def _migrate_db(conn: sqlite3.Connection):
    """Add new columns if they don't exist yet."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(matches)").fetchall()}
    for col, typ in [("home_possession", "REAL"), ("away_possession", "REAL"),
                     ("home_formation", "TEXT"), ("away_formation", "TEXT")]:
        if col not in cols:
            conn.execute(f"ALTER TABLE matches ADD COLUMN {col} {typ}")
    conn.commit()


def _backfill_possession_formation(conn: sqlite3.Connection):
    """Fetch possession/formation for matches that don't have it."""
    _migrate_db(conn)
    rows = conn.execute(
        "SELECT match_id FROM matches WHERE home_possession IS NULL"
    ).fetchall()
    match_ids = [r[0] for r in rows]
    print(f"Backfilling stats for {len(match_ids)} matches...")
    if not match_ids:
        return

    def fetch_with_delay(mid: str, idx: int) -> dict:
        time.sleep(idx * 0.05)
        return fetch_coach_info(mid)

    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {
            executor.submit(fetch_with_delay, mid, i): mid
            for i, mid in enumerate(match_ids)
        }
        done_count = 0
        for future in as_completed(futures):
            done_count += 1
            if done_count % 50 == 0 or done_count == len(match_ids):
                print(f"  Progress: {done_count}/{len(match_ids)}")
            try:
                data = future.result()
                conn.execute(
                    """UPDATE matches SET home_possession=?, away_possession=?,
                       home_formation=?, away_formation=? WHERE match_id=?""",
                    (data.get("home_possession"), data.get("away_possession"),
                     data.get("home_formation"), data.get("away_formation"),
                     data["match_id"]),
                )
            except Exception as e:
                print(f"  [WARN] {e}")
    conn.commit()
    print(f"  Updated {len(match_ids)} matches.")


def get_existing_match_ids(conn: sqlite3.Connection) -> set:
    cur = conn.execute("SELECT match_id FROM matches")
    return {row[0] for row in cur.fetchall()}


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def fetch_seasons(comp_id: str) -> list[dict]:
    """Get tournament calendar (list of seasons) for a competition."""
    url = f"{_BASE_API}/tournamentcalendar/{OUTLET}/?_rt=c&comp={comp_id}&_lcl=en"
    data = _api_get(url)
    calendars = data.get("competition", [{}])[0].get("tournamentCalendar", [])
    # Sort by startDate descending so index 0 = most recent
    calendars.sort(key=lambda s: s.get("startDate", ""), reverse=True)
    return calendars[:NUM_SEASONS]


def fetch_matches(season_id: str) -> list[dict]:
    """Get all matches for a season, paginating if needed."""
    all_matches = []
    page = 1
    while True:
        url = (
            f"{_BASE_API}/match/{OUTLET}/"
            f"?_rt=c&tmcl={season_id}&live=yes&_pgSz=200&_pgNm={page}&_lcl=en"
        )
        data = _api_get(url)
        batch = data.get("match", [])
        if not batch:
            break
        all_matches.extend(batch)
        if len(batch) < 200:
            break
        page += 1
    return all_matches


def extract_coach(lineup_entry: dict) -> tuple[str | None, str | None]:
    """Extract the head coach from a lineup entry.

    Priority: 'manager' > 'coach' > first available official.
    """
    officials = lineup_entry.get("teamOfficial", [])
    if not officials:
        return None, None

    priority = {"manager": 0, "coach": 1}
    officials_sorted = sorted(
        officials,
        key=lambda o: priority.get(o.get("type", "").lower(), 99),
    )
    best = officials_sorted[0]
    coach_id = best.get("id")
    first = best.get("firstName", "")
    last = best.get("lastName", "")
    coach_name = f"{first} {last}".strip() or None
    return coach_id, coach_name


def _get_team_stat(entry: dict, stat_name: str):
    for s in entry.get("stat", []):
        if s.get("type") == stat_name:
            return s.get("value")
    return None


def fetch_coach_info(match_id: str) -> dict:
    """Fetch matchstats and extract home/away coach, possession, formation."""
    url = (
        f"{_BASE_API}/matchstats/{OUTLET}/{match_id}"
        f"?_rt=c&detailed=yes&_lcl=en"
    )
    try:
        data = _api_get(url)
    except Exception as e:
        print(f"    [WARN] Failed to fetch matchstats for {match_id}: {e}")
        return {"match_id": match_id, "home": (None, None), "away": (None, None),
                "home_possession": None, "away_possession": None,
                "home_formation": None, "away_formation": None}

    lineups = data.get("liveData", {}).get("lineUp", [])
    contestants = data.get("matchInfo", {}).get("contestant", [])
    home_id = contestants[0]["id"] if len(contestants) > 0 else None

    result = {"match_id": match_id, "home": (None, None), "away": (None, None),
              "home_possession": None, "away_possession": None,
              "home_formation": None, "away_formation": None}
    for entry in lineups:
        cid = entry.get("contestantId")
        coach = extract_coach(entry)
        possession = _get_team_stat(entry, "possessionPercentage")
        formation = entry.get("formationUsed") or _get_team_stat(entry, "formationUsed")
        if cid == home_id:
            result["home"] = coach
            result["home_possession"] = float(possession) if possession else None
            result["home_formation"] = str(formation) if formation else None
        else:
            result["away"] = coach
            result["away_possession"] = float(possession) if possession else None
            result["away_formation"] = str(formation) if formation else None
    return result


# ---------------------------------------------------------------------------
# Main fetch logic
# ---------------------------------------------------------------------------

def process_season(
    conn: sqlite3.Connection,
    comp_name: str,
    season: dict,
    existing_ids: set,
    dry_run: bool,
):
    season_id = season["id"]
    season_name = season.get("name", season_id)
    print(f"\n  Season: {season_name}")

    matches_raw = fetch_matches(season_id)
    played = [
        m for m in matches_raw
        if m.get("liveData", {}).get("matchDetails", {}).get("matchStatus") == "Played"
    ]
    print(f"    Total matches: {len(matches_raw)}, played: {len(played)}")

    # Filter out already-fetched
    new_matches = [m for m in played if m["matchInfo"]["id"] not in existing_ids]
    print(f"    New (not in DB): {len(new_matches)}")

    if dry_run or not new_matches:
        return

    # Parse basic match data first
    match_rows = {}
    for m in new_matches:
        info = m["matchInfo"]
        mid = info["id"]
        contestants = info.get("contestant", [])
        if len(contestants) < 2:
            continue
        home = contestants[0]
        away = contestants[1]
        scores = (
            m.get("liveData", {})
            .get("matchDetails", {})
            .get("scores", {})
            .get("total", {})
        )
        match_rows[mid] = {
            "match_id": mid,
            "competition": comp_name,
            "season": season_name,
            "match_date": info.get("date", ""),
            "home_team_id": home["id"],
            "home_team_name": home.get("name", ""),
            "away_team_id": away["id"],
            "away_team_name": away.get("name", ""),
            "home_goals": int(scores.get("home", 0)),
            "away_goals": int(scores.get("away", 0)),
            "home_coach_id": None,
            "home_coach_name": None,
            "away_coach_id": None,
            "away_coach_name": None,
            "home_possession": None,
            "away_possession": None,
            "home_formation": None,
            "away_formation": None,
        }

    # Fetch coach info in parallel with rate limiting
    match_ids = list(match_rows.keys())
    print(f"    Fetching coach info for {len(match_ids)} matches...")

    def fetch_with_delay(mid: str, idx: int) -> dict:
        time.sleep(idx * 0.05)  # light stagger across 20 workers
        return fetch_coach_info(mid)

    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {
            executor.submit(fetch_with_delay, mid, i): mid
            for i, mid in enumerate(match_ids)
        }
        done_count = 0
        for future in as_completed(futures):
            done_count += 1
            if done_count % 50 == 0 or done_count == len(match_ids):
                print(f"      Progress: {done_count}/{len(match_ids)}")
            try:
                coach_data = future.result()
                mid = coach_data["match_id"]
                if mid in match_rows:
                    row = match_rows[mid]
                    row["home_coach_id"] = coach_data["home"][0]
                    row["home_coach_name"] = coach_data["home"][1]
                    row["away_coach_id"] = coach_data["away"][0]
                    row["away_coach_name"] = coach_data["away"][1]
                    row["home_possession"] = coach_data.get("home_possession")
                    row["away_possession"] = coach_data.get("away_possession")
                    row["home_formation"] = coach_data.get("home_formation")
                    row["away_formation"] = coach_data.get("away_formation")
            except Exception as e:
                print(f"      [WARN] Coach fetch error: {e}")

    # Insert into DB
    insert_sql = """
        INSERT OR IGNORE INTO matches (
            match_id, competition, season, match_date, matchday,
            home_team_id, home_team_name, away_team_id, away_team_name,
            home_goals, away_goals,
            home_coach_id, home_coach_name, away_coach_id, away_coach_name,
            home_possession, away_possession, home_formation, away_formation
        ) VALUES (
            :match_id, :competition, :season, :match_date, NULL,
            :home_team_id, :home_team_name, :away_team_id, :away_team_name,
            :home_goals, :away_goals,
            :home_coach_id, :home_coach_name, :away_coach_id, :away_coach_name,
            :home_possession, :away_possession, :home_formation, :away_formation
        )
    """
    conn.executemany(insert_sql, list(match_rows.values()))
    conn.commit()
    existing_ids.update(match_rows.keys())
    print(f"    Inserted {len(match_rows)} matches.")


# ---------------------------------------------------------------------------
# Compute team_progression
# ---------------------------------------------------------------------------

KNOWN_NAMES: dict[str, str] = {
    "Hans-Dieter Flick": "Flick",
    "Marcus Sorg": "Sorg",
    "Diego Pablo Simeone González": "Simeone",
    "Carlo Ancelotti": "Ancelotti",
    "Davide Ancelotti": "Davide Ancelotti",
    "Marcelino García Toral": "Marcelino",
    "Juan Rubén Uría Corral": "Uría Corral",
    "José Bordalás Jiménez": "Bordalás",
    "Patricio Moreno Ruiz": "Patricio",
    "Francisco Javier García Pimienta": "García Pimienta",
    "Joaquín de Jesús Caparrós Camino": "Caparrós",
    "Rubén Baraja Vegas": "Baraja",
    "Carlos Corberán Vallet": "Corberán",
    "Luis García Plaza": "Luis García Plaza",
    "Eduardo Germán Coudet": "Coudet",
    "Miguel Ángel Sánchez Muñoz": "Míchel",
    "Salvador Fúnez Sardina": "Fúnez",
    "Jagoba Arrasate Elustondo": "Arrasate",
    "Bittor Alkiza Fernández": "Alkiza",
    "Vicente Moreno Peris": "Vicente Moreno",
    "Ramón Alturo Pons": "Alturo",
    "Daniel Alberto Pendín Sánchez": "Pendín",
    "Manuel Luis Pellegrini Ripamonti": "Pellegrini",
    "Claudio Giráldez González": "Giráldez",
    "Imanol Alguacil Barrenetxea": "Imanol",
    "José Manuel González Álvarez": "Manolo González",
    "Luis Miguel Carrión Delgado": "Carrión",
    "Diego Martínez Penas": "Diego Martínez",
    "Paulo César Pezzolano Suárez": "Pezzolano",
    "Camilo Speranza Borde": "Speranza",
    "Álvaro Rubio Robres": "Álvaro Rubio",
    "Diego Martín Cocca Pera": "Cocca",
    "Ernesto Valverde Tejedor": "Valverde",
    "Borja Jiménez Sáez": "Borja Jiménez",
    "Iñigo Pérez Soto": "Iñigo Pérez",
    # 2021/2022 - 2023/2024 coaches
    "Aitor Karanka de la Hoz": "Karanka",
    "Alexander Jesús Medina Reobasco": "Medina",
    "Andoni Iraola Sagarna": "Iraola",
    "Carlos Augusto Soares da Costa Faria Carvalhal": "Carvalhal",
    "David García Cubillo": "David García",
    "Diego Martín Alonso López": "Diego Alonso",
    "Enrique Setién Solar": "Setién",
    "Enrique Sánchez Flores": "Quique Flores",
    "Francisco Escribá Segura": "Escribá",
    "Francisco Javier Pereira Mejía": "Pereira",
    "Francisco Javier Rodríguez Vílchez": "Rodríguez Vílchez",
    "Francisco José López Fernández": "Paco López",
    "Gaizka Garitano Aguirre": "Garitano",
    "Ivan Gennaro Gattuso": "Gattuso",
    "Javier Aguirre Onaindía": "Aguirre",
    "Javier Calleja Revilla": "Calleja",
    "Joan Francesc Ferrer Sicilia": "Rubi",
    "Jorge Francisco Almirón Quintana": "Almirón",
    "Jorge Luis Sampaoli Moya": "Sampaoli",
    "José Luis Mendilibar Etxebarria": "Mendilibar",
    "José Mel Pérez": "Mel",
    "José Miguel González Martín del Campo": "Míchel González",
    "José Ramón Sandoval Huertas": "Sandoval",
    "José Rojo Martín": "Pacheta",
    "Julen Lopetegui Argote": "Lopetegui",
    "Julio Velázquez Santiago": "Julio Velázquez",
    "Luis Blanco Garrido": "Luis Blanco",
    "Luis García Fernández": "Luis García",
    "Mauricio Andrés Pellegrino Luna": "Pellegrino",
    "Pablo Javier Machín Díez": "Machín",
    "Pablo Sanz Iniesta": "Pablo Sanz",
    "Rafael Benítez Maudes": "Benítez",
    "Robert Moreno González": "Robert Moreno",
    "Rubén Torrecilla González": "Torrecilla",
    "Salvador González Marco": "Voro",
    "Sebastián Andrés Beccacece": "Beccacece",
    "Sergio González Soriano": "Sergio González",
    "Unai Emery Etxegoien": "Emery",
    "Xavier Hernández Creus": "Xavi",
    "Álvaro Cervera Díaz": "Cervera",
    "Óscar Hernández Creus": "Óscar Hernández",
    "Jon Iñaki Aspiazu San Emeterio": "Emeterio",
    "Luis Miguel Ramis Monfort": "Ramis",
    "Mikel Arteta Amatriain": "Arteta",
    # Premier League
    "Bruno Miguel Silva do Nascimento": "Bruno Lage",
    "Christopher John Wilder": "Chris Wilder",
    "David William Moyes": "Moyes",
    "Edward John Frank Howe": "Eddie Howe",
    "Fabian Marc Hürzeler": "Hürzeler",
    "Francisco Javier Muñoz Llompart": "Xisco Muñoz",
    "Frank James Lampard Junior": "Lampard",
    "Gary Paul O'Neil": "O'Neil",
    "Graeme Anthony Jones": "Graeme Jones",
    "Graham Stephen Potter": "Potter",
    "Jason James Tindall": "Tindall",
    "Javier Gracia Carlos": "Javi Gracia",
    "Jesse Alan Marsch": "Marsch",
    "Josep Guardiola i Sala": "Guardiola",
    "Juan Manuel Lillo Díez": "Lillo",
    "Jürgen Norbert Klopp": "Klopp",
    "Liam James Rosenior": "Rosenior",
    "Marcelo Alberto Bielsa Caldera": "Bielsa",
    "Marco Alexandre Saraiva da Silva": "Marco Silva",
    "Mauricio Roberto Pochettino Trossero": "Pochettino",
    "Michael James Jackson": "Michael Jackson",
    "Nathan Jason Jones": "Nathan Jones",
    "Nuno Herlander Simões Espírito Santo": "Nuno",
    "Ole Gunnar Solskjær": "Solskjær",
    "Patrick Donalé Vieira": "Vieira",
    "Ralf Dietrich Rangnick": "Rangnick",
    "Robert Owen Edwards": "Rob Edwards",
    "Roberto De Zerbi": "De Zerbi",
    "Ruben Filipe Marques Diogo Amorim": "Amorim",
    "Rubén Sellés Salvador": "Sellés",
    "Rui Pedro Teixeira de Jesus da Silva": "Rui Silva",
    "Ruud van Nistelrooij": "Van Nistelrooy",
    "Ryan Glen Mason": "Ryan Mason",
    "Régis Le Bris": "Le Bris",
    "Scott Matthew Parker": "Scott Parker",
    "Sean Mark Dyche": "Dyche",
    "Simon Edward Rusk": "Rusk",
    "Steven David Cooper": "Steve Cooper",
    "Steven George Gerrard": "Gerrard",
    "Steven Peter Davis": "Steven Davis",
    "Vincent Jean Mpoy Kompany": "Kompany",
    "Vítor Manuel de Oliveira Lopes Pereira": "Vítor Pereira",
    "Xabier Alonso Olano": "Xabi Alonso",
    "Álvaro Arbeloa Coca": "Arbeloa",
    "Francesc Fàbregas i Soler": "Cesc Fàbregas",
    # Serie A / Serie B
    "Thiago Motta Santon Olivares": "Thiago Motta",
    "Eusebio Luca Di Francesco": "Di Francesco",
    "Giovanni Martusciello": "Martusciello",
    "Paulo Alexandre Rodrigues Fonseca": "Paulo Fonseca",
    # Primeira Liga
    "José Leonardo Nunes Alves Sousa Jardim": "Leonardo Jardim",
    "Sérgio Paulo Marceneiro da Conceição": "Sérgio Conceição",
    "Jorge Fernando Pinheiro de Jesus": "Jorge Jesus",
    "Nelson Alexandre da Silva Veríssimo": "Veríssimo",
    "Bruno Miguel Ribeiro Veríssimo": "Bruno Veríssimo",
    "Rui Manuel Gomes Borges": "Rui Borges",
    "José Mário dos Santos Mourinho Félix": "Mourinho",
    "Abel Fernando Moreira Ferreira": "Abel Ferreira",
    "Vítor Fernando de Carvalho Campelos": "Campelos",
    # Eredivisie
    "Giovanni Christiaan van Bronckhorst": "Van Bronckhorst",
    "Mark Peter Gertruda Andreas van Bommel": "Van Bommel",
    "John Antonius van den Brom": "Van den Brom",
    "Mitchell van der Gaag": "Van der Gaag",
    "Brian Priske Pedersen": "Brian Priske",
    "Hedwiges Eduard Martinus Maduro": "Hedwiges Maduro",
    # Brasileirão
    "Adenor Leonardo Bacchi": "Tite",
    "Luiz Elena Felipe Scolari": "Felipão",
    "Vanderlei Luxemburgo da Silva": "Luxemburgo",
    "Dorival Silvestre Júnior": "Dorival Júnior",
    "Sylvio Mendes Campos Júnior": "Mano Menezes",
    "Filipe Luís Kasmirski": "Filipe Luís",
    "Luis Francisco Zubeldía": "Zubeldía",
    "Abel Carlos da Silva Braga": "Abel Braga",
    "Fábio Luiz Carille de Araujo": "Carille",
    "Roger Machado Marques": "Roger Machado",
    "Maurício Nogueira Barbieri": "Barbieri",
    # Argentina
    "Marcelo Daniel Gallardo": "Gallardo",
    "Gabriel Alejandro Milito": "Milito",
    "Guillermo Barros Schelotto": "Barros Schelotto",
    "Juan Carlos Osorio Arbeláez": "Juan C. Osorio",
    "Gustavo Adolfo Costas Makeira": "Costas",
    "Martín Gastón Demichelis": "Demichelis",
    "Sebastián Alejandro Battaglia": "Battaglia",
    "Ricardo Alberto Zielinski": "Zielinski",
    "Antonio Ricardo Mohamed Matijevich": "Mohamed",
    "Fernando Andrés Gamboa": "Gamboa",
    "Lucas Ademar Bernardi": "Bernardi",
    # Belgian Pro League
    "Alexander Matthias Blessin": "Blessin",
    "Sven Ludwig Vandenbroeck": "Vandenbroeck",
    # Championship
    "Duncan Cowan Ferguson": "Duncan Ferguson",
    "Philip John Parkinson": "Phil Parkinson",
    "Steven William Morison": "Steve Morison",
    "Thomas William Cleverley": "Cleverley",
    # Ligue 2
    "Franck Armel Gérard Haise": "Franck Haise",
    "Didier Frédéric Thierry Digard": "Digard",
    # 2. Bundesliga
    "Dino Nicolas Toppmöller": "Toppmöller",
    "Tobias Schweinsteiger": "T. Schweinsteiger",
    # Segunda División
    "Luis Enrique Martínez García": "Luis Enrique",
    "Abelardo Fernández Antuña": "Abelardo",
    "Francisco Jémez Martín": "Paco Jémez",
    "Asier Garitano Aguirrezábal": "Garitano",
    "Juan Ignacio Martínez Jiménez": "Jim",
    "Luis Lucas Alcaraz González": "Alcaraz",
    "José Luis Oltra Castañer": "Oltra",
    "Julián Calero Fernández": "Calero",
    "Cristóbal Parralo Aguilera": "Parralo",
    # General
    "Jose Paulo Sousa da Silva": "Paulo Sousa",
    "Valérien Alexandre Ismaël": "Ismaël",
    "Paul Joseph Marie Le Guen": "Le Guen",
    "Patrick Fabio Maxime Kisnorbo": "Kisnorbo",
    "Phillip John-William Cocu": "Cocu",
    "Jess Christian Thorup": "Thorup",
}


def _known_name(full_name: str | None) -> str | None:
    if not full_name:
        return full_name
    return KNOWN_NAMES.get(full_name, full_name)


def _merge_interim_coaches(matches: list[dict]) -> list[dict]:
    """Replace interim/staff coaches with the real head coach.

    Core rule: if a coach appears in more than one stint for the same team
    in a season, they were temporarily replaced (suspension etc.) — the
    gaps are filled by staff members who get merged into the real coach.
    A sacked coach never comes back to the same team in the same season.
    """
    if len(matches) <= 2:
        return matches

    # Count how many separate stints each coach_id has
    stints: list[dict] = []
    for i, m in enumerate(matches):
        cid = m["coach_id"]
        if not stints or stints[-1]["coach_id"] != cid:
            stints.append({"coach_id": cid, "coach_name": m["coach_name"],
                           "start": i, "end": i, "count": 1})
        else:
            stints[-1]["end"] = i
            stints[-1]["count"] += 1

    if len(stints) <= 1:
        return matches

    # Find coaches that appear in multiple stints → they are the "real" coaches
    from collections import Counter
    stint_counts = Counter(s["coach_id"] for s in stints)
    recurring = {cid for cid, cnt in stint_counts.items() if cnt > 1}

    # Also check trailing stints: if the last stint(s) are short (≤2 matches)
    # and a dominant coach precedes them, it's a staff substitution at season end
    from collections import Counter
    match_totals = Counter()
    for s in stints:
        match_totals[s["coach_id"]] += s["count"]

    stint_counts = Counter(s["coach_id"] for s in stints)
    recurring = {cid for cid, cnt in stint_counts.items() if cnt > 1}

    # Trailing staff: last stint ≤2 matches, previous coach has ≥70% of season
    # Only apply on completed seasons (≥30 matches) to avoid false merges mid-season
    MAX_TRAILING = 2
    total_matches = len(matches)
    last = stints[-1]
    if last["count"] <= MAX_TRAILING and len(stints) >= 2 and total_matches >= 30:
        prev = stints[-2]
        if match_totals[prev["coach_id"]] >= total_matches * 0.7:
            for j in range(last["start"], last["end"] + 1):
                matches[j] = {**matches[j],
                              "coach_id": prev["coach_id"],
                              "coach_name": prev["coach_name"]}
            stints[-2]["end"] = last["end"]
            stints[-2]["count"] += last["count"]
            stints.pop()
            match_totals[prev["coach_id"]] += last["count"]
            # Recalculate stint_counts and recurring
            stint_counts = Counter(s["coach_id"] for s in stints)
            recurring = {cid for cid, cnt in stint_counts.items() if cnt > 1}

    if not recurring:
        return matches

    # If multiple coaches recur, the one with more total matches is the real one
    if len(recurring) > 1:
        recurring = {max(recurring, key=lambda cid: match_totals[cid])}

    # For each gap between stints of a recurring coach, merge the interim
    for real_id in recurring:
        real_name = next(s["coach_name"] for s in stints if s["coach_id"] == real_id)
        real_indices = [idx for idx, s in enumerate(stints) if s["coach_id"] == real_id]

        # If the real coach doesn't start from stint 0, merge opening stints
        # only if they are short (≤5 matches) — interim before the real coach arrived
        first_real = real_indices[0]
        if first_real > 0:
            opening_matches = sum(stints[g]["count"] for g in range(first_real))
            if opening_matches <= 5:
                for g in range(0, first_real):
                    for j in range(stints[g]["start"], stints[g]["end"] + 1):
                        matches[j] = {**matches[j],
                                      "coach_id": real_id,
                                      "coach_name": real_name}

        # Fill gaps between consecutive appearances
        for k in range(len(real_indices) - 1):
            gap_start = real_indices[k]
            gap_end = real_indices[k + 1]
            for g in range(gap_start + 1, gap_end):
                for j in range(stints[g]["start"], stints[g]["end"] + 1):
                    matches[j] = {**matches[j],
                                  "coach_id": real_id,
                                  "coach_name": real_name}

    return matches


def compute_progression(conn: sqlite3.Connection):
    """Rebuild the team_progression table from matches data."""
    print("\nComputing team_progression table...")
    conn.execute("DELETE FROM team_progression")

    rows = conn.execute("""
        SELECT match_id, competition, season, match_date,
               home_team_id, home_team_name, away_team_id, away_team_name,
               home_goals, away_goals,
               home_coach_id, home_coach_name, away_coach_id, away_coach_name
        FROM matches
        ORDER BY competition, season, match_date
    """).fetchall()

    from collections import defaultdict
    team_matches = defaultdict(list)

    for r in rows:
        (mid, comp, season, mdate,
         ht_id, ht_name, at_id, at_name,
         hg, ag, hc_id, hc_name, ac_id, ac_name) = r

        team_matches[(comp, season, ht_id)].append({
            "match_date": mdate,
            "team_name": ht_name,
            "coach_id": hc_id,
            "coach_name": hc_name,
            "goals_for": hg,
            "goals_against": ag,
        })
        team_matches[(comp, season, at_id)].append({
            "match_date": mdate,
            "team_name": at_name,
            "coach_id": ac_id,
            "coach_name": ac_name,
            "goals_for": ag,
            "goals_against": hg,
        })

    insert_sql = """
        INSERT OR REPLACE INTO team_progression (
            competition, season, team_id, team_name, matchday, match_date,
            coach_id, coach_name, result, goals_for, goals_against,
            cumulative_points, cumulative_matches, points_pct, coach_change
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    batch = []
    for (comp, season, team_id), matches in team_matches.items():
        matches.sort(key=lambda m: m["match_date"])

        # Merge interim/assistant coaches into the real head coach
        matches = _merge_interim_coaches(matches)

        coach_points = 0
        coach_matches = 0
        prev_coach_id = None

        for i, m in enumerate(matches):
            matchday = i + 1
            gf = m["goals_for"] or 0
            ga = m["goals_against"] or 0

            if gf > ga:
                result = "W"
                points = 3
            elif gf == ga:
                result = "D"
                points = 1
            else:
                result = "L"
                points = 0

            coach_change = 0
            if m["coach_id"] and prev_coach_id and m["coach_id"] != prev_coach_id:
                coach_change = 1
                coach_points = 0
                coach_matches = 0

            coach_points += points
            coach_matches += 1
            pts_pct = (coach_points / (coach_matches * 3)) * 100 if coach_matches > 0 else 0.0

            # Apply known name
            display_name = _known_name(m["coach_name"])

            batch.append((
                comp, season, team_id, m["team_name"], matchday, m["match_date"],
                m["coach_id"], display_name, result, gf, ga,
                coach_points, coach_matches, round(pts_pct, 2), coach_change,
            ))
            prev_coach_id = m["coach_id"] if m["coach_id"] else prev_coach_id

    conn.executemany(insert_sql, batch)
    conn.commit()
    print(f"  Inserted {len(batch)} progression rows.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Fetch Scoresway match data")
    parser.add_argument("--competition", type=str, help="Filter by competition name")
    parser.add_argument("--season", type=str, help="Filter by season name")
    parser.add_argument("--dry-run", action="store_true", help="Show plan without fetching")
    parser.add_argument(
        "--refresh-progression", action="store_true",
        help="Only recompute team_progression from existing data",
    )
    parser.add_argument(
        "--backfill-stats", action="store_true",
        help="Backfill possession/formation for existing matches",
    )
    args = parser.parse_args()

    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    init_db(conn)

    if args.refresh_progression:
        compute_progression(conn)
        conn.close()
        print("Done.")
        return

    if args.backfill_stats:
        _backfill_possession_formation(conn)
        conn.close()
        print("Done.")
        return

    existing_ids = get_existing_match_ids(conn)
    print(f"Existing matches in DB: {len(existing_ids)}")

    comps = COMPETITIONS
    if args.competition:
        comps = {
            k: v for k, v in COMPETITIONS.items()
            if args.competition.lower() in k.lower()
        }
        if not comps:
            print(f"No competition matching '{args.competition}'")
            sys.exit(1)

    for comp_name, comp_id in comps.items():
        print(f"\n{'='*60}")
        print(f"Competition: {comp_name}")
        print(f"{'='*60}")

        try:
            seasons = fetch_seasons(comp_id)
        except Exception as e:
            print(f"  [ERROR] Failed to fetch seasons: {e}")
            continue

        if args.season:
            seasons = [s for s in seasons if args.season.lower() in s.get("name", "").lower()]
            if not seasons:
                print(f"  No season matching '{args.season}'")
                continue

        for season in seasons:
            try:
                process_season(conn, comp_name, season, existing_ids, args.dry_run)
            except Exception as e:
                print(f"  [ERROR] Failed season {season.get('name', '?')}: {e}")
                continue

    # Recompute progression
    if not args.dry_run:
        compute_progression(conn)

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
