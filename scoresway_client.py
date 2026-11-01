"""
Scoresway HTTP client.

Scoresway serves data via api.performfeeds.com using JSONP.
Two strategies to get clean JSON:
  1. _fmt=json  → pure JSON, no callback needed  (preferred, try first)
  2. _fmt=jsonp → strip the JSONP wrapper         (fallback)

The sdapi_outlet_key is extracted automatically from the competition page.
"""

import re
import json
import time
import requests
from bs4 import BeautifulSoup
from typing import Optional

_UA = (
    "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Mobile Safari/537.36"
)

# Headers for HTML page scraping (outlet_key extraction)
_HTML_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9,es;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}

# Headers for PerformFeeds API calls (minimal — Sec-Fetch breaks it)
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-GB,en;q=0.9",
}

_BASE_API = "https://api.performfeeds.com/soccerdata"


def _strip_jsonp(text: str) -> dict:
    start = text.find("(") + 1
    end = text.rfind(")")
    return json.loads(text[start:end])


_CLBK = "W3e5b6d24ce17d07026eae50774df00c7b0fc46602"
_REFERER_BASE = "https://www.scoresway.com/"


def _api_get(url_base: str, referer: Optional[str] = None, retries: int = 3) -> dict:
    """
    Fetch a PerformFeeds URL using JSONP (required — pure JSON needs auth).
    Appends &_fmt=jsonp&sps=widgets&_clbk=W3poc automatically.
    Retries up to `retries` times with exponential backoff on API errors.
    """
    url = f"{url_base}&_fmt=jsonp&sps=widgets&_clbk={_CLBK}"
    headers = _HEADERS.copy()
    headers["Referer"] = referer or _REFERER_BASE

    last_err = None
    for attempt in range(retries):
        if attempt > 0:
            wait = 3 * attempt
            print(f"  ↩ Reintentando en {wait}s (intento {attempt+1}/{retries}) ...")
            time.sleep(wait)
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            data = _strip_jsonp(resp.text)
            if "errorCode" in data and not data.get("match") and not data.get("liveData"):
                last_err = ValueError(f"API error: {data}")
                continue
            return data
        except (requests.HTTPError, ValueError) as e:
            last_err = e
            continue
        except Exception as e:
            raise

    raise last_err


def get_outlet_key(competition_url: str) -> str:
    """
    Extract sdapi_outlet_key from a Scoresway competition page HTML.
    Falls back to checking the /results page if /fixtures fails.
    """
    for url in [competition_url, competition_url.replace("/fixtures", "/results")]:
        try:
            resp = requests.get(url, headers=_HTML_HEADERS, timeout=30)
            if resp.status_code == 403:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            for script in soup.find_all("script"):
                text = script.string or ""
                if "sdapi_outlet_key" in text:
                    m = re.search(r'sdapi_outlet_key\s*[":]\s*"([^"]+)"', text)
                    if m:
                        return m.group(1)
        except Exception:
            continue
    raise ValueError(
        "sdapi_outlet_key not found in page. "
        "Pass it manually: Competition(url, outlet_key='xxx')"
    )


def fetch_fixture(outlet_key: str, tournament_id: str, page_size: int = 400,
                  id_type: str = "tmcl") -> dict:
    """Return the fixture/match list for a tournament.

    id_type: 'tmcl' = tournament calendar (single season, default)
             'comp' = competition ID (all seasons)
    """
    param = "comp" if id_type == "comp" else "tmcl"
    url = (
        f"{_BASE_API}/match/{outlet_key}/"
        f"?_rt=c&{param}={tournament_id}&live=yes&_pgSz={page_size}&_lcl=en"
    )
    return _api_get(url)


def fetch_match_events(outlet_key: str, match_id: str, referer: Optional[str] = None) -> dict:
    """Return raw matchevent JSON for a single match (Opta format)."""
    url = f"{_BASE_API}/matchevent/{outlet_key}/{match_id}?_rt=c&_lcl=en"
    return _api_get(url, referer=referer)


def fetch_match_stats(outlet_key: str, match_id: str) -> dict:
    """Return matchstats JSON (formations, player stats)."""
    url = f"{_BASE_API}/matchstats/{outlet_key}/{match_id}?_rt=c&detailed=yes&_lcl=en"
    return _api_get(url)


def fetch_squads(outlet_key: str, tournament_id: str, team_id: Optional[str] = None) -> dict:
    """Return squads for a tournament (all teams or a specific team)."""
    if team_id:
        url = f"{_BASE_API}/squads/{outlet_key}/?_rt=c&ctst={team_id}&tmcl={tournament_id}&_pgSz=200&_lcl=en"
    else:
        url = f"{_BASE_API}/squads/{outlet_key}/?_rt=c&tmcl={tournament_id}&_pgSz=200&_lcl=en"
    return _api_get(url)
