"""!deletematch: an admin removes a stored game, and badges it earned go too."""

from types import SimpleNamespace

from cogs.replays import Replays
from community import Community
from conftest import make_match
from models.config import GuildConfig
from services import achievements


def test_deleting_a_match_revokes_what_it_earned(store):
    community = Community(None, GuildConfig(), store)
    client = SimpleNamespace(communities=SimpleNamespace(for_guild=lambda _g: community, all=lambda: [community]))
    cog = Replays(client).at(community)
    match_id = store.ingest(make_match(), "abc").match_id
    granted, _ = achievements.reconcile(store, cog.achievements)
    assert granted

    sweep = cog.remove_match(match_id, SimpleNamespace(id=1))

    assert store.match_count() == 0
    assert sweep is not None and sweep.fields  # the losses get announced
    assert achievements.reconcile(store, cog.achievements) == ([], [])
