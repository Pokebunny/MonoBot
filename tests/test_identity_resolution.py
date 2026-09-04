"""Who a typed name means.

The precedence is claim > current name > former name (see services.identity).
These cases are drawn from real collisions in the community database.
"""

import datetime

import pytest
from models.replay import MatchPlayer, MonobattleMatch
from services import identity
from services.storage import MatchStore, hash_replay

BASE = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


@pytest.fixture
def store(tmp_path):
    s = MatchStore(str(tmp_path / "t.db"))
    yield s
    s.close()


def _match(roster, at, winning_team=1):
    """roster: list of (display name, handle) for team 1 then team 2."""
    players = [
        MatchPlayer(
            name=name,
            toon_handle=handle,
            team=(1 if i < len(roster) // 2 else 2),
            race="Zerg",
            pick="Zergling",
            unit_counts={"Zergling": 10},
        )
        for i, (name, handle) in enumerate(roster)
    ]
    return MonobattleMatch(
        file_name=f"{at.isoformat()}.SC2Replay",
        map_name="Monobattle LotV - Map Rotation",
        played_at=at,
        duration_seconds=900,
        game_type="3v3",
        pick_mode="blind_random",
        pick_phase_seconds=60,
        players=players,
        winning_team=winning_team,
        winner_confidence=1.0,
        winner_method="recorded",
    )


def _play(store, roster, days):
    at = BASE + datetime.timedelta(days=days)
    store.ingest(_match(roster, at), hash_replay(str(at).encode()))


def _filler(n):
    return [(f"F{i}", f"h-f{i}") for i in range(n)]


# -- the ARudy case ------------------------------------------------------


@pytest.fixture
def rudy_and_avery(store):
    """The real collision: Avery renamed an account to "Arudy" for a stretch in
    2025 and played MORE games under that spelling than ARudy himself, so a
    plain name search handed Avery's stats to anyone looking for ARudy."""
    for day in range(6):  # Avery, as "Arudy", plays a lot
        _play(store, [("Arudy", "h-avery"), *_filler(5)], day)
    for day in range(6, 8):  # ARudy plays less, but is still called that
        _play(store, [("ARudy", "h-rudy"), *_filler(5)], day)
    _play(store, [("AveryT", "h-avery"), *_filler(5)], 9)  # Avery renames back
    return store


def test_former_name_loses_to_the_account_still_called_that(rudy_and_avery):
    people = identity.resolve(rudy_and_avery, "ARudy")
    assert people[0].handles == ("h-rudy",)
    assert people[0].via == identity.CURRENT
    # Avery still matches, but as a name he abandoned -- and with MORE games,
    # so game count alone would have picked him.
    assert people[1].handles == ("h-avery",)
    assert people[1].via == identity.FORMER
    assert people[1].games > people[0].games


def test_a_claim_beats_everything(rudy_and_avery):
    # Even if Avery were still using the name, a bound link settles it.
    rudy_and_avery.bind_specific("discord-rudy", "ARudy", "h-rudy")
    people = identity.resolve(rudy_and_avery, "ARudy")
    assert people[0].handles == ("h-rudy",)
    assert people[0].via == identity.CLAIM
    assert people[0].discord_id == "discord-rudy"


def test_a_weaker_match_is_not_ambiguity(rudy_and_avery):
    # Two people match "ARudy", but not equally -- nothing to ask about.
    assert not identity.ambiguous(identity.resolve(rudy_and_avery, "ARudy"))


def test_the_others_are_still_named(rudy_and_avery):
    note = identity.others_note(identity.resolve(rudy_and_avery, "ARudy"))
    assert "AveryT" in note


def test_lookup_is_case_insensitive(rudy_and_avery):
    assert identity.resolve(rudy_and_avery, "arudy")[0].handles == ("h-rudy",)


def test_an_abandoned_name_still_finds_its_owner(rudy_and_avery):
    # Nobody is called AveryT's old name now, so the former match is all there
    # is -- and it should still work.
    people = identity.resolve(rudy_and_avery, "AveryT")
    assert people[0].handles == ("h-avery",)


# -- genuine ambiguity ---------------------------------------------------


def test_two_live_accounts_sharing_a_name_are_ambiguous(store):
    for day in range(3):
        _play(store, [("Twin", "h-one"), *_filler(5)], day)
    for day in range(3, 5):
        _play(store, [("Twin", "h-two"), *_filler(5)], day)
    people = identity.resolve(store, "Twin")
    assert len(people) == 2
    assert identity.ambiguous(people)
    assert people[0].games > people[1].games  # best guess first, but ask


def test_merged_accounts_are_one_person(store):
    _play(store, [("Jay", "h-jay"), *_filler(5)], 0)
    _play(store, [("Luigi", "h-luigi"), *_filler(5)], 1)
    store.link_player("discord-jay", "Jay")
    store.add_account("discord-jay", "h-luigi")
    people = identity.resolve(store, "Luigi")
    assert len(people) == 1  # not two rows for one person
    assert set(people[0].handles) == {"h-jay", "h-luigi"}
    assert people[0].games == 2
    assert not identity.ambiguous(people)


def test_a_linked_player_who_has_never_played_still_resolves(store):
    # Needed by the queue: they can be added before their first game.
    store.link_player("discord-new", "Rookie")
    people = identity.resolve(store, "Rookie")
    assert people[0].discord_id == "discord-new"
    assert people[0].handles == ()
    assert people[0].games == 0


def test_unknown_name_resolves_to_nothing(store):
    assert identity.resolve(store, "nobody") == []
    assert identity.resolve(store, "") == []
    assert identity.resolve(store, "   ") == []


def test_an_unbound_claim_does_not_displace_a_live_account(store):
    # Linking a shared name leaves the claim unbound (it can't tell which
    # account you meant). That must not hand the name to the claimant when a
    # real account still answers to it.
    for day in range(3):
        _play(store, [("Twin", "h-one"), *_filler(5)], day)
    for day in range(3, 5):
        _play(store, [("Twin", "h-two"), *_filler(5)], day)
    assert store.link_player("discord-hopeful", "Twin").status == "ambiguous"
    people = identity.resolve(store, "Twin")
    assert [p.handles for p in people] == [("h-one",), ("h-two",)]
    assert all(p.via == identity.CURRENT for p in people)
