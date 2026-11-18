"""Copy MonoBattle.galaxy into the mod's custom script.

    python sc2mod/sync_mod.py [mod folder, default sc2mod/MonoBattle.SC2Mod]

The Editor keeps the script twice: XML-escaped in `Triggers`, and compiled
into `Base.SC2Data/Lib*.galaxy`, which is what the game runs. Saving doesn't
always recompile it (a published build once shipped a stale one), so both
are written here.
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
    mod = pathlib.Path(sys.argv[1] if len(sys.argv) == 2 else here / "MonoBattle.SC2Mod")
    triggers = mod / "Triggers"
    src = triggers.read_text(encoding="utf8")
    script = (here / "MonoBattle.galaxy").read_text(encoding="utf8")
    body = escape(script, {"'": "&apos;", '"': "&quot;"})
    body = "\n".join(INDENT + line if line else "" for line in body.splitlines())
    new, n = re.subn(r"(<ScriptCode>\n).*?(\n\s*</ScriptCode>)", lambda m: m[1] + body + m[2], src, flags=re.S)
    if n != 1:
        print(f"expected one <ScriptCode> in {triggers}, found {n}")
        return 1
    triggers.write_text(new, encoding="utf8", newline="\r\n")  # the Editor writes CRLF

    lib = next(p for p in (mod / "Base.SC2Data").glob("Lib*.galaxy") if not p.stem.endswith("_h"))
    compiled = lib.read_text(encoding="utf8")
    start = re.search(r"// Custom Script: .*\n//-+\n", compiled)
    end = compiled.index("\nvoid lib", start.end()) if start else -1
    if end < 0:
        print(f"can't find the custom script in {lib}")
        return 1
    compiled = compiled[: start.end()] + script.rstrip("\n") + "\n" + compiled[end:]
    lib.write_text(compiled, encoding="utf8", newline="\r\n")
    print(f"wrote {len(script.splitlines())} lines to {triggers} and {lib.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
