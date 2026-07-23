"""Discord embed builders for matches, leaderboards, and stats."""

import discord
from models.matchmaking import ProposedMatch, QueuedPlayer
from models.rating import PlayerRating
from models.replay import MatchPlayer, MonobattleMatch
from services.achievements import RARITIES, RARITY_EMOJI, AchievementSpec, Earned, is_secret
from services.achievements import SPECS as ACHIEVEMENT_SPECS
from services.awards import SPECS, game_awards, match_awards, mvp_outkilled_team
from services.rating import MIN_RANKED_GAMES

ACCENT = 0x2ECC71
WARNING = 0xE67E22
ERROR = 0xE74C3C

_PICK_MODE_LABELS = {
    "blind_random": "Blind Random",
    "single_draft": "Single Draft",
    "tier_draft": "Tier Draft",
}


def _duration(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def _team_lines(match: MonobattleMatch, team: int, mvp: MatchPlayer | None = None) -> str:
    lines = []
    for p in match.team(team):
        pick = p.pick or "?"
        repick = ""
        if p.repick_used:
            was = f" (was {p.repick_from})" if p.repick_from and p.repick_from != p.pick else ""
            repick = f" ↻{was}"
        star = " ⭐" if p is mvp else ""
        lines.append(f"**{p.name}** — {pick}{repick}{star}")
    return "\n".join(lines) or "*empty*"


def _rating_change_lines(match: MonobattleMatch, deltas: dict[str, tuple[int, int]]) -> str:
    """One line per rated participant, biggest gain first: '📈 Name +23
    (1180 → 1203)'. Winners tend to the top, but a strong loss can still show
    a small gain, so we sort by the actual change."""
    rows = []
    for p in match.players:
        d = deltas.get(p.toon_handle)
        if d is None:
            continue
        before, after = d
        change = after - before
        arrow = "📈" if change > 0 else "📉" if change < 0 else "➖"
        sign = f"+{change}" if change > 0 else str(change)
        rows.append((change, f"{arrow} **{p.name}** {sign} ({before} → {after})"))
    rows.sort(key=lambda r: r[0], reverse=True)
    return "\n".join(line for _, line in rows)


def match_summary(
    match: MonobattleMatch,
    match_id: int | None = None,
    rating_deltas: dict[str, tuple[int, int]] | None = None,
) -> discord.Embed:
    if match.winning_team is not None and match.winner_confidence >= 1.0:
        color = ACCENT
    elif match.winning_team is not None:
        color = WARNING
    else:
        color = ERROR

    embed = discord.Embed(title=match.map_name, color=color)
    embed.description = (
        f"{match.game_type} · {_PICK_MODE_LABELS.get(match.pick_mode, match.pick_mode)}"
        f" · {_duration(match.duration_seconds)}"
        f" · <t:{int(match.played_at.timestamp())}:d>"
    )

    mvp = match.mvp()
    for team_number in sorted({p.team for p in match.players}):
        trophy = " 🏆" if match.winning_team == team_number else ""
        embed.add_field(
            name=f"Team {team_number}{trophy}",
            value=_team_lines(match, team_number, mvp),
            inline=True,
        )

    lines = []
    if mvp is not None:
        detail = f"{mvp.resources_killed:,} enemy value destroyed"
        if mvp_outkilled_team(match, mvp):
            detail += " — more than the rest of their team combined"
        lines.append(f"⭐ **MVP**: {mvp.name} ({detail})")
    lines += [f"{a.emoji} **{a.title}**: {a.player.name} ({a.detail})" for a in match_awards(match)]
    lines += [f"{a.emoji} **{a.title}**: {a.detail}" for a in game_awards(match)]
    if lines:
        embed.add_field(name="Awards", value="\n".join(lines), inline=False)

    if match.winning_team is None:
        result = "Unknown — needs confirmation"
    elif match.winner_method == "recorded":
        result = f"Team {match.winning_team} (recorded in replay)"
    elif match.winner_method == "confirmed":
        result = f"Team {match.winning_team} (manually confirmed)"
    else:
        result = f"Team {match.winning_team} (inferred, {match.winner_confidence:.0%} confidence)"
    embed.add_field(name="Result", value=result, inline=False)

    if rating_deltas:
        embed.add_field(name="Rating changes", value=_rating_change_lines(match, rating_deltas), inline=False)

    if match_id is not None:
        embed.set_footer(text=f"Match #{match_id}")
    return embed


def duplicate_notice(file_name: str) -> discord.Embed:
    return discord.Embed(
        title="Already recorded",
        description=f"`{file_name}` is already in the match database.",
        color=WARNING,
    )


def parse_failure(file_name: str, error: str) -> discord.Embed:
    embed = discord.Embed(
        title="Couldn't parse replay",
        description=f"`{file_name}`",
        color=ERROR,
    )
    embed.add_field(name="Error", value=error[:1000], inline=False)
    return embed


LEADERBOARD_PAGE_SIZE = 20


def leaderboard_page_count(ratings: list[PlayerRating]) -> int:
    return max(1, (len(ratings) + LEADERBOARD_PAGE_SIZE - 1) // LEADERBOARD_PAGE_SIZE)


def leaderboard(
    ratings: list[PlayerRating],
    page: int = 0,
    min_games: int = 1,
    display_names: dict[str, str] | None = None,
    hidden: int = 0,
) -> discord.Embed:
    """display_names maps handle -> shown name (the linked member's Discord
    name); unmapped handles fall back to the account's SC2 name."""
    display_names = display_names or {}
    pages = leaderboard_page_count(ratings)
    page = max(0, min(page, pages - 1))
    start = page * LEADERBOARD_PAGE_SIZE
    lines = []
    for i, r in enumerate(ratings[start : start + LEADERBOARD_PAGE_SIZE], start + 1):
        shown = display_names.get(r.handle, r.name)
        lines.append(
            f"`{i:>2}` **{shown}** — **{r.display_rating}** ({r.wins}-{r.losses}, {100 * r.wins / r.games:.0f}%)"
        )
    embed = discord.Embed(title="Monobattle Leaderboard", color=ACCENT)
    embed.description = "\n".join(lines) or "*No rated players yet.*"
    note = f"min {min_games} games · " if min_games > 1 else ""
    more = f" · {hidden} more below the minimum (!leaderboard 1 shows all)" if hidden else ""
    embed.set_footer(text=f"{note}Page {page + 1}/{pages}{more}")
    return embed


def _rating_value(rating: PlayerRating) -> str:
    return f"**{rating.display_rating}**" + ("  *(provisional)*" if rating.provisional else "")


def _rank_value(rating: PlayerRating, rank: int | None, total_ranked: int) -> str:
    if rank is None:
        return f"Unranked ({rating.games}/{MIN_RANKED_GAMES} games)"
    return f"#{rank} of {total_ranked}"


def _rating_footer(rating: PlayerRating) -> str:
    if rating.provisional:
        return "Provisional — rating will settle as more games come in."
    return "Rating rises with wins; how much depends on the opponents' strength."


def _record_lines(records: dict[str, list[int]], limit: int) -> str:
    rows = [(k, w, losses) for k, (w, losses) in records.items()]
    rows.sort(key=lambda r: r[1] + r[2], reverse=True)  # by games played
    lines = []
    for name, w, losses in rows[:limit]:
        total = w + losses
        lines.append(f"**{name}** — {w}-{losses} ({100 * w / total:.0f}%)")
    return "\n".join(lines) or "*none*"


def player_profile(
    rating: PlayerRating,
    rank: int | None,
    total_ranked: int,
    aliases: list[str],
    race_records: dict[str, list[int]],
    unit_records: dict[str, list[int]],
    mvp_count: int = 0,
    award_counts: dict[str, int] | None = None,
    display_name: str | None = None,
    achievements: list[Earned] | None = None,
) -> discord.Embed:
    shown = display_name or rating.name
    embed = discord.Embed(title=f"{shown} — profile", color=ACCENT)
    embed.add_field(name="Rating", value=_rating_value(rating), inline=True)
    embed.add_field(name="Rank", value=_rank_value(rating, rank, total_ranked), inline=True)
    record = f"{rating.wins}-{rating.losses} ({100 * rating.wins / rating.games:.0f}%)"
    if mvp_count:
        record += f" · ⭐ {mvp_count} MVP{'s' if mvp_count != 1 else ''}"
    embed.add_field(name="Record", value=record, inline=True)
    if award_counts:
        parts = [f"{spec.emoji} {spec.title} ×{award_counts[spec.key]}" for spec in SPECS if award_counts.get(spec.key)]
        if parts:
            embed.add_field(name="Awards", value="  ·  ".join(parts), inline=False)
    if achievements:
        # earned comes rarest-first; show the count and the three rarest.
        showcase = "  ·  ".join(f"{RARITY_EMOJI[e.spec.rarity]} {e.spec.name}" for e in achievements[:3])
        embed.add_field(
            name=f"Achievements ({len(achievements)}/{len(ACHIEVEMENT_SPECS)})",
            value=f"{showcase}\n*!achievements for the full list*",
            inline=False,
        )
    embed.add_field(name="Races", value=_record_lines(race_records, 3), inline=True)
    embed.add_field(name="Most-played units", value=_record_lines(unit_records, 10), inline=True)
    others = [a for a in aliases if a.lower() != shown.lower()]
    if others:
        embed.add_field(name="Plays as", value=", ".join(others[:12]), inline=False)
    embed.set_footer(text=f"{_rating_footer(rating)} · decided games only")
    return embed


def _missing_summary(missing: list[str], limit: int = 8) -> str:
    """Name what's left, capped so a player who is 30 units from Royal Flush
    gets a readable line instead of a wall (and the field stays under 1024)."""
    shown = ", ".join(missing[:limit])
    extra = len(missing) - limit
    return f"{shown} +{extra} more" if extra > 0 else shown


def achievements_gallery(
    shown_name: str,
    earned: list[Earned],
    next_up: list[tuple[AchievementSpec, float, float, list[str]]],
    holder_counts: dict[str, int] | None = None,
) -> discord.Embed:
    """A player's earned achievements grouped by rarity (rarest first), plus
    their closest locked ones. holder_counts (key -> players holding it) adds
    a live-rarity note to Epic+ lines."""
    holder_counts = holder_counts or {}
    embed = discord.Embed(title=f"{shown_name} — achievements", color=ACCENT)
    if not earned:
        embed.description = "*Nothing yet — go play some games!*"
    for rarity in reversed(RARITIES):
        lines = []
        for e in earned:
            if e.spec.rarity != rarity:
                continue
            # A profile is public, so a secret's how is never printed here
            # (the recipe lives only in the ephemeral catalog). The name is
            # fine — earning it means it's already community-discovered.
            body = "🔒 *secret*" if is_secret(e.spec) else e.spec.description
            line = f"{e.spec.emoji} **{e.spec.name}** — {body}"
            holders = holder_counts.get(e.spec.key)
            if holders and rarity in ("Epic", "Legendary"):
                line += f" *(held by {holders})*"
            lines.append(line)
        if not lines:
            continue
        # Discord caps a field value at 1024 chars; a well-stocked rarity
        # group can exceed it, so overflow continues in unnamed fields.
        chunks, current = [], ""
        for line in lines:
            if current and len(current) + 1 + len(line) > 1024:
                chunks.append(current)
                current = line
            else:
                current = f"{current}\n{line}" if current else line
        chunks.append(current)
        embed.add_field(name=f"{RARITY_EMOJI[rarity]} {rarity}", value=chunks[0], inline=False)
        for chunk in chunks[1:]:
            embed.add_field(name="​", value=chunk, inline=False)
    if next_up:
        lines = []
        for spec, current, target, missing in next_up:
            lines.append(f"{spec.emoji} **{spec.name}** — {spec.description} ({current:,.0f}/{target:,.0f})")
            if missing:
                lines.append(f"　↳ *still need:* {_missing_summary(missing)}")
        embed.add_field(name="🔒 Next up", value="\n".join(lines), inline=False)
    embed.set_footer(text=f"{len(earned)}/{len(ACHIEVEMENT_SPECS)} unlocked · run /gallery to browse them all")
    return embed


def achievement_catalog(
    rarity: str,
    earned_keys: set[str],
    discovered_keys: set[str],
    holder_counts: dict[str, int] | None = None,
    private: bool = False,
) -> discord.Embed:
    """One rarity page of the full catalogue, for browsing everything that
    exists. Two visibility gates for secrets:
    - name: hidden as 🔒 ??? until the community has discovered it (anyone
      earned it); the rarity tier still shows (it's the page).
    - recipe: shown only when the VIEWER has earned it AND this is a private
      (ephemeral) render — a public !catalog masks it so a channel message
      never leaks the how."""
    holder_counts = holder_counts or {}
    # Secrets sink to the bottom of their rarity page (mystery slots last).
    specs = sorted((s for s in ACHIEVEMENT_SPECS if s.rarity == rarity), key=is_secret)
    lines = []
    for s in specs:
        earned = s.key in earned_keys
        mark = "✅" if earned else "▫️"
        if is_secret(s) and s.key not in discovered_keys:
            lines.append("🔒 **???** — *undiscovered secret*")
            continue
        if is_secret(s) and not (earned and private):
            body = "🔒 *secret*"  # name shown, how hidden
        else:
            body = s.description
        line = f"{mark} {s.emoji} **{s.name}** — {body}"
        holders = holder_counts.get(s.key)
        if holders and rarity in ("Epic", "Legendary"):
            line += f" *(held by {holders})*"
        lines.append(line)

    embed = discord.Embed(title=f"{RARITY_EMOJI[rarity]} {rarity} achievements", color=ACCENT)
    # Field values cap at 1024 chars, so a big rarity tier spills into extra
    # unnamed fields (same approach as the profile gallery).
    chunks, current = [], ""
    for line in lines:
        if current and len(current) + 1 + len(line) > 1024:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    chunks.append(current)
    embed.add_field(name="​", value=chunks[0], inline=False)
    for chunk in chunks[1:]:
        embed.add_field(name="​", value=chunk, inline=False)
    earned_here = sum(1 for s in specs if s.key in earned_keys)
    footer = f"{earned_here}/{len(specs)} {rarity} unlocked"
    if not private:
        footer += " · run /gallery (slash) to reveal recipes for secrets you've earned"
    embed.set_footer(text=footer)
    return embed


def achievement_unlocks(
    unlocks: list[tuple[str, Earned]], first_discovery_keys: frozenset[str] = frozenset()
) -> discord.Embed:
    """Announcement for achievements a just-ingested game unlocked, as
    (player display name, earned) pairs, rarest first. A lobby full of new
    players can unlock dozens at once, so the list is truncated to stay under
    the 4096-char embed description limit — the rarest survive the cut.

    Secrets show only their NAME here, never the how — the recipe would spoil
    it for the whole channel. `first_discovery_keys` are secrets nobody had
    earned before this game, so the finder gets a discovery credit."""
    lines, dropped, any_secret = [], 0, False
    budget = 3900  # headroom under the 4096 limit for the overflow line
    for name, e in unlocks:
        prefix = f"{RARITY_EMOJI[e.spec.rarity]} {e.spec.emoji} **{name}**"
        if is_secret(e.spec):
            any_secret = True
            if e.spec.key in first_discovery_keys:
                line = f"{prefix} is the first to discover **{e.spec.name}**! 🌟"
            else:
                line = f"{prefix} unlocked **{e.spec.name}** ✨ *(secret!)*"
        else:
            line = f"{prefix} unlocked **{e.spec.name}** — {e.spec.description.lower()}"
        if budget - len(line) - 1 < 0:
            dropped += 1
            continue
        budget -= len(line) + 1
        lines.append(line)
    if dropped:
        lines.append(f"…and {dropped} more — check your `!achievements`")
    embed = discord.Embed(title="🏅 Achievement unlocked!", description="\n".join(lines), color=ACCENT)
    if any_secret:
        embed.set_footer(text="✨ secret unlocked — run /gallery to see what you did")
    return embed


def h2h_summary(
    name1: str,
    name2: str,
    vs: list[int],
    together: list[int],
    opposed: list[tuple[int, MonobattleMatch]],
    group1: list[str],
    group2: list[str],
) -> discord.Embed:
    embed = discord.Embed(title=f"{name1} vs {name2}", color=ACCENT)
    total_vs = vs[0] + vs[1]
    if total_vs:
        embed.add_field(
            name="Head-to-head",
            value=f"**{name1}** {vs[0]} – {vs[1]} **{name2}** ({100 * vs[0] / total_vs:.0f}% for {name1})",
            inline=False,
        )
    else:
        embed.add_field(name="Head-to-head", value="No decided games on opposite teams yet.", inline=False)
    total_team = together[0] + together[1]
    if total_team:
        embed.add_field(
            name="As teammates",
            value=f"{together[0]}-{together[1]} together ({100 * together[0] / total_team:.0f}%)",
            inline=False,
        )
    g1, g2 = set(group1), set(group2)
    lines = []
    for match_id, match in opposed[-5:]:
        p1 = next(p for p in match.players if p.toon_handle in g1)
        p2 = next(p for p in match.players if p.toon_handle in g2)
        winner = name1 if p1.team == match.winning_team else name2
        lines.append(
            f"<t:{int(match.played_at.timestamp())}:d> — **{winner}** won"
            f" · {p1.pick or '?'} vs {p2.pick or '?'} · #{match_id}"
        )
    if lines:
        embed.add_field(name="Recent meetings", value="\n".join(reversed(lines)), inline=False)
    embed.set_footer(text="decided games only")
    return embed


def queue_status(players: list[QueuedPlayer], target: int) -> discord.Embed:
    embed = discord.Embed(title="Matchmaking Queue", color=ACCENT)
    if players:
        lines = []
        for p in players:
            tag = "" if p.rated else " *(unrated)*"
            lines.append(f"• {p.display_name}{tag}")
        embed.description = "\n".join(lines)
    else:
        embed.description = "*Queue is empty. Click **Join** to get in.*"
    embed.set_footer(text=f"{len(players)}/{target} — teams form automatically when full")
    return embed


def _team_field(name: str, team: list[QueuedPlayer]) -> tuple[str, str]:
    lines = "\n".join(f"• {p.display_name}" for p in team)
    return name, lines or "*empty*"


def proposed_match(match: ProposedMatch, option_index: int = 0, option_count: int = 1) -> discord.Embed:
    fav = match.team1_win_probability
    description = f"Balance: **{match.fairness:.0%}** (Team 1 win chance ≈ {fav:.0%})"
    if option_count > 1:
        description += f"\nOption {option_index + 1} of {option_count} — 🔀 Shuffle for another split."
    embed = discord.Embed(title="Match found!", description=description, color=ACCENT)
    n1, v1 = _team_field("Team 1", match.team1)
    n2, v2 = _team_field("Team 2", match.team2)
    embed.add_field(name=n1, value=v1, inline=True)
    embed.add_field(name=n2, value=v2, inline=True)
    if any(not p.rated for p in match.team1 + match.team2):
        embed.set_footer(text="Unrated players were balanced at a default skill estimate.")
    return embed


def unit_stats(records: dict[str, list[int]], min_games: int = 10) -> discord.Embed:
    rows = [(pick, wins, losses) for pick, (wins, losses) in records.items() if wins + losses >= min_games]
    rows.sort(key=lambda r: r[1] / (r[1] + r[2]), reverse=True)
    lines = [f"{'Unit':<14} {'W':>4} {'L':>4}  {'Win%':>5}"]
    for pick, wins, losses in rows:
        lines.append(f"{pick:<14} {wins:>4} {losses:>4}  {100 * wins / (wins + losses):>4.1f}%")
    embed = discord.Embed(title="Unit Win Rates", color=ACCENT)
    embed.description = "```\n" + "\n".join(lines) + "\n```"
    suffix = f" · min {min_games} games per unit" if min_games > 1 else ""
    embed.set_footer(text=f"decided matches only{suffix}")
    return embed
