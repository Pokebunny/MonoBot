# Kill attribution

How the parser credits each kill to the unit that made it, what that number is
worth, and where it lies. Written 2026-09-03 from a survey of 300 replays
(`match_players.kills_by_unit`, added in schema 12).

## What the replay actually gives us

Every `UnitDiedEvent` in the tracker stream carries a `killing_unit` and a
`killing_player` alongside the victim, and every unit knows its `minerals` and
`vespene` cost. Summing the victim's cost against the killer's unit type gives
"enemy value destroyed by your Hydralisks" directly — no reconstruction of the
game state required.

What the stream does **not** carry is per-ability damage. A kill is credited to
the *unit*, never the *ability*, so:

- Nuke kills credit to the **Ghost**. Across three games with a nuke launched,
  `Nuke` never appeared as a killer. The same holds for Storm, EMP and Fungal —
  any ability that doesn't spawn its own unit has no separate killer identity.
- A Disruptor's nova credits to the **Disruptor**, which is harmless, since
  that's all a Disruptor does.

So "launch N nukes" is expressible (the `TacticalNukeStrike` command event is
countable); "kill N value with nukes" is not, and never will be from a replay.

## Coverage

Of all deaths worth more than zero, across 300 replays:

| | value | share |
|---|---|---|
| attributed to an enemy killer | 19,916,550 | 86.5% |
| killed by its own owner | 563,150 | 2.4% |
| no killer recorded | 2,539,135 | 11.0% |

Counting *deaths* rather than value, only 71% carry a killer — but that figure
is meaningless. Most unattributed deaths are worth nothing at all:
`InvisibleTargetDummy` (engine dummies), `Larva` (consumed making units),
`ShapeApple` (map decoration), `LabMineralField750` (a patch mining out),
`AdeptPhaseShift` / `MULE` / `Locust` / `OracleStasisTrap` (temporary units
expiring). 18,114 of them are zero-value.

The self-killed 2.4% is morphs and self-destructs — Drones becoming buildings,
two templar merging into an Archon, a Baneling. Correctly excluded: they aren't
kills, and the game's own `resources_killed` doesn't count them either.

## Accuracy per player — the number that matters

Summed per player and compared against the game's own `resources_killed`
(players with 5,000+ value killed, n=1,178):

```
min 0.31 · p1 0.45 · p5 0.70 · median 1.00 · p95 1.09 · p99 1.18 · max 1.35
93.5% land within ±25%   ·   98.2% within 0.55–1.45
```

The centre is exact. The spread is real and asymmetric: a 5% tail sits under
0.70, with nothing comparable on the high side.

## The tail is air units — this is the one real bias

Share of a pick's player-games attributing under 0.70 of the official total:

```
VoidRay       18.9%       Hydralisk      4.2%
Viking        16.7%       WidowMine      3.8%
Battlecruiser 13.3%       Marauder       3.8%
Tempest       12.2%       Marine        ~2%
Carrier       10.4%       Zergling      ~2%
Phoenix        7.7%
```

Air picks are two to nine times more likely to be badly under-credited than
ground picks. It tracks the unattributed victims, which skew expensive
(`DarkTemplar`, `HighTemplar`, `VoidRay`, `Carrier`, `Battlecruiser`, `Tempest`,
`Colossus`): whatever causes an expensive unit's death to lose its killer,
players who kill expensive units eat the loss, and air units mostly kill other
air units.

**Consequence for anything built on this:** never scale a threshold from the
official kill number, and never set one air-pick threshold from ground-pick
data. Tune on this metric's own distribution, per pick.

Medians are *not* biased — every pick's median ratio lands between 0.97 and
1.04 — so a median-based or rank-based feature is safe. It's the tail that
moves, which matters for a "hit this number once" achievement and not for a
"typical performance" statistic.

## Whose kill is it — `PICK_KILLERS`

Most units kill under their own name (90–98% of their player's credited value).
Five don't, because their damage is dealt by something they spawn:

| pick | actually credited to | share |
|---|---|---|
| Carrier | `Interceptor` | 95% |
| SwarmHost | `Locust` | 92% |
| BroodLord | `BroodlingEscort` + `Broodling` | 70% |
| Raven | `AutoTurret` | 70% |
| HighTemplar | `HighTemplar` + `Archon` (merges) | 59% |

`models.replay.PICK_KILLERS` encodes exactly these; everything else maps to
itself. `MatchPlayer.own_kills` sums a player's kills over their pick's killer
set, so a feature asking "how much did their army kill" doesn't have to know
that a Carrier never kills anything itself.

Kills by things that aren't the player's army are kept separate and named:
`STATIC_DEFENSE_KILLERS` (cannons, crawlers, turrets, planetaries, bunkers) and
`WORKER_KILLERS` (Probe/SCV/Drone). A cannon rush and a Stalker are both
"kills" in `resources_killed`; here they are two different numbers.

Two picks are poorly served regardless: **Infestor** (34% of its player's value)
and **Sentry** (31%), whose damage is mostly spells the tracker credits to
nobody. Don't set a per-unit threshold on either without checking first.

### Cross-check on community games

The survey above samples the local replay archive, which is mostly pub games.
Backfilling the 137 community games whose replays are on hand gives a milder
picture (n=566 player-games with 5,000+ value killed):

```
median 1.00 · p5 0.79 · p95 1.10 · under 0.70: 3.5%
air picks under 0.70: 4.5%   ground picks: 3.0%
```

Same centre, same direction of bias, but a far smaller gap than the 12–19% vs
2–5% split in the wider sample. Community games are longer and more even, so
fewer kills are the chaotic multi-attacker deaths that lose their killer. Treat
the wider sample's numbers as the pessimistic bound and these as the realistic
one — and re-measure on whatever population a feature will actually score.

## Storage and backfill

`kills_by_unit` is a JSON column on `match_players` (schema 12), written the
same way `unit_counts` is. The migration is additive — existing rows default to
`'{}'`, which reads as "not recorded", never as "killed nothing".

Games only gain attribution when their replay is re-parsed:

```
MONOBOT_DB=/path/to/monobot.db uv run python scripts/reparse_stored.py <archive_dir>
```

Only games whose replay file is still on hand can be backfilled — matching is
by file hash, so on the server this has to run against the bot's own upload
archive (`main/resources/replays`), not a personal replay folder. Pointing it at
a general Replays directory works but wastes time failing to parse every
campaign and ladder replay it finds.

A game whose replay is no longer in the archive keeps an empty dict forever, so
any feature reading this must treat empty as unknown and exclude those games
rather than scoring them as zero — the same rule per-map stats follow for
unresolved map hashes.

## Reproducing

The survey scripts are ad hoc, not in the repo. The measurement is: load each
replay at `load_level=4`, walk `UnitDiedEvent`, bucket victim cost by
`killing_unit` name, and compare the per-player sum against the last
`PlayerStatsEvent.resources_killed`. 300 replays is enough to size the bias by
pick for common picks; the rarer picks (Infestor n=16, Sentry n=12, DarkTemplar
n=10) are thin and worth re-measuring on the full archive before anything
depends on them.
