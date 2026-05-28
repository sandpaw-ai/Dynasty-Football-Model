"""Tankathon NFL Big Board scraper (v3.3).

Pulls the current/upcoming NFL Draft big board from Tankathon:
    https://www.tankathon.com/nfl/big_board

Tankathon publishes a free, daily-refreshed 2027 big board (the
upcoming draft after this year's). PFF's 2027 big board is gated;
Phil's brief explicitly named PFF, but for a free, publicly-available
source that doesn't require login, Tankathon is the strongest substitute
right now. We fall through to PFF if/when access becomes available.

Output records carry only the fields needed by the prospects UI:
    rank, name, position, school, height, weight, jersey_number,
    college_slug (lower-cased school for join keys).
"""
from __future__ import annotations

import html as html_lib
import logging
import re
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)


BASE_URL = "https://www.tankathon.com/nfl/big_board"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"
)
CACHE_DIR = Path("data/tankathon_cache")
HTTP_TIMEOUT_SECS = 30
SKILL_POSITIONS = {"QB", "RB", "WR", "TE"}


def _http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECS) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_html(*, cache: bool = True) -> str:
    cache_path = CACHE_DIR / "big_board.html"
    if cache and cache_path.exists():
        return cache_path.read_text(encoding="utf-8", errors="replace")
    text = _http_get(BASE_URL)
    if cache:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(text, encoding="utf-8")
    return text


@dataclass
class BigBoardProspect:
    rank: int
    name: str
    position: str
    school: Optional[str]
    college_slug: Optional[str]
    height: Optional[str]
    weight: Optional[int]
    draft_year: int  # the year this player would be drafted (2027, 2028 future, etc.)

    def to_dict(self) -> dict:
        return asdict(self)


# Each "Overall Rank" row looks roughly like:
#   <div class="mock-row nfl" data-pos="WR">
#     <div class="mock-row-pick-number">1</div>
#     <div class="mock-row-logo"><a href="/nfl/colleges/ohio-state">...
#     <div class="mock-row-player">
#       <a href="/nfl/players/<slug>">
#         <div class="mock-row-name">Jeremiah Smith</div>
#         <div class="mock-row-school-position">WR | Ohio State </div>
#     </a></div>
#     <div class="mock-row-measurements ..."> ... height/weight ...
#
# A "future" (2028, 2029) row has class "mock-row nfl future" and the
# pick-number cell contains the draft-year label instead of a numeric
# rank. We capture those separately so the UI can group by draft class.

_ROW_RE = re.compile(
    r'<div class="mock-row nfl([^"]*)" data-pos="([A-Z]+)">'
    r'\s*<div class="mock-row-pick-number(?:\s+[^"]*)?">([^<]+)</div>'
    r'\s*<div class="mock-row-logo">'
    r'(?:.*?<a href="/nfl/colleges/([a-z0-9\-]+)">)?'
    r'.*?<div class="mock-row-name">([^<]+)</div>'
    r'.*?<div class="mock-row-school-position">[^|]*\|\s*([^<]+?)\s*</div>'
    r'.*?</a>\s*</div>'
    r'(?:.*?<div>(\d+)&#39;(\d+)(?:&#34;)?</div>\s*<div>(\d+)</div>)?',
    re.IGNORECASE | re.DOTALL,
)


def _text(s: str) -> str:
    return html_lib.unescape(s).strip()


def _parse_int(s: str) -> Optional[int]:
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def parse(html: str, *, current_draft_year: int = 2027) -> List[BigBoardProspect]:
    """Parse Tankathon big_board HTML into a list of BigBoardProspect.

    ``current_draft_year`` is the canonical "next NFL draft" year. The
    page lists 2027 players with a numeric rank (1..N); future-draft
    players (2028 / 2029) appear in the same flow with a year string
    in the pick-number cell. We keep both, tagging them with the right
    draft_year so the prospects UI can filter / display them.
    """
    out: List[BigBoardProspect] = []
    for m in _ROW_RE.finditer(html):
        classes, pos, pick_label, college_slug, name, school, ft, inch, wt = m.groups()
        pick_label = _text(pick_label)
        # Numeric pick = 2027 draft. Year label = future-draft row.
        rank_int = _parse_int(pick_label)
        if "future" in (classes or ""):
            draft_year = _parse_int(pick_label) or (current_draft_year + 1)
            rank = -1  # No global rank when future-tagged
        else:
            draft_year = current_draft_year
            rank = rank_int if rank_int is not None else -1
        height = f"{ft}'{inch}\"" if ft and inch else None
        out.append(BigBoardProspect(
            rank=rank,
            name=_text(name),
            position=pos,
            school=_text(school) or None,
            college_slug=college_slug,
            height=height,
            weight=_parse_int(wt) if wt else None,
            draft_year=draft_year,
        ))
    # Deduplicate by (name, school) — the "By School" tab repeats the
    # same player records lower in the same DOM. Keep the first
    # occurrence so the numeric overall-rank wins.
    seen: set = set()
    deduped: List[BigBoardProspect] = []
    for p in out:
        key = (p.name.lower(), p.school.lower() if p.school else "")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(p)
    return deduped


def fetch_and_parse(*, current_draft_year: int = 2027, cache: bool = True) -> List[BigBoardProspect]:
    return parse(fetch_html(cache=cache), current_draft_year=current_draft_year)
