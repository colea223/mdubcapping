"""
One-time seed for src/gamma_model.py's power ratings -- Sonny Moore's own
published ratings, as Cole pasted them on 2026-09-06 (a snapshot already
reflecting each team's 2026 week 1 result, i.e. "entering week 2").

THIS FILE IS A FIXED CONSTANT, NEVER TOUCHED AGAIN. Per Cole's own
description of the model: Moore's rating is the seed, and from that point
forward every team's rating self-updates using gamma_model.py's own 90/10
formula against real game results -- Moore's numbers are never re-pulled or
re-applied mid-season. If Cole wants to reseed for a LATER season, this
file's SEED_SEASON/SEED_ENTERING_WEEK/SEED_RATINGS all get replaced wholesale
with a fresh paste, not incrementally edited -- there's no way to bootstrap a
new season's opening ratings from this project's own data (that's the whole
reason this model exists in the first place: it starts from Moore's outside
opinion instead).

NAME MAPPING: Moore's list is ALL CAPS with idiosyncratic abbreviations
("NEVADA LAS VEGAS" for UNLV, "TEXAS EL PASO" for UTEP, "NORTH DAKOTA ST."
for North Dakota State) that don't match teams.py's NAME_ALIASES crosswalk
(that crosswalk is for common odds-archive spellings, not Moore's specific
quirks) -- MOORE_NAME_ALIASES below is this file's own small crosswalk,
tried first, before falling through to teams.normalize_team_name() for
anything that's already close enough (title-cased "Ohio State" style names
need no special-casing at all). Priority was accuracy for the 10 real 2026
Mountain West teams (every one hand-verified against teams.py's
MW_TEAMS_2026) -- coverage for the other ~128 FBS teams is best-effort;
gamma_model.py prints a loud warning for any team it encounters in the
`games` table with no seed rating here (see its DEFAULT_SEED_RATING
fallback) rather than silently mis-rating one, so a mapping miss surfaces
instead of hiding.
"""

from teams import normalize_team_name

SEED_SEASON = 2026
# Ratings below reflect results through week 1 -- gamma_model.py's replay
# starts applying its own update formula at week 2 and beyond; week 1 games
# are never re-processed against these numbers (they're already baked in).
SEED_ENTERING_WEEK = 2

# Moore's own idiosyncratic spellings -> CFBD-style canonical name. Applied
# BEFORE teams.normalize_team_name() (which then also runs on the result, a
# harmless no-op for names that are already canonical).
MOORE_NAME_ALIASES = {
    # These 5 are the only " ST." schools Moore rates that were missing from
    # this table (found 2026-09-08, via Cole spotting Texas as a bogus 25-pt
    # favorite over Ohio State in the Matchup Creator): normalize_team_name()
    # is a flat dict lookup with no generic "St. -> State" expansion, so
    # without an explicit entry each one fell through as the literal
    # title-cased Moore spelling ("Ohio St.", not "Ohio State") -- a key that
    # never matches CFBD's actual name, so every real lookup silently missed
    # and fell back to DEFAULT_SEED_RATING (the all-team average) instead of
    # the team's real Moore rating. Same fix pattern as every other aliased
    # "___ ST." school already below.
    "OHIO ST.": "Ohio State",
    "ARIZONA ST.": "Arizona State",
    "NORTH CAROLINA ST.": "NC State",
    "FLORIDA ST.": "Florida State",
    "MICHIGAN ST.": "Michigan State",
    "MIAMI FL.": "Miami",
    "MIAMI OHIO": "Miami (OH)",
    "MISSISSIPPI": "Ole Miss",
    "MISSISSIPPI ST.": "Mississippi State",
    "PENN.ST.": "Penn State",
    "NORTH DAKOTA ST.": "North Dakota State",
    "KANSAS ST.": "Kansas State",
    "IOWA ST.": "Iowa State",
    "TEXAS SAN ANTONIO": "UTSA",
    "WASHINGTON ST.": "Washington State",
    "TEXAS ST.": "Texas State",
    "UTAH ST.": "Utah State",
    "NEVADA LAS VEGAS": "UNLV",
    "CENTRAL FLORIDA": "UCF",
    "OHIO UNIVERSITY": "Ohio",
    "JACKSONVILLE ST.": "Jacksonville State",
    "FRESNO ST.": "Fresno State",
    "KENNESAW ST.": "Kennesaw State",
    # CFBD's own games table calls this school "Florida International", not
    # "FIU" -- the old alias below was a guess made without checking the real
    # name, which silently caused the exact same DEFAULT_SEED_RATING miss as
    # the missing " ST." schools above (see the 2026-09-08 audit comment).
    "FLORIDA INTERNATIONAL": "Florida International",
    "ARKANSAS ST.": "Arkansas State",
    "SOUTHERN MISSISSIPPI": "Southern Miss",
    "OREGON ST.": "Oregon State",
    "LOUISIANA-LAFAYETTE": "Louisiana",
    "HAWAII": "Hawai'i",
    "TROY ST.": "Troy",
    "ALABAMA BIRMINGHAM": "UAB",
    "SAN JOSE ST.": "San José State",
    "COLORADO ST.": "Colorado State",
    "MISSOURI ST.": "Missouri State",
    "NEW MEXICO ST.": "New Mexico State",
    "OKLAHOMA ST.": "Oklahoma State",
    # CFBD calls this school "App State", not "Appalachian State" -- same
    # unverified-guess bug as Florida International above.
    "APPALACHIAN ST.": "App State",
    "GEORGIA ST": "Georgia State",
    "MIDDLE TENNESSEE ST.": "Middle Tennessee",
    "KENT ST.": "Kent State",
    "NORTH CAROLINA CHARLOTTE": "Charlotte",
    "BALL ST.": "Ball State",
    # CFBD calls this school "UL Monroe", not "Louisiana Monroe" -- same
    # unverified-guess bug as Florida International/App State above.
    "LOUISIANA-MONROE": "UL Monroe",
    "SAM HOUSTON ST.": "Sam Houston",
    "SACRAMENTO ST.": "Sacramento State",
    "TEXAS EL PASO": "UTEP",
    # CFBD calls this school "Massachusetts" (not "UMass") -- same
    # unverified-guess bug as the other three fixed above.
    "MASSACHUSETTS": "Massachusetts",
    "SAN DIEGO ST.": "San Diego State",
    # Plain acronym schools: title() mangles an all-caps key into e.g. "Lsu"
    # (capitalize-first-letter-only, no acronym awareness), which then never
    # matches CFBD's own all-caps spelling -- same DEFAULT_SEED_RATING miss
    # as every other entry in this table, just from the opposite direction
    # (over-lowercasing instead of a wrong guess). Self-mapped here so they
    # pass through unchanged. Found via the same 2026-09-08 full audit.
    "LSU": "LSU",
    "BYU": "BYU",
    "SMU": "SMU",
    "TCU": "TCU",
    "UCLA": "UCLA",
    "SOUTHERN CALIFORNIA": "USC",
    "CONNECTICUT": "UConn",
}

# team name (Moore's spelling) -> power rating, i.e. the LAST number on
# each of Moore's rows. Comment after each MW team flags it explicitly --
# these 10 are the ones this whole model actually exists to predict;
# everything else is opponent-strength context.
_RAW_SEED = {
    "INDIANA": 96.96,
    "OHIO ST.": 93.37,
    "MIAMI FL.": 89.87,
    "NOTRE DAME": 88.50,
    "TEXAS TECH": 87.35,
    "OREGON": 86.91,
    "TEXAS": 85.07,
    "MISSISSIPPI": 84.94,
    "GEORGIA": 83.54,
    "TEXAS A&M": 82.78,
    "UTAH": 81.63,
    "IOWA": 81.23,
    "OKLAHOMA": 81.15,
    "PENN.ST.": 80.94,
    "ALABAMA": 80.91,
    "VANDERBILT": 80.39,
    "NORTH DAKOTA ST.": 79.45,  # -- 2026 Mountain West
    "VIRGINIA": 79.17,
    "LSU": 79.10,
    "WASHINGTON": 79.08,
    "BYU": 78.32,
    "SOUTHERN CALIFORNIA": 78.01,
    "TENNESSEE": 76.78,
    "SMU": 75.44,
    "PITTSBURGH": 75.34,
    "FLORIDA": 75.24,
    "SOUTH CAROLINA": 75.07,
    "DUKE": 74.99,
    "LOUISVILLE": 74.70,
    "MISSOURI": 74.58,
    "ARIZONA": 73.38,
    "OLD DOMINION": 72.61,
    "NAVY": 72.21,
    "NEBRASKA": 72.10,
    "MICHIGAN": 71.23,
    "WAKE FOREST": 71.02,
    "ILLINOIS": 70.99,
    "KANSAS ST.": 70.73,
    "BOISE STATE": 70.21,
    "MINNESOTA": 69.76,
    "ARIZONA ST.": 69.56,
    "AUBURN": 69.36,
    "CINCINNATI": 69.34,
    "TCU": 69.06,
    "HOUSTON": 68.93,
    "MISSISSIPPI ST.": 68.77,
    "IOWA ST.": 68.26,
    "SOUTH FLORIDA": 68.25,
    "MEMPHIS": 68.19,
    "NORTHWESTERN": 68.11,
    "WESTERN MICHIGAN": 68.06,
    "CLEMSON": 67.84,
    "JAMES MADISON": 67.80,
    "GEORGIA TECH": 67.66,
    "NORTH CAROLINA ST.": 67.23,
    "FLORIDA ST.": 67.22,
    "ARKANSAS": 66.94,
    "BAYLOR": 66.84,
    "TEXAS SAN ANTONIO": 66.61,
    "NEW MEXICO": 66.56,  # -- 2026 Mountain West
    "KENTUCKY": 66.52,
    "WASHINGTON ST.": 66.13,
    "MICHIGAN ST.": 66.05,
    "KANSAS": 65.65,
    "COLORADO": 64.93,
    "EAST CAROLINA": 64.84,
    "SAN DIEGO ST.": 64.63,
    "WISCONSIN": 64.61,
    "ARMY": 64.48,
    "VIRGINIA TECH": 64.41,
    "TULANE": 63.90,
    "NORTH TEXAS": 63.86,
    "UCLA": 63.76,
    "NEVADA LAS VEGAS": 62.59,  # -- 2026 Mountain West (UNLV)
    "CENTRAL FLORIDA": 62.34,
    "MARYLAND": 62.31,
    "TOLEDO": 62.11,
    "STANFORD": 61.39,
    "NORTH CAROLINA": 61.28,
    "RUTGERS": 61.15,
    "UTAH ST.": 60.92,
    "TEXAS ST.": 60.54,
    "CALIFORNIA": 60.37,
    "OHIO UNIVERSITY": 58.75,
    "LOUISIANA TECH": 58.31,
    "WEST VIRGINIA": 58.18,
    "JACKSONVILLE ST.": 57.99,
    "AIR FORCE": 57.53,  # -- 2026 Mountain West
    "BOSTON COLLEGE": 57.47,
    "CONNECTICUT": 57.33,
    "LIBERTY": 57.23,
    "TEMPLE": 56.92,
    "PURDUE": 56.66,
    "GEORGIA SOUTHERN": 56.15,
    "WESTERN KENTUCKY": 56.13,
    "DELAWARE": 56.06,
    "FRESNO ST.": 56.05,
    "KENNESAW ST.": 55.97,
    "SYRACUSE": 55.71,
    "FLORIDA INTERNATIONAL": 55.50,
    "TULSA": 55.33,
    "MIAMI OHIO": 55.19,
    "CENTRAL MICHIGAN": 54.97,
    "NEVADA": 54.62,  # -- 2026 Mountain West
    "ARKANSAS ST.": 54.62,
    "SOUTHERN MISSISSIPPI": 54.36,
    "OREGON ST.": 54.32,
    "LOUISIANA-LAFAYETTE": 54.27,
    "HAWAII": 54.23,  # -- 2026 Mountain West
    "TROY ST.": 53.08,
    "ALABAMA BIRMINGHAM": 52.58,
    "MARSHALL": 52.06,
    "SAN JOSE ST.": 51.73,  # -- 2026 Mountain West
    "SOUTH ALABAMA": 51.65,
    "BUFFALO": 51.51,
    "COLORADO ST.": 50.98,
    "COASTAL CAROLINA": 50.97,
    "MISSOURI ST.": 50.72,
    "NEW MEXICO ST.": 50.62,
    "FLORIDA ATLANTIC": 50.43,
    "OKLAHOMA ST.": 50.25,
    "APPALACHIAN ST.": 49.23,
    "BOWLING GREEN": 48.81,
    "WYOMING": 48.57,  # -- 2026 Mountain West
    "AKRON": 48.55,
    "GEORGIA ST": 47.43,
    "EASTERN MICHIGAN": 47.20,
    "MIDDLE TENNESSEE ST.": 47.05,
    "RICE": 46.81,
    "KENT ST.": 45.57,
    "NORTHERN ILLINOIS": 44.26,  # -- 2026 Mountain West
    "NORTH CAROLINA CHARLOTTE": 44.01,
    "BALL ST.": 43.01,
    "LOUISIANA-MONROE": 42.89,
    "SAM HOUSTON ST.": 41.11,
    "SACRAMENTO ST.": 41.05,
    "TEXAS EL PASO": 40.80,  # -- 2026 Mountain West (UTEP)
    "MASSACHUSETTS": 37.58,
}


def _canonical_seed():
    out = {}
    for raw_name, rating in _RAW_SEED.items():
        mapped = MOORE_NAME_ALIASES.get(raw_name, raw_name.title())
        canonical = normalize_team_name(mapped)
        out[canonical] = rating
    return out


# {canonical_team_name: seed_rating}. Built once at import time -- this is a
# small (~138-entry) pure dict operation, not worth lazily caching.
SEED_RATINGS = _canonical_seed()

# Used by gamma_model.py whenever a team shows up in the `games` table with
# no seed rating here (a genuine Moore-list gap, an FCS buy-game opponent
# Moore doesn't rate at all, or a name-mapping miss in MOORE_NAME_ALIASES
# above) -- the mean of every rated team, a neutral placeholder rather than
# a guess in either direction. gamma_model.py prints a warning every time
# this fallback gets used so a real mapping gap doesn't hide silently.
DEFAULT_SEED_RATING = sum(SEED_RATINGS.values()) / len(SEED_RATINGS)


if __name__ == "__main__":
    print(f"{len(SEED_RATINGS)} teams seeded for {SEED_SEASON}, entering week {SEED_ENTERING_WEEK}.")
    print(f"Default/fallback rating for an unmapped team: {DEFAULT_SEED_RATING:.2f}")
    from teams import MW_TEAMS_2026
    print("\n2026 Mountain West teams:")
    for team in MW_TEAMS_2026:
        rating = SEED_RATINGS.get(team)
        flag = "" if rating is not None else "  *** MISSING -- check MOORE_NAME_ALIASES ***"
        print(f"  {team:20s} -> {rating}{flag}")
