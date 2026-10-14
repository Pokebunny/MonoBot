import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "main"))


def community_cog(cog_cls, store, **client_attrs):
    """A community cog bound to one community around `store`, as a command
    would see it. Commands rebinding through ctx find the same community."""
    from types import SimpleNamespace

    from community import Community
    from models.config import GuildConfig

    community = Community(None, GuildConfig(), store)
    client = SimpleNamespace(communities=SimpleNamespace(for_guild=lambda _guild: community), **client_attrs)
    return cog_cls(client).at(community)
