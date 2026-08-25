"""
Shared American-odds math -- used by backtest.py (grading the model's own
spread/total/moneyline picks) and by anything reading real bets back out of
the Excel Bet Log. Kept in one small module so the payout math is identical
everywhere instead of re-derived per script.

Convention: American odds throughout (-110, +150, etc.), matching what
CFBD's lines table and any US sportsbook display.
"""


def implied_prob(american_odds: float) -> float:
    """Raw implied win probability from one side's American odds (includes the vig)."""
    if american_odds < 0:
        return -american_odds / (-american_odds + 100)
    return 100 / (american_odds + 100)


def no_vig_prob(favorite_or_home_odds: float, dog_or_away_odds: float) -> float:
    """
    Two-way no-vig probability for the FIRST side passed in, removing the
    book's overround by normalizing so both sides' implied probabilities sum
    to 1.0 instead of ~1.05. Use this (not the raw implied_prob) whenever
    comparing a model's win probability against the market's -- otherwise
    the vig makes the market look like it's always got an edge.
    """
    p1, p2 = implied_prob(favorite_or_home_odds), implied_prob(dog_or_away_odds)
    total = p1 + p2
    return p1 / total if total else float("nan")


def payout_profit(stake: float, american_odds: float, won: bool) -> float:
    """
    Profit (not total return) on a `stake`-unit bet at `american_odds`.
    A loss returns -stake; a win returns the odds-implied payout. Callers
    handle pushes themselves (profit=0) since push detection is bet-type
    specific (spread/total tie vs. a moneyline game, which CFB never pushes).
    """
    if not won:
        return -stake
    if american_odds < 0:
        return stake * (100 / abs(american_odds))
    return stake * (american_odds / 100)
