"""Shared data types. Every module imports from here; keep this file dependency-free."""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional

APP_NAME = "RimWorld Mod Manager"
APP_ID = "RimModManager"
APP_VERSION = "1.0.0"

RIMWORLD_APPID = "294100"
CORE_ID = "ludeon.rimworld"
HARMONY_ID = "brrainz.harmony"
HARMONY_WORKSHOP_ID = "2009463077"
# Official DLCs in canonical load order: package id -> (steam appid, short name)
DLCS: dict[str, tuple[str, str]] = {
    "ludeon.rimworld.royalty": ("1149640", "Royalty"),
    "ludeon.rimworld.ideology": ("1392840", "Ideology"),
    "ludeon.rimworld.biotech": ("1826140", "Biotech"),
    "ludeon.rimworld.anomaly": ("2380740", "Anomaly"),
    "ludeon.rimworld.odyssey": ("3022790", "Odyssey"),
}
DLC_BY_APPID = {appid: pid for pid, (appid, _) in DLCS.items()}
OFFICIAL_IDS = [CORE_ID, *DLCS]  # canonical order
LOADABLE_SOURCES = ("core", "dlc", "local", "workshop")   # RimWorld never sees 'steamcmd' cache / 'extra' dirs
JUNK_PACKAGE_IDS = {"scenario.rsc", "missing.packageid", "invalid.item"}   # steamDB placeholders: treat as unknown
LEVEL_RANK = {"error": 0, "warning": 1, "info": 2}   # Issue/LogEntry level sort order
WORKSHOP_URL = "https://steamcommunity.com/sharedfiles/filedetails/?id={}"
USER_AGENT = f"{APP_ID}/{APP_VERSION}"


def short_version(v: str) -> str:
    """'1.6.4871 rev590' / 'v1.6' -> '1.6'; '1.06' -> '1.6'; '' / junk -> ''."""
    m = re.match(r"\s*v?(\d+)\.(\d+)", v or "")
    return f"{int(m.group(1))}.{int(m.group(2))}" if m else ""


def compat_versions(v: str) -> tuple[str, ...]:
    """Short versions a game version accepts: RimWorld 1.7 supports 1.6 mods."""
    s = short_version(v)
    return ("1.7", "1.6") if s == "1.7" else ((s,) if s else ())


# progress(fraction or None, text).  fraction in [0,1]; None = indeterminate
Progress = Callable[[Optional[float], str], None]


def noop_progress(_f: Optional[float] = None, _t: str = "") -> None:
    pass


@dataclass
class ModDependency:
    package_id: str                 # lowercased
    display_name: str = ""
    steam_url: str = ""
    workshop_id: str = ""           # parsed from steam_url when possible
    alternatives: list[str] = field(default_factory=list)   # lowercased alternativePackageIds: any of them satisfies it
    enforced: bool = True           # False when RimWorld's ValidateDependencies drops it (no displayName / no URL / bad id)

    def ids(self) -> list[str]:
        """Every package id that satisfies this dependency."""
        return [self.package_id, *self.alternatives]


@dataclass
class Mod:
    """An installed mod (a folder on disk with About/About.xml) or Core/DLC."""
    uid: str                        # id used in ModsConfig.xml (package_id, or package_id + "_steam" for a workshop duplicate)
    package_id: str                 # lowercased packageId from About.xml
    name: str
    path: str                       # absolute folder path
    source: str                     # 'core' | 'dlc' | 'local' | 'workshop' | 'steamcmd' | 'extra'
    workshop_id: str = ""           # PublishedFileId ('' if unknown)
    authors: list[str] = field(default_factory=list)
    supported_versions: list[str] = field(default_factory=list)   # e.g. ['1.5', '1.6']
    description: str = ""
    mod_version: str = ""
    url: str = ""
    preview_path: str = ""          # About/Preview.png if present
    dependencies: list[ModDependency] = field(default_factory=list)   # byVersion list for the game version, else generic
    load_after: list[str] = field(default_factory=list)       # lowercased ids (includes forceLoadAfter); byVersion replaces generic
    load_before: list[str] = field(default_factory=list)      # lowercased ids (includes forceLoadBefore)
    incompatible_with: list[str] = field(default_factory=list)
    folder_after: list[str] = field(default_factory=list)     # lowercased ids from LoadFolders.xml IfModActive
    assemblies: list[str] = field(default_factory=list)       # dll file stems found in */Assemblies
    mtime: float = 0.0              # newest mtime among the folder + About.xml (for update checks)
    parse_error: str = ""           # non-empty if About.xml was missing/broken and fallbacks were used

    @property
    def is_official(self) -> bool:
        return self.source in ("core", "dlc")


@dataclass
class ModRef:
    """A reference to a mod that may or may not be installed (modlists, imports, AI picks)."""
    package_id: str = ""            # lowercased; may be '' when only the workshop id is known
    name: str = ""
    workshop_id: str = ""

    def key(self) -> str:
        return self.package_id or ("wid:" + self.workshop_id)


@dataclass
class ModList:
    name: str
    mods: list[ModRef] = field(default_factory=list)
    game_version: str = ""
    notes: str = ""
    source: str = "manual"          # 'manual' | 'ai' | 'import:<fmt>' | 'game'
    created: float = 0.0
    updated: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DbMod:
    """A row of the RimSort Steam Workshop Database (+ optional cached Steam Web API details)."""
    workshop_id: str                # PublishedFileId, or Steam appid for Core/DLC rows
    package_id: str = ""            # lowercased; '' when the DB does not know it
    name: str = ""                  # About.xml name
    steam_name: str = ""            # workshop title
    authors: str = ""
    game_versions: list[str] = field(default_factory=list)
    dependencies: dict[str, str] = field(default_factory=dict)   # workshop id / appid -> display name
    unpublished: bool = False
    is_dlc: bool = False            # official app row (Core/DLC)
    kind: str = ""                  # Steam tag kind: 'translation' | 'scenario' | '' (a regular mod)
    # enriched from Steam Web API cache (0/'' when unknown)
    subscriptions: int = 0
    time_updated: int = 0
    description: str = ""
    preview_url: str = ""

    @property
    def display_name(self) -> str:
        return self.name or self.steam_name or self.package_id or self.workshop_id

    @property
    def url(self) -> str:
        if self.is_dlc:
            return f"https://store.steampowered.com/app/{self.workshop_id}"
        return WORKSHOP_URL.format(self.workshop_id)

    def to_ref(self) -> ModRef:
        return ModRef(package_id=self.package_id, name=self.display_name, workshop_id=self.workshop_id)


@dataclass
class Issue:
    level: str                      # 'error' | 'warning' | 'info'
    kind: str                       # see SPEC.md "Issue kinds"
    uid: str                        # mod the issue is about ('' = global)
    message: str
    related: list[str] = field(default_factory=list)
    fix: Optional[dict] = None      # see SPEC.md "Issue fixes"


@dataclass
class DownloadResult:
    workshop_id: str
    ok: bool
    message: str = ""
    path: str = ""                  # final installed folder when ok


@dataclass
class LogEntry:
    level: str                      # 'error' | 'warning' | 'info'
    text: str                       # first line
    detail: str = ""                # full block (stack trace)
    mods: list[str] = field(default_factory=list)   # uids of installed mods suspected from the block
    count: int = 1                  # identical consecutive/duplicate blocks collapsed
