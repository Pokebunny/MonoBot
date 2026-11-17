# MonoBot replay uploader

Runs on a player's own computer, watches their StarCraft II replay folders and
posts each new monobattle replay to MonoBot through a Discord webhook. The bot
does the rest: it parses the game, skips remakes and non-4v4s, ignores copies of
a game already stored (every player may run this), and records it on the right
ladder. If all eight players are linked (`!link`) to members of a server listed
in `route_member_games_to` (e.g. BGH), the game goes there; otherwise it goes to
the server the webhook posts in. `counts_games_from` still applies afterwards,
the same as a manual upload.

Each upload gets a reaction from the bot: ✅ stored, 🔁 already had it,
⏭️ not a ladder game (a remake or not a 4v4), ❌ couldn't parse.

## For players

Needs Python 3.10+ (python.org; on Windows tick "Add to PATH"). No packages.

```
python monobot_uploader.py --webhook <URL from an admin> --name "<your SC2 name>"
```

After that, `python monobot_uploader.py` is enough, since settings are saved to
`~/.monobot_uploader.json`. Leave it running while you play. It only sends games
saved after its first run; add `--backfill-days 7` to also send the last week's.
It finds `Documents/StarCraft II/Accounts/*/*/Replays/Multiplayer` (OneDrive
too) on Windows and the macOS equivalent; pass `--folder <path>` (repeatable)
for anything else. It picks replays by file name: SC2 names them after the map,
and the monobattle map's name starts with `Monobattle`.

To start it with Windows, put a shortcut to `pythonw monobot_uploader.py` in
`shell:startup`.

## For admins

1. In the replay channel's settings: Integrations → Webhooks → New Webhook.
   Copy its URL. The number after `/webhooks/` is its id.
2. In `config.json`, on the community that owns that channel:

   ```json
   "upload_webhook_ids": [<webhook id>],
   "route_member_games_to": [<BGH server id>]
   ```

   Restart the bot. The bot needs Add Reactions in that channel.
3. Share the URL with players. Anyone holding it can post files there, so share
   it only inside the community. If it leaks, delete the webhook and make a new
   one. Uploads aren't credited to a person (no Chronicler progress), since a
   webhook can't prove who sent it.
