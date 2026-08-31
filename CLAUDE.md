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
- Ladder **seasons** are time windows (`seasons` table: name + `started_at` /
  `ended_at`), never a tag on each match: a match belongs to the season whose
  window holds its `played_at`, so a late-uploaded old replay still scores
  against the season it was played in. Starting a season resets ratings by
  moving the window, deleting nothing — past boards stay reconstructible.
  Only ratings are season-scoped; match history, profile stats and the
  achievement ledger are career-wide.
- **Pair stats** (`!duos`, the teammate half of `!h2h`) come from
  `rating.duo_records`, which walks history chronologically and banks each
  pair's *expected* wins from the model's pre-match prediction. Synergy =
  wins - expected, and it is deliberately a shrunk estimate: a duo's wins also
  lift their own ratings. Boards offer both sorts because neither is the whole
  answer — raw win rate mostly ranks whoever is individually strong, and
  synergy can rank a losing pair above a winning one.
- The **map** every game shows is not `matches.map_name`: every monobattle is
  played on one arcade map whose name never changes. The rotation happens when
  its author republishes it with new terrain, changing `matches.map_hash`, and
  the terrain's real name exists only inside the published map file. It is
  fetched once per hash from Blizzard's depot and cached in `map_versions`
  (`services/map_versions.py`); unresolved versions fall back to the arcade
  name. `scripts/backfill_map_hashes.py` fills hashes in for older games.
  Per-map stats group on the map's **name** (`map_versions.group_by_map`),
  never its hash — the same terrain is republished under new hashes — and
  leave out games whose map isn't known.
- `scripts/` (repo root) — one-shot utilities, e.g. `backfill_archive.py` to
  seed the database from a folder of replays (idempotent, dedupes by hash).

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
