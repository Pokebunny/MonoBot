# MonoBot

Discord bot for StarCraft 2 monobattles: matchmaking, rating/ranking, and
replay analysis (parsing monobattle replay files).

Code lives under `main/`. Environment and dependencies are managed with **uv**
(Python 3.14, pinned in `.python-version`; deps in `pyproject.toml` via
`uv add`). Run from `main/`: `uv run MonoBot.py`.

## Layout

- `cogs/` — discord.py `commands.Cog` modules; one feature area each. Listed in
  `MonoBot.py`'s `cog_files` and loaded via `load_extension`.
- `services/` — stateless helpers cogs call.
- `models/` — Pydantic models, grouped by feature.
- `resources/` — config + data loaded at import.

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
