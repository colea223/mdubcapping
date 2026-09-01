"""
Thin wrapper around the cfbd (CollegeFootballData.com) Python client.
Get a free key at https://collegefootballdata.com/key and put it in .env
(see .env.example) before running any pull script.
"""
import cfbd
from config import require_api_key


def get_api_client() -> cfbd.ApiClient:
    key = require_api_key()
    configuration = cfbd.Configuration(access_token=key)
    return cfbd.ApiClient(configuration)


def games_api(client):
    return cfbd.GamesApi(client)


def stats_api(client):
    return cfbd.StatsApi(client)


def betting_api(client):
    return cfbd.BettingApi(client)


def recruiting_api(client):
    return cfbd.RecruitingApi(client)


def ratings_api(client):
    return cfbd.RatingsApi(client)


def teams_api(client):
    return cfbd.TeamsApi(client)


def drives_api(client):
    return cfbd.DrivesApi(client)


def plays_api(client):
    return cfbd.PlaysApi(client)
