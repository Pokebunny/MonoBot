"""Career and single-game achievements.

Split responsibilities: this module DERIVES achievement state from match
history (a pure function, like ratings), but what a player HOLDS is the
`achievement_unlocks` ledger in storage — an append-only record written when
a crossing is first observed at ingest. The ledger is what profiles and the
gallery show; it is never revoked by later data corrections, threshold
tuning, or spec removal (grandfathering). The derived book supplies candidate
state, progress bars, and the diff that decides what to grant and announce.

Two kinds of spec, split by `Tally` view:
- career achievements read `history.career` — accumulated over ALL stored
  matches, so long-time players get credit for their record (granted
  silently by `ensure_seeded` when a deployment first turns achievements on).
- moment achievements read `history.live` — only matches played after the
  deployment's achievement epoch (stamped in the DB's meta table) count, so
  single-game feats and streaks are earned live at the table, never handed
  out retroactively — including by a future backfill of old replays.

Secret achievements are hidden from the gallery and progress display until
unlocked. Only genuine surprises are flagged secret — tier extensions of
visible families never are (the tier below telegraphs them anyway). Career
tier ladders always top out beyond the current record holder, so every
player, including the most veteran, has a next rung.

Thresholds are calibrated against the community DB + a 90-replay archive
sample (per-player-game percentiles): 25k kills ≈ p95, 50k ≈ p99, 10k econ
killed ≈ p99, 8k tech ≈ p99, 5x trade ≈ p97, 30k value lost ≈ p98.
"""

import datetime
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, NamedTuple

from models.replay import MatchPlayer, MonobattleMatch
from services.rating import MIN_DURATION_SECONDS, MIN_WINNER_CONFIDENCE

logger = logging.getLogger(__name__)

RARITIES = ["Common", "Uncommon", "Rare", "Epic", "Legendary"]
RARITY_EMOJI = {"Common": "⚪", "Uncommon": "🟢", "Rare": "🔵", "Epic": "🟣", "Legendary": "🟠"}

# Late-night window in UTC (~11pm–4am US Eastern, where the community plays).
_NIGHT_HOURS = range(4, 9)

# Picks that fly (an "air unit" as an enemy, whether or not it shoots).
_AIR_PICKS = {
    "Mutalisk", "Corruptor", "BroodLord", "Viper",
    "Phoenix", "VoidRay", "Oracle", "Carrier", "Tempest", "Mothership",
    "Banshee", "Battlecruiser", "Viking", "Liberator", "Raven", "Medivac",
    "WarpPrism", "Observer", "Overseer",
}  # fmt: skip

# Picks with NO way to shoot up. Casters whose spells hit air (HighTemplar's
# storm, Infestor's fungal, Raven's turrets, Ghost's rifle) don't qualify.
_NO_ANTI_AIR_PICKS = {
    "Zealot", "Adept", "DarkTemplar", "Immortal", "Colossus", "Disruptor",
    "Reaper", "Hellion", "Hellbat", "SiegeTank",
    "Zergling", "Baneling", "Roach", "Ravager", "Ultralisk", "Lurker", "SwarmHost",
    "BroodLord", "Banshee", "Oracle", "Medivac", "WarpPrism", "Observer", "Overseer",
}  # fmt: skip

# A drop play = this many transport/Nydus unload commands in one game (top
# ~10% of player-games in a 40-replay sample).
DROP_PLAY_COMMANDS = 5


def _naive(dt: datetime.datetime) -> datetime.datetime:
    """Match timestamps come from sc2reader (naive UTC) or tests (aware);
    normalize so epoch comparisons never mix the two."""
    return dt.replace(tzinfo=None)


@dataclass
class _MatchContext:
    """Per-match facts shared by every player's tally update. The first
    block is derived from the match alone; the second is filled in by the
    book, which knows identity merges and everyone's prior history."""

    match: MonobattleMatch
    mvp: MatchPlayer | None
    min_kills: int | None  # lobby's lowest recorded kill value (6+ recorded)
    team_kills: dict[int, int]  # team -> total recorded kills
    team_dup: dict[int, int]  # team -> highest same-pick multiplicity
    canonical: dict[str, str] = field(default_factory=dict)  # raw -> merged handle
    newcomers: set[str] = field(default_factory=set)  # canonical handles in their first game
    all_veterans: bool = False  # every player has 50+ prior games
    community_opening: bool = False  # first game after 6+ community-wide quiet hours


@dataclass
class Tally:
    """One player's accumulated stats over a set of decided matches."""

    games: int = 0
    wins: int = 0
    streak: int = 0
    best_streak: int = 0
    loss_streak: int = 0
    bounce_backs: int = 0  # wins immediately after a 3+ loss streak
    units_played: set[str] = field(default_factory=set)
    units_won: set[str] = field(default_factory=set)
    units_won_by_race: dict[str, set[str]] = field(default_factory=dict)
    units_beaten: set[str] = field(default_factory=set)  # enemy picks defeated
    races_won: set[str] = field(default_factory=set)
    pure_race_wins: dict[str, int] = field(default_factory=dict)  # all-one-race team wins
    teammates: set[str] = field(default_factory=set)
    best_unit_wins: int = 0
    _unit_win_counts: dict[str, int] = field(default_factory=dict)
    best_teammate_wins: int = 0
    _teammate_wins: dict[str, int] = field(default_factory=dict)
    best_opponent_wins: int = 0
    _opponent_wins: dict[str, int] = field(default_factory=dict)
    best_duo_streak: int = 0
    _duo_streaks: dict[str, int] = field(default_factory=dict)
    welcomes: int = 0  # games shared with someone playing their first game
    fresh_blood_wins: int = 0  # wins alongside 3+ first-timers
    veteran_lobbies: int = 0  # games where everyone had 50+ prior games
    mvps: int = 0
    losing_mvps: int = 0
    mvp_streak: int = 0
    best_mvp_streak: int = 0
    best_session_mvps: int = 0  # most MVPs in one sitting
    _session_mvps: int = 0
    repicks: int = 0
    repick_wins: int = 0
    units_built: int = 0
    max_units_one_game: int = 0
    max_kills: int = 0
    max_econ_killed: int = 0
    max_tech_killed: int = 0
    best_trade: float = 0.0
    max_lost_in_win: int = 0
    outkilled_enemy_team: int = 0
    fewest_kills_wins: int = 0  # won a long game with the lobby's lowest kills
    mirror_wins: int = 0
    comeback_wins: int = 0
    repick_regrets: int = 0  # lost after repicking while a foe won with the original roll
    finders_keepers: int = 0  # won with a unit an opponent repicked away from
    twin_wins: int = 0  # won with 2+ of the same unit on the team
    triplet_wins: int = 0  # won with 3+ of the same unit on the team
    did_nothing_wins: int = 0  # long wins with almost no fighting
    max_kills_in_loss: int = 0
    thrifty_wins: int = 0  # long wins with a near-zero median bank
    delivery_wins: int = 0  # wins with real drop play
    max_static_defense: int = 0  # most defensive structures in one real game
    greed_wins: int = 0  # wins after expanding heavily before making a unit
    max_orbitals: int = 0  # most Orbital Commands owned in one game
    grounded_wins: int = 0  # long wins with a no-anti-air pick vs 2+ enemy air
    team_grounded_wins: int = 0  # long wins where the WHOLE team lacked anti-air vs enemy air
    fastest_win: int | None = None
    longest_game: int = 0
    max_win_duration: int = 0
    night_games: int = 0
    return_wins: int = 0  # wins in one's first game back after 30+ days away
    _last_played: datetime.datetime | None = None
    same_pick_run: int = 0  # consecutive blind-random games with one unit
    best_same_pick_run: int = 0
    _last_random_pick: str | None = None
    best_session: int = 0  # most games in one sitting (<6h between games)
    _session_games: int = 0
    opening_acts: int = 0  # played in the lobby that started a session night
    weekend_days: set[int] = field(default_factory=set)  # distinct Sat/Sun dates played
    best_week_run: int = 0  # consecutive calendar weeks with at least one game
    _week_run: int = 0
    _last_week: int | None = None
    anniversary_games: int = 0  # games on the month/day of one's first game
    _first_played: datetime.datetime | None = None
    best_alternation: int = 0  # longest strict win/loss alternation
    _alt_run: int = 0
    _last_result: bool | None = None

    def update(self, player: MatchPlayer, ctx: _MatchContext) -> None:
        match = ctx.match
        won = player.team == match.winning_team
        now = _naive(match.played_at)
        self.games += 1
        self.longest_game = max(self.longest_game, match.duration_seconds)
        if now.hour in _NIGHT_HOURS:
            self.night_games += 1

        # -- sessions & calendar (reads _last_played BEFORE it advances) --
        in_session = self._last_played is not None and now - self._last_played < datetime.timedelta(hours=6)
        self._session_games = self._session_games + 1 if in_session else 1
        self.best_session = max(self.best_session, self._session_games)
        if not in_session:
            self._session_mvps = 0
        if ctx.community_opening:
            self.opening_acts += 1
        date = now.date()
        if date.weekday() >= 5:  # Sat/Sun collapse to that week's Saturday
            self.weekend_days.add(date.toordinal() - (date.weekday() - 5))
        week = date.toordinal() // 7
        if week != self._last_week:
            self._week_run = self._week_run + 1 if self._last_week is not None and week == self._last_week + 1 else 1
            self.best_week_run = max(self.best_week_run, self._week_run)
            self._last_week = week
        if self._first_played is None:
            self._first_played = now
        elif (date.month, date.day) == (self._first_played.month, self._first_played.day) and (
            date.year > self._first_played.year
        ):
            self.anniversary_games += 1
        self._alt_run = self._alt_run + 1 if self._last_result is not None and won != self._last_result else 1
        self.best_alternation = max(self.best_alternation, self._alt_run)
        self._last_result = won

        if match.pick_mode == "blind_random":
            # The unit the game DEALT them — a repick means the roll was
            # repick_from, not the unit they ended up playing.
            rolled = player.repick_from if (player.repick_used and player.repick_from) else player.pick
            if rolled:
                self.same_pick_run = self.same_pick_run + 1 if rolled == self._last_random_pick else 1
                self.best_same_pick_run = max(self.best_same_pick_run, self.same_pick_run)
                self._last_random_pick = rolled

        if won:
            self.wins += 1
            if self.loss_streak >= 3:
                self.bounce_backs += 1
            self.loss_streak = 0
            self.streak += 1
            self.best_streak = max(self.best_streak, self.streak)
            if self.fastest_win is None or match.duration_seconds < self.fastest_win:
                self.fastest_win = match.duration_seconds
            self.max_win_duration = max(self.max_win_duration, match.duration_seconds)
            if self._last_played is not None and _naive(match.played_at) - self._last_played >= datetime.timedelta(
                days=30
            ):
                self.return_wins += 1
            if match.comeback_deficit is not None and match.comeback_deficit >= 8000:
                self.comeback_wins += 1
        else:
            self.streak = 0
            self.loss_streak += 1

        if player.pick:
            self.units_played.add(player.pick)
            if won:
                self.units_won.add(player.pick)
                self.units_won_by_race.setdefault(player.race, set()).add(player.pick)
                n = self._unit_win_counts.get(player.pick, 0) + 1
                self._unit_win_counts[player.pick] = n
                self.best_unit_wins = max(self.best_unit_wins, n)
            if won and any(q.pick == player.pick and q.team != player.team for q in match.players):
                self.mirror_wins += 1
            if won and any(
                q.team != player.team and q.repick_used and q.repick_from == player.pick for q in match.players
            ):
                self.finders_keepers += 1
        if won:
            self.races_won.add(player.race)
            self.units_beaten.update(q.pick for q in match.players if q.team != player.team and q.pick)
            team = match.team(player.team)
            if len(team) >= 3:
                team_races = {q.race for q in team}
                if len(team_races) == 1:
                    race = team_races.pop()
                    self.pure_race_wins[race] = self.pure_race_wins.get(race, 0) + 1

        me = ctx.canonical.get(player.toon_handle, player.toon_handle)
        for q in match.players:
            if q is player:
                continue
            other = ctx.canonical.get(q.toon_handle, q.toon_handle)
            if other == me:
                continue  # the same person on another linked account
            if q.team == player.team:
                self.teammates.add(other)
                if won:
                    n = self._teammate_wins.get(other, 0) + 1
                    self._teammate_wins[other] = n
                    self.best_teammate_wins = max(self.best_teammate_wins, n)
                self._duo_streaks[other] = self._duo_streaks.get(other, 0) + 1 if won else 0
                self.best_duo_streak = max(self.best_duo_streak, self._duo_streaks[other])
            elif won:
                n = self._opponent_wins.get(other, 0) + 1
                self._opponent_wins[other] = n
                self.best_opponent_wins = max(self.best_opponent_wins, n)
        if me not in ctx.newcomers and ctx.newcomers:
            # Welcoming someone requires already being part of the community
            # yourself — a lobby of mutual debuts welcomes no one.
            self.welcomes += 1
            if won and len(ctx.newcomers) >= 3:
                self.fresh_blood_wins += 1
        if ctx.all_veterans:
            self.veteran_lobbies += 1
        if won and ctx.team_dup.get(player.team, 1) >= 2:
            self.twin_wins += 1
            if ctx.team_dup[player.team] >= 3:
                self.triplet_wins += 1

        if player.repick_used:
            self.repicks += 1
            if won:
                self.repick_wins += 1
            elif player.repick_from and any(
                q.pick == player.repick_from and q.team == match.winning_team for q in match.players
            ):
                self.repick_regrets += 1

        built = sum(player.unit_counts.values())
        self.units_built += built
        self.max_units_one_game = max(self.max_units_one_game, built)

        self._last_played = _naive(match.played_at)

        if ctx.mvp is player:
            self.mvps += 1
            self.mvp_streak += 1
            self.best_mvp_streak = max(self.best_mvp_streak, self.mvp_streak)
            self._session_mvps += 1
            self.best_session_mvps = max(self.best_session_mvps, self._session_mvps)
            if not won:
                self.losing_mvps += 1
        elif ctx.mvp is not None:
            # Only a game that crowned someone else breaks an MVP run; games
            # with no kill stats at all leave it untouched.
            self.mvp_streak = 0
        if player.resources_killed:
            self.max_kills = max(self.max_kills, player.resources_killed)
            enemy_total = sum(v for t, v in ctx.team_kills.items() if t != player.team)
            if player.resources_killed >= 10000 and 1000 <= enemy_total < player.resources_killed:
                self.outkilled_enemy_team += 1
        if player.econ_killed:
            self.max_econ_killed = max(self.max_econ_killed, player.econ_killed)
        if player.tech_killed:
            self.max_tech_killed = max(self.max_tech_killed, player.tech_killed)
        if player.resources_killed and player.resources_lost and player.resources_lost >= 2000:
            self.best_trade = max(self.best_trade, player.resources_killed / player.resources_lost)
        if not won and player.resources_killed:
            self.max_kills_in_loss = max(self.max_kills_in_loss, player.resources_killed)
        if won and player.resources_lost is not None:
            self.max_lost_in_win = max(self.max_lost_in_win, player.resources_lost)
        if won and player.drop_commands is not None and player.drop_commands >= DROP_PLAY_COMMANDS:
            self.delivery_wins += 1
        # Queens don't stop the "first unit" clock (they're inject
        # infrastructure), so all races face comparable odds and no race
        # split is needed. 3 is archive-common only because of pub-lobby
        # nonsense; in community games it's genuinely rare.
        if won and player.bases_before_unit is not None and player.bases_before_unit >= 3:
            self.greed_wins += 1
        if player.orbitals is not None:
            self.max_orbitals = max(self.max_orbitals, player.orbitals)
        if player.static_defense is not None:
            self.max_static_defense = max(self.max_static_defense, player.static_defense)
        if won and match.duration_seconds >= 600:
            if (
                player.resources_killed is not None
                and player.resources_killed <= 1000
                and player.resources_lost is not None
                and player.resources_lost <= 1000
            ):
                self.did_nothing_wins += 1
            if player.resources_floated is not None and player.resources_floated < 200:
                self.thrifty_wins += 1
            if player.pick in _NO_ANTI_AIR_PICKS:
                enemy_air = sum(1 for q in match.players if q.team != player.team and q.pick in _AIR_PICKS)
                if enemy_air >= 2:
                    self.grounded_wins += 1
                # The bigger predicament: the WHOLE team can't shoot up
                # (happens ~once per 200 games).
                team_picks = [q.pick for q in match.team(player.team)]
                if enemy_air >= 1 and len(team_picks) >= 3 and all(pick in _NO_ANTI_AIR_PICKS for pick in team_picks):
                    self.team_grounded_wins += 1
        if (
            won
            and match.duration_seconds >= 600
            and ctx.min_kills is not None
            and player.resources_killed == ctx.min_kills
        ):
            self.fewest_kills_wins += 1


@dataclass
class PlayerHistory:
    career: Tally = field(default_factory=Tally)  # all decided matches
    live: Tally = field(default_factory=Tally)  # matches since ACHIEVEMENT_EPOCH


class AchievementSpec(NamedTuple):
    key: str  # stable id
    name: str
    emoji: str
    rarity: str  # one of RARITIES
    description: str  # shown in the gallery; phrased as the requirement
    check: Callable[[PlayerHistory], bool]
    # (current, target) toward unlocking, for the "next up" display; None for
    # achievements with no meaningful progress bar.
    progress: Callable[[PlayerHistory], tuple[float, float]] | None = None
    # Hidden until unlocked: not shown in the gallery or progress display,
    # only teased as a count. For Easter eggs and true surprises only — a
    # tier extension of a visible family is never secret.
    secret: bool = False


def is_secret(spec: AchievementSpec) -> bool:
    return spec.secret


def _career(attr: str, target: float) -> tuple[Callable, Callable]:
    return (
        lambda h: getattr(h.career, attr) >= target,
        lambda h: (getattr(h.career, attr), target),
    )


def _career_len(attr: str, target: int) -> tuple[Callable, Callable]:
    return (
        lambda h: len(getattr(h.career, attr)) >= target,
        lambda h: (len(getattr(h.career, attr)), target),
    )


def _live(attr: str, target: float) -> tuple[Callable, Callable]:
    return (
        lambda h: getattr(h.live, attr) >= target,
        lambda h: (getattr(h.live, attr), target),
    )


def _spec(key, name, emoji, rarity, description, checks, secret=False) -> AchievementSpec:
    check, progress = checks
    return AchievementSpec(key, name, emoji, rarity, description, check, progress, secret)


def _race_wins(race: str, target: int) -> tuple[Callable, Callable]:
    return (
        lambda h: len(h.career.units_won_by_race.get(race, set())) >= target,
        lambda h: (len(h.career.units_won_by_race.get(race, set())), target),
    )


# The statistically worst units, from the full pub archive (30+ games each:
# BroodLord 33%, Archon 37%, Queen 37%). Revisit if the meta shifts.
UNDERDOG_UNITS = ("BroodLord", "Archon", "Queen")

# Chronicler is granted from upload counts, not match history (see the
# replays cog); its check never fires in the derived engine.
CHRONICLER_UPLOADS = 25


SPECS: list[AchievementSpec] = [
    # Career tier ladders end WELL above the current record holder (2026-07:
    # 347 games, 216 wins, 41 units won with, 16.4k units built, 184
    # repicks, 54 teammates): at the most active pace (~175 games/yr) each
    # top rung is a multi-year chase, so there is always something to play
    # for. Epic rungs may be held; Legendary rungs must be years out.
    # -- career: volume ---------------------------------------------------
    _spec("first_game", "Fresh Meat", "🐣", "Common", "Play your first monobattle", _career("games", 1)),
    _spec("settling_in", "Settling In", "🏕️", "Common", "Play 10 games", _career("games", 10)),
    _spec("regular", "Regular", "🪑", "Common", "Play 25 games", _career("games", 25)),
    _spec("veteran", "Veteran", "🎖️", "Rare", "Play 100 games", _career("games", 100)),
    _spec("lifer", "Lifer", "🗿", "Epic", "Play 300 games", _career("games", 300)),
    _spec("millennium", "Millennium", "♾️", "Legendary", "Play 1,000 games", _career("games", 1000)),
    _spec("first_win", "On the Board", "✅", "Common", "Win your first game", _career("wins", 1)),
    _spec("warming_up", "Warming Up", "💪", "Uncommon", "Win 10 games", _career("wins", 10)),
    _spec("proven", "Proven", "📈", "Uncommon", "Win 25 games", _career("wins", 25)),
    _spec("centurion", "Centurion", "🏛️", "Epic", "Win 100 games", _career("wins", 100)),
    _spec("warlord", "Warlord", "👑", "Legendary", "Win 500 games", _career("wins", 500)),
    # -- career: MVP ------------------------------------------------------
    _spec("first_mvp", "Star of the Game", "⭐", "Uncommon", "Earn your first MVP", _career("mvps", 1)),
    _spec("serial_star", "Serial Star", "🌟", "Rare", "Earn 10 MVPs", _career("mvps", 10)),
    _spec("constellation", "Constellation", "💫", "Epic", "Earn 25 MVPs", _career("mvps", 25)),
    _spec("supernova", "Supernova", "🌠", "Legendary", "Earn 250 MVPs", _career("mvps", 250)),
    # -- career: variety --------------------------------------------------
    _spec("sampler", "Sampler", "🍬", "Common", "Play 3 different units", _career_len("units_played", 3)),
    _spec("field_tester", "Field Tester", "🧪", "Common", "Play 10 different units", _career_len("units_played", 10)),
    _spec("connoisseur", "Connoisseur", "🍷", "Rare", "Play 25 different units", _career_len("units_played", 25)),
    _spec(
        "one_of_everything",
        "One of Everything",
        "📦",
        "Epic",
        "Play all 42 units",
        _career_len("units_played", 42),
    ),
    _spec(
        "ten_ways_to_win",
        "Ten Ways to Win",
        "🔟",
        "Uncommon",
        "Win with 10 different units",
        _career_len("units_won", 10),
    ),
    _spec("winning_hand", "Winning Hand", "🃏", "Epic", "Win with 25 different units", _career_len("units_won", 25)),
    # 42 = every unit the map has ever dealt (stable across 716+ archive
    # games); the collection capstone. Best so far: 41.
    _spec("royal_flush", "Royal Flush", "♠️", "Legendary", "Win with all 42 units", _career_len("units_won", 42)),
    _spec("triple_threat", "Triple Threat", "🔱", "Uncommon", "Win with all three races", _career_len("races_won", 3)),
    _spec(
        "exterminator",
        "Exterminator",
        "☠️",
        "Rare",
        "Defeat all 42 units",
        (
            lambda h: len(h.career.units_beaten) >= 42,
            lambda h: (len(h.career.units_beaten), 42),
        ),
    ),
    _spec("zoo_keeper", "Zoo Keeper", "🦎", "Rare", "Win with 8 different Zerg units", _race_wins("Zerg", 8)),
    _spec("machinist", "Machinist", "🔩", "Rare", "Win with 8 different Terran units", _race_wins("Terran", 8)),
    _spec("artificer", "Artificer", "🔮", "Rare", "Win with 8 different Protoss units", _race_wins("Protoss", 8)),
    _spec(
        "bottom_of_barrel",
        "Bottom of the Barrel",
        "🛢️",
        "Epic",
        f"Win with {', '.join(UNDERDOG_UNITS[:-1])}, and {UNDERDOG_UNITS[-1]} — the three lowest-winrate units",
        (
            lambda h: set(UNDERDOG_UNITS) <= h.career.units_won,
            lambda h: (len(set(UNDERDOG_UNITS) & h.career.units_won), len(UNDERDOG_UNITS)),
        ),
    ),
    _spec("squadmates", "Squadmates", "🤝", "Common", "Play with 5 different teammates", _career_len("teammates", 5)),
    _spec(
        "social_butterfly",
        "Social Butterfly",
        "🦋",
        "Rare",
        "Play with 20 different teammates",
        _career_len("teammates", 20),
    ),
    _spec(
        "knows_everybody",
        "Knows Everybody",
        "🌐",
        "Epic",
        "Play with 75 different teammates",
        _career_len("teammates", 75),
    ),
    _spec("specialist", "Specialist", "🎯", "Rare", "Win 10 games with the same unit", _career("best_unit_wins", 10)),
    _spec(
        "grandmaster_of_one",
        "Grandmaster of One",
        "🥋",
        "Legendary",
        "Win 25 games with the same unit",
        _career("best_unit_wins", 25),
    ),
    _spec("production_line", "Production Line", "🏭", "Epic", "Build 10,000 units", _career("units_built", 10000)),
    _spec("war_machine", "War Machine", "⚙️", "Legendary", "Build 50,000 units", _career("units_built", 50000)),
    _spec("second_thoughts", "Second Thoughts", "🤔", "Common", "Use your first repick", _career("repicks", 1)),
    _spec("serial_repicker", "Serial Repicker", "🎰", "Rare", "Repick 25 times", _career("repicks", 25)),
    _spec("wheel_of_fortune", "Wheel of Fortune", "🎡", "Epic", "Repick 400 times", _career("repicks", 400)),
    # -- career: rivalries & community ------------------------------------
    _spec("nemesis", "Nemesis", "⚔️", "Uncommon", "Beat the same opponent 10 times", _career("best_opponent_wins", 10)),
    _spec(
        "bodyguard", "Bodyguard", "🛡️", "Rare", "Win 15 games with the same teammate", _career("best_teammate_wins", 15)
    ),
    _spec(
        "chronicler",
        "Chronicler",
        "📜",
        "Rare",
        f"Upload {CHRONICLER_UPLOADS} replays",
        (lambda h: False, None),  # granted from upload counts, never derived
    ),
    # -- career: calendar -------------------------------------------------
    _spec(
        "weekend_warrior",
        "Weekend Warrior",
        "🏖️",
        "Rare",
        "Play on 5 different weekends",
        _career_len("weekend_days", 5),
    ),
    _spec(
        "regular_customer",
        "Regular Customer",
        "☕",
        "Epic",
        "Play in 8 consecutive calendar weeks",
        _career("best_week_run", 8),
    ),
    _spec(
        "anniversary",
        "Anniversary",
        "🎂",
        "Rare",
        "Play on the anniversary of your first game",
        _career("anniversary_games", 1),
    ),
    _spec("insomniac", "Insomniac", "🌃", "Epic", "Play 50 late-night games", _career("night_games", 50)),
    # -- moment: streaks (live only) --------------------------------------
    _spec("heating_up", "Heating Up", "♨️", "Common", "Win 3 games in a row", _live("best_streak", 3)),
    _spec("on_fire", "On Fire", "🔥", "Rare", "Win 5 games in a row", _live("best_streak", 5)),
    _spec("unstoppable", "Unstoppable", "🌋", "Epic", "Win 8 games in a row", _live("best_streak", 8)),
    _spec("the_run", "The Run", "☄️", "Legendary", "Win 10 games in a row", _live("best_streak", 10)),
    _spec(
        "bounce_back",
        "Bounce Back",
        "🏀",
        "Uncommon",
        "Win right after losing 3 or more in a row",
        _live("bounce_backs", 1),
    ),
    # -- moment: single-game feats (live only) ----------------------------
    _spec("rampage", "Rampage", "😤", "Rare", "Destroy 25,000 enemy value in one game", _live("max_kills", 25000)),
    _spec(
        "extinction_event",
        "Extinction Event",
        "🦖",
        "Legendary",
        "Destroy 50,000 enemy value in one game",
        _live("max_kills", 50000),
    ),
    _spec(
        "economic_crash",
        "Economic Crash",
        "📉",
        "Epic",
        "Destroy 10,000 economy in one game",
        _live("max_econ_killed", 10000),
    ),
    _spec(
        "scorched_earth",
        "Scorched Earth",
        "💥",
        "Epic",
        "Destroy 8,000 tech in one game",
        _live("max_tech_killed", 8000),
    ),
    _spec(
        "highway_robbery",
        "Highway Robbery",
        "🦝",
        "Rare",
        "Trade at 5x efficiency (2,000+ value lost)",
        _live("best_trade", 5.0),
    ),
    _spec(
        "pyrrhic_victory",
        "Pyrrhic Victory",
        "🩸",
        "Epic",
        "Win while losing 30,000+ of your own value",
        _live("max_lost_in_win", 30000),
    ),
    _spec(
        "one_man_army",
        "One-Man Army",
        "🦾",
        "Legendary",
        "Outkill the entire enemy team by yourself",
        _live("outkilled_enemy_team", 1),
        secret=True,  # not a tier of anything — a genuine surprise
    ),
    _spec("moral_victory", "Moral Victory", "🥈", "Uncommon", "Be the MVP of a game you lost", _live("losing_mvps", 1)),
    _spec(
        "wasted_brilliance",
        "Wasted Brilliance",
        "🥀",
        "Epic",
        "Destroy 30,000 enemy value in a game you lose",
        _live("max_kills_in_loss", 30000),
    ),
    _spec(
        "cheerleader",
        "Cheerleader",
        "📣",
        "Rare",
        "Win a 10+ minute game with under 1,000 value killed and lost",
        _live("did_nothing_wins", 1),
    ),
    _spec(
        "every_mineral_counts",
        "Every Mineral Counts",
        "💎",
        "Rare",
        "Win a 10+ minute game with a median bank under 200",
        _live("thrifty_wins", 1),
    ),
    _spec(
        "seeing_double",
        "Seeing Double",
        "👯",
        "Uncommon",
        "Win with two of the same unit on your team",
        _live("twin_wins", 1),
    ),
    _spec(
        "send_in_clones",
        "Send in the Clones",
        "🧬",
        "Legendary",
        "Win with three of the same unit on your team",
        _live("triplet_wins", 1),
    ),
    _spec(
        "dynamic_duo",
        "Dynamic Duo",
        "🦸",
        "Epic",
        "Win 5 games in a row with the same teammate",
        _live("best_duo_streak", 5),
    ),
    _spec(
        "hat_trick",
        "Hat Trick",
        "🎩",
        "Rare",
        "Earn 3 MVPs in one sitting",
        _live("best_session_mvps", 3),
    ),
    _spec(
        "purity_of_essence",
        "Purity of Essence",
        "🧫",
        "Epic",
        "Win with an all-Zerg team",
        (lambda h: h.live.pure_race_wins.get("Zerg", 0) >= 1, None),
    ),
    _spec(
        "purity_of_form",
        "Purity of Form",
        "🔷",
        "Epic",
        "Win with an all-Protoss team",
        (lambda h: h.live.pure_race_wins.get("Protoss", 0) >= 1, None),
    ),
    _spec(
        "purity_of_man",
        "Purity of Man",
        "🚬",
        "Epic",
        "Win with an all-Terran team",
        (lambda h: h.live.pure_race_wins.get("Terran", 0) >= 1, None),
    ),
    _spec(
        "welcoming_committee",
        "Welcoming Committee",
        "👋",
        "Uncommon",
        "Play in someone's first monobattle",
        _live("welcomes", 1),
    ),
    _spec(
        "fresh_blood",
        "Fresh Blood",
        "🌱",
        "Rare",
        "Win a game with three or more first-timers in the lobby",
        _live("fresh_blood_wins", 1),
    ),
    _spec(
        "old_guard",
        "Old Guard Reunion",
        "🏰",
        "Rare",
        "Play a game where all eight players have 50+ games",
        _live("veteran_lobbies", 1),
    ),
    _spec(
        "marathon_session",
        "Marathon Session",
        "🏃",
        "Uncommon",
        "Play 6 games in one sitting",
        _live("best_session", 6),
    ),
    _spec(
        "opening_act",
        "Opening Act",
        "🎬",
        "Common",
        "Play in the game that kicks off a session night",
        _live("opening_acts", 1),
    ),
    _spec(
        "back_to_back",
        "Back-to-Back",
        "🔁",
        "Uncommon",
        "Be the MVP of two games in a row",
        _live("best_mvp_streak", 2),
    ),
    _spec(
        "the_prodigal",
        "The Prodigal",
        "🕊️",
        "Uncommon",
        "Win your first game back after 30+ days away",
        _live("return_wins", 1),
    ),
    _spec(
        "mirror_master",
        "Mirror Master",
        "🪞",
        "Uncommon",
        "Win a game where an opponent had your unit",
        _live("mirror_wins", 1),
    ),
    _spec(
        "never_doubted",
        "Never Doubted",
        "🔄",
        "Rare",
        "Win a comeback game (8,000+ behind)",
        _live("comeback_wins", 1),
    ),
    _spec(
        "along_for_the_ride",
        "Along for the Ride",
        "🎠",
        "Uncommon",
        "Win a 10+ minute game with the fewest kills in the lobby",
        _live("fewest_kills_wins", 1),
    ),
    _spec("the_swarm", "The Swarm", "🐜", "Epic", "Build 500 units in one game", _live("max_units_one_game", 500)),
    _spec("no_regrets", "No Regrets", "🎲", "Common", "Win a game after repicking", _live("repick_wins", 1)),
    _spec(
        "blitz",
        "Blitz",
        "⚡",
        "Rare",
        "Win a game in under 5 minutes",
        (lambda h: h.live.fastest_win is not None and h.live.fastest_win < 300, None),
    ),
    _spec("the_closer", "The Closer", "🥊", "Uncommon", "Win a 20+ minute game", _live("max_win_duration", 1200)),
    _spec(
        "special_delivery",
        "Special Delivery",
        "🚁",
        "Rare",
        "Win a game with heavy drop or Nydus play",
        _live("delivery_wins", 1),
    ),
    _spec(
        "greed_is_good",
        "Greed Is Good",
        "🤑",
        "Rare",
        "Win after taking 3 bases before making a single unit",
        _live("greed_wins", 1),
    ),
    _spec(
        "orbital_farm",
        "Orbital Farm",
        "🛰️",
        "Rare",
        "Own 5 Orbital Commands in one game",
        _live("max_orbitals", 5),
    ),
    _spec(
        "great_wall",
        "Great Wall",
        "🧱",
        "Rare",
        "Build 50 static defense structures in one game",
        _live("max_static_defense", 50),
    ),
    _spec(
        "fortress_city",
        "Fortress City",
        "🏰",
        "Epic",
        "Build 100 static defense structures in one game",
        _live("max_static_defense", 100),
    ),
    _spec(
        "birdwatcher",
        "Birdwatcher",
        "🐦",
        "Rare",
        "Win a 10+ minute game with a unit that can't shoot up, against two or more air units",
        _live("grounded_wins", 1),
    ),
    _spec(
        "sticks_and_stones",
        "Sticks and Stones",
        "🪨",
        "Legendary",
        "Win a 10+ minute game where nobody on your team can shoot up, against an air unit",
        _live("team_grounded_wins", 1),
    ),
    _spec("long_haul", "The Long Haul", "🐢", "Epic", "Play a 30+ minute game", _live("longest_game", 1800)),
    _spec("night_shift", "Night Shift", "🦉", "Rare", "Play 10 late-night games", _live("night_games", 10)),
    # -- secret Easter eggs (live only; hidden until unlocked) -------------
    _spec(
        "deja_vu",
        "Déjà Vu",
        "🌀",
        "Rare",
        "Roll the same random unit 3 games in a row",
        _live("best_same_pick_run", 3),
        secret=True,
    ),
    _spec(
        "repick_regret",
        "The One That Got Away",
        "💔",
        "Uncommon",
        "Lose after repicking, while an opponent wins with the unit you gave up",
        _live("repick_regrets", 1),
        secret=True,
    ),
    _spec(
        "finders_keepers",
        "Finders Keepers",
        "🧲",
        "Uncommon",
        "Win with a unit an opponent repicked away from",
        _live("finders_keepers", 1),
        secret=True,
    ),
    _spec(
        "rubber_band",
        "Rubber Band",
        "🪀",
        "Uncommon",
        "Alternate wins and losses for 6 straight games",
        _live("best_alternation", 6),
        secret=True,
    ),
]

SPECS_BY_KEY = {s.key: s for s in SPECS}
SECRET_KEYS = {s.key for s in SPECS if is_secret(s)}


class Earned(NamedTuple):
    spec: AchievementSpec
    earned_at: datetime.datetime  # played_at of the match that unlocked it


def is_countable(match: MonobattleMatch) -> bool:
    """Same gate as rating: only decided, real games feed achievements."""
    return (
        match.winning_team is not None
        and match.winner_confidence >= MIN_WINNER_CONFIDENCE
        and match.duration_seconds >= MIN_DURATION_SECONDS
    )


def _match_context(match: MonobattleMatch) -> _MatchContext:
    scored = [p.resources_killed for p in match.players if p.resources_killed is not None]
    min_kills = min(scored) if len(scored) >= 6 else None
    team_kills: dict[int, int] = {}
    for p in match.players:
        if p.resources_killed is not None:
            team_kills[p.team] = team_kills.get(p.team, 0) + p.resources_killed
    team_dup = {
        n: max(Counter(p.pick for p in match.team(n) if p.pick).values(), default=1)
        for n in {p.team for p in match.players}
    }
    return _MatchContext(match, match.mvp(), min_kills, team_kills, team_dup)


class AchievementBook:
    """Every player's DERIVED achievement state, built in one chronological
    pass. Handles are canonical (post-merge); look up through `for_handle`.
    This is the detector — what players actually hold is the unlock ledger
    (see `ledger_for_group` / `grant_new_unlocks`).

    `epoch` is the deployment's achievement launch time (matches played
    before it feed only the career tally). None means every match counts as
    live — only sensible for tests or throwaway analysis."""

    def __init__(self, merge_map: dict[str, str] | None = None, epoch: datetime.datetime | None = None):
        self._merge = merge_map or {}
        self._epoch = epoch
        self.histories: dict[str, PlayerHistory] = {}
        self.earned: dict[str, dict[str, Earned]] = {}  # handle -> key -> Earned
        self._last_countable_at: datetime.datetime | None = None

    @classmethod
    def from_matches(
        cls, matches, merge_map: dict[str, str] | None = None, epoch: datetime.datetime | None = None
    ) -> "AchievementBook":
        book = cls(merge_map, epoch)
        for match in sorted(matches, key=lambda m: _naive(m.played_at)):
            if is_countable(match):
                book._tally_match(match)
        return book

    def canonical(self, handle: str) -> str:
        return self._merge.get(handle, handle)

    def _tally_match(self, match: MonobattleMatch) -> None:
        ctx = _match_context(match)
        ctx.canonical = {p.toon_handle: self.canonical(p.toon_handle) for p in match.players}
        handles = set(ctx.canonical.values())
        ctx.newcomers = {h for h in handles if h not in self.histories}
        ctx.all_veterans = all(h in self.histories and self.histories[h].career.games >= 50 for h in handles)
        played = _naive(match.played_at)
        ctx.community_opening = (
            self._last_countable_at is not None and played - self._last_countable_at >= datetime.timedelta(hours=6)
        )
        self._last_countable_at = played
        live = self._epoch is None or played >= _naive(self._epoch)
        for player in match.players:
            handle = self.canonical(player.toon_handle)
            history = self.histories.setdefault(handle, PlayerHistory())
            history.career.update(player, ctx)
            if live:
                history.live.update(player, ctx)
            unlocked = self.earned.setdefault(handle, {})
            for spec in SPECS:
                if spec.key not in unlocked and spec.check(history):
                    unlocked[spec.key] = Earned(spec, match.played_at)

    # -- reads -----------------------------------------------------------

    def for_handle(self, handle: str) -> list[Earned]:
        """Derived earned achievements, rarest first then oldest."""
        return _rarest_first(list(self.earned.get(self.canonical(handle), {}).values()))

    def for_group(self, handles: list[str]) -> list[Earned]:
        """Merged groups collapse to one canonical handle, so any member
        resolves to the same set."""
        return self.for_handle(handles[0]) if handles else []

    def next_up(self, handle: str, limit: int = 3) -> list[tuple[AchievementSpec, float, float]]:
        """The closest not-yet-earned achievements with measurable progress,
        as (spec, current, target), most complete first."""
        history = self.histories.get(self.canonical(handle))
        if history is None:
            return []
        unlocked = self.earned.get(self.canonical(handle), {})
        candidates = []
        for spec in SPECS:
            if spec.key in unlocked or spec.progress is None or is_secret(spec):
                continue
            current, target = spec.progress(history)
            if current > 0:
                candidates.append((spec, current, target))
        candidates.sort(key=lambda c: c[1] / c[2], reverse=True)
        return candidates[:limit]

    def holder_counts(self) -> dict[str, int]:
        """key -> how many players have earned it (for live rarity display)."""
        counts: dict[str, int] = {}
        for unlocked in self.earned.values():
            for key in unlocked:
                counts[key] = counts.get(key, 0) + 1
        return counts


class AchievementCache:
    """An AchievementBook derived from a match store, rebuilt only when the
    store changes (same pattern as RatingCache)."""

    def __init__(self, store):
        self._store = store
        self._book: AchievementBook | None = None
        self._version = -1

    def book(self) -> AchievementBook:
        if self._book is None or self._version != self._store.change_count:
            merge_map = self._store.merge_map() if hasattr(self._store, "merge_map") else None
            epoch = self._store.achievement_epoch() if hasattr(self._store, "achievement_epoch") else None
            self._book = AchievementBook.from_matches((m for _, m in self._store.all_matches()), merge_map, epoch)
            self._version = self._store.change_count
        return self._book


# -- the unlock ledger (what players actually hold) -----------------------


def _rarest_first(earned: list[Earned]) -> list[Earned]:
    return sorted(earned, key=lambda e: (-RARITIES.index(e.spec.rarity), e.earned_at))


def ledger_for_group(store, handles: list[str]) -> list[Earned]:
    """A merge group's held achievements from the ledger, rarest first.
    Rows whose spec no longer exists are kept in the DB but not shown."""
    out = []
    for key, earned_at in store.unlocks_for(handles):
        spec = SPECS_BY_KEY.get(key)
        if spec is not None:
            out.append(Earned(spec, datetime.datetime.fromisoformat(earned_at)))
    return _rarest_first(out)


def ledger_holder_counts(store, merge_map: dict[str, str] | None = None) -> dict[str, int]:
    """key -> how many players hold it, collapsing merge groups."""
    merge_map = merge_map or {}
    holders: dict[str, set[str]] = {}
    for handle, key in store.all_unlocks():
        holders.setdefault(key, set()).add(merge_map.get(handle, handle))
    return {key: len(hs) for key, hs in holders.items()}


def ensure_seeded(store, cache: AchievementCache) -> int:
    """One-time launch grant: when a deployment first turns achievements on
    over an existing match history, write everything currently derivable into
    the ledger silently (career backfill per design; moment achievements are
    empty because the epoch was just stamped). No-op once any unlock exists,
    and on an empty database — a brand-new community starts announcing from
    its very first game. Returns the number of rows seeded."""
    if store.unlock_count() or not store.match_count():
        return 0
    book = cache.book()
    rows = [
        (handle, earned.spec.key, _naive(earned.earned_at).isoformat())
        for handle, unlocked in book.earned.items()
        for earned in unlocked.values()
    ]
    store.record_unlocks(rows)
    logger.info("Seeded achievement ledger with %d unlocks", len(rows))
    return len(rows)


def grant_direct(store, handle: str, key: str, earned_at: datetime.datetime) -> bool:
    """Grant a ledger-only achievement (one the derived engine can't see,
    e.g. Chronicler's upload count). True if newly granted."""
    held = {k for k, _ in store.unlocks_for(store.merged_handles(handle))}
    if key in held:
        return False
    store.record_unlocks([(handle, key, _naive(earned_at).isoformat())])
    return True


def sweep_grants(store, cache: AchievementCache) -> int:
    """Grant every derived-but-unrecorded achievement across ALL players,
    silently — used after bulk writes (channel backfills, re-parses) where
    per-match announcements would be a wall of stale badges. Returns how many
    rows were granted."""
    book = cache.book()
    rows = []
    for handle, unlocked in book.earned.items():
        held = {key for key, _ in store.unlocks_for(store.merged_handles(handle))}
        rows += [
            (handle, key, _naive(earned.earned_at).isoformat()) for key, earned in unlocked.items() if key not in held
        ]
    if rows:
        store.record_unlocks(rows)
    return len(rows)


def grant_new_unlocks(store, cache: AchievementCache, match: MonobattleMatch) -> list[tuple[str, Earned]]:
    """After a store write touching `match`, grant this match's players any
    derived achievements the ledger doesn't record yet, and return them for
    announcement as (player name, Earned), rarest first. Idempotent: what's
    already in the ledger is never returned again."""
    book = cache.book()
    rows, out, seen = [], [], set()
    for player in match.players:
        handle = book.canonical(player.toon_handle)
        if handle in seen:
            continue
        seen.add(handle)
        held = {key for key, _ in store.unlocks_for(store.merged_handles(player.toon_handle))}
        for key, earned in book.earned.get(handle, {}).items():
            if key not in held:
                rows.append((handle, key, _naive(earned.earned_at).isoformat()))
                out.append((player.name, earned))
    if rows:
        store.record_unlocks(rows)
    out.sort(key=lambda ne: (RARITIES.index(ne[1].spec.rarity), ne[1].earned_at), reverse=True)
    return out
