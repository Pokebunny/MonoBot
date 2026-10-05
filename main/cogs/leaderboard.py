"""Ratings and stats commands, derived from the stored match history."""

import logging

import discord
from checks import is_bot_admin
from discord.ext import commands
from services import achievements, identity, map_versions, match_embeds
from services.achievements import AchievementCache
from services.rating import (
    MIN_DURATION_SECONDS,
    MIN_RANKED_GAMES,
    MIN_WINNER_CONFIDENCE,
    SOFT_RESET_CARRYOVER,
    SOFT_RESET_SIGMA,
    DuoCache,
    RatingCache,
    UnitCache,
    season_book,
    unit_baseline,
)
from services.storage import MatchStore
from views import ExpiringView, PagedBoardView, person_or_pick

logger = logging.getLogger(__name__)

# Default minimum games to appear on the board: trims one-off historical
# accounts now that the ladder is in real use. !leaderboard 1 shows everyone.
DEFAULT_MIN_GAMES = MIN_RANKED_GAMES

# MVP rate needs a much higher floor than the rating board. A player with six
# games and two MVPs sits at 33% on nothing but variance, so a low floor makes
# the board a list of small samples. !mvprate <n> overrides it.
MVP_RATE_MIN_GAMES = 50

# Games a pair must have played together to make the duo board. Pairs are far
# noisier than players — most of a duo's record is the other two teammates —
# so the floor is high; !duos <n> overrides it.
DUO_MIN_GAMES = 15

# Default minimum games for a unit to make the unit board. Most picks clear
# it easily; it only trims picks too rare to have moved off the prior.
UNIT_MIN_GAMES = 10

# Words that turn !leaderboard into the unit board, and the words on it that
# sort by raw win rate instead of rating.
_UNIT_WORDS = {"unit", "units", "pick", "picks"}
_UNIT_RAW_WORDS = {"raw", "winrate", "wins", "rate", "record"}

# Words that turn !leaderboard into one race's board.
_RACES = {
    **dict.fromkeys(("protoss", "toss", "p"), "Protoss"),
    **dict.fromkeys(("terran", "t"), "Terran"),
    **dict.fromkeys(("zerg", "z"), "Zerg"),
}

# Words that pick a !duos sort. Rating is the default, so its own words only
# exist so nobody has to remember which way round it is.
_DUO_SORTS = {
    **dict.fromkeys(("rating", "rated", "strength", "best"), "rating"),
    **dict.fromkeys(("raw", "winrate", "wins", "rate", "record"), "raw"),
    **dict.fromkeys(("synergy", "chemistry", "expected", "adjusted"), "synergy"),
}


class CatalogView(ExpiringView):
    """◀ ▶ pagination over the full achievement catalogue, one rarity per
    page. `private` (an ephemeral slash invocation) reveals the recipes of
    secrets the viewer has earned; a public !catalog keeps them masked so a
    channel message never leaks the how."""

    def __init__(self, earned_keys: set[str], discovered_keys: set[str], holder_counts: dict[str, int], private: bool):
        super().__init__()
        self.earned_keys = earned_keys
        self.discovered_keys = discovered_keys
        self.holder_counts = holder_counts
        self.private = private
        self.page = 0
        self.pages = len(achievements.RARITIES)
        self._sync()

    def _sync(self):
        at_start = self.page <= 0
        at_end = self.page >= self.pages - 1
        self.first.disabled = self.prev.disabled = at_start
        self.next.disabled = self.last.disabled = at_end

    def embed(self) -> discord.Embed:
        return match_embeds.achievement_catalog(
            achievements.RARITIES[self.page], self.earned_keys, self.discovered_keys, self.holder_counts, self.private
        )

    async def _show(self, interaction: discord.Interaction):
        self._sync()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(emoji="⏮", style=discord.ButtonStyle.secondary)
    async def first(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = 0
        await self._show(interaction)

    @discord.ui.button(emoji="◀", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        await self._show(interaction)

    @discord.ui.button(emoji="▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.pages - 1, self.page + 1)
        await self._show(interaction)

    @discord.ui.button(emoji="⏭", style=discord.ButtonStyle.secondary)
    async def last(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = self.pages - 1
        await self._show(interaction)


class MatchBrowserView(ExpiringView):
    """⏮ ◀ ▶ ⏭ browsing over a snapshot of match history (oldest→newest);
    opens on the newest game, ◀ steps back in time."""

    def __init__(self, matches, map_names: dict[str, str] | None = None):
        super().__init__()
        self.matches = matches
        self.map_names = map_names or {}
        self.index = len(matches) - 1
        self._sync()

    def embed(self) -> discord.Embed:
        match_id, match = self.matches[self.index]
        embed = match_embeds.match_summary(match, match_id, map_label=map_versions.label(match, self.map_names))
        embed.set_footer(text=f"Match #{match_id} · {self.index + 1}/{len(self.matches)}")
        return embed

    def _sync(self):
        at_oldest = self.index <= 0
        at_newest = self.index >= len(self.matches) - 1
        self.oldest.disabled = self.older.disabled = at_oldest
        self.newer.disabled = self.newest.disabled = at_newest

    async def _show(self, interaction: discord.Interaction):
        self._sync()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(emoji="⏮", style=discord.ButtonStyle.secondary)
    async def oldest(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = 0
        await self._show(interaction)

    @discord.ui.button(emoji="◀", style=discord.ButtonStyle.secondary)
    async def older(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = max(0, self.index - 1)
        await self._show(interaction)

    @discord.ui.button(emoji="▶", style=discord.ButtonStyle.secondary)
    async def newer(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = min(len(self.matches) - 1, self.index + 1)
        await self._show(interaction)

    @discord.ui.button(emoji="⏭", style=discord.ButtonStyle.secondary)
    async def newest(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = len(self.matches) - 1
        await self._show(interaction)


class ConfirmSeasonView(ExpiringView):
    """Confirmation for a season reset. Visible to the whole channel, so the
    button is locked to the mod who ran the command."""

    def __init__(self, store, ratings, name: str, invoker_id: int, hard: bool = False, timeout: float = 60):
        super().__init__(timeout=timeout)
        self.store = store
        self.ratings = ratings
        self.name = name
        self.invoker_id = invoker_id
        self.hard = hard

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the mod who ran `!newseason` can confirm.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Start season", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.hard:
            season = self.store.start_season(self.name)
            reset = "every rating is back to the default"
        else:
            season = self.store.start_season(self.name, SOFT_RESET_CARRYOVER, SOFT_RESET_SIGMA)
            reset = "ratings carry over from career, pulled toward the middle and wide open to move"
        # The book is windowed to the open season, so it re-seeds on next read.
        self.ratings.book()
        logger.info(
            "Season %d (%s) started by %s (%s reset)",
            season.id,
            season.name,
            interaction.user,
            "hard" if self.hard else "soft",
        )
        self.stop()
        await interaction.response.edit_message(content=f"**{season.name}** has begun — {reset}. Good luck.", view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Season unchanged.", view=None)


def _text(content: str) -> dict:
    """Send/edit kwargs for a plain reply that replaces any embed."""
    return {"content": content, "embed": None}


class Leaderboard(commands.Cog):
    def __init__(self, client):
        self.client = client
        if not hasattr(client, "match_store"):
            client.match_store = MatchStore()
        if not hasattr(client, "rating_cache"):
            client.rating_cache = RatingCache(client.match_store)
        if not hasattr(client, "achievement_cache"):
            client.achievement_cache = AchievementCache(client.match_store)
        if not hasattr(client, "duo_cache"):
            client.duo_cache = DuoCache(client.match_store)
        if not hasattr(client, "unit_cache"):
            client.unit_cache = UnitCache(client.match_store)
        self.store: MatchStore = client.match_store
        self.duos_cache: DuoCache = client.duo_cache
        self.units_cache: UnitCache = client.unit_cache
        self.ratings: RatingCache = client.rating_cache
        self.achievements: AchievementCache = client.achievement_cache
        achievements.ensure_seeded(self.store, self.achievements)

    @commands.hybrid_command(
        aliases=["ladder"],
        help="show the rating leaderboard — add a race (!leaderboard zerg), a season (s1), 'career' for all-time, "
        "or 'units' to rate unit picks",
    )
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def leaderboard(self, ctx, *, query: str = ""):
        if any(token.lower() in _UNIT_WORDS for token in query.split()):
            await self._unit_board(ctx, query)
            return
        min_games, race, season_query = self._parse_board_query(query)
        career = season_query.lower() in ("career", "all", "alltime", "all-time")
        if career:
            season, label = None, "All-Time"
        elif season_query:
            season = self.store.find_season(season_query)
            if season is None:
                known = ", ".join(s.name for s in self.store.seasons())
                await ctx.send(f"No season called **{season_query}**. Seasons so far: {known}.")
                return
            label = season.name
        else:
            season = self.store.current_season()
            label = season.name

        book = self._book_for(season, career, by_race=race is not None)
        everyone = book.leaderboard(min_games=1, race=race)
        board = [r for r in everyone if r.games >= min_games]
        hidden = len(everyone) - len(board)
        names = {r.handle: self._shown_name(ctx, r.handle, r.name) for r in board}
        # Only ever one season on the board — name it so an empty or shuffled
        # ladder right after a reset reads as intentional, not as data loss.
        final = season is not None and season.ended_at is not None
        view = PagedBoardView(
            lambda page: match_embeds.leaderboard(board, page, min_games, names, hidden, label, final, race),
            match_embeds.page_count(board),
        )
        await self._send_board(ctx, view)

    @staticmethod
    def _parse_board_query(query: str) -> tuple[int, str | None, str]:
        """Split '!leaderboard [min_games] [race] [season]' into its parts. A
        bare number stays min_games — that predates seasons and is what the
        board's own footer tells people to type — so a season needs a name
        ('s1'). A race word can go anywhere."""
        min_games, race, season_words = DEFAULT_MIN_GAMES, None, []
        for token in query.split():
            if token.lower() in _RACES:
                race = _RACES[token.lower()]
            elif token.isdigit() and not season_words:
                min_games = int(token)
            else:
                season_words.append(token)
        return min_games, race, " ".join(season_words)

    def _book_for(self, season, career: bool, by_race: bool = False):
        """The rating book for a season. The open season comes from the shared
        cache; past seasons, the career board and race boards are built on
        demand — they're rare reads, and at this history size a full replay is
        ~50ms."""
        if not career and not by_race and season is not None and season.ended_at is None:
            return self.ratings.book()
        return season_book(self.store, None if career else season, by_race)

    def _shown_name(self, ctx, handles, fallback: str) -> str:
        """The Discord display name of whoever these accounts are linked to —
        the member is the source of truth for identity — else the SC2 name."""
        for handle in handles if isinstance(handles, list) else [handles]:
            discord_id = self.store.discord_id_for_handle(handle)
            if discord_id is None:
                continue
            member = ctx.guild.get_member(int(discord_id)) if ctx.guild else None
            user = member or self.client.get_user(int(discord_id))
            return user.display_name if user else fallback
        return fallback

    def _career_book(self):
        """Every match ever, ignoring season windows. Profiles resolve against
        this so a player who hasn't played yet this season still has one."""
        return self._book_for(None, career=True)

    def _resolve_person(self, person, others: int = 1):
        """The same tuple, for an already-chosen person."""
        book = self._career_book()
        rating = next((r for r in (book.rating_for(h) for h in person.handles) if r is not None), None)
        if rating is None:
            return None
        return (*self._with_season(rating), others)

    def _rank_of(self, book, rating) -> tuple[int | None, int]:
        """(rank among ranked players, size of the ranked board). Players
        under MIN_RANKED_GAMES are unranked (None)."""
        ranked = book.leaderboard(min_games=MIN_RANKED_GAMES)
        rank = next((i for i, r in enumerate(ranked, 1) if r.handle == rating.handle), None)
        return rank, len(ranked)

    def _with_season(self, career_rating):
        """Pair a career rating with the same player's standing in the open
        season (None until they play in it) and their season rank."""
        book = self.ratings.book()
        season_rating = book.rating_for(career_rating.handle)
        if season_rating is None:
            return career_rating, None, None, len(book.leaderboard(min_games=MIN_RANKED_GAMES))
        rank, ranked_total = self._rank_of(book, season_rating)
        return career_rating, season_rating, rank, ranked_total

    def _resolve_self(self, author):
        """The command author's own most-active rated account, or None."""
        book = self._career_book()
        best = None
        for h in self.store.handles_for(str(author.id)):
            r = book.rating_for(h)
            if r is not None and (best is None or r.games > best.games):
                best = r
        if best is None:
            return None
        return (*self._with_season(best), 1)

    async def _resolve_or_reply(self, ctx, player: str | None):
        """Only for the no-name (yourself) case; named lookups go through
        _person_or_pick so a shared name can ask."""
        resolved = self._resolve_self(ctx.author)
        if resolved is None:
            await ctx.send("You haven't linked a rated SC2 account yet — use `!link <name>`, or pass a name.")
        return resolved

    def _profile_embed(self, ctx, resolved, player: str | None):
        rating, season_rating, rank, total, _n = resolved
        group = self.store.merged_handles(rating.handle)  # all merged accounts, e.g. Jay+Luigi
        return match_embeds.player_profile(
            rating,
            rank,
            total,
            self.store.aliases_for_handles(group),
            self.store.player_records_by(group, "race", MIN_WINNER_CONFIDENCE, MIN_DURATION_SECONDS),
            self.store.player_records_by(group, "pick", MIN_WINNER_CONFIDENCE, MIN_DURATION_SECONDS),
            self.store.mvp_count(group, MIN_WINNER_CONFIDENCE, MIN_DURATION_SECONDS),
            self.store.award_counts(group, MIN_WINNER_CONFIDENCE, MIN_DURATION_SECONDS),
            display_name=self._shown_name(ctx, group, rating.name),
            achievements=achievements.ledger_for_group(self.store, group),
            season_rating=season_rating,
            season_name=self.store.current_season().name,
        )

    @commands.hybrid_command(aliases=["rank"], help="show a player's full profile (yourself if no name given)")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def profile(self, ctx, *, player: str | None = None):
        if player is None:
            resolved = await self._resolve_or_reply(ctx, None)
            if resolved is not None:
                await ctx.send(embed=self._profile_embed(ctx, resolved, player))
            return

        async def show(interaction, person):
            resolved = self._resolve_person(person)
            if resolved is None:
                await interaction.response.edit_message(
                    content=f"**{person.sc2_name}** has no rated games yet.", view=None
                )
                return
            await interaction.response.edit_message(
                content=None, embed=self._profile_embed(ctx, resolved, player), view=None
            )

        picked = await self._person_or_pick(ctx, player, show)
        if picked is None:
            return
        person, note = picked
        resolved = self._resolve_person(person)
        if resolved is None:
            await ctx.send(f"No rated games found for **{player}**.")
            return
        await ctx.send(embed=self._profile_embed(ctx, resolved, player))
        if note:
            await ctx.send(note)

    async def _person_or_pick(self, ctx, query: str, on_pick):
        """(person, note on the weaker matches) for a typed name, or None when
        the caller has been answered already. Who the name means is decided by
        views.person_or_pick, so every command agrees; this only adds that a
        stats command needs someone with games."""
        people = await person_or_pick(ctx, self.store, query, on_pick)
        if people is None:
            return None
        person = people[0]
        if not person.handles:
            await ctx.send(self._no_games(person))
            return None
        return person, identity.others_note(people)

    @staticmethod
    def _no_games(person) -> str:
        if person.via == identity.DISCORD:
            return f"**{person.discord_name}** hasn't linked an SC2 account yet (`!link <name>`)."
        return f"No games found for **{person.sc2_name}** yet."

    def _own_group(self, author) -> list[str]:
        group: list[str] = []
        for handle in self.store.handles_for(str(author.id)):
            for h in self.store.merged_handles(handle):
                if h not in group:
                    group.append(h)
        return group

    @commands.hybrid_command(aliases=["ach"], help="show a player's achievements (yourself if no name given)")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def achievements(self, ctx, *, player: str | None = None):
        # A slash invocation renders privately, which is what lets it unmask
        # the recipes of secrets the VIEWER has earned (same gate as
        # /gallery). Text !ach stays public, so it stays masked.
        private = ctx.interaction is not None
        if player is None:
            group = self._own_group(ctx.author)
            if not group:
                await ctx.send("Link your SC2 account first (`!link <name>`), or give a name.")
                return
            shown = ctx.author.display_name
        else:

            async def picked(interaction, person):
                await interaction.response.edit_message(
                    content=None,
                    embed=self._achievement_embed(ctx, list(person.handles), person.sc2_name, private),
                    view=None,
                )

            chosen = await self._person_or_pick(ctx, player, picked)
            if chosen is None:
                return
            person, _note = chosen
            group, name = list(person.handles), person.sc2_name
            shown = self._shown_name(ctx, group, name)
        embed = self._achievement_embed(ctx, group, shown, private, author=ctx.author)
        await ctx.send(embed=embed, ephemeral=private)

    def _achievement_embed(self, ctx, group: list[str], shown: str, private: bool, author=None):
        earned = achievements.ledger_for_group(self.store, group)
        next_up = self.achievements.book().next_up(group[0], ensure_detail=True)
        holders = achievements.ledger_holder_counts(self.store, self.store.merge_map())
        # Only the viewer's own secrets, so looking someone else up privately
        # can't hand over a recipe you haven't earned yourself.
        reveal = self._own_secret_keys(author or ctx.author) if private else set()
        return match_embeds.achievements_gallery(shown, earned, next_up, holders, reveal)

    def _own_secret_keys(self, author) -> set[str]:
        """Keys of the secrets the invoking user holds, for recipe reveal."""
        group = self._own_group(author)
        if not group:
            return set()
        return {e.spec.key for e in achievements.ledger_for_group(self.store, group) if achievements.is_secret(e.spec)}

    @commands.hybrid_command(
        aliases=["catalog"], help="browse the full achievement gallery (secret recipes reveal only via /gallery)"
    )
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def gallery(self, ctx):
        # Recipes for secrets you've earned are only revealed on a PRIVATE
        # (ephemeral) render, which needs a slash invocation. A text !catalog
        # still works — it just keeps secret recipes masked, since the message
        # is public.
        group = self._own_group(ctx.author)
        earned_keys = {e.spec.key for e in achievements.ledger_for_group(self.store, group)} if group else set()
        discovered = self.store.discovered_keys()
        holders = achievements.ledger_holder_counts(self.store, self.store.merge_map())
        private = ctx.interaction is not None
        view = CatalogView(earned_keys, discovered, holders, private)
        view.message = await ctx.send(embed=view.embed(), view=view, ephemeral=private)

    @commands.hybrid_command(help="browse recent matches (◀ steps back in time) — optionally a player's")
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def last(self, ctx, *, player: str | None = None):
        matches = self.store.all_matches()  # oldest first
        if player:

            async def picked(interaction, person):
                handles = set(person.handles)
                theirs = [
                    (i, m) for i, m in self.store.all_matches() if any(p.toon_handle in handles for p in m.players)
                ]
                if not theirs:
                    await interaction.response.edit_message(
                        content=f"No games found for **{person.sc2_name}**.", view=None
                    )
                    return
                browser = MatchBrowserView(theirs, self.store.map_version_names())
                await interaction.response.edit_message(
                    content=None, embed=browser.embed(), view=browser if len(theirs) > 1 else None
                )
                if len(theirs) > 1:
                    browser.message = await interaction.original_response()

            chosen = await self._person_or_pick(ctx, player, picked)
            if chosen is None:
                return
            handles = set(chosen[0].handles)
            matches = [(i, m) for i, m in matches if any(p.toon_handle in handles for p in m.players)]
        if not matches:
            await ctx.send("No matches stored yet.")
            return
        view = MatchBrowserView(matches, self.store.map_version_names())
        if len(matches) == 1:
            await ctx.send(embed=view.embed())
            return
        view.message = await ctx.send(embed=view.embed(), view=view)

    @commands.hybrid_command(help="head-to-head between two players — !h2h <name> means you vs them")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def h2h(self, ctx, player1: str, player2: str | None = None):
        # Either name can need a picker; the second is only asked once the
        # first is settled, so each prompt is about one name.
        async def first_picked(interaction, person):
            if player2 is None:
                await interaction.response.edit_message(view=None, **self._h2h_reply(ctx, person, None))
                return
            shown = self._shown_name(ctx, list(person.handles), person.discord_name or person.sc2_name)
            await interaction.response.edit_message(content=f"Head-to-head: **{shown}** vs…", view=None)
            await self._h2h_against(ctx, person, player2)

        chosen = await self._person_or_pick(ctx, player1, first_picked)
        if chosen is None:
            return
        if player2 is None:
            await ctx.send(**self._h2h_reply(ctx, chosen[0], None))
        else:
            await self._h2h_against(ctx, chosen[0], player2)

    async def _h2h_against(self, ctx, first, player2: str):
        async def picked(interaction, person):
            await interaction.response.edit_message(view=None, **self._h2h_reply(ctx, first, person))

        chosen = await self._person_or_pick(ctx, player2, picked)
        if chosen is not None:
            await ctx.send(**self._h2h_reply(ctx, first, chosen[0]))

    def _h2h_reply(self, ctx, first, second) -> dict:
        """The h2h message as send/edit kwargs. `second` None means the
        author."""
        for person in (first, second):
            if person is not None and not person.handles:
                return _text(self._no_games(person))
        group1 = list(first.handles)
        name1 = self._shown_name(ctx, group1, first.sc2_name)
        if second is None:
            group2 = self._own_group(ctx.author)
            if not group2:
                return _text("Link your SC2 account first (`!link <name>`), or give two names.")
            name2 = ctx.author.display_name
        else:
            group2 = list(second.handles)
            name2 = self._shown_name(ctx, group2, second.sc2_name)
        if set(group1) & set(group2):
            return _text(f"**{name1}** and **{name2}** are the same player.")
        vs, together, opposed = self.store.h2h_records(group1, group2, MIN_WINNER_CONFIDENCE, MIN_DURATION_SECONDS)
        if not (sum(vs) + sum(together)):
            return _text(f"**{name1}** and **{name2}** haven't shared a decided game yet.")
        duo = self._duo_for(group1, group2) if sum(together) else None
        embed = match_embeds.h2h_summary(name1, name2, vs, together, opposed, group1, group2, duo)
        return {"content": None, "embed": embed}

    def _duo_for(self, group1: list[str], group2: list[str]):
        """This pair's entry on the duo board, or None if they've never been
        teamed. Keyed the way duo_records keys them: canonical handles sorted,
        so either argument order finds the same row."""
        merge = self.store.merge_map()
        pair = tuple(sorted({merge.get(group1[0], group1[0]), merge.get(group2[0], group2[0])}))
        return self.duos_cache.records().get(pair)

    @commands.hybrid_command(aliases=["mvps"], help="rank players by how often they're the MVP")
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def mvprate(self, ctx, min_games: int = MVP_RATE_MIN_GAMES):
        # Career, not seasonal: MVP rate is a slow stat and a season's worth of
        # games is far too few for it to mean anything.
        min_games = max(1, min_games)
        book = self._career_book()
        counts = self.store.mvp_counts(MIN_WINNER_CONFIDENCE, MIN_DURATION_SECONDS, self.store.merge_map())
        rows = [(r.handle, r.name, counts.get(r.handle, 0), r.games) for r in book.leaderboard(min_games=min_games)]
        if not rows:
            await ctx.send(f"Nobody has {min_games} games yet.")
            return
        rows.sort(key=lambda row: row[2] / row[3], reverse=True)
        # Same resolution as the rating board: the linked member's Discord name.
        names = {handle: self._shown_name(ctx, handle, name) for handle, name, _mvps, _games in rows}
        view = PagedBoardView(
            lambda page: match_embeds.mvp_rates(rows, page, min_games, names),
            match_embeds.page_count(rows),
        )
        await self._send_board(ctx, view)

    @commands.hybrid_command(
        aliases=["pairs", "duo"],
        help="rank the best pairs of teammates — !duos raw and !duos synergy sort other ways",
    )
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def duos(self, ctx, *, query: str = ""):
        min_games, sort = self._parse_duo_query(query)
        rows = [d for d in self.duos_cache.records().values() if d.games >= min_games]
        if not rows:
            await ctx.send(f"No pair has played {min_games} games together yet.")
            return
        keys = {
            "rating": lambda d: d.ordinal,
            "raw": lambda d: (d.win_rate, d.games),
            "synergy": lambda d: d.synergy,
        }
        rows.sort(key=keys[sort], reverse=True)
        names = {h: self._shown_name(ctx, h, n) for d in rows for h, n in zip(d.handles, d.names)}
        view = PagedBoardView(
            lambda page: match_embeds.duo_board(rows, page, min_games, names, sort),
            match_embeds.page_count(rows),
        )
        await self._send_board(ctx, view)

    @staticmethod
    def _parse_duo_query(query: str) -> tuple[int, str]:
        """Split '!duos [min_games] [sort]'. The pair's own rating is the
        default: it is the only one of the three sorts that is both about the
        pair and stable enough to rank on (see match_embeds.duo_board)."""
        min_games, sort = DUO_MIN_GAMES, "rating"
        for token in query.lower().split():
            if token.isdigit():
                min_games = max(1, int(token))
            elif token in _DUO_SORTS:
                sort = _DUO_SORTS[token]
        return min_games, sort

    async def _send_board(self, ctx, view: PagedBoardView):
        """Post a paged board, hiding the arrows when there's only one page."""
        view.message = await ctx.send(embed=view.embed(), view=view if view.multipage else None)

    @commands.hybrid_command(help="rate unit picks, adjusted for who picked them — same as !leaderboard units")
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def unitstats(self, ctx, *, query: str = ""):
        await self._unit_board(ctx, query)

    async def _unit_board(self, ctx, query: str):
        min_games, raw = self._parse_unit_query(query)
        units = self.units_cache.units()
        # The zero is the average pick across every unit, not just the ones
        # shown, so raising the floor doesn't shift everyone's number.
        baseline = unit_baseline(units.values())
        rows = [u for u in units.values() if u.games >= min_games]
        if not rows:
            await ctx.send(f"No unit has {min_games} rated games yet.")
            return
        rows.sort(key=(lambda u: (u.win_rate, u.games)) if raw else (lambda u: u.mu), reverse=True)
        sort = "raw" if raw else "rating"
        view = PagedBoardView(
            lambda page: match_embeds.unit_board(rows, page, min_games, baseline, sort),
            match_embeds.page_count(rows),
        )
        await self._send_board(ctx, view)

    @staticmethod
    def _parse_unit_query(query: str) -> tuple[int, bool]:
        """Split '[units] [min_games] [raw]' into (min_games, sort by raw win
        rate). Unit boards are career-wide, so season words are ignored."""
        min_games, raw = UNIT_MIN_GAMES, False
        for token in query.lower().split():
            if token.isdigit():
                min_games = max(1, int(token))
            elif token in _UNIT_RAW_WORDS:
                raw = True
        return min_games, raw

    @commands.hybrid_command(help="show the current ladder season")
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def season(self, ctx):
        current = self.store.current_season()
        played = len(self.store.season_matches(current))
        past = [s for s in self.store.seasons() if s.id != current.id]
        started = current.started_at[:10]
        # Season 1 opens at the dawn of time to absorb pre-seasons history;
        # a date of year 1 would be nonsense to show.
        opened = "" if started.startswith("0001") else f" · started {started}"
        embed = discord.Embed(
            title=current.name,
            description=f"**{played}** games played this season{opened}.",
            color=match_embeds.ACCENT,
        )
        if past:
            embed.add_field(
                name="Past seasons",
                value="\n".join(f"{s.name} — {len(self.store.season_matches(s))} games" for s in reversed(past)),
                inline=False,
            )
        if current.carryover is not None:
            embed.add_field(
                name="Ratings",
                value=f"Started from career ratings, {current.carryover:.0%} carried over and uncertainty reset.",
                inline=False,
            )
        footer = "Ratings cover this season only · match history and achievements are all-time"
        if past:
            footer = "!leaderboard s1 shows a past season · !leaderboard career is all-time\n" + footer
        embed.set_footer(text=footer)
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        help="start a new ladder season: ratings carry over from career, pulled toward the middle "
        "(add --hard to reset everyone to the default) (mods)"
    )
    @is_bot_admin()
    async def newseason(self, ctx, *, name: str | None = None):
        current = self.store.current_season()
        words = (name or "").split()
        hard = "--hard" in words
        name = " ".join(w for w in words if w != "--hard") or self._next_season_name()
        played = len(self.store.season_matches(current))
        if hard:
            reset = "resets every rating to the default"
        else:
            reset = (
                f"starts every rating from career, keeping {SOFT_RESET_CARRYOVER:.0%} of each player's distance "
                f"from the middle, with uncertainty reset so everyone can move fast"
            )
        view = ConfirmSeasonView(self.store, self.ratings, name, ctx.author.id, hard=hard)
        message = await ctx.send(
            f"Start **{name}**? This ends **{current.name}** ({played} games) and {reset} — nothing is "
            f"deleted, and match history, profile stats and achievements are untouched.",
            view=view,
        )
        view.message = message

    def _next_season_name(self) -> str:
        """Default name continues the numbering: 'Season 1' -> 'Season 2'."""
        return f"Season {len(self.store.seasons()) + 1}"

    @commands.hybrid_command(help="how many matches are stored")
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def matchcount(self, ctx):
        await ctx.send(f"{self.store.match_count()} matches stored.")


async def setup(client):
    await client.add_cog(Leaderboard(client))
