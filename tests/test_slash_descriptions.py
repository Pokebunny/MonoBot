"""Discord rejects a whole `!sync` if any slash command's description runs
past 100 characters, and a hybrid command falls back to its `help` text for
one — so a long help string silently breaks every slash command's upload."""

import importlib
import inspect
import pkgutil

import cogs
from discord.ext import commands

DISCORD_DESCRIPTION_LIMIT = 100


def _app_commands():
    for info in pkgutil.iter_modules(cogs.__path__):
        module = importlib.import_module(f"cogs.{info.name}")
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, commands.Cog) and cls.__module__ == module.__name__:
                for command in cls.__cog_commands__:
                    app_command = getattr(command, "app_command", None)
                    if app_command is not None:
                        yield command.qualified_name, app_command


def test_slash_descriptions_fit_discords_limit():
    found = list(_app_commands())
    assert found
    too_long = {name: len(c.description) for name, c in found if len(c.description) > DISCORD_DESCRIPTION_LIMIT}
    assert not too_long, too_long
