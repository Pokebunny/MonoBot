"""List researchable upgrades the mod doesn't lock: each is open to every pick.

    python sc2mod/audit_upgrades.py <mods dir>

<mods dir> is the `mods` folder of a dump of Blizzard's game data with
liberty/swarm/void and their *multi mods (e.g. github.com/SC2Mapster/SC2GameData,
sparse-checked-out). Run it after a balance patch: an upgrade it prints should
either be locked in MB_DisallowEverything and unlocked for its pick in
MB_Unlock, or be one every player may have (static defense, shared transports).
"""

import pathlib
import re
import sys

MODS = ("liberty", "libertymulti", "swarm", "swarmmulti", "void", "voidmulti")  # later ones override
# Weapons/armour levels and the like, which every pick shares.
SHARED = re.compile(r"(Weapons|Armors|Shields|Armor|Plating|Carapace|Level\d)|^(Burrow|overlordspeed)")
# Researchable, not unit-specific, deliberately left on (see README.md).
LEFT_ON = {
    "HiSecAutoTracking",
    "NeosteelFrame",
    "MedivacRapidDeployment",
    "MedivacIncreaseSpeedBoost",  # Rapid Reignition
    "ObserverGraviticBooster",
    "SecretedCoating",
    "overlordtransport",
}


def researchable(mods: pathlib.Path) -> dict[str, set[str]]:
    slots: dict[str, dict[str, str]] = {}
    for mod in MODS:
        path = mods / f"{mod}.sc2mod/base.sc2data/GameData/AbilData.xml"
        if not path.exists():
            continue
        src = path.read_text(encoding="utf8")
        for m in re.finditer(r'<CAbilResearch id="([^"]+)"[^>]*>(.*?)</CAbilResearch>', src, re.S):
            for index, upgrade in re.findall(r'<InfoArray index="([^"]+)"[^>]*Upgrade="([^"]+)"', m.group(2)):
                slots.setdefault(m.group(1), {})[index] = upgrade
    upgrades: dict[str, set[str]] = {}
    for ability, by_slot in slots.items():
        for upgrade in by_slot.values():
            upgrades.setdefault(upgrade, set()).add(ability)
    return upgrades


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    upgrades = researchable(pathlib.Path(sys.argv[1]))
    script = (pathlib.Path(__file__).parent / "MonoBattle.galaxy").read_text(encoding="utf8")
    locked = set(re.findall(r'TechTreeUpgradeAllow\(lp_player, "(\w+)", false\)', script))
    open_ = sorted(u for u in upgrades if u not in locked and u not in LEFT_ON and not SHARED.search(u))
    for upgrade in open_:
        print(f"{upgrade}  (researched at {', '.join(sorted(upgrades[upgrade]))})")
    print(f"{len(open_)} unlocked unit upgrade(s) of {len(upgrades)} researchable")
    return 1 if open_ else 0


if __name__ == "__main__":
    sys.exit(main())
