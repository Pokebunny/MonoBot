"""Per-match stat awards, with Best Trader's ratio under the spotlight: it is
the one award whose value can't be read straight off a stat line."""

import datetime

from models.replay import MatchPlayer, MonobattleMatch
from services.awards import match_awards

BASE = datetime.datetime(2026, 8, 30, 3, 0, tzinfo=datetime.timezone.utc)


def _match(lines):
    """lines: (name, killed, lost) per player, team 1 then team 2."""
    players = [
        MatchPlayer(
            name=name,
            toon_handle=f"h-{name}",
            team=(1 if i < len(lines) // 2 else 2),
            race="Zerg",
            pick="Zergling",
            resources_killed=killed,
            resources_lost=lost,
            unit_counts={"Zergling": 50},
        )
        for i, (name, killed, lost) in enumerate(lines)
    ]
    return MonobattleMatch(
        file_name="test.SC2Replay",
        map_name="Monobattle LotV - Map Rotation",
        played_at=BASE,
        duration_seconds=900,
        game_type="4v4",
        pick_mode="blind_random",
        pick_phase_seconds=63,
        players=players,
        winning_team=1,
        winner_confidence=1.0,
        winner_method="recorded",
    )


def _trader(match):
    return next((a.player.name for a in match_awards(match, limit=4) if a.key == "best_trader"), None)


class TestBestTrader:
    def test_a_flawless_game_wins(self):
        """Killing 10,000 and losing nothing beats a smaller, tighter trade:
        the prior is added to everyone's losses, not floored under a few."""
        m = _match(
            [
                ("Flawless", 10000, 0),
                ("Tight", 5000, 400),
                ("Even", 4000, 4000),
                ("Poor", 2000, 6000),
                ("Ok", 3000, 3000),
                ("Bad", 1500, 5000),
                ("Meh", 2500, 4000),
                ("Worse", 1000, 7000),
            ]
        )
        assert _trader(m) == "Flawless"

    def test_zero_losses_no_longer_disqualifies(self):
        """The regression this rule replaced: requiring 1,000 value lost hid
        the best trader in the game — it went to a 3.2x player instead."""
        m = _match(
            [
                ("Draknas", 13900, 0),
                ("StalkerMan", 10100, 3200),
                ("Third", 5000, 4000),
                ("Fourth", 4000, 5000),
                ("Fifth", 3000, 4000),
                ("Sixth", 2000, 6000),
                ("Seventh", 2500, 5000),
                ("Eighth", 1500, 7000),
            ]
        )
        assert _trader(m) == "Draknas"

    def test_barely_fighting_does_not_win(self):
        """A player who killed almost nothing and lost nothing scores 0.2x,
        not infinity, so they can't take the award off the lobby."""
        m = _match(
            [
                ("Bystander", 200, 0),
                ("Worker", 6000, 1000),
                ("Third", 3000, 3000),
                ("Fourth", 2000, 4000),
                ("Fifth", 2500, 3500),
                ("Sixth", 1500, 5000),
                ("Seventh", 2000, 4500),
                ("Eighth", 1000, 6000),
            ]
        )
        assert _trader(m) == "Worker"

    def test_an_ordinary_lobby_earns_nothing(self):
        """Everyone trading about evenly is not an outlier performance."""
        m = _match([(f"P{i}", 3000 + 100 * i, 3000) for i in range(8)])
        assert _trader(m) is None

    def test_unmeasured_players_are_skipped(self):
        """Old parses have no kill stats; under four measured players there
        is no distribution to call anyone unusual against."""
        lines = [(n, None, None) for n in ("A", "B", "C", "D", "E")]
        lines += [("F", 9000, 0), ("G", 3000, 3000), ("H", 2000, 4000)]
        m = _match(lines)
        assert _trader(m) is None
