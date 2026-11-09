# MonoBattle mod

An extension mod that turns any 4v4 melee map into a monobattle: each player
builds one army unit. It replaces the arcade map *Monobattle LotV - Map
Rotation* for our community, and writes the facts MonoBot needs into every
replay. The design and the reasons behind it are in the map spec doc.

| File | What it is |
| --- | --- |
| `MonoBattle.galaxy` | The whole mod: vote, four pick modes, unit rules, battle start, ending, trade lock |
| `UpgradeData.xml` | The replay markers (135 no-effect upgrades), generated from the script's roster |
| `RaceSwapTest.galaxy` | A throwaway test mod for the one thing no existing mod proved: switching race mid-game |
| `lint_galaxy.py` | Static checks for the scripts (calls, argument counts, constants, Galaxy's locals-first rule) |

Nothing here has run in the game yet. The scripts pass `lint_galaxy.py`, but
only the Editor's compiler and a real game can prove them.

## How a game runs

1. **Load.** The map's own melee setup runs, then the mod pauses the game
   clock, removes every player's starting units, turns off melee
   victory/defeat and locks all army tech.
2. **Vote** (14 s): Blind Random, Single Draft, Pick Three or Captains Draft.
   Most votes wins; ties are random.
3. **Pick phase**, in the chosen mode:
   - *Blind Random* (30 s): pick a race button. Random gives one repick, always
     a different unit. Allies see each other's units; enemies don't.
   - *Single Draft*: a pool of 20 from all 42, picked one player at a time in
     snake order (A-B-B-A-A-B-B-A), 30 s a turn.
   - *Pick Three* (30 s): one unit of each race offered, never the same unit
     twice in a team; everyone picks at once. Allies see each other's offers
     and choices.
   - *Captains Draft*: volunteers for 10 s, then a random volunteer per team
     (a random player if none) drafts their team's units from the shared pool
     of 20 in snake order, 30 s a pick, then assigns them to players in 30 s.
     Unassigned units go out at random.
4. **Battle.** Each player's race is set to their pick's, their pick and its
   upgrades are unlocked, and a melee base spawns at their start location.
   The clock starts at 0:00. The **picks panel** appears in the top-left
   corner, as on the old map: a portrait per player (team 1 above team 2),
   their name beneath in their colour, "unit - player" on hover, and a Picks
   button that collapses it. It's shown to everyone, observers included.
5. **End.** A team with no structures left, or no players left, loses. Every
   game ends with Victory/Defeat. Typing **gg** opens a **concede vote** for
   your team (your gg counts as yes; 20 s); when more than half of the team's
   remaining players agree, the game ends for everyone at once. That's what
   gets the result into every replay: a replay only records Victory/Defeat if
   its recorder is still in the game when it ends. A failed vote locks the
   team out for a minute.

Unit rules are the old map's: only the pick's own upgrades (including ones
newer than the old map, like Interference Matrix and Lunge), morph precursors
can't attack (e.g. a Baneling player's Zerglings), Medivacs never heal,
Ravens are shared but can't use abilities unless picked. Changes from
the old map: **Viper** is pickable and **Queen** isn't; Queens are shared and
uncapped; Medivacs get Afterburners and Rapid Reignition; Tier Draft
is gone; **no resource trading until 10:00** on the
clock; balance is live LotV (Void Multi, no balance mod).

Not ported: the stats bank (MonoBot holds the real history), votekick, the
gg sound, the resource radial menu, the Tab stats screen.

Wishlist (observers): a live pick-phase view for observers (the pick panels
are per player today, so observers see nothing until the battle), and more
caster-friendly overlays. Both wait on the observer test below showing how
custom UI reaches observers.

## Replay markers

Granted with `TechTreeUpgradeAddLevel`, so each lands in the replay's tracker
events with its player and time. `main/services/replay_parser.py` reads them
(`_read_markers`) and falls back to its old inference when they're absent, so
replays from the old map parse as before.

| Marker | To | When |
| --- | --- | --- |
| `MB_Version` (level = `MB_VERSION`) | Every player | Load |
| `MB_Mode_Blind` / `_Single` / `_PickThree` / `_Captains` | Every player | Vote ends |
| `MB_Offer_<Unit>` | The player | Each Blind Random roll; each Pick Three offer |
| `MB_Captain` | Each captain | Captains chosen |
| `MB_Draft_<Unit>` | The captain | Each Captains Draft pick |
| `MB_Pick_<Unit>` | Every player | Battle starts |
| `MB_Start` | Every player | Battle starts |
| `MB_Win` / `MB_Loss` | Every player | Game ends |

`<Unit>` is MonoBot's unit name (`models/replay.py`'s `UNIT_RACE` keys, e.g.
`SiegeTank`, `Lurker`, `Viking`). `tests/test_mod_markers.py` fails if the
script, the XML and the parser drift apart.

## Building it in the Editor

Menu names below are from memory of the SC2 Editor and may differ slightly.

1. **New mod.** File → New → Mod. Set its dependencies to Core and Void Multi
   (the ones melee LotV maps use).
2. **Script.** Trigger module → New → Custom Script. Paste in
   `MonoBattle.galaxy` and set its initialization function to `MB_Init`.
3. **Markers.** Save the mod as a folder (component format), close the
   Editor, and copy `UpgradeData.xml` to
   `<mod>.SC2Mod/Base.SC2Data/GameData/UpgradeData.xml`. If the Editor doesn't
   list the `MB_*` upgrades when reopened, create any upgrade in the Data
   module, save, and replace the file again.
4. **Compile.** Save. Fix anything the Editor reports and tell MonoBot's
   maintainers (or paste the errors back to Claude).
5. **Test** on a 4v4 melee map (set one as the map used for testing mods in
   the Editor's preferences), then publish as an extension mod.

## Test 1: race swap (do this first)

Build `RaceSwapTest.galaxy` the same way (initialization function `RT_Init`,
with `UpgradeData.xml`) and run it on any 4v4 melee map, ideally with real
players or AI in several slots. Each player's base disappears and comes back
as the next race (Terran → Zerg → Protoss → Terran). Check, as the new race:

- [ ] The base, workers and (Zerg) larva and Overlord appear at your start location
- [ ] Command card: buildings and units of the new race, and none of the old
- [ ] Supply works: pylons/depots/overlords raise the cap
- [ ] Race mechanics: Chrono Boost, MULE, Queen inject, creep
- [ ] Research works (e.g. +1 weapons)
- [ ] The loading/score screen and race portrait don't show anything broken
- [ ] `-end` in chat: team 1 gets Victory, team 2 Defeat
- [ ] Upload the replay to MonoBot: it should show Blind Random, Roach/Stalker/
      Marine picks and team 1 winning by `marker`

If race switching fails, the fallbacks are in the spec doc.

## Test 2: the mod

Run `MonoBattle.galaxy` on a 4v4 melee map through each mode, then check:

- [ ] Every mode's buttons, timers and info text behave as described above
- [ ] The picks panel shows every portrait, name and hover text, sits clear of
      the game's own top-left buttons, and the Picks button collapses it
- [ ] An observer in the lobby, and a replay of the game, both show the picks
      panel (it's shown to all, but observers' view of custom UI is unproven)
- [ ] Only your pick (and its morph chain and upgrades) can be built
- [ ] Resource trading is refused before 10:00 and works after
- [ ] Killing a team's last structure ends the game for everyone
- [ ] gg opens the concede vote for your team only; a majority ends the game,
      a failed vote can't be restarted for a minute, and every player's replay
      shows the result
- [ ] The `MB_*` upgrades don't show anywhere in the UI or score screen
- [ ] Hatcheries start with their usual larva (the old map added an extra
      larva-spawn effect; it's left out until we know it's needed)
- [ ] The replay uploads to MonoBot with the right mode, picks and winner

## Checking the scripts

```
git clone --depth 1 --filter=blob:none --sparse https://github.com/SC2Mapster/SC2GameData
cd SC2GameData && git sparse-checkout set mods/core.sc2mod/base.sc2data/TriggerLibs \
    mods/{liberty,libertymulti,swarm,swarmmulti,void,voidmulti}.sc2mod/base.sc2data/GameData && cd ..
python sc2mod/lint_galaxy.py SC2GameData/mods/core.sc2mod/base.sc2data/TriggerLibs sc2mod/*.galaxy
python sc2mod/audit_upgrades.py SC2GameData/mods
```

`lint_galaxy.py` checks the scripts against Blizzard's function list.
`audit_upgrades.py` lists researchable upgrades the mod leaves open to every
pick; run it after a balance patch. Hi-Sec Auto Tracking, Neosteel Frame,
Medivac Rapid Deployment and Rapid Reignition, the Observer speed upgrade, Secreted Coating and
Overlord transport are left on deliberately. That dump stopped updating in
2026, so a function or upgrade it doesn't know is not necessarily wrong.
