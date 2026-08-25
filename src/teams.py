"""
Canonical team reference for the 2026 Mountain West and a name-alias crosswalk.

Why this file exists: the #1 source of silent bugs in CFB data work is team-name
drift across sources ("San Jose State" vs "San José State", odds archives that
abbreviate to "San Jose St", etc). Route every source through normalize_team_name()
before joining anything.
"""

# The 10 Mountain West football members for the 2026 season, after the
# Boise St / Colorado St / Fresno St / San Diego St / Utah St departure to the
# rebuilt Pac-12 and the UTEP / Northern Illinois / North Dakota State arrivals.
MW_TEAMS_2026 = {
    "Air Force": {
        "prior_conference": "Mountain West",
        "joined_2026": False,
        "notes": "Full MW history available. Option offense; home elevation ~6,621 ft.",
    },
    "Hawai'i": {
        "prior_conference": "Mountain West",
        "joined_2026": False,
        "notes": "Full history. Travel distance/time-zone is a real situational factor for every road game.",
    },
    "Nevada": {
        "prior_conference": "Mountain West",
        "joined_2026": False,
        "notes": "Full history available.",
    },
    "New Mexico": {
        "prior_conference": "Mountain West",
        "joined_2026": False,
        "notes": "Full history. Home elevation ~5,000+ ft.",
    },
    "North Dakota State": {
        "prior_conference": "FCS (Missouri Valley Football Conference)",
        "joined_2026": True,
        "notes": (
            "Zero FBS history — historically dominant FCS program. Pull its FCS-era "
            "schedule separately (see pull_ndsu_games() in pull_games.py) since a plain division='fbs' "
            "query will not surface it before 2026. Likely market-mispricing candidate "
            "in year one since books/public have thin FBS-level reference data on it."
        ),
    },
    "Northern Illinois": {
        "prior_conference": "MAC",
        "joined_2026": True,
        "notes": "No MW history; bring its full MAC-era game log (already FBS, so it's in a plain division='fbs' pull).",
    },
    "San José State": {
        "prior_conference": "Mountain West",
        "joined_2026": False,
        "notes": "Full history available. Watch for 'San Jose State' (no accent) in some sources — same team.",
    },
    "UNLV": {
        "prior_conference": "Mountain West",
        "joined_2026": False,
        "notes": "Full history available.",
    },
    "UTEP": {
        "prior_conference": "Conference USA",
        "joined_2026": True,
        "notes": "No MW history; bring its full C-USA-era game log (already FBS).",
    },
    "Wyoming": {
        "prior_conference": "Mountain West",
        "joined_2026": False,
        "notes": "Full history. Home elevation ~7,220 ft — coldest, highest venue in the league.",
    },
}

# Teams that left for the rebuilt Pac-12 in 2026. Keep them in the historical
# join/crosswalk (their game logs are still valid opponent data for the seasons
# they were in the MW), but they are no longer 2026 MW matchups themselves.
DEPARTED_2026 = ["Boise State", "Colorado State", "Fresno State", "San Diego State", "Utah State"]

# Common alternate spellings seen in odds archives / older box scores / CSVs.
# Left side: alias as it might appear in a raw source. Right side: canonical name
# matching CFBD's `school` field (and the keys in MW_TEAMS_2026 above).
NAME_ALIASES = {
    "San Jose State": "San José State",
    "San Jose St": "San José State",
    "San Jose St.": "San José State",
    "SJSU": "San José State",
    "Hawaii": "Hawai'i",
    "Hawai’i": "Hawai'i",
    "UNLV Rebels": "UNLV",
    "Nevada-Las Vegas": "UNLV",
    "UTEP Miners": "UTEP",
    "Texas-El Paso": "UTEP",
    "NIU": "Northern Illinois",
    "N. Illinois": "Northern Illinois",
    "NDSU": "North Dakota State",
    "N. Dakota State": "North Dakota State",
    "New Mexico Lobos": "New Mexico",
    "Air Force Falcons": "Air Force",
    "Wyoming Cowboys": "Wyoming",
    "Nevada Wolf Pack": "Nevada",
    "Boise St": "Boise State",
    "Boise St.": "Boise State",
    "Colorado St": "Colorado State",
    "Colorado St.": "Colorado State",
    "Fresno St": "Fresno State",
    "Fresno St.": "Fresno State",
    "San Diego St": "San Diego State",
    "San Diego St.": "San Diego State",
    "Utah St": "Utah State",
    "Utah St.": "Utah State",
}


def normalize_team_name(raw_name: str) -> str:
    """Map any known alias to its canonical CFBD-style school name. Pass-through if unknown."""
    if raw_name is None:
        return raw_name
    stripped = raw_name.strip()
    return NAME_ALIASES.get(stripped, stripped)


def is_2026_mw_team(canonical_name: str) -> bool:
    return canonical_name in MW_TEAMS_2026


if __name__ == "__main__":
    print(f"{len(MW_TEAMS_2026)} teams in the 2026 Mountain West:")
    for team, meta in MW_TEAMS_2026.items():
        flag = " (NEW)" if meta["joined_2026"] else ""
        print(f"  - {team}{flag}: prior = {meta['prior_conference']}")
    print(f"\nDeparted for Pac-12: {', '.join(DEPARTED_2026)}")
