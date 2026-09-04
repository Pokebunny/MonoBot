"""The secret-visibility gates in the achievement embeds:
- name hidden until the community has discovered it (Gate 1),
- recipe shown only to a viewer who earned it, and only on a private render (Gate 2).
"""

import datetime

from services import match_embeds
from services.achievements import SPECS_BY_KEY, Earned

DT = datetime.datetime(2026, 1, 1)
SECRET = SPECS_BY_KEY["one_man_army"]  # secret: "Outkill the entire enemy team by yourself"
SECRET_RECIPE = SECRET.description
OPEN = SPECS_BY_KEY["first_game"]  # not secret
# Take the tier from the spec, not a literal — rarities get re-tuned against
# how often a badge actually turns up, and that must not break these gates.
SECRET_TIER = SECRET.rarity
OPEN_TIER = OPEN.rarity


def _text(embed) -> str:
    return "\n".join([embed.description or ""] + [f.value for f in embed.fields])


# -- catalog -------------------------------------------------------------


def test_catalog_hides_undiscovered_secret_name():
    embed = match_embeds.achievement_catalog(SECRET_TIER, earned_keys=set(), discovered_keys=set())
    text = _text(embed)
    assert "One-Man Army" not in text
    assert SECRET_RECIPE not in text
    assert "???" in text


def test_catalog_shows_discovered_secret_name_but_masks_recipe():
    # Discovered by the community, but this viewer hasn't earned it.
    embed = match_embeds.achievement_catalog(
        SECRET_TIER, earned_keys=set(), discovered_keys={"one_man_army"}, private=True
    )
    text = _text(embed)
    assert "One-Man Army" in text
    assert SECRET_RECIPE not in text
    assert "secret" in text


def test_catalog_reveals_recipe_only_when_earned_and_private():
    earned, discovered = {"one_man_army"}, {"one_man_army"}
    private = match_embeds.achievement_catalog(SECRET_TIER, earned, discovered, private=True)
    public = match_embeds.achievement_catalog(SECRET_TIER, earned, discovered, private=False)
    assert SECRET_RECIPE in _text(private)  # ephemeral view: the how is revealed
    assert SECRET_RECIPE not in _text(public)  # public !catalog: still masked
    assert "One-Man Army" in _text(public)  # ...but the name shows


def test_catalog_lists_open_achievements_plainly():
    embed = match_embeds.achievement_catalog(OPEN_TIER, earned_keys={"first_game"}, discovered_keys=set())
    text = _text(embed)
    assert OPEN.description in text  # non-secret recipe always visible
    assert "✅" in text  # earned marker


# -- unlock announcement -------------------------------------------------


def test_announcement_hides_secret_recipe():
    embed = match_embeds.achievement_unlocks([("Bob", Earned(SECRET, DT))])
    text = _text(embed)
    assert "One-Man Army" in text
    assert SECRET_RECIPE.lower() not in text.lower()
    assert "secret" in text.lower()


def test_announcement_credits_first_discovery():
    embed = match_embeds.achievement_unlocks([("Bob", Earned(SECRET, DT))], frozenset({"one_man_army"}))
    text = _text(embed)
    assert "first to discover" in text.lower()
    assert SECRET_RECIPE.lower() not in text.lower()


def test_announcement_shows_open_recipe():
    embed = match_embeds.achievement_unlocks([("Bob", Earned(OPEN, DT))])
    assert OPEN.description.lower() in _text(embed).lower()


# -- profile -------------------------------------------------------------


def test_profile_masks_secret_recipe():
    embed = match_embeds.achievements_gallery("Bob", [Earned(SECRET, DT), Earned(OPEN, DT)], next_up=[])
    text = _text(embed)
    assert "One-Man Army" in text  # name shown
    assert SECRET_RECIPE not in text  # recipe never on a public profile
    assert OPEN.description in text  # non-secret recipe fine
    assert "reveals recipes" in (embed.footer.text or "")  # points at the private render


def test_profile_reveals_own_secret_on_private_render():
    # Gate 2 on the profile: the viewer earned it AND this render is ephemeral.
    embed = match_embeds.achievements_gallery(
        "Bob", [Earned(SECRET, DT), Earned(OPEN, DT)], next_up=[], reveal_keys={"one_man_army"}
    )
    text = _text(embed)
    assert SECRET_RECIPE in text
    assert "*secret:*" in text  # still tagged as a secret once unlocked
    assert "reveals recipes" not in (embed.footer.text or "")  # nothing left masked


def test_profile_keeps_masking_secrets_the_viewer_lacks():
    # Looking someone else up privately must not hand over their recipes.
    embed = match_embeds.achievements_gallery(
        "Bob", [Earned(SECRET, DT), Earned(OPEN, DT)], next_up=[], reveal_keys={"some_other_secret"}
    )
    assert SECRET_RECIPE not in _text(embed)


def test_catalog_tags_unlocked_secret_as_secret():
    # Recipe revealed, but the badge must still read as a secret.
    embed = match_embeds.achievement_catalog(
        SECRET_TIER, earned_keys={"one_man_army"}, discovered_keys={"one_man_army"}, private=True
    )
    text = _text(embed)
    assert SECRET_RECIPE in text
    assert "*secret:*" in text
