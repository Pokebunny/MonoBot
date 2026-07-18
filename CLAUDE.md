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
  would lose user-written tables (`player_links`, `account_merges`).
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
