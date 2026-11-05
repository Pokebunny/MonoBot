"""Resolving a typed SC2 name to a person (a whole merge group).

Names aren't identities — accounts rename and share names — so matches rank by
evidence: CLAIM (linked to a Discord account) > CURRENT (the account's latest
name) > FORMER (a name it used to have). Discord members (`for_discord`) are
tried only after all three. Within a tier more games wins; a tie in the best
tier is genuine ambiguity, so callers ask (`ambiguous`).
"""

from dataclasses import dataclass

CLAIM = "claim"
CURRENT = "current"
FORMER = "former"
DISCORD = "discord"

# Lower sorts first.
_TIER = {CLAIM: 0, CURRENT: 1, FORMER: 2, DISCORD: 3}

_WHY = {
    CLAIM: "linked to their Discord account",
    CURRENT: "the account's current in-game name",
    FORMER: "a name this account used to play under",
    DISCORD: "their Discord name",
}


@dataclass(frozen=True)
class Person:
    """One player: their whole merge group, not a single account."""

    handles: tuple[str, ...]  # every SC2 account of theirs, may be empty
    discord_id: str | None
    sc2_name: str  # most recent display name across the group
    games: int  # across the whole group
    via: str  # CLAIM | CURRENT | FORMER | DISCORD
    discord_name: str | None = None  # set when found by Discord name

    @property
    def why(self) -> str:
        return _WHY[self.via]


def clean_name(text: str | None) -> str:
    """Strip angle brackets people copy from `!link <your SC2 name>`."""
    return (text or "").replace("<", "").replace(">", "").strip()


def resolve(store, query: str) -> list[Person]:
    """People matching a name, best first; check `ambiguous` before taking [0]."""
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
        # Only a claim bound to an account outranks history.
        handles = store.handles_for(discord_id)
        if handles:
            offer(handles[0], CLAIM)  # any handle expands to the whole group
    for handle in store.handles_by_current_name(query):
        offer(handle, CURRENT)
    for handle in store.handles_for_name(query):
        offer(handle, FORMER)
    if not found and discord_id is not None:
        # Linked but never played: the queue can still add them.
        found[("discord", discord_id)] = (CLAIM, Person((), discord_id, query, 0, CLAIM))

    people = [person for _, person in found.values()]
    return sorted(people, key=lambda p: (_TIER[p.via], -p.games))


def for_discord(store, discord_id: str, discord_name: str) -> Person:
    """The person behind a Discord member, with no accounts if never linked."""
    handles = store.handles_for(discord_id)
    if not handles:
        return Person((), discord_id, discord_name, 0, DISCORD, discord_name)
    group = store.merged_handles(handles[0]) or handles
    person = _person(store, list(group), DISCORD)
    return Person(person.handles, discord_id, person.sc2_name, person.games, DISCORD, discord_name)


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
    """Two or more people match on the same tier."""
    return len(people) > 1 and people[0].via == people[1].via


def others_note(people: list[Person]) -> str:
    """A line naming the weaker matches, or empty."""
    rest = [p for p in people[1:]]
    if not rest:
        return ""
    names = ", ".join(f"**{p.sc2_name}**" for p in rest[:3])
    tail = f" (+{len(rest) - 3} more)" if len(rest) > 3 else ""
    return f"*Also played under this name: {names}{tail}.*"
