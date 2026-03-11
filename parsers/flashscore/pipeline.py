from .leagues_parser import parse_leagues
from .seasons_parser import parse_seasons
from .teams_parser import parse_teams
from .players_parser import parse_players
from .coaches_parser import parse_coaches
from .referees_parser import parse_referees
from .matches_parser import parse_matches


def run():

    parse_leagues()

    parse_seasons()

    parse_teams()

    parse_players()

    parse_coaches()

    parse_referees()

    parse_matches()