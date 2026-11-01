"""
FMInside coach photo downloader for the coach progression app.

Strategy:
  1. For each coach, search fminside by name (multiple variations)
  2. Match results by name similarity + team cross-reference with our DB
  3. Download photos from img.fminside.net CDN (no anti-bot protection)

Resumable: saves progress after every 10 coaches. Ctrl+C safe.

Run: python3 scrape_coach_photos.py
"""

import json
import csv
import time
import re
import signal
import threading
import sqlite3
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.request import urlopen, Request

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://fminside.net"
UPDATE_FILTER_URL = f"{BASE_URL}/resources/inc/ajax/update_filter.php"

SCRIPT_DIR = Path(__file__).parent
RESULTS_FILE = SCRIPT_DIR / "data" / "fminside_staff.json"
COACHES_CSV = SCRIPT_DIR / "static" / "coaches" / "coach_list.csv"
PHOTOS_DIR = SCRIPT_DIR / "static" / "coaches"
DB_PATH = SCRIPT_DIR / "data" / "coaches.db"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Referer": f"{BASE_URL}/staff",
}
AJAX_HEADERS = {**HEADERS, "X-Requested-With": "XMLHttpRequest", "Accept": "*/*"}

_shutdown = threading.Event()

def _handle_sigint(sig, frame):
    print("\n[!] Ctrl+C — saving and stopping...")
    _shutdown.set()

signal.signal(signal.SIGINT, _handle_sigint)


def normalize(name):
    name = unicodedata.normalize("NFD", name)
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")
    name = name.lower().strip()
    name = re.sub(r"[^a-z0-9 ]", "", name)
    name = re.sub(r"\s+", " ", name)
    return name


def parse_staff_rows(html) -> list[dict]:
    """Parse all staff rows from an fminside HTML page."""
    soup = BeautifulSoup(html, "html.parser")
    rows = [tr for tr in soup.find_all("tr")
            if tr.find("a", href=re.compile(r"/staff/\d+-fm-\d+/\d+"))]
    results = []
    for tr in rows:
        a = tr.find("a", href=re.compile(r"/staff/\d+-fm-\d+/\d+"))
        if not a:
            continue
        staff_url = a["href"]
        if not staff_url.startswith("http"):
            staff_url = BASE_URL + staff_url

        fm_id_match = re.search(r"/staff/\d+-fm-\d+/(\d+)-", staff_url)
        fm_id = fm_id_match.group(1) if fm_id_match else ""

        name_el = a.find("strong")
        name = name_el.get_text(strip=True) if name_el else ""
        if not name:
            continue

        club_el = tr.find(class_="staff-person-club")
        club_name = club_el.get("title", "") if club_el else ""

        role_el = tr.find(class_="staff-person-role")
        role = role_el.get_text(strip=True) if role_el else ""

        results.append({
            "name": name,
            "url": staff_url,
            "fm_id": fm_id,
            "club_name": club_name,
            "role": role,
            "name_normalized": normalize(name),
        })
    return results


def search_fminside(session, search_name) -> list[dict]:
    """Search fminside staff by name using GET params."""
    try:
        r = session.get(
            f"{BASE_URL}/staff",
            params={"q": search_name, "database_id": "7"},
            headers=HEADERS, timeout=15,
        )
        return parse_staff_rows(r.text)
    except Exception as e:
        return []


def init_session():
    session = requests.Session()
    session.get(f"{BASE_URL}/staff", headers=HEADERS, timeout=30)
    return session


def load_coaches() -> list[dict]:
    coaches = []
    with open(COACHES_CSV, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row["name_normalized"] = normalize(row["coach_name"])
            coaches.append(row)
    return coaches


def load_coach_teams() -> dict:
    """Load all teams each coach has managed from our DB. Returns {coach_id: [team_names]}."""
    db = sqlite3.connect(str(DB_PATH))
    rows = db.execute(
        "SELECT DISTINCT coach_id, team_name FROM team_progression WHERE coach_id IS NOT NULL"
    ).fetchall()
    db.close()
    teams = {}
    for coach_id, team_name in rows:
        if coach_id not in teams:
            teams[coach_id] = []
        teams[coach_id].append(normalize(team_name))
    return teams


# Common club name mappings between our DB and fminside
CLUB_ALIASES = {
    "real madrid": {"r. madrid", "real madrid cf", "real madrid"},
    "barcelona": {"fc barcelona", "barcelona", "barça"},
    "atletico de madrid": {"atletico madrid", "atl. madrid", "atlético de madrid", "atlético madrid"},
    "atletico madrid": {"atletico de madrid", "atl. madrid"},
    "bayern munich": {"fc bayern münchen", "bayern münchen", "bayern munich"},
    "borussia dortmund": {"bor. dortmund", "borussia dortmund"},
    "paris saint-germain": {"paris saint-germain", "psg", "paris sg"},
    "inter": {"internazionale", "inter milan", "inter"},
    "ac milan": {"ac milan", "milan"},
    "rb leipzig": {"rb leipzig", "rasenballsport leipzig"},
    "tottenham hotspur": {"tottenham", "spurs"},
    "manchester united": {"man. united", "man utd", "manchester united"},
    "manchester city": {"man. city", "man city", "manchester city"},
}


def teams_match(our_teams_norm, fm_club_name):
    """Check if fminside club matches any of our coach's teams."""
    if not fm_club_name:
        return False
    fm_norm = normalize(fm_club_name)

    for our_team in our_teams_norm:
        # Direct substring match
        if our_team in fm_norm or fm_norm in our_team:
            return True
        # Check parts overlap (e.g. "palmeiras" in "SE Palmeiras")
        our_parts = set(our_team.split())
        fm_parts = set(fm_norm.split())
        common = our_parts & fm_parts
        # If main club word matches (skip tiny words like "fc", "sc", "cf")
        significant = {w for w in common if len(w) >= 4}
        if significant:
            return True
        # Check aliases
        for key, aliases in CLUB_ALIASES.items():
            if key in our_team or our_team in key:
                if any(a in fm_norm for a in aliases):
                    return True
    return False


def build_search_terms(coach_name):
    """Generate multiple search term variations for a coach name."""
    parts = coach_name.split()
    terms = []

    if len(parts) == 1:
        # Single name like "Simeone" -> search as-is
        terms.append(coach_name)
    elif len(parts) == 2:
        # "Marco Silva" -> try full, then just surname
        terms.append(coach_name)
        terms.append(parts[-1])
    elif len(parts) == 3:
        # "Gian Piero Gasperini" -> full, first+last, just last
        terms.append(coach_name)
        terms.append(f"{parts[0]} {parts[-1]}")
        terms.append(parts[-1])
    else:
        # "José Leonardo Nunes Alves" -> first+last, just last, full
        terms.append(f"{parts[0]} {parts[-1]}")
        terms.append(parts[-1])
        terms.append(coach_name)

    # Deduplicate preserving order
    seen = set()
    unique = []
    for t in terms:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return unique


def find_best_match(coach_name, coach_norm, coach_teams_norm, results):
    """Find the best matching staff member, using name + team verification."""
    coach_parts = coach_norm.split()
    coach_set = set(coach_parts)
    candidates = []

    for s in results:
        sn = s["name_normalized"]
        s_parts = set(sn.split())
        common = coach_set & s_parts
        if not common:
            continue

        # Must match the surname
        surname = coach_parts[-1]
        if surname not in s_parts:
            if len(coach_parts) >= 2 and coach_parts[-2] not in s_parts:
                continue

        # For multi-part names, require at least 2 parts in common
        if len(coach_set) >= 2 and len(s_parts) >= 2 and len(common) < 2:
            continue

        name_score = len(common) / max(len(coach_set), 1) + len(common) / max(len(s_parts), 1)

        # Bonus for team match
        team_bonus = 0.5 if teams_match(coach_teams_norm, s.get("club_name", "")) else 0

        # Bonus for manager role
        role = s.get("role", "").lower()
        role_bonus = 0.3 if "manager" in role else 0.1 if "coach" in role or "head" in role else 0

        total_score = name_score + team_bonus + role_bonus
        candidates.append((s, total_score, name_score))

    if not candidates:
        return None, 0

    # Sort by total score, pick best
    candidates.sort(key=lambda x: x[1], reverse=True)
    best, total_score, name_score = candidates[0]

    # Accept if name score >= 1.0 (good name match) OR team matches
    if name_score >= 1.0:
        return best, total_score
    return None, 0


def phase1_search_coaches(coaches, coach_teams):
    """Phase 1: Search fminside for each coach by name."""
    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Load existing progress
    done = {}
    if RESULTS_FILE.exists():
        with open(RESULTS_FILE, encoding="utf-8") as f:
            for entry in json.load(f):
                cid = entry.get("coach_id")
                if cid:
                    done[cid] = entry

    pending = [c for c in coaches if c["coach_id"] not in done]
    matched_so_far = sum(1 for v in done.values() if v.get("fm_id"))
    print(f"[Phase 1] Searching fminside for {len(coaches)} coaches")
    print(f"  Already done: {len(done)} ({matched_so_far} matched)")
    print(f"  Pending: {len(pending)}")

    if not pending:
        return list(done.values())

    session = init_session()
    errors_in_row = 0

    for i, coach in enumerate(pending):
        if _shutdown.is_set():
            break

        coach_name = coach["coach_name"]
        coach_id = coach["coach_id"]
        coach_norm = coach["name_normalized"]
        coach_teams_norm = coach_teams.get(coach_id, [])

        search_terms = build_search_terms(coach_name)
        match_found = None

        for term in search_terms:
            if _shutdown.is_set():
                break

            results = search_fminside(session, term)
            time.sleep(1.5)

            if results:
                errors_in_row = 0
                best, score = find_best_match(coach_name, coach_norm, coach_teams_norm, results)
                if best:
                    match_found = {
                        "coach_id": coach_id,
                        "coach_name": coach_name,
                        "fm_id": best["fm_id"],
                        "fm_name": best["name"],
                        "fm_club": best.get("club_name", ""),
                        "fm_role": best.get("role", ""),
                        "our_teams": ", ".join(coach_teams_norm[:3]),
                        "score": round(score, 2),
                    }
                    break
            else:
                errors_in_row += 1
                if errors_in_row >= 5:
                    print("  Too many empty results — reinitializing session...")
                    try:
                        session = init_session()
                        errors_in_row = 0
                        time.sleep(3)
                    except Exception:
                        time.sleep(10)

        if not match_found:
            match_found = {
                "coach_id": coach_id,
                "coach_name": coach_name,
                "fm_id": "",
                "fm_name": "",
                "score": 0,
            }

        done[coach_id] = match_found

        if (i + 1) % 10 == 0 or _shutdown.is_set():
            matched = sum(1 for v in done.values() if v.get("fm_id"))
            print(f"  Progress: {i + 1}/{len(pending)} searched, {matched}/{len(done)} matched")
            with open(RESULTS_FILE, "w", encoding="utf-8") as f:
                json.dump(list(done.values()), f, ensure_ascii=False)

    # Final save
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(list(done.values()), f, ensure_ascii=False)

    matched = sum(1 for v in done.values() if v.get("fm_id"))
    print(f"[Phase 1] Done — {matched} matched out of {len(done)} searched.")
    return list(done.values())


def phase2_download_photos(results):
    """Phase 2: Download photos from fminside CDN."""
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)

    to_download = []
    for r in results:
        fm_id = r.get("fm_id", "")
        coach_id = r.get("coach_id", "")
        if not fm_id or not coach_id:
            continue
        dest = PHOTOS_DIR / f"{coach_id}.png"
        if dest.exists():
            continue
        url = f"https://img.fminside.net/facesfm26/{fm_id}.png"
        to_download.append((coach_id, fm_id, url, dest))

    if not to_download:
        matched = sum(1 for r in results if r.get("fm_id"))
        print(f"\n[Phase 2] All {matched} matched photos already downloaded.")
        return

    print(f"\n[Phase 2] Downloading {len(to_download)} photos from CDN...")

    downloaded = 0
    failed = 0

    def dl(item):
        coach_id, fm_id, url, dest = item
        try:
            req = Request(url, headers={"User-Agent": "CoachApp/1.0"})
            with urlopen(req, timeout=15) as resp:
                data = resp.read()
            if len(data) < 500:
                return False
            with open(dest, "wb") as f:
                f.write(data)
            return True
        except Exception:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {}
        for item in to_download:
            if _shutdown.is_set():
                break
            futures[pool.submit(dl, item)] = item
            time.sleep(0.1)

        for future in as_completed(futures):
            try:
                if future.result():
                    downloaded += 1
                else:
                    failed += 1
            except Exception:
                failed += 1

            total = downloaded + failed
            if total % 50 == 0:
                print(f"  Progress: {total}/{len(to_download)} ({downloaded} ok, {failed} failed)")

    print(f"[Phase 2] Done — {downloaded} downloaded, {failed} failed.")


def main():
    coaches = load_coaches()
    print(f"Loaded {len(coaches)} coaches from CSV.")

    coach_teams = load_coach_teams()
    print(f"Loaded team history for {len(coach_teams)} coaches from DB.\n")

    # Phase 1: search & match
    results = phase1_search_coaches(coaches, coach_teams)
    if _shutdown.is_set():
        print("\nStopped — progress saved. Run again to resume.")
        return

    # Summary
    matched = [r for r in results if r.get("fm_id")]
    unmatched = [r for r in results if not r.get("fm_id")]
    print(f"\nSummary: {len(matched)} matched, {len(unmatched)} unmatched out of {len(results)}")

    # Phase 2: download photos
    phase2_download_photos(results)

    print(f"\nAll done! Photos in: {PHOTOS_DIR}")


if __name__ == "__main__":
    main()
