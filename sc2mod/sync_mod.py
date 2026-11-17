"""Copy MonoBattle.galaxy into the mod's custom script.

    python sc2mod/sync_mod.py [mod folder, default sc2mod/MonoBattle.SC2Mod]

The Editor keeps the script, XML-escaped, in the mod's `Triggers` file; open
the mod in the Editor and save it to recompile.
"""

import pathlib
import re
import sys
from xml.sax.saxutils import escape

INDENT = " " * 16  # how the Editor indents ScriptCode


def main() -> int:
    here = pathlib.Path(__file__).parent
    if len(sys.argv) > 2:
        print(__doc__)
        return 2
    triggers = pathlib.Path(sys.argv[1] if len(sys.argv) == 2 else here / "MonoBattle.SC2Mod") / "Triggers"
    src = triggers.read_text(encoding="utf8")
    script = (here / "MonoBattle.galaxy").read_text(encoding="utf8")
    body = escape(script, {"'": "&apos;", '"': "&quot;"})
    body = "\n".join(INDENT + line if line else "" for line in body.splitlines())
    new, n = re.subn(r"(<ScriptCode>\n).*?(\n\s*</ScriptCode>)", lambda m: m[1] + body + m[2], src, flags=re.S)
    if n != 1:
        print(f"expected one <ScriptCode> in {triggers}, found {n}")
        return 1
    triggers.write_text(new, encoding="utf8", newline="\r\n")  # the Editor writes CRLF
    print(f"wrote {len(script.splitlines())} lines to {triggers}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
