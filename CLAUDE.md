# MonoBot

Discord bot for StarCraft 2 monobattles: matchmaking, rating/ranking, and
replay analysis (parsing monobattle replay files).

Code lives under `main/`. Environment and dependencies are managed with **uv**
(Python 3.14, pinned in `.python-version`; deps in `pyproject.toml` via
`uv add`). Run from `main/`: `uv run MonoBot.py`.

## Layout

- `cogs/` — discord.py `commands.Cog` modules; one feature area each. Listed in
  `MonoBot.py`'s `cog_files` and loaded via `load_extension`. `replays` ingests
  `.SC2Replay` attachments; `leaderboard` serves ratings/stats commands.
- `services/` — stateless helpers cogs call. Third-party engines are isolated
  here: sc2reader behind `replay_parser`, openskill behind `rating`, sqlite
  behind `storage`. Embed builders live in `match_embeds`.
- `models/` — Pydantic models, grouped by feature.
- `resources/` — config + data loaded at import; also holds the gitignored
  `monobot.db` match database (source of truth = matches; ratings are always
  derived by replaying stored matches through `RatingBook.from_matches`).
  Schema changes go through numbered migrations in `services/storage.py`
  (bump `SCHEMA_VERSION`, add to `_MIGRATIONS`) — never a DB rebuild, which
  would lose the user-written `player_links` table.
- **Communities**: one ladder per Discord server (`community.py`). Each has
  its own match DB, caches (`RatingCache`/`AchievementCache`/`DuoCache`/
  `UnitCache`/`PickCache`) and config slice; nothing is shared. The home
  community is `config.json`'s top-level fields + `resources/monobot.db`;
  each server under `guilds` gets `resources/guilds/<id>/monobot.db`.
  Unconfigured servers get none (`home_guild_id: null`, the legacy default,
  makes home serve every unlisted server). Cogs reach data only through their
  server's community: a `CommunityCog` command's first line is
  `self = self.at(ctx)`, which binds a per-call copy holding that community's
  `store`/`ratings`/`achievements`/`duos`/`units`/`pick_boards` (helpers and view callbacks
  closing over `self` then read the right ladder); listeners and loops bind
  with `self.at(community)`. Matchmaking keeps a `Lobby` per community and its
  persistent views take `lobby_for(guild)`. Daily jobs use
  `community.start_daily` — one loop per community, since `tasks.loop`
  dedupes times by wall clock across timezones. Never construct a
  `MatchStore()` in a cog. `scripts/sync_live_db.py` pulls only the home DB.
- Ladder **seasons** are time windows (`seasons` table: name + `started_at` /
  `ended_at`), never a tag on each match: a match belongs to the season whose
  window holds its `played_at`, so a late-uploaded old replay still scores
  against the season it was played in. Starting a season resets ratings by
  moving the window, deleting nothing — past boards stay reconstructible.
  A reset is hard (everyone at the prior; seasons 1–2) or soft (the default
  since season 3): the season row stores only `carryover`/`seed_sigma`, and
  each player is seeded from their career rating at `started_at`, derived
  like everything else (`rating.season_start`; parameters chosen by
  `scripts/soft_reset_backtest.py`). Seeds live in `RatingBook.seeds`, apart
  from `ratings`, so boards and season ranks still list only players who have
  played; matchmaking and before/after deltas read `standing_for`. Build a
  season's ratings only through `season_book`/`season_start`, or a soft reset
  shows on some screens and not others.
  A player's **reported level** (`skill_levels` table, asked on their first
  Join, locked once set or once they've played) moves only where their
  first game starts (`rating.SKILL_LEVEL_MU`, as `RatingBook.priors`, set
  in `season_start` for both the career and season walks); before any game
  is uploaded, matchmaking reads it directly.
  Only ratings are season-scoped; match history, profile stats and the
  achievement ledger are career-wide.
- **Typed player names** resolve through `views.person_or_pick` and nothing
  else: SC2 names first (claim > current > former, `services/identity.py`),
  then a Discord member's name/mention/id (case-insensitive,
  `converters.MemberConverter`), with `PersonPickView` whenever the name is
  shared. A command that looks names up any other way will disagree with the
  rest about who a name means. (`!link` is the exception: it picks an
  *account* to claim, not a person.)
- **Pair stats** (`!duos`, the teammate half of `!h2h`) come from
  `rating.duo_records`, one chronological walk that gives each pair three
  numbers. The headline is the pair's **own rating**: the duo is rated as a
  single entity standing in for its two roster slots, with its other two
  teammates and the four opponents entering at their individual ratings, so it
  is opponent-adjusted. Also banked are the raw record and *synergy* (wins
  minus the model's summed pre-match win probability). Synergy is kept as an
  opt-in curiosity and labelled as one — measured over this history it does
  not repeat (split-half r < 0, and adding it to a win prediction never helps
  out of sample), whereas the duo rating does (split-half r ~ +0.5, and ~ +0.4
  on the part left after regressing out the two players' combined skill).
  Ranking on a level holds up where ranking on a residual does not; keep it
  that way. Career-wide and cached in `DuoCache`, since the walk does twelve
  model updates per match.
- **Unit ratings** (`!leaderboard units`) come from
  `rating.unit_ratings`: each pick is an extra member of its picker's team, and
  the walk updates only the units — players enter at their pre-match ladder
  rating and are never changed by it, so the ladder is untouched. Shown as
  points over the games-weighted average pick. Career-wide, cached in
  `UnitCache`; a named season (`!leaderboard units s3`) moves units only on
  its games, with players still at their career rating. Updating players and units jointly was tried and predicted
  worse; keep players fixed. Not to be confused with a **one-unit player
  board** (`!leaderboard marine`, `rating.pick_ratings`): players rated on
  their games with that unit, where only the focal player's slot is a
  per-unit entry and the other seven enter at their overall rating — fully
  per-unit lineups (the race-board way) would be too noisy at a handful of
  games per player per unit. All-time unless a season is named. Rushes are
  boards too: a cannon rush or a spine rush (`match_players.cannon_rush`/
  `spine_rush`, set by the parser; thresholds and why in
  `replay_parser.py`) adds a `CannonRush`/`SpineRush` board that the game
  counts on *as well as* the rusher's pick (`rating.boards_of`) — never
  instead of it.
- **Achievements** are derived from history but *held* in the
  `achievement_unlocks` ledger, which is reconciled rather than append-only:
  `achievements.reconcile` grants and revokes, and the startup hook announces
  both (a silent revoke is a bug report waiting to happen). Revoking is only
  safe while two properties hold — specs flagged `grant_only` are skipped
  (Chronicler comes from upload counts, so "not derived" means "not visible"),
  and every context a spec reads is computed from matches BEFORE the one being
  scored. The second one was once false: Overqualified's unit win-rate table
  spanned all of history, so a game played today changed who the underdog was
  in July and the badge flickered on and off. Any new spec must satisfy both.
  Rarity is display-only and may move either way.
- The **map** every game shows is not `matches.map_name`: every monobattle is
  played on one arcade map whose name never changes. The rotation happens when
  its author republishes it with new terrain, changing `matches.map_hash`, and
  the terrain's real name exists only inside the published map file. It is
  fetched once per hash from Blizzard's depot and cached in `map_versions`
  (`services/map_versions.py`); unresolved versions fall back to the arcade
  name.
  Per-map stats group on the map's **name** (`map_versions.group_by_map`),
  never its hash — the same terrain is republished under new hashes — and
  leave out games whose map isn't known.
- **Kill attribution**: `match_players.kills_by_unit` credits destroyed value
  to the unit type that killed it (`MatchPlayer.own_kills` for just the
  player's pick, seeing through Carrier->Interceptor and the other spawners in
  `PICK_KILLERS`). It is its OWN metric, not a breakdown of `resources_killed`:
  the medians agree but a 5% tail under-attributes, concentrated in air picks.
  Read `docs/kill-attribution.md` before setting any threshold on it. Every
  stored game has been parsed with attribution (checked Oct 2026), so an
  empty dict means nothing was credited to the player — usually they
  destroyed nothing, occasionally a kill the tracker credits to no unit.
- **One game, many recordings**: every player's replay of a game is a
  different file, so ingest matches games, not files — by the game's random
  seed (`game_seed`, identical in every recording) when both sides have one,
  else by `storage._same_game`: same accounts, teams and picks, starting less
  than a game-length apart. Never by start time alone: `played_at` is the
  recorder's own clock, seconds to minutes off another player's, and the old
  start-minute key let 29 games in twice (merged by migration 18). The
  archive (`resources/replays`, shared by all communities) keeps only stored
  recordings: the weaker file is deleted unless some database still stores
  it (`cogs.replays.keep_archive`; `prune_matches.py --files` for old ones).
- `scripts/` (repo root) — one-shot utilities, e.g. `backfill_archive.py` to
  seed the database from a folder of replays (idempotent, dedupes by hash).
  `reparse_stored.py` re-applies parser improvements to stored games
  (`MONOBOT_DB` picks the database).

## Conventions

- Functions and variables: `snake_case`. Classes: `PascalCase`.
  Module-level constants: `UPPER_SNAKE_CASE`.
- Modules: lowercase, feature-named. A feature is sliced across layer
  directories by repeating the name (e.g. `cogs/matchmaking.py`,
  `models/matchmaking.py`).
- Logging via the `logging` module (`logger = logging.getLogger(__name__)`),
  not `print`. Root logger is configured by `bot.run(..., root_logger=True)`.

## Config & secrets

- Secrets (`BOT_TOKEN`): `main/.env` via dotenv, gitignored. Never log secret
  values.
