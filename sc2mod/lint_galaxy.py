"""Static checks for the mod's Galaxy scripts, short of the Editor's compiler.

    python sc2mod/lint_galaxy.py <TriggerLibs dir> sc2mod/*.galaxy

<TriggerLibs dir> is core.sc2mod/base.sc2data/TriggerLibs from a dump of
Blizzard's trigger libraries (e.g. github.com/Talv/sc2-data-trigger). Checks:
calls resolve to a native or an earlier function (with the right argument
count), c_* constants and gv_* globals exist, locals are declared before the
first statement of a function (Galaxy's rule), TriggerCreate names a trigger
function, and braces balance. Not a type checker: run the Editor before
publishing.
"""

import pathlib
import re
import sys

TYPES = r"(?:void|int|fixed|bool|string|text|unit|unitgroup|playergroup|point|region|trigger|color|order|abilcmd|revealer|soundlink|unitfilter|wave|timer)"
KEYWORDS = {
    "if",
    "else",
    "while",
    "for",
    "return",
    "break",
    "continue",
    "include",
    "const",
    "native",
    "struct",
    "true",
    "false",
    "null",
}


def _strip(src: str) -> str:
    """Comments and string contents blanked, line structure kept."""
    src = re.sub(r"//[^\n]*", "", src)
    return re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', src)


def _arg_count(params: str) -> int:
    params = params.strip()
    return 0 if not params or params == "void" else params.count(",") + 1


def load_natives(libs: pathlib.Path) -> tuple[dict[str, int], set[str]]:
    natives, consts = {}, set()
    files = [libs / "natives.galaxy", libs / "natives_missing.galaxy", *sorted((libs / "GameData").glob("*.galaxy"))]
    for path in files:
        if not path.exists():
            continue
        src = _strip(path.read_text(encoding="utf8", errors="replace"))
        for m in re.finditer(r"\bnative\s+\w+\s+(\w+)\s*\(([^)]*)\)", src):
            natives[m.group(1)] = _arg_count(m.group(2))
        for m in re.finditer(r"\bconst\s+\w+\s+(\w+)\s*=", src):
            consts.add(m.group(1))
    return natives, consts


def _call_args(text: str, open_paren: int) -> tuple[int, int]:
    """(argument count, index after the closing paren) for the call at open_paren."""
    depth, args, seen = 0, 1, False
    for i in range(open_paren, len(text)):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return (args if seen else 0), i + 1
        elif ch == "," and depth == 1:
            args += 1
        elif depth >= 1 and not ch.isspace():
            seen = True
    return -1, len(text)


def lint(path: pathlib.Path, natives: dict[str, int], consts: set[str]) -> list[str]:
    raw = path.read_text(encoding="utf8")
    src = _strip(raw)
    problems = []

    def where(pos: int) -> str:
        return f"{path.name}:{src.count(chr(10), 0, pos) + 1}"

    if src.count("{") != src.count("}"):
        problems.append(f"{path.name}: unbalanced braces ({src.count('{')} open, {src.count('}')} close)")

    consts = consts | set(re.findall(r"^\s*const\s+\w+\s+(\w+)\s*=", src, re.M))
    globals_ = set(re.findall(rf"^{TYPES}(?:\[\w+\])*\s+(gv_\w+)\s*;", src, re.M))
    functions: dict[str, tuple[int, int]] = {}  # name -> (arg count, first position declared)
    triggers = set()
    for m in re.finditer(rf"^({TYPES})\s+(\w+)\s*\(([^)]*)\)\s*([{{;])", src, re.M):
        name = m.group(2)
        functions.setdefault(name, (_arg_count(m.group(3)), m.start()))
        if m.group(1) == "bool" and re.fullmatch(r"\s*bool\s+\w+\s*,\s*bool\s+\w+\s*", m.group(3)):
            triggers.add(name)

    for m in re.finditer(r"\b(\w+)\s*\(", src):
        name = m.group(1)
        if name in KEYWORDS or re.fullmatch(TYPES, name):
            continue
        line_start = src.rfind("\n", 0, m.start()) + 1
        if re.match(rf"\s*{TYPES}\s+{name}\s*\(", src[line_start:]) and line_start == src.rfind("\n", 0, m.start()) + 1:
            if re.match(rf"^{TYPES}\s+{name}\s*\(", src[line_start:]):
                continue  # its own definition
        count, _ = _call_args(src, m.end() - 1)
        if name in natives:
            expected = natives[name]
        elif name in functions:
            expected, declared = functions[name]
            if declared > m.start():
                problems.append(f"{where(m.start())}: {name} used before it is declared")
        else:
            problems.append(f"{where(m.start())}: unknown function {name}")
            continue
        if count != expected:
            problems.append(f"{where(m.start())}: {name} takes {expected} arguments, called with {count}")

    for m in re.finditer(r"\bc_\w+", src):
        if m.group(0) not in consts:
            problems.append(f"{where(m.start())}: unknown constant {m.group(0)}")
    for m in re.finditer(r"\bgv_\w+", src):
        if m.group(0) not in globals_:
            problems.append(f"{where(m.start())}: unknown global {m.group(0)}")

    for m in re.finditer(r'TriggerCreate\("(\w+)"\)', raw):
        if m.group(1) not in triggers:
            problems.append(f"{path.name}: TriggerCreate names {m.group(1)}, which isn't a bool f(bool, bool) function")

    # Locals first: inside each top-level function, no declaration after a statement.
    for m in re.finditer(rf"^{TYPES}\s+\w+\s*\([^)]*\)\s*\{{", src, re.M):
        depth, i, body_start = 1, m.end(), m.end()
        while depth and i < len(src):
            depth += {"{": 1, "}": -1}.get(src[i], 0)
            i += 1
        statements_seen = False
        for line_match in re.finditer(r"[^\n]+", src[body_start : i - 1]):
            line = line_match.group(0).strip()
            if not line:
                continue
            if re.fullmatch(rf"{TYPES}(?:\[\w+\])*\s+\w+\s*;", line):
                if statements_seen:
                    problems.append(
                        f"{where(body_start + line_match.start())}: local declared after a statement: {line}"
                    )
            else:
                statements_seen = True
    return problems


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    natives, consts = load_natives(pathlib.Path(sys.argv[1]))
    if not natives:
        print(f"no natives found under {sys.argv[1]}")
        return 2
    problems = [p for f in sys.argv[2:] for p in lint(pathlib.Path(f), natives, consts)]
    print("\n".join(problems) or f"ok: {len(sys.argv) - 2} file(s), {len(natives)} natives known")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
