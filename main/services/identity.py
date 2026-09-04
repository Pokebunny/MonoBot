"""Resolving a typed name to a PERSON.

Every command that takes a player name — `!rank`, `!profile`, `!h2h`,
`!achievements`, `!add` — asks this module, so they all agree on who "rudy"
means. What they get back is a person: a whole account-merge group, the
Discord user it belongs to, and how the name matched.

Names are not identities. An SC2 account can rename freely, two accounts can
share a display name, and one person can own several accounts. Matching on
`match_players.name` alone therefore answers the wrong question — it finds
every account that has EVER been called this, which is how searching "ARudy"
used to return Avery's stats (Avery renamed an account to "Arudy" for 73 games
in 2025, more than ARudy himself had played under that exact spelling).

The fix is a precedence order, strongest evidence first:

1. CLAIM   — someone linked this name to their Discord account. A human said
             "this is me", which beats anything inferred from history.
2. CURRENT — an account is called this NOW, in its most recent game. Renaming
             away from a name gives up your hold on it; renaming into one
             takes it.
3. FORMER  — an account used to be called this. Last resort, so an abandoned
             name still finds its old owner when nothing better matches.

Within a tier, more games wins. A tie INSIDE the best tier is genuine
ambiguity — two live accounts really do share the name — and callers should
ask rather than guess (see `ambiguous`).
"""

from dataclasses import dataclass

CLAIM = "claim"
CURRENT = "current"
FORMER = "former"

# Lower sorts first.
_TIER = {CLAIM: 0, CURRENT: 1, FORMER: 2}

_WHY = {
    CLAIM: "linked to their Discord account",
    CURRENT: "the account's current in-game name",
    FORMER: "a name this account used to play under",
}


@dataclass(frozen=True)
class Person:
    """One player: their whole merge group, not a single account."""

    handles: tuple[str, ...]  # every SC2 account of theirs, may be empty
    discord_id: str | None
    sc2_name: str  # most recent display name across the group
    games: int  # across the whole group
    via: str  # CLAIM | CURRENT | FORMER

    @property
    def why(self) -> str:
        return _WHY[self.via]


def resolve(store, query: str) -> list[Person]:
    """People matching a typed name, best match first. Empty if nothing
    matches. Several results mean the name is shared; check `ambiguous` before
    assuming the first one is right."""
    query = (query or "").strip()
    if not query:
        return []

    found: dict[tuple, tuple[str, Person]] = {}

    def offer(handle: str, via: str) -> None:
        group = tuple(store.merged_handles(handle) or [handle])
        key = tuple(sorted(group))
        if key in found and _TIER[found[key][0]] <= _TIER[via]:
            return  # already matched on stronger evidence
        found[key] = (via, _person(store, list(group), via))

    discord_id = store.discord_id_for(query)
    if discord_id is not None:
        # A claim only outranks history once it is BOUND to an account. An
        # unbound claim (the name was ambiguous when they linked, or they have
        # never played) names a Discord user but no games, so it must not
        # displace an account that is actually called this today.
        handles = store.handles_for(discord_id)
        if handles:
            offer(handles[0], CLAIM)  # any handle expands to the whole group
    for handle in store.handles_by_current_name(query):
        offer(handle, CURRENT)
    for handle in store.handles_for_name(query):
        offer(handle, FORMER)
    if not found and discord_id is not None:
        # Linked but never seen in a game: still a person, and still the right
        # answer for the queue, which can add them before their first match.
        found[("discord", discord_id)] = (CLAIM, Person((), discord_id, query, 0, CLAIM))

    people = [person for _, person in found.values()]
    return sorted(people, key=lambda p: (_TIER[p.via], -p.games))


def _person(store, group: list[str], via: str) -> Person:
    aliases = store.aliases_for_handles(group)
    discord_id = next((d for d in (store.discord_id_for_handle(h) for h in group) if d), None)
    return Person(
        handles=tuple(group),
        discord_id=discord_id,
        sc2_name=aliases[0] if aliases else group[0],
        games=store.game_count_for_handles(group),
        via=via,
    )


def ambiguous(people: list[Person]) -> bool:
    """True when the name is genuinely shared: two or more people match on the
    SAME strength of evidence. A weaker match behind a stronger one is not
    ambiguity — a claimed name beats an account that merely used to have it,
    and asking the caller to choose there would be noise."""
    return len(people) > 1 and people[0].via == people[1].via


def others_note(people: list[Person]) -> str:
    """A line for the weaker matches, so a player whose old name someone else
    now uses can still see where their stats went. Empty when nothing else
    matched."""
    rest = [p for p in people[1:]]
    if not rest:
        return ""
    names = ", ".join(f"**{p.sc2_name}**" for p in rest[:3])
    tail = f" (+{len(rest) - 3} more)" if len(rest) > 3 else ""
    return f"*Also played under this name: {names}{tail}.*"
