# RimWorld Mod Manager — build spec (contract between modules)

Single-file Windows `.exe` (PyInstaller `--onefile --windowed`), Python 3.13, **tkinter/ttk only** for UI.
Runtime deps: **stdlib only**, except the optional `anthropic` SDK (lazily imported inside `rmm/ai.py` only).
Target: weak laptops → no heavy libs, no big in-memory structures, all slow work on background threads,
SQLite (stdlib) for the 57k-row mod database.

Shared types live in `rmm/models.py` (already written — read it, do not redefine these types).
Everything below is a contract: implement the exact public names/signatures. Private helpers are free.
Style: plain functions + small classes, type hints, short docstrings, no frameworks, no abstract base classes.
Every module ends with a `_selftest()` function (assert-based, no network unless stated, returns None, raises on failure)
that `tests/selftest.py` calls. Network-dependent checks go in `_selftest_online()`.

## Environment facts (verified on the dev machine)

- Steam registry: `HKCU\Software\Valve\Steam` value `SteamPath` (forward slashes, lowercase possible).
  Also `HKLM\SOFTWARE\WOW6432Node\Valve\Steam` `InstallPath`. Libraries: `<steam>/steamapps/libraryfolders.vdf`
  (Valve KeyValues text; each entry has `"path"` and `"apps"` with appid keys). RimWorld appid `294100`,
  manifest `appmanifest_294100.acf` → `"installdir" "RimWorld"` → `<library>/steamapps/common/RimWorld`.
- GOG: registry `HKLM\SOFTWARE\WOW6432Node\GOG.com\Games\1094900386` value `path` (best effort), default `C:\GOG Games\RimWorld`.
- Game dir contains `RimWorldWin64.exe`, `Version.txt` (e.g. `1.6.4871 rev590`), `Data/{Core,Royalty,Ideology,Biotech,Anomaly,Odyssey}`,
  `Mods/` (local mods; on this machine they are folders named by workshop id, e.g. `Mods/1084452457`),
  `RimWorldWin64_Data/Plugins/x86_64/steam_api64.dll` (Steamworks flat API; exports include `SteamAPI_Init`,
  `SteamAPI_Shutdown`, `SteamAPI_RunCallbacks`, `SteamAPI_SteamUGC_v016`, `SteamAPI_SteamUtils_v010`,
  `SteamAPI_ISteamUGC_SubscribeItem`, `SteamAPI_ISteamUGC_UnsubscribeItem`, `SteamAPI_ISteamUGC_GetNumSubscribedItems`,
  `SteamAPI_ISteamUGC_GetSubscribedItems`, `SteamAPI_ISteamUGC_GetItemState`, `SteamAPI_ISteamUGC_DownloadItem`,
  `SteamAPI_ISteamUGC_GetItemInstallInfo`, `SteamAPI_ISteamUtils_IsAPICallCompleted`). Accessor version suffix varies by DLL → probe `v010..v025`.
- Workshop content: `<library>/steamapps/workshop/content/294100/<workshop_id>/`.
- User data dir: `%USERPROFILE%\AppData\LocalLow\Ludeon Studios\RimWorld by Ludeon Studios\` containing `Config/ModsConfig.xml`,
  `ModLists/*.rml`, `Saves/*.rws`, `Player.log`.
- `ModsConfig.xml` (UTF-8 **with BOM**):
  ```xml
  <?xml version="1.0" encoding="utf-8"?>
  <ModsConfigData>
    <version>1.6.4871 rev591</version>
    <activeMods><li>ludeon.rimworld</li>...</activeMods>
    <knownExpansions><li>ludeon.rimworld.royalty</li>...</knownExpansions>
  </ModsConfigData>
  ```
  RimWorld lowercases ids. If a local mod and a workshop mod share a packageId, the **workshop** copy's id gets suffix `_steam`.
- `.rml` (RimWorld's own saved mod list, in `ModLists/`, tab-indented, UTF-8 BOM):
  `<savedModList><meta><gameVersion/><modIds><li/></modIds><modSteamIds><li>0 or id</li></modSteamIds><modNames><li/></modNames></meta><modList><ids><li/></ids><names><li/></names></modList></savedModList>`
  (meta = the list at save time; modList = same ids/names — write both identically).
- Save files `.rws` have `<savegame><meta><gameVersion/><modIds><li/>…</modIds><modSteamIds>…</modSteamIds><modNames>…</modNames></meta>…` (can be 100MB → use `iterparse`, stop after `</meta>`).
- About.xml: `<ModMetaData>` with `packageId`, `name`, `author` or `authors/li`, `supportedVersions/li`, `description`, `modVersion`, `url`,
  `modDependencies/li{packageId,displayName,steamWorkshopUrl,downloadUrl}`, `modDependenciesByVersion/v1.6/li{…}`,
  `loadAfter/li`, `loadAfterByVersion/v1.6/li`, `loadBefore/li`, `loadBeforeByVersion/v1.6/li`, `forceLoadAfter/li`, `forceLoadBefore/li`,
  `incompatibleWith/li`, `incompatibleWithByVersion/v1.6/li`. Tags are case-sensitive in theory but treat them case-insensitively;
  files may have BOM, bad entities (`&` unescaped), junk before `<?xml`. Must never crash: fall back to regex extraction.
  `About/PublishedFileId.txt` holds the workshop id. `About/Preview.png` preview.
- RimSort DBs (verified formats):
  - Steam Workshop DB: zip `https://codeload.github.com/RimSort/Steam-Workshop-Database/zip/refs/heads/main` (3.5 MB) containing
    `Steam-Workshop-Database-main/steamDB.json` (49 MB). Raw: `https://raw.githubusercontent.com/RimSort/Steam-Workshop-Database/main/steamDB.json`.
    Format `{"version": <unix>, "database": {"<workshop_id or appid>": {url, steamName, packageId?, name?, authors? (str), gameVersions? [str],
    dependencies? {"<wid|appid>": [name, url]}, tags? [{tag, display_name}], unpublished? bool, appid? true}}}`.
    57,679 entries; only 18,639 have `packageId` (e.g. `"scenario.rsc"`, `"missing.packageid"`, `"invalid.item"` are junk → treat as unknown).
  - Community Rules: `https://raw.githubusercontent.com/RimSort/Community-Rules-Database/main/communityRules.json`
    `{"timestamp": int, "rules": {"<packageId>": {"loadAfter": {"<pid>": {"name": [..], "comment": [..]}}, "loadBefore": {...},
    "incompatibleWith": {"<pid>": {"name": str, "comment": str}}, "loadTop": {"value": true, "comment": ..}, "loadBottom": {"value": true}}}}`
    Keys are packageIds with **original case** → lowercase them.
  - Use This Instead: `https://raw.githubusercontent.com/emipa606/UseThisInstead/main/replacements.json.gz` (gzip JSON, **utf-8-sig**):
    `{"version": str, "rules": [{oldWorkshopId, oldName, oldAuthor, oldPackageId, newWorkshopId, newName, newAuthor, newPackageId, oldVersions[], newVersions[]}]}`
  - No Version Warning: `https://raw.githubusercontent.com/emipa606/NoVersionWarning/main/<ver>/ModIdsToFix.xml` → `<ModIdsToFix><li>pkgId</li>…` for ver in 1.0..1.9 (404s are fine).
  - Local RimSort copies (fallback): `%LOCALAPPDATA%\RimSort\dbs\{Steam-Workshop-Database\steamDB.json, Community-Rules-Database\communityRules.json,
    UseThisInstead\replacements.json.gz, NoVersionWarning\<ver>\ModIdsToFix.xml, userRules.json}`; also `%LOCALAPPDATA%\RimSort\settings.json`
    keys `external_steam_metadata_file_path`, `external_community_rules_file_path`.
- Steam Web API, **no key needed**:
  - `POST https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/` form `itemcount=N&publishedfileids[0]=…`
    → `response.publishedfiledetails[]` with `publishedfileid, result (1=ok, 9=not found), consumer_app_id, title, description, time_updated,
    subscriptions, favorited, file_size, preview_url, banned, tags[]` (batch ≤ 100 ids per request).
  - `POST https://api.steampowered.com/ISteamRemoteStorage/GetCollectionDetails/v1/` `collectioncount=1&publishedfileids[0]=<id>`
    → `response.collectiondetails[0].children[] {publishedfileid, sortorder, filetype}` (filetype 2 = nested collection; result 9 = not a collection).
  - With an optional user key: `GET https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/?key=…&appid=294100&search_text=…&return_metadata=1&numperpage=50&query_type=…`.
  - Keyless workshop search fallback: scrape `https://steamcommunity.com/workshop/browse/?appid=294100&searchtext=<q>&browsesort=textsearch&section=readytouseitems&p=<n>`.
- SteamCMD: `https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip`. Anonymous login can download RimWorld workshop items:
  `steamcmd +runscript <file>` where file has `@ShutdownOnFailedCommand 0`, `@NoPromptForPassword 1`, `force_install_dir "<dir>"`,
  `login anonymous`, one `workshop_download_item 294100 <id> validate` per id, `quit`. Output lines:
  `Success. Downloaded item <id> to "<path>" (<n> bytes)` / `ERROR! Download item <id> failed (<reason>).` → retry failures (Timeout is common for big items).
  Files land in `<dir>/steamapps/workshop/content/294100/<id>`.
- Network: all HTTP via `urllib.request` with a UA header `RimModManager/1.0`, timeouts, and `certifi` NOT available at runtime → rely on the
  Windows cert store (default `ssl.create_default_context()` works on Windows).

## Files and owners

```
main.py                    entry point (dispatch --steamworks worker / --workshop-browser window / --selftest / GUI)
rmm/models.py              shared types (DONE)
rmm/config.py              app dir, settings, logging, DPAPI secrets, http helper
rmm/paths.py               Steam/RimWorld/GOG detection, VDF parser, game version
rmm/mods.py                About.xml parsing, scanning, ModsConfig/.rml/.rws/text import/export, backups, share codes
rmm/database.py            RimSort DBs → SQLite (+FTS5), fallbacks, queries, Steam details cache
rmm/steam.py               Steam Web API, workshop search, SteamCMD, Steamworks (ctypes) worker, steam:// + browser
rmm/browser.py             Steam Workshop browser: pywebview (WebView2) window process + page script, picks as JSON lines
rmm/textures.py            todds texture optimizer (download, run, clean, remove half-written .dds after a stop)
rmm/sorting.py             dependency resolution, load-order sort, issue detection
rmm/ai.py                  LLM providers + anti-hallucination modlist generator (+ offline generator)
rmm/core.py                Manager: the single façade the GUI uses
rmm/gui/app.py             main window, theme, background runner, events, status bar, dialogs helpers
rmm/gui/theme.py           colors/fonts/ttk style setup (dark + light)
rmm/gui/mods_tab.py        main mod manager tab (inactive/active lists, details, issues)
rmm/gui/workshop_tab.py    browse/search the mod database + workshop
rmm/gui/ai_tab.py          AI generator tab
rmm/gui/modlists_tab.py    saved modlists (profiles), import/export
rmm/gui/downloads_tab.py   download queue/log, update checks, SteamCMD management
rmm/gui/tools_tab.py       Player.log viewer, backups, database status
rmm/gui/settings_tab.py    settings
rmm/gui/widgets.py         small helpers shared by the tabs (startup guard, placeholders, sortable trees, relative times)
tests/selftest.py          runs every module's _selftest()
build.bat                  venv + PyInstaller onefile build
```

## main.py

```python
# python main.py                 -> GUI (rmm.gui.app.main())
# main.exe --steamworks '<json>' -> sys.exit(rmm.steam.steamworks_worker_main(json))   (no GUI, no console window)
# main.exe --workshop-browser <args.json> -> sys.exit(rmm.browser.browser_main(path))  (the Workshop window, no Tk)
# main.exe --selftest [--online] -> runs tests/selftest logic (imports each module's _selftest), prints results, exit code
```
Must work both frozen (PyInstaller, `sys.frozen`) and from source. Imports of GUI modules happen only in GUI mode.

## Testing rules (IMPORTANT)

- NEVER write to the real `ModsConfig.xml`, real `ModLists/`, the real game `Mods/` folder or the real workshop folder in tests. Copy to a temp dir.
- NEVER subscribe/unsubscribe on the user's Steam account in tests. Steamworks may only be exercised read-only (`list_subscribed`, `state`).
- Reading the real install (scan, parse, read ModsConfig) is fine and encouraged.
- Use `.venv\Scripts\python.exe` (has `anthropic` + `pyinstaller`). Scratch files go in `%TEMP%`.

## rmm/config.py

```python
APP_DIR: str            # resolved at import: <exe_dir>/data if a file 'portable.txt' sits next to the exe/main.py, else %APPDATA%/RimModManager. Created.
def app_path(*parts) -> str                 # join under APP_DIR, creating parent dirs of the result
def get_logger() -> logging.Logger          # 'rmm', rotating file APP_DIR/logs/app.log (1MB x 3) + stderr when not frozen
DEFAULTS: dict  # keys below
class Settings:
    def __init__(self, path: str | None = None)   # default app_path('settings.json'); loads, merges DEFAULTS, tolerates corrupt file (backs it up)
    def get(self, key, default=None)
    def set(self, key, value) -> None             # does not save
    def save(self) -> None                        # atomic write (tmp + os.replace)
    def get_secret(self, key) -> str              # decrypt DPAPI (base64 stored under key); '' on failure
    def set_secret(self, key, value: str) -> None # encrypt with DPAPI (CryptProtectData via ctypes); if DPAPI unavailable store 'plain:'+value
def http_get(url, timeout=30, headers=None) -> bytes              # raises OSError subclasses on failure; UA header set
def http_post_form(url, data: dict, timeout=30) -> bytes
def download_file(url, dest, progress: Progress = noop_progress, timeout=60, cancel: threading.Event|None=None) -> str   # streams to dest+'.part' then replace; progress by Content-Length
def open_path(path) -> None    # os.startfile on files/dirs/URLs
def is_process_running(exe_name: str) -> bool   # tasklist /FI, CREATE_NO_WINDOW
NO_WINDOW: int  # subprocess.CREATE_NO_WINDOW on Windows else 0
```
DEFAULTS keys: `game_dir:""`, `config_dir:""`, `workshop_dir:""`, `extra_mod_dirs:[]`, `steamcmd_dir:""` (→ app_path('steamcmd')),
`download_method:"steamcmd"` (`steamcmd|steamworks|steam_client|browser`), `install_mode:"move"` (`move|copy|link`),
`steamcmd_retries:3`, `ai_provider:"anthropic"` (`anthropic|openai_compat|offline`), `ai_preset:"Anthropic (Claude)"`,
`ai_model:"claude-opus-5"`, `ai_base_url:""`, `ai_effort:"medium"`, `ai_live_verify:true`, `sort_tiebreak:"current"` (`current|alphabetical`),
`theme:"dark"`, `db_auto_update_days:3`, `launch_via_steam:true`, `launch_args:""`, `backup_keep:30`, `window_geometry:""`,
`last_tab:0`, `browser_batch:10`, `show_incompatible_versions:false`. Secrets (via get/set_secret): `ai_api_key`, `steam_api_key`.

## rmm/paths.py

```python
def parse_vdf(text: str) -> dict                       # Valve KeyValues → nested dicts (keys as-is; handles escapes, comments, conditionals ignored)
def find_steam() -> str | None                          # registry HKCU/HKLM, then default Program Files paths; normalized abs path or None
def steam_libraries(steam_path: str | None = None) -> list[str]   # from libraryfolders.vdf (+ steam_path itself); existing dirs only; deduped
def find_game_dir(settings=None) -> str | None          # settings['game_dir'] if valid → Steam appmanifest in libraries → GOG registry → common paths
                                                        # (Program Files (x86)/Steam/steamapps/common/RimWorld, C:/GOG Games/RimWorld, D:/..., etc.)
def is_game_dir(path: str) -> bool                      # has Data/Core/About/About.xml or RimWorldWin64.exe
def find_workshop_dir(game_dir: str | None, settings=None) -> str | None   # settings → sibling steamapps of game_dir → every library
def find_config_dir(settings=None) -> str | None        # settings → LocalLow path above (…/Config). Returns the Config folder.
def user_data_dir(config_dir: str) -> str               # parent of Config ('RimWorld by Ludeon Studios')
def game_version(game_dir: str | None) -> str           # Version.txt first line stripped, '' if unknown
def short_version(v: str) -> str                        # '1.6.4871 rev590' -> '1.6' ; '' -> ''
def game_exe(game_dir: str) -> str | None               # RimWorldWin64.exe (or RimWorldWin.exe)
def steam_api_dll(game_dir: str) -> str | None          # RimWorldWin64_Data/Plugins/x86_64/steam_api64.dll (or …/Plugins/steam_api64.dll)
def rimsort_dbs_dir() -> str | None                     # %LOCALAPPDATA%/RimSort/dbs if exists
```

## rmm/mods.py

```python
def parse_about(mod_dir: str, game_version_short: str = "") -> dict
    # returns keys: package_id(lower, '' if missing), name, authors[list], supported_versions[list], description, mod_version, url,
    # dependencies[list[ModDependency]] (a byVersion 'v<short>' list REPLACES the generic one, even when empty — RimWorld 1.6;
    #   deduped by package_id; ModDependency.alternatives = lowercased alternativePackageIds (any of them satisfies it, see .ids());
    #   enforced=False when RimWorld's ValidateDependencies drops it: bad id format, no displayName, or no steamWorkshopUrl/downloadUrl),
    # load_after, load_before (byVersion replaces generic; force* appended), incompatible_with (lists of lower ids), workshop_id (PublishedFileId.txt), preview_path, parse_error
    # Never raises. Regex fallback when XML is broken.
def scan_mod_dir(path: str, source: str, game_version_short: str = "") -> Mod | None   # one folder; None if not a mod (no About/About.xml and not a Data/* official)
def scan_all(game_dir: str | None, workshop_dir: str | None, extra_dirs: list[str], steamcmd_content_dir: str | None,
             game_version_short: str = "", progress: Progress = noop_progress) -> dict[str, Mod]
    # Sources in order: game Data/* ('core' for Core, 'dlc' others), game Mods/* ('local'), workshop_dir/* ('workshop'),
    # steamcmd_content_dir/* ('steamcmd'), extra dirs ('extra').  Keyed by uid.
    # Duplicate package ids: first keeps plain id; a 'workshop' duplicate of a non-workshop mod gets uid package_id+'_steam'
    # (RimWorld behaviour); any other duplicate gets uid package_id+'_dup<N>' and parse_error notes it.
    # Missing packageId fallback: workshop_id-derived 'workshop.<id>' is NOT used — instead use folder name lowercased with a note in parse_error.
    # Uses a thread pool (max 8) for parsing; must handle 1000+ folders in < ~2s on SSD.
def read_mods_config(config_dir: str) -> tuple[list[str], list[str], str]   # (active ids lowercased, knownExpansions, version); ([],[],'') if missing
def write_mods_config(config_dir: str, active: list[str], known_expansions: list[str] | None, version: str,
                      backup_dir: str | None = None, keep: int = 30) -> str | None
    # Backs up existing file to backup_dir/ModsConfig-YYYYmmdd-HHMMSS.xml (returns that path), prunes to `keep`, writes UTF-8 BOM atomically.
    # Preserves existing knownExpansions when known_expansions is None. Never writes an empty activeMods unless active is explicitly [].
def list_backups(backup_dir: str) -> list[str]           # newest first
def restore_backup(backup_path: str, config_dir: str, backup_dir: str) -> None   # backs up current first
def read_rml(path) -> ModList
def write_rml(ml: ModList, path, game_version: str, installed: dict[str, Mod] | None = None) -> None   # modSteamIds from ref.workshop_id / DLC appids / '0'
def read_save_meta(path) -> ModList                      # .rws via iterparse (stop at </meta>)
def read_modsconfig_file(path) -> ModList                # any ModsConfig-format xml (also RimPy/RimSort exports that use <activeMods>)
def parse_text_list(text: str) -> list[ModRef]           # one item per line/whitespace/comma: workshop URLs (id=\d+), steam://…/<id>,
                                                         # bare digits (>=5 digits → workshop id), package ids (contain '.'); dedupe keep order
def write_text_list(ml: ModList, path) -> None           # "name | package_id | https://steamcommunity.com/sharedfiles/filedetails/?id=…" per line + header comment
def write_html_list(ml: ModList, path) -> None           # standalone page: table with links (workshop page + steam:// link), "open all" button (JS) — the browser method
def encode_share_code(ml: ModList) -> str                # 'RMM1:' + base64url(zlib(json [['', '', list name], [pid, wid, name]…]))
def decode_share_code(code: str) -> ModList              # raises ValueError on bad code; name = the sender's list name, else 'Shared list'
                                                         # (codes may be line-wrapped; pids with whitespace/markup/control chars -> '')
def load_modlist_json(path) -> ModList ; def save_modlist_json(ml, path) -> None   # our own format (ModList.to_dict), atomic
def import_any(path: str) -> ModList                     # dispatch by extension/content: .rml, .rws, .json (ours or RimSort/RimPy-ish lists), .xml (ModsConfig-like), .txt
def mod_to_ref(m: Mod) -> ModRef
```

## rmm/database.py

```python
class ModDatabase:
    def __init__(self, path: str | None = None)          # default config.app_path('moddb.sqlite'); creates schema; thread-safe (one connection, check_same_thread=False, a Lock)
    def status(self) -> dict                             # {'steam_count', 'steam_version', 'rules_count', 'rules_timestamp', 'uti_count', 'nvw_count', 'updated_at' (float), 'source': str}
    def is_empty(self) -> bool
    def needs_update(self, max_age_days: float) -> bool
    def update(self, progress: Progress = noop_progress, cancel=None, force: bool = False) -> str
        # A call made while another update runs waits for it (cancel stops the wait) and, unless force, returns its summary.
        # Fetch all four DBs with fallbacks, each independently (one failing must not block others):
        # steamDB: codeload zip → raw JSON → jsDelivr (https://cdn.jsdelivr.net/gh/RimSort/Steam-Workshop-Database@main/steamDB.json)
        #          → local RimSort copy → keep existing rows.  Conditional GET / skip when 'version' unchanged unless force.
        # rules:   raw → jsDelivr → local RimSort copy → keep existing.   UTI: raw → local.   NVW: raw per version → local.
        # Rebuild inside one transaction per table (DELETE + executemany), then rebuild FTS. Returns a human summary. Raises only if
        # the DB is still empty after all fallbacks.
    def import_steamdb_file(self, path) -> int ; def import_rules_file(self, path) -> int   # manual import (Settings/Tools "Load DB from file")
    def get(self, workshop_id: str) -> DbMod | None
    def get_many(self, ids: list[str]) -> dict[str, DbMod]
    def by_package_id(self, package_id: str) -> list[DbMod]           # case-insensitive; published first
    def search(self, query: str, version: str = "", limit: int = 50, offset: int = 0, require_package_id: bool = False,
               sort: str = "relevance") -> list[DbMod]
        # FTS5 over name, steam_name, authors, package_id (prefix match per token, AND); fallback LIKE on FTS syntax error;
        # version filter: game_versions contains version (e.g. '1.6'); excludes unpublished; sort 'relevance'|'popular'(cached subscriptions desc)|'name'.
        # Empty query + sort='popular' lists most-subscribed cached rows.
    def rules(self) -> dict[str, dict]                   # community rules, lowercased: {pid: {'load_after': {pid: comment}, 'load_before': {...},
                                                         #   'incompatible_with': {...}, 'load_top': bool, 'load_bottom': bool}}
    def replacement_for(self, workshop_id: str = "", package_id: str = "") -> dict | None   # UTI rule dict (keys as in source) or None
    def no_version_warning(self, version_short: str) -> set[str]     # lowercased package ids
    def cache_details(self, details: dict[str, dict]) -> None        # store Steam API publishedfiledetails json by id (+fetched_at)
    def store_items(self, items: dict[str, dict]) -> int
        # upsert Steam PublishedFileDetails like the Steam sync (versions from tags, deps from children, details cached)
        # without moving steam_sync_at; returns how many were new
    def cached_details(self, ids: list[str], max_age_s: float = 86400) -> dict[str, dict]
```
DbMod.kind ('translation' | 'scenario' | '') comes from the steamDB tags. DbMod enrichment: `get/get_many/search` fill `subscriptions/time_updated/description/preview_url` from the details cache when present.

## rmm/steam.py

```python
WORKSHOP_URL = "https://steamcommunity.com/sharedfiles/filedetails/?id={}"
def get_details(ids: list[str], db=None, max_age_s: float = 3600) -> dict[str, dict]
    # GetPublishedFileDetails in batches of 100; uses db.cached_details/cache_details when db given; missing/failed ids omitted.
def _collection_tree(ids) -> dict[str, list[str]] ; def _flatten(ids, tree) -> list[str]   # used by Manager.import_text
                                                                # ({collection: children}, nested ones resolved; raises offline)
def search_workshop(query: str, page: int = 1, api_key: str = "") -> list[dict]
    # [{'workshop_id','title','author'?, 'subscriptions'?}]; QueryFiles when api_key else HTML scrape; [] on failure (never raises)
def query_items(text, key, version="", n=50) -> list[dict]   # QueryFiles text search, requiredtags=version, raw details
def user_items(steamid, key, max_pages=5) -> list[dict]      # GetUserFiles: one uploader's RimWorld items, 100 per page
def item_details(ids, key) -> dict[str, dict]                # IPublishedFileService/GetDetails with tags, children, creator
    # all three: raw PublishedFileDetails (tags, children = required items), RimWorld + result 1 only; raise on network
    # failure; the key is in the URL, so callers log exception types only
def workshop_url(wid) -> str
def open_in_browser(ids: list[str]) -> None                     # webbrowser.open_new_tab each
def open_in_steam(ids: list[str]) -> None                       # steam://url/CommunityFilePage/<id> via os.startfile
class SteamCMD:
    def __init__(self, root_dir: str)
    exe: str; content_dir: str                                  # <root>/steamcmd.exe ; <root>/steamapps/workshop/content/294100
    def is_installed(self) -> bool
    def install(self, progress: Progress = noop_progress, cancel=None) -> None   # download zip, extract, run '+quit' once (self-update, stream output to progress)
    def download(self, ids: list[str], progress: Progress = noop_progress, cancel=None, retries: int = 3, batch: int = 40) -> dict[str, DownloadResult]
        # runscript per batch; subprocess with CREATE_NO_WINDOW, stdout parsed line by line (progress text = line); retry failed ids;
        # result path = content dir of the item. cancel.set() kills the process.
def install_items(results: dict[str, DownloadResult], mods_dir: str, mode: str = "move",
                  targets: dict[str, str] | None = None) -> dict[str, DownloadResult]
    # move/copy/junction(link: `cmd /c mklink /J`) each successful item into <mods_dir>/<id>, or into targets[id] (an existing folder
    # directly inside mods_dir; core passes targets only for update=True downloads with the same packageId, after backing the old
    # folder up); updates .path. Crash-safe: built as '<name>.rmm_new_<ts>' in <parent of Mods>/_RimModManager_staging (same volume,
    # so RimWorld never sees a half-copied mod), then swapped into Mods with renames; the old copy is parked there as
    # '<name>.rmm_old_<ts>' only until the swap succeeded. Only ever replaces a folder directly inside mods_dir named by the id or in targets.
def cleanup_stale_installs(mods_dir: str) -> list[str]
    # after a crash/kill: a lone '<name>.rmm_old_*' whose <name> is missing is restored; other '*.rmm_old_*'/'*.rmm_new_*' (in the
    # staging folder, or legacy ones inside Mods) are deleted (junction-safe). Returns messages; [] without touching anything while an
    # install_items call runs. Manager.scan/download call it.
def kill_children() -> None       # kill every live SteamCMD / Steamworks helper started by this module (also registered with atexit)
def steamworks_available(game_dir: str | None) -> bool
def steamworks_call(action: str, ids: list[str], game_dir: str, progress: Progress = noop_progress, timeout: float = 600,
                    cancel=None) -> dict      # cancel.set() kills the helper promptly → {'ok': False, 'error': 'cancelled', ...}
    # Spawns the worker in a SUBPROCESS (so Steam sees 'RimWorld running' only briefly): frozen → [sys.executable, '--steamworks', ...],
    # dev → [sys.executable, <main.py>, '--steamworks', ...]; args passed as one JSON string; env SteamAppId=294100, SteamGameId=294100.
    # Worker prints JSON lines {"progress": f, "text": s} and a final {"result": {...}}. actions: 'subscribe', 'unsubscribe', 'list_subscribed',
    # 'state' (per-id flags), 'download' (subscribe + DownloadItem + wait until installed or timeout).
    # Returns {'ok': bool, 'error': str, 'items': {id: {...}}, 'subscribed': [...]}
def steamworks_worker_main(arg_json: str) -> int                  # the worker body (ctypes; SteamAPI_Init; probe SteamUGC/SteamUtils accessor versions;
                                                                  # RunCallbacks loop; SteamAPI_Shutdown in finally). Called by main.py.
```

## rmm/sorting.py

```python
TIER_TOP, TIER_MID, TIER_BOTTOM = 0, 1, 2
def rules_from_json(data) -> dict        # RimSort userRules/communityRules JSON or our user rules -> the rules shape below (lowercased,
                                         # case-variant keys merged, empty rules dropped); used for user_rules.json and Tools imports
def merge_rules(base: dict, extra: dict) -> tuple[dict, int]   # (merged copy, number of rules added)
# Load-order edges: dependencies, load_after, load_before (reversed), community + user rules (rules shape:
# {pid: {'load_after': {pid: comment}, 'load_before': {...}, 'incompatible_with': {...}, 'load_top': bool, 'load_bottom': bool}});
# rule/mod ids are package ids → every active uid with that package_id; user rules beat opposite edges.
def sort_order(active: list[str], installed: dict[str, Mod], rules: dict | None = None, user_rules: dict | None = None,
               tiebreak: str = "current") -> tuple[list[str], list[str]]
    # (sorted uids, warnings). Tiers: Harmony first, then Core, then DLCs in canonical order, then load_top mods (+ what they need),
    # then the rest, then load_bottom mods. Kahn's algorithm per tier with a heap keyed by (current index | lowercased name).
    # Cycles: emit the ready-less node with the fewest remaining preds (ties by key), add a warning naming the cycle members.
    # Unknown (not installed) uids keep their relative position at the end of the mid tier.
def resolve_dependencies(active: list[str], installed: dict[str, Mod], db=None, version_short: str = "") -> tuple[list[str], list[ModRef], list[str]]
    # (uids to add that are installed, refs to download (not installed; workshop id: the About.xml steamWorkshopUrl only competes with
    #  the DB uploads of that package id — ranked by published, supports the version, is the link, already on disk, DB order — so a
    #  stale link to a legacy upload is not followed; Harmony is always 2009463077),
    #  unresolvable messages). Recursive (deps of deps, and the DB deps of download refs with the download_closure rules). DLC deps are never
    # "download" refs: missing DLC → unresolvable message. Never adds something already active. A dependency is met by its package id or any
    # alternative. Only loadable copies (source core/dlc/local/workshop) are activated; a 'steamcmd'/'extra' copy is downloaded instead.
def download_closure(refs: list[ModRef], installed: dict[str, Mod], db=None, version_short: str = "") -> list[ModRef]
    # refs (in order, deduped) + the Steam 'required items' (DB deps) that still need downloading, walked one level at a time and never
    # through a met one (installed loadable mod or another ref by package/workshop id, a variant upload like '… - Dev Snapshot', or superseded
    # by its Use-This-Instead replacement). Outdated deps are swapped for a UTI replacement that supports the version. Never DLC/app ids.
def check(active: list[str], installed: dict[str, Mod], db=None, version_short: str = "", owned_dlcs: set[str] | None = None,
          user_rules: dict | None = None) -> list[Issue]
```
### Issue kinds and fixes
`missing_core` (error, fix `{'action':'add','uids':['ludeon.rimworld']}`), `not_installed` (error; fix `{'action':'download','refs':[ModRef]}` when a
workshop id is known else `{'action':'remove','uids':[uid]}`), `missing_dependency` (error; fix `add` if installed else `download`;
'warning' when RimWorld does not enforce that dependency, see ModDependency.enforced),
`not_in_game` (error: an active copy RimWorld will not load — a `_dupN` duplicate folder → fix `replace` with the plain uid, or `remove`
when that is active; a 'steamcmd' cache copy → `download`; an 'extra'-folder copy → `{'action':'install_local','uids':[uid]}`, which
junctions (else copies) it into <game>/Mods and rescans),
`dlc_missing` (error, no fix), `incompatible` (error, related=[other uid], fix `{'action':'remove','uids':[uid]}`),
`duplicate` (error, same package id active twice), `order` (warning, fix `{'action':'sort'}`), `cycle` (warning),
`version` (warning: mod's supported_versions lacks version_short and pid not in db.no_version_warning), `replacement` (info: UTI suggests a
newer mod, fix `{'action':'replace','uid':uid,'ref':ModRef}`), `no_packageid` (warning: parse_error set).
Sorted by level (error, warning, info) then by active index.

## rmm/ai.py

```python
PRESETS: dict[str, dict]   # name -> {'provider','base_url','model','key_url','needs_key'}:
    # 'Anthropic (Claude)' anthropic, model 'claude-opus-5'; 'OpenAI' https://api.openai.com/v1; 'OpenRouter' https://openrouter.ai/api/v1;
    # 'Groq' https://api.groq.com/openai/v1; 'Google Gemini' https://generativelanguage.googleapis.com/v1beta/openai;
    # 'Ollama (local)' http://localhost:11434/v1 (no key); 'LM Studio (local)' http://localhost:1234/v1 (no key); 'Custom (OpenAI-compatible)';
    # 'Offline (no AI)' provider 'offline'.
CLAUDE_MODELS = ['claude-opus-5', 'claude-sonnet-5', 'claude-haiku-4-5', 'claude-opus-5-5']
class LLMError(Exception)
class LLM:
    def __init__(self, provider: str, model: str, api_key: str = "", base_url: str = "", effort: str = "medium")
    def complete_json(self, system: str, user: str, schema: dict, max_tokens: int = 16000) -> dict
        # anthropic: `import anthropic` lazily; client.beta.messages.create(model, max_tokens, system, messages,
        #   thinking={'type':'adaptive'} (omit for claude-haiku-4-5), output_config={'effort': effort, 'format': {'type':'json_schema','schema': schema}}
        #   (omit effort for haiku), and for claude-opus-5*/claude-fable-*: betas=['server-side-fallback-2026-07-01'], fallbacks='default'
        #   (pass via extra_body if the installed SDK rejects the kwarg)). Check stop_reason=='refusal' → LLMError; read first text block → json.loads.
        #   If the schema is rejected (400), retry once without output_config.format and parse JSON from text.
        # openai_compat: urllib POST {base}/chat/completions, response_format json_schema → on 400 json_object → on 400 none; parse the first
        #   JSON object from the text robustly (strip ``` fences). Bearer key if given.
        # Timeouts 180s; friendly LLMError messages for 401/403/404/429/connection refused (e.g. "Is Ollama running?").
    def list_models(self) -> list[str]     # anthropic: client.models.list(); openai_compat: GET {base}/models; [] on failure
class GenRequest: prompt: str; version_short: str; target_count: int = 40; owned_dlcs: set[str]; exclude_workshop_ids: set[str];
                  require_package_id: bool = False; live_verify: bool = True; include_popular_qol: bool = True;
                  installed_ids: set[str]; steam_key: str = "" (repr=False)   (dataclass)
class Pick: workshop_id: str; package_id: str; name: str; reason: str; source: str  # 'ai' | 'dependency' | 'offline' | 'replacement' | 'link'   (dataclass)
class GenResult: picks: list[Pick]; rejected: list[tuple[str, str]]; queries: list[str]; summary: str; log: list[str]   (dataclass)
def generate(req: GenRequest, db, llm: LLM | None, progress: Progress = noop_progress, cancel=None,
             details_fn=None, workshop=None) -> GenResult
    # llm None or provider 'offline' → offline generator. details_fn defaults to steam.get_details (injectable for tests).
    # workshop (default _Workshop(req.steam_key) when req.live_verify, else None): the live Steam Workshop —
    #   search(text, version) / author(steamid) need the key; details(ids) / collections(ids) work without it.
    #  0 LINKS: workshop links (also steam://…/CommunityFilePage/<id>) / bare 8-20 digit ids in the prompt, collections
    #    expanded → picks with source 'link'. Ids or names in negated phrases ("except <link>", "without Combat Extended",
    #    via _negations) are left out; removed/banned/DLC/conflicting ones rejected; one without the game version is
    #    swapped for its Use-This-Instead successor or kept with a warning. The AI then fills target - those picks.
    #    LIVE (key): each query's QueryFiles title matches, plus the GetUserFiles page of an author a query names (2-3 DB
    #    mods by one single-author name, matched by that name not the title, re-uploads skipped, all from ONE uploader).
    #    Only mod items (file_type 0 / keyless: creator app 294100). Keyed items the DB lacks are stored first via
    #    db.store_items, then go through every check below; keyless details (no required items) are rows for this run
    #    only. Stops asking Steam on cancel or the first network/429/401/403 error (sets steam_down when unreachable).
    #    Dependencies the DB lacks: workshop.details. Table notes mark req.installed_ids "installed".
    #    _offline_queries ignores links and ids (no 'https'/'com' search words).
    # Pipeline (anti-hallucination — the model NEVER supplies package ids or names that we trust):
    #  1 PLAN: LLM returns {'queries':[{'query','category','want'}], 'avoid':[str], 'notes'} (≤ 30 queries) from the user prompt + version + DLCs.
    #  2 RETRIEVE: db.search(query, version=req.version_short, limit=25) per query (+ 'avoid' terms filter names); dedupe;
    #    enrich with details_fn (subscriptions/description) — failures tolerated; keep ≤ 12 per query by popularity, ≤ 300 total.
    #  3 SELECT: LLM gets a compact candidate table (id | name | authors | subs | 160-char description) and must return
    #    {'picks':[{'id','reason'}],'summary'} where schema 'id' has "enum": candidate ids (drop enum if > 400 candidates).
    #  4 VALIDATE: keep only ids in the candidate set AND in the DB AND not unpublished AND supporting version; every other id → rejected
    #    with reason ('not offered to the model', 'unknown id', …). Names/package ids come from the DB row only.
    #  5 LIVE VERIFY (req.live_verify): details_fn → result==1, consumer_app_id==294100, banned==0; failures rejected; network failure → skip with log line.
    #  6 DEPENDENCIES: sorting.download_closure (same rules as the Download buttons) → add missing deps as source 'dependency';
    #    DLC deps not owned → log warning.
    #  7 REPLACEMENTS: db.replacement_for(old) when the pick doesn't support version → swap to replacement (source 'replacement').
    #  Offline generator: keywords from the prompt (stopwords removed) + a small built-in theme→keywords map (medieval, scifi, performance,
    #  qol, combat, medical, animals, building, factions, storytelling, graphics, …) → db.search per keyword (version filtered) → rank by
    #  subscriptions (details_fn) → top req.target_count.
```

## rmm/core.py — the façade (GUI talks ONLY to this + models)

```python
class Manager:
    def __init__(self, settings: Settings | None = None, db: ModDatabase | None = None)
    settings; db; log (logger)
    game_dir: str | None; config_dir: str | None; workshop_dir: str | None; game_version: str; version_short: str
    installed: dict[str, Mod]          # uid -> Mod (after scan)
    active: list[str]                  # working copy of the active list (uids), edited by the GUI, saved with save_active_to_game()
    known_expansions: list[str]; dirty: bool
    user_rules: dict                   # APP_DIR/user_rules.json, same shape as community rules (pid -> {'load_after': {...}, ...})
    # paths / scan
    def detect_paths(self) -> dict                     # {'game_dir','config_dir','workshop_dir','game_version','steam'} (fills attributes)
    def scan(self, progress=noop_progress) -> dict[str, Mod]
    def owned_dlcs(self) -> set[str]                   # DLC package ids with a Data/<name> folder
    def db_for_mod(self, mod_or_uid) -> DbMod | None   # by workshop id then package id
    # active list editing (all set dirty=True; never duplicate uids)
    def load_active_from_game(self) -> list[str]       # read ModsConfig.xml → active (keeps unknown ids), dirty False
    def save_active_to_game(self, force: bool = False, running_ok: bool = False) -> str
                                                                # raises GameRunningError if RimWorld is running and not force/running_ok
                                                                # (GUI 'Save anyway' = running_ok: the external-change check still runs);
                                                                # ExternalChangeError if ModsConfig.xml changed on disk since the app loaded/saved it
                                                                # (GUI: Overwrite = force / Reload = load_active_from_game / Cancel);
                                                                # RuntimeError('The mod list has not been loaded yet') before the first load;
                                                                # writes the installed game version; write_mods_config with backups in app_path('backups');
                                                                # returns backup path ('' if none); dirty False unless active changed during the write
    def set_active(self, uids: list[str]) -> None
    def activate(self, uids: list[str], index: int | None = None) -> None
    def deactivate(self, uids: list[str]) -> None
    def move(self, uids: list[str], to_index: int) -> None     # move block, preserving their relative order
    def auto_sort(self) -> list[str]                            # sorts self.active using db.rules() + user_rules + settings sort_tiebreak; returns warnings
    def check(self) -> list[Issue]
    def apply_fix(self, issue: Issue) -> list[ModRef]           # performs add/remove/sort/replace; returns refs that still need downloading
    def resolve_dependencies(self) -> tuple[list[str], list[ModRef], list[str]]   # adds installed deps to active; returns (added, to_download, unresolvable)
    def undo(self) -> bool ; def redo(self) -> bool             # snapshot stack of self.active (max 50)
    # modlists (profiles), stored as APP_DIR/modlists/<safe name>.json
    def list_modlists(self) -> list[ModList]
    def save_modlist(self, ml: ModList, overwrite: bool = False) -> None   # never clobbers a DIFFERENT list whose file name collides (case,
                                                                # '_' mapping, truncation): renames ml.name to 'Name (2)'…; callers read ml.name back
    def unique_modlist_name(self, name: str) -> str ; def delete_modlist(self, name: str) -> None ; def rename_modlist(self, old, new) -> None
    def modlist_from_active(self, name: str) -> ModList
    def apply_modlist(self, ml: ModList) -> list[ModRef]        # set active to installed matches (match by package_id, then workshop id, then '_steam' uid) + returns missing refs
                                                                # a list naming no Core/DLC keeps the currently active Core/DLCs (after a leading Harmony)
    def match_ref(self, ref: ModRef) -> str | None              # uid of installed mod matching the ref
    def refs_status(self, refs: list[ModRef]) -> list[tuple[ModRef, str]]   # status 'installed' | 'missing' | 'unknown' (no workshop id & not installed)
    def import_path(self, path: str) -> ModList                 # mods.import_any + fill missing names/workshop ids from installed/DB
    def import_text(self, text: str, progress=noop_progress) -> ModList   # share code (anywhere in the text, any case, wrapped) / text list /
                                                                # collection URLs (children spliced in place via steam._collection_tree)
    # refs without a workshop id and no installed match (.rml '0' ids) get one from a unique exact DB title match (name/steam_name,
    # case/punctuation-insensitive; unique among rows supporting the version, else overall); values only ever come from DB rows
    def export_modlist(self, ml: ModList, path: str, fmt: str) -> None      # fmt: 'rml' | 'modsconfig' | 'txt' | 'json' | 'html'
    def export_to_game_modlists(self, ml: ModList, overwrite: bool = False) -> str   # write <user_data>/ModLists/<name>.rml, returns path;
                                                                # FileExistsError on a same-name .rml (any case) unless overwrite (backed up first)
    def expand_download_deps(self, refs: list[ModRef]) -> list[ModRef]   # sorting.download_closure; every Download button uses it
    def game_modlists(self) -> list[str]                        # paths of .rml in <user_data>/ModLists
    def share_code(self, ml: ModList) -> str
    # downloads
    def missing_refs(self, refs: list[ModRef] | None = None) -> list[ModRef]   # default: active uids not installed (+ unresolved deps), with workshop ids from DB
    def download(self, refs: list[ModRef], method: str | None = None, progress=noop_progress, cancel=None, *, activate: bool = False,
                 with_deps: bool = True, update: bool = False) -> list[DownloadResult]
        # with_deps: expand_download_deps(refs) first, and after installing also fetch the About.xml dependencies Steam does not list
        #   (results include them). An explicit digit workshop id is never swapped for an installed copy's; deduped by workshop id.
        # activate: afterwards activate every downloaded mod (+ its installed deps) not active yet, then auto_sort().
        # update: only then a mod installed as Mods/<other name> is replaced in place, and only when the download has its packageId
        #   (the old folder is copied to <data>/backups/Mods first); otherwise every download goes to Mods/<id>.
        # method default settings['download_method']: 'steamcmd' (install if needed, then install_items into <game>/Mods per install_mode),
        # 'steamworks' (steam.steamworks_call 'download'), 'steam_client' (open_in_steam, results ok=False message 'opened in Steam'),
        # 'browser' (open_in_browser in batches of settings browser_batch, same). Refs without workshop id → failed result with message.
        # Rescans afterwards when anything was installed.
    def check_updates(self, progress=noop_progress) -> list[tuple[Mod, int]]   # installed workshop/steamcmd/local mods with a workshop id whose Steam
                                                                              # time_updated > mod.mtime (+ 60s slack) → (mod, remote time)
    def steamcmd(self) -> SteamCMD
    # game
    def launch_game(self) -> None                               # steam://rungameid/294100 (launch_via_steam) or game exe + launch_args
    def is_game_running(self) -> bool
    def backups(self) -> list[str] ; def restore_backup(self, path: str) -> None   # then reloads active
    def read_player_log(self, max_entries: int = 2000) -> list[LogEntry]   # <user_data>/Player.log; group error/warning blocks; attribute mods by
                                                                           # package id / assembly stem / folder name mentions; collapse duplicates
    # database
    def update_db(self, progress=noop_progress, cancel=None, force=False) -> str     # GUI: only through App.update_db (one job, one Cancel)
    def close(self) -> None                                     # app exit: steam.kill_children(), close the DB
    def search_db(self, query: str, **kw) -> list[DbMod]        # db.search with version default self.version_short
    def generate_ai(self, prompt: str, target_count: int = 40, progress=noop_progress, cancel=None, **kw) -> GenResult   # builds LLM from settings
class GameRunningError(Exception)
```

## rmm/gui — tkinter, clean & light

- `app.py`: `class App` — `root` (tk.Tk), `mgr` (Manager), `style`, `notebook` with tabs in order: **Mods, Modlists, Workshop, AI Generator,
  Downloads, Tools, Settings**. API used by tabs:
  - `app.run_bg(fn, *args, on_done=None, on_error=None, busy: str = "", progress: bool = False, cancellable: bool = False, **kw)` → runs `fn`
    on a daemon thread; if `progress=True` passes `progress=<thread-safe callback>` kwarg (updates the status bar progress) and if
    `cancellable=True` passes `cancel=<threading.Event>` (status bar shows a Cancel button). Callbacks run on the Tk thread via a queue polled
    with `root.after(50)`. Errors → `on_error(exc)` or an error dialog + log. Returns the Event.
  - `app.on(event: str, cb)` / `app.emit(event: str, *args)` — events: `'scanned'`, `'active_changed'`, `'modlists_changed'`, `'db_updated'`,
    `'settings_changed'`, `'downloads_done'`, `'theme_changed'`.
  - `app.status(text: str)`, `app.toast(text, level='info')` (transient label in status bar), `app.ask_yes_no(title, msg) -> bool`,
    `app.error(title, msg)`, `app.info(title, msg)`, `app.colors` (dict from theme), `app.fonts`, `app.select_tab(name)`.
  - `app.show_mod_details(uid=None, db_mod=None)` → switches to Mods tab and selects/shows a mod (tabs may call).
  - `app.queue_download(refs: list[ModRef], method: str | None = None, **opts)` → emits 'queue_download' (refs, method, opts); the
    Downloads tab runs `mgr.download(refs, method, **opts)` (opts kept per item for Retry) and emits 'downloads_done'. Callers: AI
    'Add to active + download rest', Modlists Apply/Download missing, Mods-tab fixes and Download links, Downloads 'Download missing
    (active)' → activate=True; Downloads 'Update selected' → update=True, with_deps=False; Workshop Download/Subscribe → no opts.
  - Actions that change the active list or download are refused (toast) until the first 'scanned' (startup load done).
  - Startup: window shows instantly; then bg: detect_paths → scan → load_active_from_game → emit 'scanned' & 'active_changed';
    DB: if empty or needs_update → bg update with progress, emit 'db_updated'. First run with no game dir found → dialog pointing to Settings.
  - Title shows `* ` when `mgr.dirty`; on close with unsaved changes ask Save/Discard/Cancel. Saves window geometry + last tab.
  - Keyboard: Ctrl+S save, Ctrl+Z/Ctrl+Y undo/redo, Ctrl+F focus search, F5 rescan.
- `theme.py`: `apply_theme(root, name: str) -> dict` (colors) using ttk 'clam' as base. Dark palette: bg `#1e1f22`, panel `#2b2d31`,
  field `#232428`, text `#e6e6e6`, muted `#9aa0a6`, accent `#5b8def`, ok `#3ba55d`, warn `#e0a526`, err `#e5484d`, select `#35507a`, border `#3a3c42`.
  Light palette equivalents. Font `Segoe UI` 10 (fallback `TkDefaultFont`), headings 11 bold, mono `Consolas` 9. Style names used by tabs:
  `Accent.TButton`, `Danger.TButton`, `Card.TFrame`, `Muted.TLabel`, `Heading.TLabel`, `Treeview` rows tags `error`, `warning`, `info`, `missing`,
  `official`, `dim` configured with colors by the tab (`tree.tag_configure`). Treeview rowheight 24. Also style tk widgets (Text, Listbox,
  Menu) via `root.option_add`.
- Tabs are classes `XxxTab(ttk.Frame)` with `__init__(self, parent, app)`; they subscribe to events and never block the Tk thread
  (anything touching disk/network/DB → `app.run_bg`). Treeviews for big lists must be refreshed efficiently (delete all + insert; for >2000
  rows insert in chunks with `after`).
- **mods_tab.py**: three columns (PanedWindow): Inactive list (search box, filter combobox All/Local/Workshop/SteamCMD/Official/Outdated),
  Active list (search box, count, drag-and-drop reorder, multi-select, double-click toggles, right-click menu: open folder, workshop page,
  open in Steam, copy package id, move to top/bottom, deactivate, show in DB), Details pane (Preview.png via tk.PhotoImage subsampled to ≤ 320px
  wide — only PNG/GIF, else placeholder; name, authors, package id, workshop id (link), source, versions (red if unsupported), dependencies
  (✔ active / ⚠ inactive / ✖ missing), load before/after, path, description scrolled text). Buttons: Activate ▶, ◀ Deactivate, ▲ ▼,
  **Auto-sort**, **Fix dependencies**, **Check**, **Save** (accent), Revert (reload from game), Clear, Undo/Redo, **Launch game**.
  Issues panel at bottom (Treeview: level icon/text, message) with "Fix" / "Fix all safe" buttons (apply_fix; download fixes → app.queue_download).
  Status line shows `N active / M installed · X errors · Y warnings`. Rows colored: missing (not installed) red, version warning amber, official muted.
- **modlists_tab.py**: left list of saved modlists; right: name, notes, mod table with status (installed/missing) column. Buttons:
  New from active, Apply to active (asks, then offers download of missing), Download missing, Rename, Duplicate, Delete, Import (file dialog
  .xml/.rml/.rws/.txt/.json/.html? → import_path; plus "Import text/URL/share code" dialog → import_text), Export (fmt choice dialog),
  (Export… also offers "RimWorld's ModLists"), Copy share code, Load from RimWorld ModLists (list game_modlists). Edit list: remove selected, move up/down.
- **workshop_tab.py**: search entry + version filter (default current, option 'Any') + sort (Relevance/Popular/Name) + checkbox
  "only with packageId" + "Live Steam search" toggle (uses steam.search_workshop when DB misses). Results Treeview (Name, Author, Versions,
  Installed ✔, Active ✔, Subs). Paging (Load more). Details panel: title, authors, package id (or "unknown until downloaded"), versions,
  dependencies list, description (fetched via steam.get_details in bg), buttons: Add to active (installed ones), Download (queue), Subscribe via
  Steam (steamworks), Open page, Open in Steam, Add to modlist… (choose). DB status line + "Update database" button.
  "Open Steam Workshop": rmm.browser.run on a bg job; each pick ('workshop_pick' event) → import_text (collections expand)
  → missing_refs → app.queue_download.
- **ai_tab.py**: provider summary line (preset/model, "change in Settings"), prompt Text (placeholder examples), target count Spinbox (5–300),
  version (current), options checkboxes: live verify, only mods with packageId, include QoL/performance essentials. Generate (accent) + Cancel.
  Progress log (Text, read-only, mono). Results Treeview with checkbox-like column (☑/☐ toggle on click), Name, Source (AI/Dependency/…),
  Reason, Installed. Rejected section (collapsible Text: "Rejected N hallucinated/invalid ids"). Buttons: Save as modlist, Add to active
  (installed ones) + queue download of the rest, Download all missing.
- **downloads_tab.py**: method selector (SteamCMD / Steam (Steamworks subscribe) / Steam client pages / Browser) with a one-line explanation
  each; SteamCMD status (installed?, path, Install/Reinstall button); install mode (move/copy/link). Queue Treeview (Name, Workshop id, Status).
  Buttons: Download missing (active), Check for updates → list → Update selected, Retry failed, Clear, Cancel. Log Text (mono) streaming progress lines.
  Implements the handler for `app.queue_download`.
- **tools_tab.py**: sub-notebook: *Game log* (load Player.log → Treeview of entries with level, count, suspected mods; detail Text;
  filter errors/warnings; search), *Backups* (list backups, Restore, Open folder), *Database* (status table, Update now, Force update,
  Import steamDB.json / communityRules.json from file, Open app data folder), *User rules* (simple editor: pick mod A "load after" mod B, list/remove rules),
  *Unused mods*, *Textures* (todds: Optimize / Remove for active or all installed mods; progress + Cancel in the status bar).
- **settings_tab.py**: Paths (game dir, config dir, workshop dir, SteamCMD dir; Browse + Auto-detect buttons; detected values shown),
  extra mod folders (list add/remove), AI (preset combobox → fills provider/base URL/model; model combobox editable + "Fetch models";
  API key entry (show/hide) stored with set_secret; "Get a key" link; effort; Test connection button), Steam Web API key (optional, secret),
  Downloads (method, install mode, retries, browser batch), Sorting tiebreak, Theme (dark/light, applies live), DB auto-update days,
  Launch (via Steam checkbox, args). Save button → settings.save() + emit 'settings_changed' (Manager re-detects paths when path fields changed).
```
