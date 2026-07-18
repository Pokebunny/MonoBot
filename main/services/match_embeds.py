"""Discord embed builders for matches, leaderboards, and stats."""

import discord
from models.matchmaking import ProposedMatch, QueuedPlayer
from models.rating import PlayerRating
from models.replay import MatchPlayer, MonobattleMatch
from services.awards import SPECS, game_awards, match_awards, mvp_outkilled_team

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


def match_summary(match: MonobattleMatch, match_id: int | None = None) -> discord.Embed:
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


def leaderboard(ratings: list[PlayerRating], page: int = 0, min_games: int = 1) -> discord.Embed:
    pages = leaderboard_page_count(ratings)
    page = max(0, min(page, pages - 1))
    start = page * LEADERBOARD_PAGE_SIZE
    lines = []
    for i, r in enumerate(ratings[start : start + LEADERBOARD_PAGE_SIZE], start + 1):
        prov = " ·" if r.provisional else ""
        lines.append(
            f"`{i:>2}` **{r.name}** — **{r.display_rating}**{prov} ({r.wins}-{r.losses}, {100 * r.wins / r.games:.0f}%)"
        )
    embed = discord.Embed(title="Monobattle Leaderboard", color=ACCENT)
    embed.description = "\n".join(lines) or "*No rated players yet.*"
    note = f"min {min_games} games · " if min_games > 1 else ""
    tail = "  ·  = still provisional (few games)"
    embed.set_footer(text=f"{note}Page {page + 1}/{pages} · higher rating = stronger{tail}")
    return embed


def _rating_value(rating: PlayerRating) -> str:
    return f"**{rating.display_rating}**" + ("  *(provisional)*" if rating.provisional else "")


def _rating_footer(rating: PlayerRating) -> str:
    if rating.provisional:
        return "Provisional — rating will settle as more games come in."
    return "Rating rises with wins; how much depends on the opponents' strength."


def player_rank(rating: PlayerRating, rank: int, total_ranked: int, aliases: list[str] | None = None) -> discord.Embed:
    embed = discord.Embed(title=rating.name, color=ACCENT)
    embed.add_field(name="Rating", value=_rating_value(rating), inline=True)
    embed.add_field(name="Rank", value=f"#{rank} of {total_ranked}", inline=True)
    embed.add_field(
        name="Record",
        value=f"{rating.wins}-{rating.losses} ({100 * rating.wins / rating.games:.0f}%)",
        inline=True,
    )
    others = [a for a in (aliases or []) if a.lower() != rating.name.lower()]
    if others:
        embed.add_field(name="Also played as", value=", ".join(others[:12]), inline=False)
    embed.set_footer(text=_rating_footer(rating))
    return embed


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
    rank: int,
    total_ranked: int,
    aliases: list[str],
    race_records: dict[str, list[int]],
    unit_records: dict[str, list[int]],
    mvp_count: int = 0,
    award_counts: dict[str, int] | None = None,
) -> discord.Embed:
    embed = discord.Embed(title=f"{rating.name} — profile", color=ACCENT)
    embed.add_field(name="Rating", value=_rating_value(rating), inline=True)
    embed.add_field(name="Rank", value=f"#{rank} of {total_ranked}", inline=True)
    record = f"{rating.wins}-{rating.losses} ({100 * rating.wins / rating.games:.0f}%)"
    if mvp_count:
        record += f" · ⭐ {mvp_count} MVP{'s' if mvp_count != 1 else ''}"
    embed.add_field(name="Record", value=record, inline=True)
    if award_counts:
        parts = [f"{spec.emoji} {spec.title} ×{award_counts[spec.key]}" for spec in SPECS if award_counts.get(spec.key)]
        if parts:
            embed.add_field(name="Awards", value="  ·  ".join(parts), inline=False)
    embed.add_field(name="Races", value=_record_lines(race_records, 3), inline=True)
    embed.add_field(name="Most-played units", value=_record_lines(unit_records, 10), inline=True)
    others = [a for a in aliases if a.lower() != rating.name.lower()]
    if others:
        embed.add_field(name="Also played as", value=", ".join(others[:12]), inline=False)
    embed.set_footer(text=f"{_rating_footer(rating)} · decided games only")
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


def proposed_match(match: ProposedMatch) -> discord.Embed:
    fav = match.team1_win_probability
    embed = discord.Embed(
        title="Match found!",
        description=f"Balance: **{match.fairness:.0%}** (Team 1 win chance ≈ {fav:.0%})",
        color=ACCENT,
    )
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
