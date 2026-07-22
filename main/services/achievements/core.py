"""Achievement primitives: the per-player `Tally` that accumulates stats from
match history, the `AchievementSpec` type, and the `_spec` builder factories.

The declarative catalogue of specs lives in `specs`; the detector/ledger engine
in `engine`. Both build on what's here. Nothing in this module imports the other
two, so the package's dependency flow stays one-way (core <- specs <- engine)."""

import datetime
from dataclasses import dataclass, field
from typing import Callable, NamedTuple

from models.replay import MatchPlayer, MonobattleMatch
from services.rating import MIN_DURATION_SECONDS, MIN_WINNER_CONFIDENCE

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

# The statistically worst units, from the full pub archive (30+ games each:
# BroodLord 33%, Archon 37%, Queen 37%). Revisit if the meta shifts.
UNDERDOG_UNITS = ("BroodLord", "Archon", "Queen")

# Overqualified pulls unit win rates LIVE from match history (never hardcoded),
# so the badge tracks the community meta with no upkeep. A unit needs this many
# decided games before it can be named the lobby's worst — an unlucky game or
# two shouldn't crown an under-sampled unit. No win-rate floor: being the MVP on
# the lobby's lowest-win-rate unit means everyone else drew a better one, so it
# reads as a real feat even when that unit isn't bad in absolute terms.
MIN_UNIT_GAMES_FOR_RANKING = 10

# Chronicler is granted from upload counts, not match history (see the
# replays cog); its check never fires in the derived engine.
CHRONICLER_UPLOADS = 25


def _naive(dt: datetime.datetime) -> datetime.datetime:
    """Match timestamps come from sc2reader (naive UTC) or tests (aware);
    normalize so epoch comparisons never mix the two."""
    return dt.replace(tzinfo=None)


def is_countable(match: MonobattleMatch) -> bool:
    """Same gate as rating: only decided, real games feed achievements."""
    return (
        match.winning_team is not None
        and match.winner_confidence >= MIN_WINNER_CONFIDENCE
        and match.duration_seconds >= MIN_DURATION_SECONDS
    )


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
    worst_winrate_picks: set[str] = field(default_factory=set)  # lobby's lowest-winrate pick(s), if a real underdog


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
    underdog_mvps: int = 0  # MVP while playing the lobby's worst unit by win rate
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
    max_kills_in_loss: int = 0
    thrifty_wins: int = 0  # long wins with a near-zero median bank
    hoard_wins: int = 0  # wins while sitting on a huge median bank
    delivery_wins: int = 0  # wins with real drop play
    max_static_defense: int = 0  # most defensive structures in one real game
    greed_wins: int = 0  # wins after expanding heavily before making a unit
    max_orbitals: int = 0  # most Orbital Commands owned in one game
    baseless_wins: int = 0  # wins after being wiped down to zero town halls
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
            if player.pick and player.pick in ctx.worst_winrate_picks:
                self.underdog_mvps += 1
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
        # No duration or dwell gate: across 527 community games every player
        # who won after losing their last town hall had been baseless for at
        # least 40s (median ~2.5 min), so there is no last-second false
        # positive to filter out — winning from nothing takes time by nature.
        if won and player.lost_all_bases:
            self.baseless_wins += 1
        if player.static_defense is not None:
            self.max_static_defense = max(self.max_static_defense, player.static_defense)
        if won and player.resources_floated is not None and player.resources_floated >= 5000:
            self.hoard_wins += 1  # won without ever spending the mountain of resources
        if won and match.duration_seconds >= 600:
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


class Earned(NamedTuple):
    spec: AchievementSpec
    earned_at: datetime.datetime  # played_at of the match that unlocked it


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
