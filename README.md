# RimWorld Mod Manager

A lightweight, standalone Windows mod manager for RimWorld (1.4 – 1.6). Single portable `.exe` with zero installation, zero external runtimes, and fast startup built for weak laptops and large 400+ modlists.

[![Download on itch.io](https://img.shields.io/badge/Download-itch.io-fa5c5c?logo=itchdotio&logoColor=white)](https://therexpo.itch.io/rimmodmanager-fast-mod-manager-ai-modlist-builder)
[![Support on Ko-fi](https://img.shields.io/badge/Support-Ko--fi-ff5e5b?logo=kofi&logoColor=white)](https://ko-fi.com/theexpotentialrobloxian29574)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Download:** Get the pre-built `RimModManager.exe` on [Itch.io](https://therexpo.itch.io/rimmodmanager-fast-mod-manager-ai-modlist-builder).

Your settings, saved modlists, backups and the mod database live in `%APPDATA%\RimModManager`. To run in portable mode, place an empty `portable.txt` next to the executable.


## What it does

| Tab | What for |
|---|---|
| **Mods** | Active/inactive lists with drag and drop, search and details (preview, dependencies, versions). Also **Auto-sort** (RimSort community rules plus your own rules), **Fix dependencies**, and **Check** for missing deps, incompatibilities, wrong versions, duplicates and bad order. **Save** writes `ModsConfig.xml` and backs up the old one first. Undo/redo, and **Launch game**. **Delete…** (or right-click → Delete from disk / unsubscribe) removes mod folders and unsubscribes Steam workshop mods. |
| **Modlists** | Saved profiles: apply one, download what's missing, compare with the active list, rename, duplicate. Import `.rml`, save games (`.rws`), `ModsConfig.xml`, text/URL lists, Steam collections and share codes. Export to `.rml`, ModsConfig, txt, json, an HTML page, a share code, or straight into RimWorld's own saved mod lists (asks before replacing one, backs the old one up). |
| **Workshop** | Search the RimSort Steam Workshop database (57k mods) or search Steam live. Then add, download or subscribe. **Open Steam Workshop** shows the real workshop pages in their own window (like RimSort's browser): every mod gets a **+ Download** button, collections get **Download all**, installed mods are marked, and each click goes straight to the download queue with its dependencies. A Steam login there is remembered. |
| **AI Generator** | Describe a playthrough and get a checked modlist. See below. |
| **Downloads** | Four ways to get mods (see below), plus update checks (optionally at startup). SteamCMD and Steam downloads check free disk space first. Unfinished downloads can be resumed after a crash or close. Failed downloads can be saved as a modlist. |
| **Tools** | Player.log viewer that points at the mod causing each error, ModsConfig backups and restore, database status, your own load-order rules, **Unused mods** (find and delete installed mods that are neither active nor in any saved modlist), and **Textures**: RimSort's todds optimizer converts mod textures to DDS (faster game start, less video memory) and can remove them again. |
| **Settings** | Paths (auto-detected), AI provider and key, download method, theme. |

### Getting the full mod list (fallbacks)

- **Installed mods:** game `Data/` (Core and DLCs), game `Mods/`, every Steam library's workshop folder, and any extra folders you add. Broken `About.xml` files are still read with a lenient parser.
- **Workshop database** (tried in this order):
  1. RimSort's database, as a GitHub zip
  2. the raw JSON
  3. your local RimSort install's copy (`%LOCALAPPDATA%\RimSort\dbs`)
  4. the last cached copy
  5. a manual file import from Tools
- **Mod details and search:** live from the Steam Web API (no key needed) or scraped workshop search. A Steam Web API key is optional: set it in Settings, or the app will use a `STEAM_API_KEY` environment variable.

### Download methods

1. **SteamCMD:** anonymous, no Steam account needed. SteamCMD installs itself on first use, and mods go into the game's `Mods` folder. Mods SteamCMD can't get are subscribed through Steam instead when Steam is running (Settings can turn this off).
2. **Steam subscribe:** uses your logged-in Steam client through the Steamworks API from the game's own `steam_api64.dll`. Steam then downloads the mods itself.
3. **Steam client pages:** opens each mod page in Steam so you click Subscribe.
4. **Browser:** opens workshop pages in batches, or exports an HTML page of links.

Every download also pulls in the mod's dependencies. Downloads started from a list (AI, Apply modlist, Fix) get activated and auto-sorted when they finish.

### AI generator: why it can't invent mods

The AI never supplies an ID the app trusts:

1. The model writes search queries.
2. The app searches the local database.
3. The model picks only from that candidate table. For Claude, the allowed IDs are also enforced by the JSON schema when there are 400 candidates or fewer.
4. Each pick is re-checked against the database, and live on Steam (it must exist, not be banned, and be a RimWorld item).

Names and packageIds always come from the database, never from the model. Dependencies and "Use This Instead" replacements are added deterministically. Anything invalid is listed as rejected.

With a Steam Web API key (Settings) and "Use the live Steam Workshop" on, the generator also reaches past the database:
- **Live search:** every query also runs on Steam's Workshop search, so mods newer than the database are found by their titles.
- **Author pages:** a query that names a mod author (e.g. "Andromeda") brings in everything on that author's Workshop page.
- **Links:** Workshop or collection links pasted in the request go into the list as they are (collections expanded).
- **Dependencies:** required mods the database lacks are looked up on Steam.

Mods found this way are saved in the database first (Steam's data, not the model's), so they get the same checks, and the next search finds them too. Mods you already have are marked "installed" for the AI.

Providers:
- **Anthropic (Claude)**, which is the default (`claude-opus-5`)
- **OpenAI-compatible services:** OpenAI, OpenRouter, Groq, Google Gemini
- **Claude Code (your subscription):** uses the installed `claude` CLI and its Pro/Max login, with no API key and no API billing. Log in once by running `claude` in a terminal.
- **Local models:** Ollama, LM Studio
- **Offline (no AI)**, a keyword/theme generator that needs no key

API keys are encrypted with your Windows account (DPAPI).

## Build from source

Needs Python 3.13. `build.bat` creates `.venv`, installs PyInstaller, the Anthropic SDK and pywebview, and writes `dist\RimModManager.exe`.
Dev run: `.venv\Scripts\python.exe main.py`.
Self-test: `main.py --selftest [--online]`, or `RimModManager.exe --selftest`, which writes a report to `%TEMP%\rmm_selftest.txt`.
