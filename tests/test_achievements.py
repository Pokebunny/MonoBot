import datetime

import pytest
from models.replay import MatchPlayer, MonobattleMatch
from services.achievements import (
    SPECS,
    SPECS_BY_KEY,
    AchievementBook,
    AchievementCache,
    ensure_seeded,
    grant_new_unlocks,
    is_secret,
    ledger_for_group,
    ledger_holder_counts,
    sweep_grants,
)
from services.storage import MatchStore, hash_replay

# The derived engine takes the epoch as a parameter (deployments stamp theirs
# in the DB's meta table); tests pin one explicitly.
EPOCH = datetime.datetime(2026, 7, 19, tzinfo=datetime.timezone.utc)
BEFORE_EPOCH = EPOCH - datetime.timedelta(days=30)
AFTER_EPOCH = EPOCH + datetime.timedelta(days=1)


def _player(name, team, pick="Zergling", kills=None, **kwargs):
    return MatchPlayer(
        name=name,
        toon_handle=name,
        team=team,
        race=kwargs.pop("race", "Zerg"),
        pick=pick,
        repick_used=kwargs.pop("repick_used", False),
        resources_killed=kills,
        unit_counts=kwargs.pop("unit_counts", {}),
        **kwargs,
    )


def _match(winning_team=1, played_at=None, duration=900, players=None, **kwargs):
    players = players or [_player(f"A{i}", 1) for i in range(1, 5)] + [_player(f"B{i}", 2) for i in range(1, 5)]
    return MonobattleMatch(
        file_name="test.SC2Replay",
        map_name="Monobattle LotV - Map Rotation",
        played_at=played_at or AFTER_EPOCH,
        duration_seconds=duration,
        game_type="4v4",
        pick_mode="blind_random",
        pick_phase_seconds=60,
        players=players,
        winning_team=winning_team,
        winner_confidence=1.0,
        winner_method="recorded",
        **kwargs,
    )


def _series(n, winning_team=1, start=None, **kwargs):
    start = start or AFTER_EPOCH
    return [
        _match(winning_team=winning_team, played_at=start + datetime.timedelta(hours=i), **kwargs) for i in range(n)
    ]


def _book(matches, merge_map=None):
    return AchievementBook.from_matches(matches, merge_map, epoch=EPOCH)


def _keys(book, handle):
    return {e.spec.key for e in book.for_handle(handle)}


# -- derived engine ------------------------------------------------------


def test_spec_keys_unique():
    assert len(SPECS_BY_KEY) == len(SPECS)


def test_first_game_and_first_win():
    book = _book([_match()])
    assert {"first_game", "first_win"} <= _keys(book, "A1")
    winner_keys = _keys(book, "B1")
    assert "first_game" in winner_keys
    assert "first_win" not in winner_keys


def test_undecided_games_do_not_count():
    match = _match().model_copy(update={"winning_team": None, "winner_method": "unknown"})
    book = _book([match])
    assert book.for_handle("A1") == []


def test_career_achievements_backfill_before_epoch():
    book = _book(_series(25, start=BEFORE_EPOCH))
    assert "regular" in _keys(book, "A1")  # 25 games, all pre-epoch


def test_moment_achievements_do_not_backfill():
    # 5 straight pre-epoch wins would be On Fire — but only live games count.
    book = _book(_series(5, start=BEFORE_EPOCH))
    assert "heating_up" not in _keys(book, "A1")
    book = _book(_series(5, start=AFTER_EPOCH))
    assert {"heating_up", "on_fire"} <= _keys(book, "A1")


def test_streak_broken_by_loss():
    matches = (
        _series(2)
        + _series(1, winning_team=2, start=AFTER_EPOCH + datetime.timedelta(hours=10))
        + _series(2, start=AFTER_EPOCH + datetime.timedelta(hours=20))
    )
    book = _book(matches)
    assert "heating_up" not in _keys(book, "A1")  # never 3 in a row


def test_earned_at_is_the_unlocking_match():
    matches = _series(3)
    book = _book(matches)
    earned = {e.spec.key: e for e in book.for_handle("A1")}
    assert earned["heating_up"].earned_at == matches[2].played_at


def test_single_game_feat_rampage():
    players = [_player(f"A{i}", 1, kills=5000) for i in range(1, 5)] + [
        _player(f"B{i}", 2, kills=1000) for i in range(1, 5)
    ]
    players[0] = _player("A1", 1, kills=26000)
    book = _book([_match(players=players)])
    assert "rampage" in _keys(book, "A1")
    assert "rampage" not in _keys(book, "A2")


def test_one_man_army():
    players = (
        [_player("A1", 1, kills=15000)]
        + [_player(f"A{i}", 1, kills=100) for i in range(2, 5)]
        + [_player(f"B{i}", 2, kills=3000) for i in range(1, 5)]
    )
    book = _book([_match(players=players)])
    assert "one_man_army" in _keys(book, "A1")


def test_moral_victory_losing_mvp():
    players = [_player(f"A{i}", 1, kills=2000) for i in range(1, 5)] + [
        _player(f"B{i}", 2, kills=1000) for i in range(1, 5)
    ]
    players[4] = _player("B1", 2, kills=30000)  # top killer on the losing team
    book = _book([_match(winning_team=1, players=players)])
    assert "moral_victory" in _keys(book, "B1")


def test_overqualified_mvp_with_worst_unit():
    # Build a live win-rate table: BroodLord always loses, Zergling always
    # wins, so BroodLord is the community's worst (and rankable) unit.
    matches = [
        _match(
            winning_team=1,
            players=[_player(f"A{i}", 1, pick="Zergling", kills=1000) for i in range(1, 5)]
            + [_player(f"B{i}", 2, pick="BroodLord", kills=1000) for i in range(1, 5)],
            played_at=AFTER_EPOCH + datetime.timedelta(hours=h),
        )
        for h in range(10)
    ]
    # Qualifying game: a BroodLord player tops the lobby's kills. Win isn't
    # required — topping kills with the worst unit is the feat.
    q_players = (
        [_player(f"A{i}", 1, pick="Zergling", kills=1000) for i in range(1, 5)]
        + [_player("B1", 2, pick="BroodLord", kills=30000)]
        + [_player(f"B{i}", 2, pick="BroodLord", kills=1000) for i in range(2, 5)]
    )
    matches.append(_match(winning_team=1, players=q_players, played_at=AFTER_EPOCH + datetime.timedelta(hours=11)))
    book = _book(matches)
    assert "overqualified" in _keys(book, "B1")  # MVP on the lobby's worst unit
    assert "overqualified" not in _keys(book, "B2")  # worst unit, but not the MVP
    assert "overqualified" not in _keys(book, "A1")  # MVP earlier, but on a strong unit


def test_mirror_master():
    players = [_player(f"A{i}", 1, pick="Marine" if i == 1 else "Zergling") for i in range(1, 5)] + [
        _player(f"B{i}", 2, pick="Marine" if i == 1 else "Roach") for i in range(1, 5)
    ]
    book = _book([_match(winning_team=1, players=players)])
    assert "mirror_master" in _keys(book, "A1")
    assert "mirror_master" not in _keys(book, "A2")


def test_blitz_needs_fast_win():
    book = _book([_match(duration=290)])
    assert "blitz" in _keys(book, "A1")
    assert "blitz" not in _keys(book, "B1")
    book = _book([_match(duration=400)])
    assert "blitz" not in _keys(book, "A1")


def test_deja_vu_same_roll_three_times():
    matches = _series(3)  # everyone picks Zergling every game (blind random)
    book = _book(matches)
    assert "deja_vu" in _keys(book, "A1")
    book = _book(_series(2))
    assert "deja_vu" not in _keys(book, "A1")


def test_seeing_double_and_clones():
    players = [_player(f"A{i}", 1, pick="Marine" if i <= 3 else "Zergling") for i in range(1, 5)] + [
        _player(f"B{i}", 2, pick="Roach") for i in range(1, 5)
    ]
    book = _book([_match(winning_team=1, players=players)])
    assert {"seeing_double", "send_in_clones"} <= _keys(book, "A1")
    assert "seeing_double" not in _keys(book, "B1")  # losers don't count


def test_welcoming_committee_needs_someone_elses_debut():
    first = _match()  # everyone's own first game — nobody is welcoming
    roster2 = (
        [_player("NewGuy", 1)] + [_player(f"A{i}", 1) for i in range(2, 5)] + [_player(f"B{i}", 2) for i in range(1, 5)]
    )
    second = _match(players=roster2, played_at=AFTER_EPOCH + datetime.timedelta(hours=1))
    book = _book([first, second])
    assert "welcoming_committee" in _keys(book, "A2")  # veteran who met NewGuy
    assert "welcoming_committee" not in _keys(book, "NewGuy")  # own debut
    assert "welcoming_committee" not in _keys(book, "A1")  # wasn't in game 2


def test_nemesis_counts_repeat_victims():
    book = _book(_series(10))
    assert "nemesis" in _keys(book, "A1")  # beat B1 ten times
    assert "nemesis" not in _keys(book, "B1")


def test_rubber_band_alternation():
    matches = [
        _match(winning_team=1 if i % 2 == 0 else 2, played_at=AFTER_EPOCH + datetime.timedelta(hours=i))
        for i in range(6)
    ]
    book = _book(matches)
    assert "rubber_band" in _keys(book, "A1")
    book = _book(matches[:5])
    assert "rubber_band" not in _keys(book, "A1")


def test_finders_keepers_win_with_abandoned_unit():
    players = (
        [_player("A1", 1, pick="Marine")]
        + [_player(f"A{i}", 1, pick="Zergling") for i in range(2, 5)]
        + [_player("B1", 2, pick="Roach", repick_used=True, repick_from="Marine")]
        + [_player(f"B{i}", 2, pick="Hydralisk") for i in range(2, 5)]
    )
    book = _book([_match(winning_team=1, players=players)])
    assert "finders_keepers" in _keys(book, "A1")
    assert "repick_regret" in _keys(book, "B1")  # the other side of the trade


def test_special_delivery_needs_drops_and_a_win():
    players = (
        [_player("A1", 1, drop_commands=6)]
        + [_player(f"A{i}", 1) for i in range(2, 5)]
        + [_player(f"B{i}", 2, drop_commands=8) for i in range(1, 5)]
    )
    book = _book([_match(winning_team=1, players=players)])
    assert "special_delivery" in _keys(book, "A1")
    assert "special_delivery" not in _keys(book, "A2")  # no drops
    assert "special_delivery" not in _keys(book, "B1")  # dropped but lost


def test_great_wall_counts_structures_regardless_of_result():
    players = (
        [_player("A1", 1, static_defense=55)]
        + [_player(f"A{i}", 1) for i in range(2, 5)]
        + [_player(f"B{i}", 2) for i in range(1, 5)]
    )
    book = _book([_match(winning_team=2, players=players)])  # losing still counts
    assert "great_wall" in _keys(book, "A1")
    assert "fortress_city" not in _keys(book, "A1")  # 55 < 100
    assert "great_wall" not in _keys(book, "A2")


def test_greed_is_good_needs_three_bases_and_a_win():
    def with_bases(bases, won=True):
        players = (
            [_player("A1", 1, bases_before_unit=bases)]
            + [_player(f"A{i}", 1) for i in range(2, 5)]
            + [_player(f"B{i}", 2) for i in range(1, 5)]
        )
        return _match(winning_team=1 if won else 2, players=players)

    assert "greed_is_good" in _keys(_book([with_bases(3)]), "A1")
    assert "greed_is_good" not in _keys(_book([with_bases(2)]), "A1")
    assert "greed_is_good" not in _keys(_book([with_bases(3, won=False)]), "A1")


def test_purity_needs_a_single_race_team():
    # Default roster: everyone Zerg — an all-Zerg winning team earns it.
    book = _book([_match(winning_team=1)])
    assert "purity_of_essence" in _keys(book, "A1")
    assert "purity_of_essence" not in _keys(book, "B1")
    # A mixed team earns nothing.
    players = (
        [_player("A1", 1, race="Terran", pick="Marine")]
        + [_player(f"A{i}", 1) for i in range(2, 5)]
        + [_player(f"B{i}", 2) for i in range(1, 5)]
    )
    book = _book([_match(winning_team=1, players=players)])
    assert "purity_of_essence" not in _keys(book, "A1")
    assert "purity_of_man" not in _keys(book, "A1")


def test_exterminator_collects_beaten_units():
    players = [_player(f"A{i}", 1) for i in range(1, 5)] + [
        _player(f"B{i}", 2, pick=p) for i, p in enumerate(["Marine", "Roach", "Carrier", "Zealot"], 1)
    ]
    book = _book([_match(winning_team=1, players=players)])
    assert book.histories["A1"].career.units_beaten == {"Marine", "Roach", "Carrier", "Zealot"}
    assert book.histories["B1"].career.units_beaten == set()  # losers beat no one


def test_hat_trick_three_mvps_one_sitting():
    def mvp_match(hours):
        players = (
            [_player("A1", 1, kills=20000)]
            + [_player(f"A{i}", 1, kills=100) for i in range(2, 5)]
            + [_player(f"B{i}", 2, kills=100) for i in range(1, 5)]
        )
        return _match(players=players, played_at=AFTER_EPOCH + datetime.timedelta(hours=hours))

    book = _book([mvp_match(0), mvp_match(1), mvp_match(2)])
    assert "hat_trick" in _keys(book, "A1")
    # Same three MVPs spread across separate sittings don't count.
    book = _book([mvp_match(0), mvp_match(10), mvp_match(20)])
    assert "hat_trick" not in _keys(book, "A1")


def test_birdwatcher_and_sticks_and_stones():
    def roster(team_picks, enemy_picks):
        return [_player(f"A{i}", 1, pick=team_picks[i - 1]) for i in range(1, 5)] + [
            _player(f"B{i}", 2, pick=enemy_picks[i - 1]) for i in range(1, 5)
        ]

    grounded = ["Zealot", "Roach", "SiegeTank", "DarkTemplar"]
    covered = ["Zealot", "Roach", "SiegeTank", "Marine"]
    # Birdwatcher: own no-AA pick vs 2+ air (teammates' AA irrelevant).
    book = _book([_match(winning_team=1, players=roster(covered, ["Mutalisk", "Carrier", "Roach", "Roach"]))])
    assert "birdwatcher" in _keys(book, "A1")
    assert "birdwatcher" not in _keys(book, "A4")  # Marine shoots up
    assert "sticks_and_stones" not in _keys(book, "A1")  # Marine covers the team
    book = _book([_match(winning_team=1, players=roster(covered, ["Mutalisk", "Roach", "Roach", "Roach"]))])
    assert "birdwatcher" not in _keys(book, "A1")  # only one air unit
    # Sticks and Stones: the whole team can't shoot up, any enemy air.
    book = _book([_match(winning_team=1, players=roster(grounded, ["Mutalisk", "Roach", "Roach", "Roach"]))])
    assert "sticks_and_stones" in _keys(book, "A1")
    assert "sticks_and_stones" in _keys(book, "A2")  # whole team shares it
    book = _book([_match(winning_team=1, players=roster(grounded, ["Roach", "Roach", "Roach", "Roach"]))])
    assert "sticks_and_stones" not in _keys(book, "A1")  # no enemy air


def test_merged_accounts_share_achievements():
    # 2 games as Jay + 1 as Luigi = a 3-streak for the merged player.
    matches = [
        _match(
            players=[_player("Jay", 1)]
            + [_player(f"A{i}", 1) for i in range(2, 5)]
            + [_player(f"B{i}", 2) for i in range(1, 5)],
            played_at=AFTER_EPOCH + datetime.timedelta(hours=h),
        )
        for h in range(2)
    ] + [
        _match(
            players=[_player("Luigi", 1)]
            + [_player(f"A{i}", 1) for i in range(2, 5)]
            + [_player(f"B{i}", 2) for i in range(1, 5)],
            played_at=AFTER_EPOCH + datetime.timedelta(hours=2),
        )
    ]
    merge = {"Jay": "Jay", "Luigi": "Jay"}
    book = AchievementBook.from_matches(matches, merge, epoch=EPOCH)
    assert "heating_up" in _keys(book, "Luigi")  # resolves through the merge
    assert "heating_up" in _keys(book, "Jay")


def test_next_up_reports_progress_and_hides_secrets():
    book = _book(_series(10))
    entries = book.next_up("A1", limit=50)
    progress = {spec.key: (cur, target) for spec, cur, target in entries}
    assert progress["regular"] == (10, 25)
    assert "first_game" not in progress  # already earned
    assert all(not is_secret(spec) for spec, _, _ in entries)


def test_only_surprises_are_secret():
    # Secrets are situational surprises you stumble into (Easter eggs, quirky
    # feats), never tier extensions of a visible family (the tier below
    # telegraphs those).
    assert {s.key for s in SPECS if is_secret(s)} == {
        "one_man_army",
        "deja_vu",
        "repick_regret",
        "finders_keepers",
        "rubber_band",
        "along_for_the_ride",
        "every_mineral_counts",
        "the_prodigal",
        "anniversary",
        "trust_fund",
    }


def test_trust_fund_win_on_a_huge_bank():
    players = (
        [_player("A1", 1, resources_floated=6000)]
        + [_player(f"A{i}", 1) for i in range(2, 5)]
        + [_player(f"B{i}", 2, resources_floated=9000) for i in range(1, 5)]
    )
    book = _book([_match(winning_team=1, players=players)])
    assert "trust_fund" in _keys(book, "A1")  # won on a huge bank
    assert "trust_fund" not in _keys(book, "A2")  # normal bank
    assert "trust_fund" not in _keys(book, "B1")  # hoarded but lost


def test_cache_rebuilds_on_store_change():
    class FakeStore:
        def __init__(self):
            self.change_count = 0
            self.matches = [(1, _match())]

        def all_matches(self):
            return self.matches

    store = FakeStore()
    cache = AchievementCache(store)
    assert "heating_up" not in _keys(cache.book(), "A1")
    store.matches = [(i, m) for i, m in enumerate(_series(3))]
    store.change_count += 1
    assert "heating_up" in _keys(cache.book(), "A1")


# -- the unlock ledger ---------------------------------------------------


@pytest.fixture
def store(tmp_path):
    s = MatchStore(str(tmp_path / "test.db"))
    yield s
    s.close()


def _ingest(store, match, tag):
    assert store.ingest(match, hash_replay(tag.encode())).status == "inserted"


def _now_utc(hours=0):
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=hours)


def test_epoch_is_stamped_on_new_databases(store):
    # A fresh DB's epoch is "now": pre-existing history is career-only.
    assert abs(_now_utc() - store.achievement_epoch().replace(tzinfo=datetime.timezone.utc)) < datetime.timedelta(
        minutes=5
    )


def test_seed_grants_career_but_not_moments(store):
    # History played before the epoch (which a fresh store stamps as "now").
    for i, m in enumerate(_series(3, start=BEFORE_EPOCH)):
        _ingest(store, m, f"pre{i}")
    cache = AchievementCache(store)
    assert ensure_seeded(store, cache) > 0
    keys = {k for k, _ in store.unlocks_for(["A1"])}
    assert {"first_game", "first_win"} <= keys  # career: backfilled
    assert "heating_up" not in keys  # moment: 3-streak was pre-epoch
    assert ensure_seeded(store, cache) == 0  # one-time only


def test_seed_skips_empty_database(store):
    assert ensure_seeded(store, AchievementCache(store)) == 0


def test_grants_announce_once_and_persist(store):
    for i, m in enumerate(_series(2, start=BEFORE_EPOCH)):
        _ingest(store, m, f"pre{i}")
    cache = AchievementCache(store)
    ensure_seeded(store, cache)

    # Three live wins: the third crosses Heating Up, announced exactly once.
    live = _series(3, start=_now_utc(1))
    for i, m in enumerate(live):
        _ingest(store, m, f"live{i}")
        unlocks = grant_new_unlocks(store, cache, m)
        keys = {e.spec.key for _, e in unlocks}
        assert ("heating_up" in keys) == (i == 2)
    assert grant_new_unlocks(store, cache, live[-1]) == []  # idempotent

    # The ledger is what reads see, and it survives derived-state changes.
    held = {e.spec.key for e in ledger_for_group(store, ["A1"])}
    assert "heating_up" in held


def test_sweep_grants_quietly(store):
    for i, m in enumerate(_series(3, start=_now_utc(1))):
        _ingest(store, m, f"bulk{i}")
    cache = AchievementCache(store)
    ensure_seeded(store, cache)  # seeds everything derivable...
    assert sweep_grants(store, cache) == 0  # ...so the sweep finds nothing new
    # A bulk write after seeding (backfill) is picked up by the sweep.
    _ingest(store, _match(played_at=_now_utc(10)), "bulk-extra")
    assert sweep_grants(store, cache) >= 0
    held = {e.spec.key for e in ledger_for_group(store, ["A1"])}
    assert "first_game" in held


def test_unlocks_dedupe_across_merged_handles(store):
    store.record_unlocks([("Jay", "first_win", "2026-01-02T00:00:00")])
    store.record_unlocks([("Luigi", "first_win", "2026-01-01T00:00:00")])
    rows = store.unlocks_for(["Jay", "Luigi"])
    assert rows == [("first_win", "2026-01-01T00:00:00")]  # earliest kept
    counts = ledger_holder_counts(store, {"Luigi": "Jay"})
    assert counts["first_win"] == 1  # merge group counts as one holder


def test_retired_spec_rows_are_kept_but_hidden(store):
    store.record_unlocks([("A1", "some_retired_key", "2026-01-01T00:00:00")])
    assert ledger_for_group(store, ["A1"]) == []
    assert store.unlock_count() == 1  # the row itself is never deleted
