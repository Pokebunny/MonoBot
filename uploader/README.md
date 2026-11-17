# MonoBot replay uploader

Runs on a player's own computer, watches their StarCraft II replay folders and
sends each new monobattle replay to MonoBot's upload API. The bot does the
rest: it parses the game, skips remakes and non-4v4s, ignores copies of a game
it already has (every player may run this), and records it on the right
ladder.

**Which ladder:** a game goes to BGH when at least 6 of its 8 players are
linked (`!link`) to members of the BGH server. Otherwise it goes to the
Monobattle server, which also collects every BGH game through
`counts_games_from`. A pub with a few BGH regulars in it therefore stays off
BGH, while a BGH game with one or two new, unlinked people still counts there.
(Config: `upload_api.default_guild_id`, plus `route_member_games_to` and
`route_min_members` on that community.)

## For players

Needs Python 3.10+ (python.org; on Windows tick "Add to PATH"). No packages.

1. In Discord, run `/uploadtoken`. The bot privately replies with a command
   containing your personal token.
2. Run that command in this folder:

   ```
   python monobot_uploader.py --server https://... --token ...
   ```

After that, `python monobot_uploader.py` is enough, since settings are saved to
`~/.monobot_uploader.json`. Leave it running while you play. Each upload logs
what happened, e.g. `stored on BGH as match #1520` or `already on Mono`.

- It only sends games saved after its first run. Add `--backfill-days 7` to
  also send the last week's.
- It finds `Documents/StarCraft II/Accounts/*/*/Replays/Multiplayer` (OneDrive
  too) on Windows and the macOS equivalent. Pass `--folder <path>`
  (repeatable) for anything else.
- It picks replays by file name: SC2 names them after the map, and the
  monobattle map's name starts with `Monobattle`.
- It logs to `~/.monobot_uploader.log` as well as the console.

### Keep it running (Windows)

After the first run has saved your settings, start it hidden at every login:

1. Press Win+R, type `shell:startup`, press Enter.
2. Right-click in that folder → New → Shortcut. For the location, enter
   `pythonw` followed by the full path to `monobot_uploader.py` in quotes,
   e.g. `pythonw "C:\Users\you\MonoBotUploader\monobot_uploader.py"`.
   (`pythonw` is Python without a window; use its full path, e.g.
   `C:\Users\you\AppData\Local\Programs\Python\Python314\pythonw.exe`, if
   plain `pythonw` isn't found.)
3. Name it "MonoBot Uploader", then double-click it once to start it now.

To check it's working, open `%USERPROFILE%\.monobot_uploader.log`. To stop
it, end `pythonw.exe` in Task Manager (and delete the shortcut to stop it
starting at login).

Uploads count toward your Chronicler achievement. `/uploadtoken` again gives
you a new token and turns off the old one; `/uploadtoken revoke` turns it off.

## For admins

The API runs inside the bot. Turn it on in `config.json`:

```json
"upload_api": {
  "port": 8080,
  "public_url": "https://uploads.example.com",
  "default_guild_id": 662204879653044234
},
"guilds": {
  "662204879653044234": {
    "route_member_games_to": [1052625283288662016],
    "route_min_members": 6
  }
}
```

It listens on 127.0.0.1 only, so a TLS proxy has to face the internet:
see "Replay upload API" in `deploy/README.md`.

API: `GET /v1/me` checks a token. `POST /v1/replays` takes the raw replay as
the body, with `Authorization: Bearer <token>` and `X-Replay-Name: <URL-encoded
file name>`. It answers `{"status": "stored" | "duplicate" | "rejected" |
"failed", "server", "match", "reason"}`.
