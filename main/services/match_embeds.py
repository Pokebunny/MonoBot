"""Discord embed builders for matches, leaderboards, and stats."""

import discord
from models.matchmaking import ProposedMatch, QueuedPlayer
from models.rating import PlayerRating
from models.replay import MonobattleMatch

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


def _team_lines(match: MonobattleMatch, team: int) -> str:
    lines = []
    for p in match.team(team):
        pick = p.pick or "?"
        repick = " ↻" if p.repick_used else ""
        lines.append(f"**{p.name}** — {pick}{repick}")
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

    for team_number in sorted({p.team for p in match.players}):
        trophy = " 🏆" if match.winning_team == team_number else ""
        embed.add_field(
            name=f"Team {team_number}{trophy}",
            value=_team_lines(match, team_number),
            inline=True,
        )

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


def leaderboard(ratings: list[PlayerRating], min_games: int, limit: int = 20) -> discord.Embed:
    lines = []
    for i, r in enumerate(ratings[:limit], 1):
        lines.append(f"`{i:>2}` **{r.name}** — {r.ordinal:.1f} ({r.wins}W-{r.losses}L, {100 * r.wins / r.games:.0f}%)")
    embed = discord.Embed(title="Monobattle Leaderboard", color=ACCENT)
    embed.description = "\n".join(lines) or "*No rated players yet.*"
    prefix = f"min {min_games} games · " if min_games > 1 else ""
    embed.set_footer(text=f"{prefix}rating = conservative skill estimate (μ−3σ)")
    return embed


def player_rank(
    rating: PlayerRating, rank: int, total_ranked: int, aliases: list[str] | None = None
) -> discord.Embed:
    embed = discord.Embed(title=rating.name, color=ACCENT)
    embed.add_field(name="Rating", value=f"{rating.ordinal:.1f}", inline=True)
    embed.add_field(name="Rank", value=f"#{rank} of {total_ranked}", inline=True)
    embed.add_field(
        name="Record",
        value=f"{rating.wins}W-{rating.losses}L ({100 * rating.wins / rating.games:.0f}%)",
        inline=True,
    )
    others = [a for a in (aliases or []) if a.lower() != rating.name.lower()]
    if others:
        embed.add_field(name="Also played as", value=", ".join(others[:12]), inline=False)
    embed.set_footer(text=f"μ={rating.mu:.1f} σ={rating.sigma:.1f}")
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
