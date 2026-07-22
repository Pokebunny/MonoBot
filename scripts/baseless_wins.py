"""Rarity probe for a proposed achievement: win a game after losing every base.

Scans the local replay archive, replaying unit birth/death events to track how
many town halls each player owns over time, then crosses "hit zero" against the
parser's winner inference. Prints frequency per player-game and per win.
"""

import argparse
import glob
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "main"))

import sc2reader  # noqa: E402
from services import replay_parser  # noqa: E402
from services.storage import content_key  # noqa: E402

ARCHIVE = r"C:\Users\nrtab\OneDrive\Documents\StarCraft II\Accounts\85516\1-S2-1-539205\Replays\Multiplayer"

BASES = {
    "Nexus",
    "CommandCenter",
    "CommandCenterFlying",
    "OrbitalCommand",
    "OrbitalCommandFlying",
    "PlanetaryFortress",
    "Hatchery",
    "Lair",
    "Hive",
}

# Capture the replay object parse_replay loads so we only decode each file once.
_last_replay = {}
_real_load = sc2reader.load_replay


def _capturing_load(path, **kw):
    replay = _real_load(path, **kw)
    _last_replay["r"] = replay
    return replay


replay_parser.sc2reader.load_replay = _capturing_load


def base_timeline(replay, game_start):
    """Per player name: (ever_zero_second, seconds_spent_at_zero, peak_bases)."""
    alive = defaultdict(set)  # owner name -> live base unit ids
    peak = defaultdict(int)
    zero_at = {}
    zero_since = {}
    zero_time = defaultdict(int)

    for event in replay.events:
        name = type(event).__name__
        if name == "UnitBornEvent":
            owner, unit = event.unit_controller, event.unit
        elif name == "UnitDoneEvent":
            owner, unit = event.unit.owner, event.unit
        elif name == "UnitDiedEvent":
            owner, unit = event.unit.owner, event.unit
        else:
            continue
        if owner is None or unit.name not in BASES:
            continue
        who = owner.name
        if name == "UnitDiedEvent":
            alive[who].discard(unit.id)
        else:
            alive[who].add(unit.id)
        n = len(alive[who])
        peak[who] = max(peak[who], n)
        if event.second < game_start:
            continue
        if n == 0 and peak[who] > 0:
            if who not in zero_at:
                zero_at[who] = event.second
            zero_since.setdefault(who, event.second)
        elif n > 0 and who in zero_since:
            zero_time[who] += event.second - zero_since.pop(who)

    # Not game_length: that is real time, while event.second is game time (the
    # map runs at Faster), so it can fall *before* the last event.
    end = max((e.second for e in replay.events[-50:]), default=0)
    for who, since in zero_since.items():
        zero_time[who] += end - since
    return {who: (zero_at.get(who), zero_time.get(who, 0), peak[who]) for who in peak}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dir", default=ARCHIVE)
    ap.add_argument("--all-files", action="store_true", help="skip the monobattle filename filter")
    args = ap.parse_args()

    files = sorted(
        f
        for f in glob.glob(os.path.join(args.dir, "*.SC2Replay"))
        if args.all_files or "monobattle lotv - map rotation" in os.path.basename(f).lower()
    )
    if args.limit:
        files = files[: args.limit]
    print(f"{len(files)} replays", flush=True)

    player_games = wins = 0
    zero_games = zero_wins = 0
    zero_games_30s = zero_wins_30s = 0
    team_wipe_wins = 0
    hits = []
    dwell = []
    seen_games = set()
    winners_baseless = []

    for i, path in enumerate(files, 1):
        try:
            match = replay_parser.parse_replay(path)
            replay = _last_replay["r"]
            tl = base_timeline(replay, match.pick_phase_seconds)
        except Exception as exc:  # noqa: BLE001
            print(f"  skip {os.path.basename(path)}: {exc}", flush=True)
            continue
        if match.winning_team is None:
            continue
        # Several players upload their own recording of the same game; the DB
        # collapses those by content_key, so count each game once here too.
        key = content_key(match)
        if key in seen_games:
            continue
        seen_games.add(key)
        wiped_teams = defaultdict(list)
        for p in match.players:
            won = p.team == match.winning_team
            player_games += 1
            wins += won
            zero_at, zero_secs, peak = tl.get(p.name, (None, 0, 0))
            if zero_at is None:
                continue
            zero_games += 1
            zero_wins += won
            if won:
                dwell.append(zero_secs)
                winners_baseless.append(
                    (os.path.basename(path), p.name, p.pick, zero_at, zero_secs, match.duration_seconds)
                )
            if zero_secs >= 30:
                zero_games_30s += 1
                zero_wins_30s += won
                if won:
                    wiped_teams[p.team].append(p)
                    hits.append((os.path.basename(path), p.name, p.pick, zero_at, zero_secs, match.duration_seconds))
        for team, ps in wiped_teams.items():
            if len(ps) >= 2:
                team_wipe_wins += 1
        if i % 25 == 0:
            print(f"  {i}/{len(files)}...", flush=True)

    print()
    print(f"player-games (known winner): {player_games}  wins: {wins}")
    print(f"hit 0 bases at any point:    {zero_games} ({zero_games / player_games:.2%} of player-games)")
    print(f"  ...and won:                {zero_wins} ({zero_wins / max(wins, 1):.2%} of wins)")
    print(f"zero for 30s+:               {zero_games_30s} ({zero_games_30s / player_games:.2%})")
    print(f"  ...and won:                {zero_wins_30s} ({zero_wins_30s / max(wins, 1):.2%} of wins)")
    print(f"games where 2+ winners were baseless: {team_wipe_wins}")
    print()
    buckets = [(0, 10), (10, 30), (30, 60), (60, 120), (120, 10**6)]
    for lo, hi in buckets:
        n = sum(1 for d in dwell if lo <= d < hi)
        print(f"  baseless winners who stayed baseless {lo}-{hi}s: {n}")
    print()
    print("ALL baseless winners (any dwell):")
    for h in sorted(winners_baseless, key=lambda x: -x[4]):
        print(f"  {h[0][:50]:52} {h[1][:14]:15} {h[2] or '?':12} zero@{h[3]}s for {h[4]}s (game {h[5]}s)")
    print()
    for h in sorted(hits, key=lambda x: -x[4])[:40]:
        print(f"  {h[0][:50]:52} {h[1][:14]:15} {h[2]:12} zero@{h[3]}s for {h[4]}s (game {h[5]}s)")


if __name__ == "__main__":
    main()
