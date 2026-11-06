"""Dependency resolution, load-order sorting and issue detection. Pure logic: no disk, no network.

`db` is optional everywhere and duck-typed (rmm.database.ModDatabase or a fake): by_package_id, get_many,
replacement_for, no_version_warning, rules. A failing db call is logged and treated as "unknown".
"""
from __future__ import annotations

import heapq
import logging
import re
from collections import deque
from types import SimpleNamespace

from .models import (CORE_ID, DLC_BY_APPID, DLCS, HARMONY_ID, HARMONY_WORKSHOP_ID, JUNK_PACKAGE_IDS, LEVEL_RANK,
                     LOADABLE_SOURCES, OFFICIAL_IDS, RIMWORLD_APPID, DbMod, Issue, Mod, ModDependency, ModRef,
                     compat_versions, short_version)

TIER_TOP, TIER_MID, TIER_BOTTOM = 0, 1, 2
_TIER_OFFICIAL = -1   # Harmony, Core, DLCs + whatever they need to load before them
_TIER_LABEL = {_TIER_OFFICIAL: "Harmony/Core/DLC", TIER_TOP: "load-top", TIER_MID: "main", TIER_BOTTOM: "load-bottom"}
_CANON = {pid: i for i, pid in enumerate([HARMONY_ID, *OFFICIAL_IDS])}   # official load order
# 'Name - Dev Snapshot', 'Name [1.4]', 'Name (Continued)': a variant upload of the mod called 'Name'
_VARIANT = re.compile(r"\s*[\[(]|\s+[-\u2013\u2014|]\s+(?=[^\[(]*\b(?:dev|snapshot|continued|unofficial|updated"
                      r"|fork|beta|legacy|\d+\.\d+)\b)")
_log = logging.getLogger("rmm")


# ---------------------------------------------------------------- helpers

def _pid_of(uid: str, installed: dict[str, Mod]) -> str:
    m = installed.get(uid)
    if m is not None:
        return m.package_id
    return uid[:-6] if uid.endswith("_steam") else uid


def _official_name(pid: str) -> str:
    if pid == CORE_ID:
        return "Core"
    if pid == HARMONY_ID:
        return "Harmony"
    return DLCS[pid][1] if pid in DLCS else pid


def _name(uid: str, installed: dict[str, Mod]) -> str:
    m = installed.get(uid)
    if m is not None and m.name:
        return m.name
    pid = _pid_of(uid, installed)
    return _official_name(pid) if pid in _CANON else uid


def _names(uids, installed, limit: int = 4) -> str:
    uids = list(uids)
    s = ", ".join(_name(u, installed) for u in uids[:limit])
    return s + (f" and {len(uids) - limit} more" if len(uids) > limit else "")


def _supports(versions: list[str], vs: str) -> bool:
    cv = compat_versions(vs)
    return any(short_version(v) in cv for v in versions)


_PAIR_KEYS = ("load_after", "load_before", "incompatible_with")
_FLAG_KEYS = ("load_top", "load_bottom")


def _empty_rule() -> dict:
    return {"load_after": {}, "load_before": {}, "incompatible_with": {}, "load_top": False, "load_bottom": False}


def _comment(meta) -> str:
    """Comment text from a rule value: str, list, or RimSort's {'name': .., 'comment': ..}."""
    if isinstance(meta, dict):
        meta = meta.get("comment", "")
    if isinstance(meta, list):
        return "; ".join(str(x) for x in meta if x)
    return str(meta or "")


def rules_from_json(data) -> dict:
    """RimSort userRules/communityRules JSON (camelCase, original-case ids) or our own user rules ->
    {pid: {'load_after': {pid: comment}, 'load_before': {...}, 'incompatible_with': {...}, 'load_top': bool,
    'load_bottom': bool}}. A fresh deep copy with lowercased ids; case-variant keys are merged, empty rules dropped."""
    src = data.get("rules", data) if isinstance(data, dict) else {}
    out: dict[str, dict] = {}
    if not isinstance(src, dict):
        return out
    for pid, r in src.items():
        if not isinstance(r, dict) or not str(pid).strip():
            continue
        e = _empty_rule()
        for key, ours in (("loadAfter", "load_after"), ("loadBefore", "load_before"),
                          ("incompatibleWith", "incompatible_with"), *((k, k) for k in _PAIR_KEYS)):
            v = r.get(key)
            if isinstance(v, dict):
                for other, meta in v.items():
                    e[ours][str(other).strip().lower()] = _comment(meta)
            elif isinstance(v, list):
                for other in v:
                    e[ours][str(other).strip().lower()] = ""
        for key, ours in (("loadTop", "load_top"), ("loadBottom", "load_bottom"), *((k, k) for k in _FLAG_KEYS)):
            v = r.get(key)
            if isinstance(v, dict):
                v = v.get("value")
            if v:
                e[ours] = True
        if any(e.values()):
            dst = out.setdefault(str(pid).strip().lower(), _empty_rule())
            for k in _PAIR_KEYS:
                dst[k].update(e[k])
            for k in _FLAG_KEYS:
                dst[k] = dst[k] or e[k]
    return out


def merge_rules(base: dict, extra: dict) -> tuple[dict, int]:
    """base + extra (both normalized) -> (new dict, number of rules added)."""
    out = rules_from_json(base)
    added = 0
    for pid, e in extra.items():
        dst = out.setdefault(pid, _empty_rule())
        for k in _PAIR_KEYS:
            for other, c in e.get(k, {}).items():
                if other not in dst[k]:
                    added += 1
                dst[k][other] = c or dst[k].get(other, "")
        for k in _FLAG_KEYS:
            if e.get(k) and not dst[k]:
                dst[k] = True
                added += 1
    return out, added


def _norm_rules(rules: dict | None) -> dict[str, dict]:
    """db.rules() / user rules (already rules_from_json shape) keyed by lowercased package id."""
    if not rules:
        return {}
    return {str(k).strip().lower(): v for k, v in rules.items() if isinstance(v, dict)}


def _rule_ids(rule: dict, kind: str) -> list[str]:
    v = rule.get(kind)
    if not v:
        return []
    if isinstance(v, str):
        v = [v]
    return [s for s in (str(x).strip().lower() for x in v) if s]


def _rule_flag(rule: dict, kind: str) -> bool:
    v = rule.get(kind)
    if isinstance(v, dict):
        v = v.get("value")
    return bool(v)


def _db_call(db, method: str, *args, **kw):
    fn = getattr(db, method, None) if db is not None else None
    if fn is None:
        return None
    try:
        return fn(*args, **kw)
    except Exception as e:  # a broken/locked DB must not break sorting or checks
        _log.warning("sorting: db.%s failed: %s", method, e)
        return None


def _loadable(installed: dict[str, Mod]) -> dict[str, Mod]:
    return {u: m for u, m in installed.items() if m.source in LOADABLE_SOURCES}


def _cached_wids(installed: dict[str, Mod]) -> dict[str, str]:
    """package id -> workshop id of copies the game does not load (SteamCMD cache, extra folders): download hints."""
    return {m.package_id: m.workshop_id for m in installed.values() if m.source not in LOADABLE_SOURCES and m.workshop_id}


def _variant_of(name: str, names: set[str]) -> bool:
    """name is one of names, or one of them followed by a variant tag (see _VARIANT)."""
    n = name.strip().lower()
    return bool(n) and (n in names or any(n[:m.start()].strip() in names for m in _VARIANT.finditer(n)))


def _installed_by_pid(installed: dict[str, Mod]) -> dict[str, list[str]]:
    """package id -> installed uids, the plain uid (== package id) first."""
    out: dict[str, list[str]] = {}
    for uid, m in installed.items():
        out.setdefault(m.package_id, []).append(uid)
    for pid, uids in out.items():
        if len(uids) > 1:
            uids.sort(key=lambda u: (u != pid, u))
    return out


def _lookup_wid(pid: str, hint: str, db, vs: str, on_disk=()) -> tuple[str, str, str]:
    """(workshop id, DB display name, reason when none) for a package id that is not installed.
    hint (About.xml steamWorkshopUrl, SteamCMD cache copy) is only a candidate next to the DB uploads of pid: About.xml
    links often point at a legacy upload ('[1.0] Vanilla Factions Expanded - Core' for the Framework). Ranked by:
    published; not known to the DB as another mod; supports vs; is the hint; already on disk; DB order."""
    if pid == HARMONY_ID:
        return HARMONY_WORKSHOP_ID, "Harmony", ""
    rows = [r for r in (_db_call(db, "by_package_id", pid) or [])
            if not r.is_dlc and str(r.workshop_id).isdigit()] if pid else []
    h = (_db_call(db, "get", hint) or DbMod(hint, pid)) if hint else None
    cands = ([h] if h else []) + [r for r in rows if not r.unpublished and r.workshop_id != hint]
    fits = lambda r: not vs or not r.game_versions or _supports(r.game_versions, vs)   # noqa: E731
    cands.sort(key=lambda r: (r.unpublished, r.package_id not in ("", pid), not fits(r), r is not h,
                              r.workshop_id not in on_disk))
    if cands:
        return cands[0].workshop_id, cands[0].name or cands[0].steam_name, ""
    if rows:
        return "", rows[0].display_name, "it was removed from the Steam Workshop"
    return "", "", "not installed and no Steam Workshop id is known" + ("" if db is not None else " (mod database unavailable)")


# ---------------------------------------------------------------- edges / plan

def _raw_edges(nodes: list[str], installed: dict[str, Mod], rules: dict, user_rules: dict) -> dict[str, dict[str, str]]:
    """{uid: {uid that must load before it: reason}} over installed active uids. User rules win over opposite edges."""
    by_pid: dict[str, list[str]] = {}
    for u in nodes:
        by_pid.setdefault(installed[u].package_id, []).append(u)
    edges: dict[str, dict[str, str]] = {u: {} for u in nodes}

    def link(before, after, reason, user=False):
        for n in after:
            e, pn = edges[n], installed[n].package_id
            for p in before:
                if p == n or installed[p].package_id == pn:
                    continue
                if user:
                    e[p] = reason
                    if edges[p].get(n, reason) != reason:   # drop the contradicting non-user edge
                        del edges[p][n]
                elif p not in e:
                    e[p] = reason

    def ids(pids):
        return [u for pid in pids for u in by_pid.get(pid.strip().lower(), ())]

    for u in nodes:
        m, me = installed[u], (u,)
        link(ids(p for d in m.dependencies for p in d.ids()), me, "dependency")   # whichever alternative is active
        link(ids(m.load_after), me, "loadAfter in About.xml")
        link(me, ids(m.load_before), "loadBefore in About.xml")
        r = rules.get(m.package_id)
        if r:
            link(ids(_rule_ids(r, "load_after")), me, "community rule")
            link(me, ids(_rule_ids(r, "load_before")), "community rule")
    for u in nodes:
        r = user_rules.get(installed[u].package_id)
        if r:
            link(ids(_rule_ids(r, "load_after")), (u,), "user rule", True)
            link((u,), ids(_rule_ids(r, "load_before")), "user rule", True)
    for u in nodes:     # weakest: never against an explicit edge the other way
        link([p for p in ids(installed[u].folder_after) if u not in edges[p]], (u,), "LoadFolders.xml content for it")
    return edges


def _assign_tiers(nodes, officials, installed, edges, rules, user_rules) -> dict[str, int]:
    tier = {u: _TIER_OFFICIAL for u in officials}
    succ: dict[str, list[str]] = {u: [] for u in nodes}
    for n in nodes:
        for p in edges[n]:
            succ[p].append(n)
    top, bottom = [], []
    for u in nodes:
        if u in tier:
            continue
        pid = installed[u].package_id
        cr, ur = rules.get(pid) or {}, user_rules.get(pid) or {}
        ut, ub = _rule_flag(ur, "load_top"), _rule_flag(ur, "load_bottom")
        if ut or (_rule_flag(cr, "load_top") and not ub):
            top.append(u)
        elif ub or _rule_flag(cr, "load_bottom"):
            bottom.append(u)

    def spread(seeds, nbrs, t):
        q = deque(seeds)
        while q:
            for v in nbrs.get(q.popleft(), ()):
                if v not in tier:
                    tier[v] = t
                    q.append(v)

    for u in bottom:
        tier[u] = TIER_BOTTOM
    # things Core/DLCs must load after are pulled into the official block (e.g. rules saying "before Royalty")
    spread([u for u in officials if _pid_of(u, installed) != HARMONY_ID], edges, _TIER_OFFICIAL)
    for u in top:
        tier.setdefault(u, TIER_TOP)
    spread(bottom, succ, TIER_BOTTOM)   # whatever must load after a bottom mod goes bottom too
    spread([u for u in top if tier[u] == TIER_TOP], edges, TIER_TOP)   # what top mods need
    for u in nodes:
        tier.setdefault(u, TIER_MID)
    return tier


def _sccs(nodes: list[str], preds: dict[str, dict]) -> list[list[str]]:
    """Tarjan (iterative). Returns all strongly connected components."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack, on, out, i = [], set(), [], 0
    for root in nodes:
        if root in index:
            continue
        index[root] = low[root] = i
        i += 1
        stack.append(root)
        on.add(root)
        work = [(root, iter(preds[root]))]
        while work:
            v, it = work[-1]
            for w in it:
                if w not in index:
                    index[w] = low[w] = i
                    i += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(preds[w])))
                    break
                if w in on and index[w] < low[v]:
                    low[v] = index[w]
            else:
                work.pop()
                if work and low[v] < low[work[-1][0]]:
                    low[work[-1][0]] = low[v]
                if low[v] == index[v]:
                    comp = []
                    while True:
                        w = stack.pop()
                        on.discard(w)
                        comp.append(w)
                        if w == v:
                            break
                    out.append(comp)
    return out


def _plan(active: list[str], installed: dict[str, Mod], rules, user_rules) -> SimpleNamespace:
    """Everything sort_order and check share: tiers, the kept edge set, dropped-edge warnings, cycles."""
    rules, user_rules = _norm_rules(rules), _norm_rules(user_rules)
    order = list(dict.fromkeys(active))
    pos = {u: i for i, u in enumerate(order)}
    nodes = [u for u in order if u in installed]
    ranked = [u for u in order if u in installed or _pid_of(u, installed) in _CANON]
    unknown = [u for u in order if u not in installed and _pid_of(u, installed) not in _CANON]
    officials = [u for u in ranked if _pid_of(u, installed) in _CANON]
    raw = _raw_edges(nodes, installed, rules, user_rules)
    tier = _assign_tiers(nodes, officials, installed, raw, rules, user_rules)

    kept: dict[str, dict[str, str]] = {u: {} for u in ranked}
    dropped = []
    for n in nodes:
        tn, pn = tier[n], installed[n].package_id
        for p, why in raw[n].items():
            tp, pp = tier[p], installed[p].package_id
            bad = ""
            if pn == HARMONY_ID:
                bad = "Harmony always loads first"
            elif tp > tn:
                bad = f"'{_name(p, installed)}' is in the {_TIER_LABEL[tp]} group, '{_name(n, installed)}' in the {_TIER_LABEL[tn]} group"
            elif pp in _CANON and pn in _CANON and _CANON[pp] > _CANON[pn]:
                bad = "official content keeps its canonical order"
            if bad:
                dropped.append(f"Ignored load rule '{_name(p, installed)}' before '{_name(n, installed)}' ({why}): {bad}.")
            else:
                kept[n][p] = why
    # implied official order: Harmony first, then Core and DLCs in canonical order
    chain = sorted(officials, key=lambda u: (_CANON[_pid_of(u, installed)], pos[u]))
    for a, b in zip(chain, chain[1:]):
        kept[b].setdefault(a, "official order")
    for h in (u for u in chain if _pid_of(u, installed) == HARMONY_ID):
        for n in ranked:
            if n != h and tier[n] == _TIER_OFFICIAL and _pid_of(n, installed) != HARMONY_ID:
                kept[n].setdefault(h, "Harmony loads first")

    comps = _sccs(ranked, kept)
    comp = {u: ci for ci, members in enumerate(comps) for u in members}
    cycles = [sorted(c, key=pos.__getitem__) for c in comps if len(c) > 1]
    return SimpleNamespace(order=order, pos=pos, nodes=nodes, ranked=ranked, unknown=unknown, tier=tier,
                           kept=kept, dropped=dropped, comps=comps, comp=comp, cycles=cycles)


def _order_cycle(members: list[str], kept: dict[str, dict], key: dict) -> list[str]:
    """Order one strongly connected component: Kahn inside it, breaking with the node that has the fewest
    remaining predecessors (ties by key)."""
    left = set(members)
    rem = {n: sum(1 for p in kept[n] if p in left) for n in members}
    succ: dict[str, list[str]] = {n: [] for n in members}
    for n in members:
        for p in kept[n]:
            if p in left:
                succ[p].append(n)
    heap, out = [], []
    while left:
        n = heapq.heappop(heap)[1] if heap else min(left, key=lambda x: (rem[x], key[x]))
        left.discard(n)
        out.append(n)
        for s in succ[n]:
            if s in left:
                rem[s] -= 1
                if rem[s] == 0:
                    heapq.heappush(heap, (key[s], s))
    return out


# ---------------------------------------------------------------- public API

def sort_order(active: list[str], installed: dict[str, Mod], rules: dict | None = None, user_rules: dict | None = None,
               tiebreak: str = "current") -> tuple[list[str], list[str]]:
    """(sorted uids, warnings). Harmony, Core, DLCs (canonical) [+ mods they need], load-top, main, not-installed, load-bottom."""
    p = _plan(active, installed, rules, user_rules)
    if tiebreak == "alphabetical":
        tb = {u: (_name(u, installed).lower(), u) for u in p.ranked}
    else:
        tb = {u: (p.pos[u], u) for u in p.ranked}
    key = {}
    for u in p.ranked:
        c = _CANON.get(_pid_of(u, installed))
        key[u] = (0, (c, "")) if c is not None else (1, tb[u])   # officials as early as possible in their block

    ckey = [(p.tier[m[0]], min(key[x] for x in m)) for m in p.comps]
    indeg = [0] * len(p.comps)
    succ: dict[str, list[str]] = {u: [] for u in p.ranked}
    for n in p.ranked:
        cn = p.comp[n]
        for q in p.kept[n]:
            succ[q].append(n)
            if p.comp[q] != cn:
                indeg[cn] += 1
    heap = [(ckey[i], i) for i in range(len(p.comps)) if indeg[i] == 0]
    heapq.heapify(heap)
    out: list[str] = []
    while heap:
        ci = heapq.heappop(heap)[1]
        members = p.comps[ci]
        for n in (members if len(members) == 1 else _order_cycle(members, p.kept, key)):
            out.append(n)
            for s in succ[n]:
                cs = p.comp[s]
                if cs != ci:
                    indeg[cs] -= 1
                    if indeg[cs] == 0:
                        heapq.heappush(heap, (ckey[cs], cs))

    cut = next((i for i, u in enumerate(out) if p.tier[u] == TIER_BOTTOM), len(out))
    out[cut:cut] = p.unknown

    warnings = []
    if len(p.order) != len(active):
        warnings.append(f"Removed {len(active) - len(p.order)} duplicate entr{'y' if len(active) - len(p.order) == 1 else 'ies'} from the active list.")
    warnings += list(dict.fromkeys(p.dropped))
    for c in p.cycles:
        warnings.append(f"Load order loop between {_names(c, installed, 8)}: their load rules contradict each other; "
                        "they were placed as close to their rules as possible.")
    if p.unknown:
        warnings.append(f"{len(p.unknown)} active mod(s) are not installed and were kept at the end of the main group: "
                        f"{_names(p.unknown, installed, 6)}.")
    return out, warnings


def resolve_dependencies(active: list[str], installed: dict[str, Mod], db=None,
                         version_short: str = "") -> tuple[list[str], list[ModRef], list[str]]:
    """(installed uids to add, refs to download, unresolvable messages), recursively. Never adds something already active.
    Only loadable copies are activated ('steamcmd' cache / 'extra' copies are downloaded instead)."""
    return _resolve(list(dict.fromkeys(active)), installed, db, short_version(version_short))


def download_closure(refs: list[ModRef], installed: dict[str, Mod], db=None, version_short: str = "") -> list[ModRef]:
    """refs (in order, deduped) + the Steam 'required items' (DB dependencies) that still need downloading, walked one
    level at a time and never through a dependency that is already met: by an installed loadable mod or another ref
    (package id / workshop id / a variant upload of it), or superseded by its Use-This-Instead replacement. An outdated
    dependency is swapped for its replacement when that supports the game version. DLC/app ids are never returned."""
    loadable = [u for u, m in installed.items() if m.source in LOADABLE_SOURCES]
    return _resolve(loadable, installed, db, short_version(version_short), seeds=refs)[1]


def _resolve(order: list[str], installed: dict[str, Mod], db, vs: str,
             seeds: list[ModRef] | None = None) -> tuple[list[str], list[ModRef], list[str]]:
    """Shared by resolve_dependencies and download_closure (seeds: refs to download whose DB deps are wanted; then the
    About.xml dependencies of `order` are not walked)."""
    loadable = _loadable(installed)
    by_pid = _installed_by_pid(loadable)
    by_wid: dict[str, str] = {}
    for u, m in loadable.items():
        if m.workshop_id:
            by_wid.setdefault(m.workshop_id, u)
    active_set = set(order)
    have = {_pid_of(u, installed) for u in order if u not in installed or u in loadable}
    active_wids = {loadable[u].workshop_id for u in order if u in loadable and loadable[u].workshop_id}
    names = {m.name.strip().lower() for m in loadable.values() if m.name and not m.is_official}
    hints = _cached_wids(installed)
    on_disk = {m.workshop_id for m in installed.values() if m.workshop_id}
    add: list[str] = []
    refs: list[ModRef] = []
    unresolved: dict[str, list] = {}          # key -> [label, reason, requesters]
    queued: set[str] = set()                  # workshop ids already in refs
    pending: list[str] = []                   # download ids whose DB dependencies are not expanded yet
    expanded: set[str] = set()
    queue = deque() if seeds is not None else deque(u for u in order if u in loadable)
    seen: set[str] = set()
    for r in seeds or ():
        w, p = (r.workshop_id or "").strip(), (r.package_id or "").strip().lower()
        k = w or p or (r.name or "").strip().lower()
        if not k or k in seen:
            continue
        seen.add(k)
        refs.append(r)
        if w:
            queued.add(w)
            pending.append(w)
        if p:
            have.add(p[:-6] if p.endswith("_steam") else p)
        if r.name:
            names.add(r.name.strip().lower())

    def take(uid: str) -> None:
        m = installed[uid]
        add.append(uid)
        active_set.add(uid)
        have.add(m.package_id)
        if m.workshop_id:
            active_wids.add(m.workshop_id)
        queue.append(uid)

    def fail(key: str, label: str, reason: str, who: str) -> None:
        unresolved.setdefault(key, [label or key, reason, []])[2].append(who)

    def need(pid: str, wid: str, label: str, who: str, alts=()) -> None:
        pid = (pid or "").strip().lower()
        pid = "" if pid in JUNK_PACKAGE_IDS else pid
        wid = (wid or "").strip()
        key = pid or wid
        if not key or (pid and (pid in have or any(a in have for a in alts))) or \
                (not pid and (wid in active_wids or wid in queued)):
            return
        if key in unresolved:
            unresolved[key][2].append(who)
            return
        if pid == CORE_ID or pid in DLCS:
            if pid in by_pid:
                take(by_pid[pid][0])
            else:
                fail(pid, label or _official_name(pid), f"the {_official_name(pid)} DLC is not installed/owned"
                     if pid in DLCS else "Core was not found (check the game folder)", who)
            return
        cands = (by_pid.get(pid) or next((by_pid[a] for a in alts if a in by_pid), None)) if pid else \
            ([by_wid[wid]] if wid in by_wid else None)          # an installed alternative beats a download
        if cands:
            if cands[0] not in active_set:
                take(cands[0])
            return
        w, dbname, why = _lookup_wid(pid, wid or hints.get(pid, ""), db, vs, on_disk) if pid else (wid, "", "")
        if not w:
            fail(key, label or dbname, why, who)
            return
        if wid and w != wid:                 # a stale link was swapped: its label may name the legacy upload
            label = dbname or label
        if w in by_wid:                      # installed under an outdated/different package id
            if by_wid[w] not in active_set:
                take(by_wid[w])
            return
        if w not in queued:
            refs.append(ModRef(package_id=pid, name=label or dbname or pid or w, workshop_id=w))
            queued.add(w)
            pending.append(w)
            if label or dbname:
                names.add((label or dbname).strip().lower())
        if pid:
            have.add(pid)

    def steam_dep(w: str, r, dname: str, who: str) -> None:
        """One Steam 'required item' w (DB row r) of a queued download. Steam cannot say 'either/or' and lists
        variant uploads (dev builds, old versions), so it is only a hint: the downloaded About.xml is checked later."""
        opid = DLC_BY_APPID.get(w) or (CORE_ID if w == RIMWORLD_APPID else "")
        if opid:
            need(opid, "", dname or _official_name(opid), who)
            return
        if r is not None and r.is_dlc:
            return
        label = (r.display_name if r is not None else "") or dname
        pid = "" if r is None or r.package_id in JUNK_PACKAGE_IDS else r.package_id
        if (pid and (pid in have or pid in by_pid)) or w in by_wid or w in queued or w in active_wids:
            need(pid, w, label, who)                     # met, or installed: activate it
            return
        rep = _db_call(db, "replacement_for", workshop_id=w, package_id=pid) if r is not None else None
        if rep:
            nwid = str(rep.get("newWorkshopId") or "").strip()
            npid = str(rep.get("newPackageId") or "").strip().lower()
            present = (npid and (npid in have or npid in by_pid)) or nwid in by_wid or nwid in queued
            nvers = [short_version(str(v)) for v in rep.get("newVersions") or []]
            outdated = vs and r.game_versions and not _supports(r.game_versions, vs) and _supports(nvers, vs)
            if (present or outdated) and (npid or nwid) and nwid != w:
                need(npid, nwid, str(rep.get("newName") or npid or nwid), who)
                return
        if r is not None and r.unpublished:
            fail(pid or w, label, "it was removed from the Steam Workshop", who)
            return
        if not pid and _variant_of(label, names - {who.strip().lower()}):
            return                                       # e.g. 'Combat Extended - Dev Snapshot' while CE is there
        need(pid, w, label, who)

    while queue or pending:
        while queue:
            m = installed[queue.popleft()]
            for d in m.dependencies:
                need(d.package_id, d.workshop_id, d.display_name, m.name or m.uid, d.alternatives)
        batch = [w for w in dict.fromkeys(pending) if w not in expanded]
        pending.clear()
        if db is None or not batch:
            continue
        expanded.update(batch)
        rows = _db_call(db, "get_many", batch) or {}
        deps: dict[str, tuple[str, str]] = {}     # dependency id -> (its name, requester name), first requester wins
        for w in batch:
            r = rows.get(w)
            for dep_id, dep_name in ((r.dependencies or {}).items() if r else ()):
                deps.setdefault(str(dep_id).strip(), (dep_name, r.display_name))
        drows = _db_call(db, "get_many", [w for w in deps if w not in queued]) or {}
        for w, (dname, who) in deps.items():
            steam_dep(w, drows.get(w), dname, who)

    msgs = [f"{label}" + (f" ({key})" if key != label else "") + f" is required by {_join(who)} but {reason}."
            for key, (label, reason, who) in unresolved.items()]
    return add, refs, msgs


def _join(names: list[str]) -> str:
    names = [n for n in dict.fromkeys(names) if n] or ["another mod"]
    return ", ".join(names[:4]) + (f" and {len(names) - 4} more" if len(names) > 4 else "")


def check(active: list[str], installed: dict[str, Mod], db=None, version_short: str = "",
          owned_dlcs: set[str] | None = None, user_rules: dict | None = None) -> list[Issue]:
    """All problems of the active list, sorted by level (error, warning, info) then active index."""
    vs = short_version(version_short)
    rules = _norm_rules(_db_call(db, "rules"))
    urules = _norm_rules(user_rules)
    p = _plan(active, installed, rules, urules)
    pos = p.pos
    owned = set(owned_dlcs) if owned_dlcs is not None else {m.package_id for m in installed.values() if m.source == "dlc"}
    loadable = _loadable(installed)     # fixes never point at a 'steamcmd'/'extra' copy: the game does not load those
    by_pid, hints = _installed_by_pid(loadable), _cached_wids(installed)
    on_disk = {m.workshop_id for m in installed.values() if m.workshop_id}
    active_pids = {_pid_of(u, installed) for u in p.order if u not in installed or u in loadable}
    active_wids = {loadable[u].workshop_id for u in p.nodes if u in loadable and loadable[u].workshop_id}
    issues: list[Issue] = []

    def name(u):
        return _name(u, installed)

    def add(level, kind, uid, msg, related=None, fix=None):
        issues.append(Issue(level=level, kind=kind, uid=uid, message=msg, related=list(related or []), fix=fix))

    if CORE_ID not in active_pids:
        add("error", "missing_core", "", "Core (ludeon.rimworld) is not active; RimWorld needs it first in the list.",
            fix={"action": "add", "uids": [CORE_ID]})

    # duplicates: same uid listed twice, or the same package id active under two uids ('foo' + 'foo_steam')
    seen: set[str] = set()
    for u in active:
        if u in seen:
            add("error", "duplicate", u, f"{name(u)} is listed twice in the active list.", [u])
        seen.add(u)
    first: dict[str, str] = {}
    for u in p.order:
        pid = _pid_of(u, installed)
        if pid in first:
            add("error", "duplicate", u, f"{name(u)} is active twice ('{first[pid]}' and '{u}' share the package id "
                f"'{pid}'); keep only one copy.", [first[pid]])
        else:
            first[pid] = u

    # active but not installed
    for u in p.order:
        if u in installed:
            continue
        pid = _pid_of(u, installed)
        if pid in DLCS:
            add("error", "dlc_missing", u, f"The {DLCS[pid][1]} DLC is active but not installed/owned.")
            continue
        if pid == CORE_ID:
            add("error", "not_installed", u, "Core is active but was not found; check the game folder in Settings.")
            continue
        alts = by_pid.get(pid, [])
        if any(a in pos for a in alts):
            add("error", "not_installed", u, f"'{u}' is not installed; another copy of {name(alts[0])} is already active.",
                [a for a in alts if a in pos], {"action": "remove", "uids": [u]})
        elif alts:
            a = alts[0]
            add("error", "not_installed", u, f"'{u}' is not installed, but {name(a)} is installed as '{a}'.", [a],
                {"action": "replace", "uid": u, "ref": ModRef(pid, name(a), installed[a].workshop_id)})
        else:
            wid, dbname, why = _lookup_wid(pid, "", db, vs, on_disk)
            if wid:
                add("error", "not_installed", u, f"{dbname or u} is active but not installed.",
                    fix={"action": "download", "refs": [ModRef(pid, dbname or pid, wid)]})
            else:
                add("error", "not_installed", u, f"'{u}' is active but not installed ({why}).",
                    fix={"action": "remove", "uids": [u]})

    # dependencies (one level; resolve_dependencies does the recursive work)
    for u in p.nodes:
        m = installed[u]
        for d in m.dependencies:
            dp = d.package_id.strip().lower()
            dp = "" if dp in JUNK_PACKAGE_IDS else dp
            if (dp and (dp == m.package_id or dp == CORE_ID or any(x in active_pids for x in [dp, *d.alternatives]))) \
                    or (not dp and (not d.workshop_id or d.workshop_id in active_wids)):
                continue
            # RimWorld drops (with a log warning) a dependency without displayName / download link / valid id
            lvl, note = ("error", "") if d.enforced else ("warning", " (declared in About.xml, but RimWorld does not "
                                                                      "enforce it: the entry is incomplete)")
            label = d.display_name or (_official_name(dp) if dp in _CANON else "")
            if dp in DLCS:
                if dp in owned or dp in by_pid:
                    add(lvl, "missing_dependency", u, f"{name(u)} requires the {DLCS[dp][1]} DLC, which is not active.{note}",
                        [dp], {"action": "add", "uids": [by_pid.get(dp, [dp])[0]]})
                else:
                    add(lvl, "dlc_missing", u, f"{name(u)} requires the {DLCS[dp][1]} DLC, which you don't have installed.{note}")
                continue
            cands = (by_pid.get(dp) or next((by_pid[a] for a in d.alternatives if a in by_pid), None)) if dp else \
                [x for x, im in loadable.items() if im.workshop_id == d.workshop_id][:1]
            if cands:
                add(lvl, "missing_dependency", u, f"{name(u)} requires {name(cands[0])}, which is installed but not active.{note}",
                    [cands[0]], {"action": "add", "uids": [cands[0]]})
                continue
            wid, dbname, why = _lookup_wid(dp, d.workshop_id or hints.get(dp, ""), db, vs, on_disk)
            nm = label or dbname or dp or wid
            if wid:
                add(lvl, "missing_dependency", u, f"{name(u)} requires {nm}, which is not installed.{note}",
                    fix={"action": "download", "refs": [ModRef(dp, nm, wid)]})
            else:
                add(lvl, "missing_dependency", u, f"{name(u)} requires {nm}" + (f" ({dp})" if dp != nm else "") + f": {why}.{note}")

    # incompatibilities (declared by either side, About.xml or rules): one issue per pair, on the later mod
    by_pid_active: dict[str, list[str]] = {}
    for u in p.nodes:
        by_pid_active.setdefault(installed[u].package_id, []).append(u)
    pairs: set[frozenset] = set()
    for u in p.nodes:
        m = installed[u]
        srcs = [(x, f"declared by {name(u)}") for x in m.incompatible_with]
        for label, rs in (("community rule", rules), ("user rule", urules)):
            r = rs.get(m.package_id)
            if r:
                srcs += [(x, label) for x in _rule_ids(r, "incompatible_with")]
        for ip, why in srcs:
            for o in by_pid_active.get(ip.strip().lower(), ()):
                pair = frozenset((u, o))
                if o == u or installed[o].package_id == m.package_id or pair in pairs:
                    continue
                pairs.add(pair)
                later, earlier = (u, o) if pos[u] > pos[o] else (o, u)
                add("error", "incompatible", later, f"{name(later)} is incompatible with {name(earlier)} ({why}).",
                    [earlier], {"action": "remove", "uids": [later]})

    # load order: exactly the edge set and tiers sort_order uses, so a freshly sorted list is clean
    problems: dict[str, list[str]] = {}
    related: dict[str, list[str]] = {}
    for n in p.ranked:
        for q, why in p.kept[n].items():
            if pos[q] > pos[n] and p.comp[q] != p.comp[n]:
                problems.setdefault(n, []).append(f"after {name(q)} ({why})")
                related.setdefault(n, []).append(q)
    top_tier, holder = None, ""
    for n in p.ranked:
        t = p.tier[n]
        if top_tier is not None and t < top_tier:
            problems.setdefault(n, []).append(f"before {name(holder)} ({_TIER_LABEL[t]} mods load before "
                                              f"{_TIER_LABEL[top_tier]} mods)")
            related.setdefault(n, []).append(holder)
        elif top_tier is None or t > top_tier:
            top_tier, holder = t, n
    for n, probs in problems.items():
        more = f" and {len(probs) - 3} more" if len(probs) > 3 else ""
        add("warning", "order", n, f"{name(n)} should load " + "; ".join(probs[:3]) + more + ".",
            list(dict.fromkeys(related[n])), {"action": "sort"})
    for c in p.cycles:
        add("warning", "cycle", c[0], f"Load order loop between {_names(c, installed, 8)}: their load rules contradict "
            "each other.", c[1:])

    # versions
    if vs:
        nvw = set().union(*(_db_call(db, "no_version_warning", v) or set() for v in compat_versions(vs)))
        for u in p.nodes:
            m = installed[u]
            if m.is_official or m.package_id in OFFICIAL_IDS or m.package_id in nvw:
                continue
            if not m.supported_versions:
                add("warning", "version", u, f"{name(u)} does not declare any supported game version.")
            elif not _supports(m.supported_versions, vs):
                add("warning", "version", u, f"{name(u)} does not list support for {vs} "
                    f"(supports {', '.join(m.supported_versions)}).")

    # Use This Instead replacements
    if db is not None:
        for u in p.nodes:
            m = installed[u]
            if m.is_official:
                continue
            r = _db_call(db, "replacement_for", workshop_id=m.workshop_id, package_id=m.package_id)
            if not r:
                continue
            nwid = str(r.get("newWorkshopId") or "").strip()
            npid = str(r.get("newPackageId") or "").strip().lower()
            if (not nwid and not npid) or (nwid and nwid == m.workshop_id) or nwid == "3819799478":
                continue
            nvers = [short_version(str(v)) for v in r.get("newVersions") or []]
            if vs and nvers and not _supports(nvers, vs) and _supports(m.supported_versions, vs):
                continue
            nname = str(r.get("newName") or npid or nwid)
            add("info", "replacement", u, f"'Use This Instead' suggests replacing {name(u)} with {nname}"
                + (f" (supports {vs})." if vs and _supports(nvers, vs) else "."),
                fix={"action": "replace", "uid": u, "ref": ModRef(npid, nname, nwid)})

    # a '<pid>_dupN' copy: RimWorld loads only the first folder with a package id and cannot resolve '_dupN' ids
    for u in p.nodes:
        m = installed[u]
        if m.source in LOADABLE_SOURCES and u not in (m.package_id, m.package_id + "_steam"):
            fix = {"action": "remove", "uids": [u]} if any(x in pos for x in (m.package_id, m.package_id + "_steam")) else \
                {"action": "replace", "uid": u, "ref": ModRef(m.package_id, m.name, "")}
            add("error", "not_in_game", u, f"RimWorld ignores this duplicate copy of {name(u)} ('{u}' in {m.path}); "
                f"activate '{m.package_id}' instead.", [m.package_id], fix)

    for u in p.nodes:
        if installed[u].parse_error:
            add("warning", "no_packageid", u, f"{name(u)}: {installed[u].parse_error}")

    issues.sort(key=lambda i: (LEVEL_RANK.get(i.level, 3), pos.get(i.uid, -1)))
    return issues


# ---------------------------------------------------------------- self test

def _selftest() -> None:
    import random
    import time

    def mk(pid, name=None, deps=(), after=(), before=(), incompat=(), versions=("1.6",), source="local", wid="",
           uid=None, parse_error=""):
        return Mod(uid=uid or pid, package_id=pid, name=name or pid, path="", source=source, workshop_id=wid,
                   supported_versions=list(versions), load_after=list(after), load_before=list(before),
                   incompatible_with=list(incompat), parse_error=parse_error,
                   dependencies=[d if isinstance(d, ModDependency) else ModDependency(d) for d in deps])

    def inst(*mods):
        return {m.uid: m for m in mods}

    def official():
        return [mk(HARMONY_ID, "Harmony", before=[CORE_ID], wid=HARMONY_WORKSHOP_ID, versions=("1.5", "1.6")),
                mk(CORE_ID, "Core", versions=(), source="core")] + [
            mk(pid, n, after=[CORE_ID], source="dlc") for pid, (_, n) in DLCS.items()]

    def is_before(order, a, b):
        return order.index(a) < order.index(b)

    def build_edges(active, installed, rules=None):     # {uid: uids that must load before it}
        nodes = [u for u in dict.fromkeys(active) if u in installed]
        return {u: set(ps) for u, ps in _raw_edges(nodes, installed, _norm_rules(rules), {}).items()}

    # --- tiers, deps, loadBefore, top/bottom, pull-up into the official block
    I = inst(*official(),
             mk("a", deps=["b"]), mk("b"), mk("c", before=["a"]),
             mk("top1", deps=["lib"]), mk("lib"),
             mk("z.bottom"), mk("y", after=["z.bottom"]), mk("w", before=["y"]),
             mk("startup"), mk("optimizer"))
    rules = rules_from_json({"TOP1": {"loadTop": {"value": True}},      # raw RimSort shape, original case
                             "z.bottom": {"load_bottom": True},
                             "startup": {"load_after": {CORE_ID: ""}, "load_before": {"ludeon.rimworld.royalty": ""}},
                             "optimizer": {"loadAfter": {HARMONY_ID: {}}, "loadBefore": {CORE_ID: {}}}})
    act = ["z.bottom", "y", "a", "ludeon.rimworld.biotech", "ghost1", "ludeon.rimworld.royalty", CORE_ID, "c", "w",
           "startup", "top1", "b", HARMONY_ID, "ghost2", "lib", "optimizer", "ludeon.rimworld.odyssey"]
    r = rules_from_json({"rules": {"Foo.Bar": {"loadAfter": {"A.B": {"comment": ["x"]}}}, "foo.bar": {"loadAfter": {}}}})
    assert list(r) == ["foo.bar"] and r["foo.bar"]["load_after"] == {"a.b": "x"}, r   # case variants merge, none lost
    out, warns = sort_order(act, I, rules)
    assert sorted(out) == sorted(act), out
    assert out[:5] == [HARMONY_ID, "optimizer", CORE_ID, "startup", "ludeon.rimworld.royalty"], out
    assert out[5:7] == ["ludeon.rimworld.biotech", "ludeon.rimworld.odyssey"], out
    assert out[7:9] == ["lib", "top1"], out
    assert is_before(out, "b", "a") and is_before(out, "c", "a") and is_before(out, "w", "y")
    assert out[-2:] == ["z.bottom", "y"], out
    assert out[-4:-2] == ["ghost1", "ghost2"], out                  # unknown: end of main group, relative order kept
    assert any("not installed" in w for w in warns), warns
    assert not [i for i in check(out, I, user_rules=None, db=_FakeDB(rules=rules)) if i.kind in ("order", "cycle")]
    assert [i.kind for i in check(act, I, db=_FakeDB(rules=rules)) if i.kind == "order"]
    assert sort_order(out, I, rules)[0] == out                     # idempotent
    e = build_edges(act, I, rules)
    assert e["a"] == {"b", "c"} and "ghost1" not in e and e["y"] >= {"z.bottom", "w"}
    assert e["startup"] == {CORE_ID} and e["ludeon.rimworld.royalty"] >= {"startup", CORE_ID}

    # --- alphabetical tiebreak + current tiebreak
    I2 = inst(*official()[:2], mk("m.b", "Beta"), mk("m.a", "alpha"), mk("m.c", "Gamma"))
    assert sort_order(["m.c", "m.b", CORE_ID, "m.a"], I2, tiebreak="alphabetical")[0] == [CORE_ID, "m.a", "m.b", "m.c"]
    assert sort_order(["m.c", "m.b", CORE_ID, "m.a"], I2)[0] == [CORE_ID, "m.c", "m.b", "m.a"]
    # a LoadFolders.xml 'IfModActive' folder loads after that mod, unless an explicit rule says otherwise
    addon, ce = mk("soviet.gun"), mk("ceteam.combatextended")
    addon.folder_after = ["ceteam.combatextended"]
    assert sort_order([CORE_ID, "soviet.gun", "ceteam.combatextended"], inst(*official()[:2], addon, ce))[0] \
        == [CORE_ID, "ceteam.combatextended", "soviet.gun"]
    ce2 = mk("ceteam.combatextended", after=["soviet.gun"])
    assert sort_order([CORE_ID, "soviet.gun", "ceteam.combatextended"], inst(*official()[:2], addon, ce2))[0] \
        == [CORE_ID, "soviet.gun", "ceteam.combatextended"]

    # --- cycles, dropped edges, duplicates, user rules override community rules
    I3 = inst(*official()[:2], mk("p", after=["q"]), mk("q", after=["p"]), mk("r", before=[HARMONY_ID]),
              mk("s"), mk("t"), mk("bot"), mk("needsbot", before=["topm"]), mk("topm"))
    rules3 = {"bot": {"load_bottom": True}, "topm": {"load_top": True}, "needsbot": {"load_after": {"bot": ""}},
              "s": {"load_before": {"t": ""}}}
    act3 = ["t", "s", "q", "p", CORE_ID, "r", HARMONY_ID, "bot", "needsbot", "topm", "p"]
    out, warns = sort_order(act3, I3, rules3)
    assert len(out) == len(set(out)) == 10 and any("duplicate" in w for w in warns), warns
    assert out[:2] == [HARMONY_ID, CORE_ID] and out[2] == "topm" and out[-2:] == ["bot", "needsbot"], out
    assert is_before(out, "s", "t"), out
    assert any("loop" in w and "p" in w and "q" in w for w in warns), warns
    assert any("Ignored" in w and "Harmony always loads first" in w for w in warns), warns
    assert any("Ignored" in w and "load-bottom" in w for w in warns), warns
    iss = check(out, I3, db=_FakeDB(rules=rules3))
    assert not [i for i in iss if i.kind == "order"], iss
    assert [i for i in iss if i.kind == "cycle" and set([i.uid, *i.related]) == {"p", "q"}]
    out_u, _ = sort_order(act3, I3, rules3, user_rules={"t": {"load_before": {"s": ""}}})
    assert is_before(out_u, "t", "s"), out_u
    assert not [i for i in check(out_u, I3, db=_FakeDB(rules=rules3), user_rules={"t": {"load_before": {"s": ""}}})
                if i.kind in ("order", "cycle") and "s" in (i.uid, *i.related)]

    # --- '_steam' duplicates are both sorted and flagged
    I4 = inst(*official()[:2], mk("foo", "Foo"), mk("foo", "Foo (workshop)", uid="foo_steam", source="workshop"),
              mk("bar", deps=["foo"]))
    out, _ = sort_order(["bar", "foo_steam", CORE_ID, "foo"], I4)
    assert out[0] == CORE_ID and is_before(out, "foo", "bar") and is_before(out, "foo_steam", "bar"), out
    assert out == [CORE_ID, "foo_steam", "foo", "bar"], out
    dup = [i for i in check(out, I4) if i.kind == "duplicate"]
    assert len(dup) == 1 and dup[0].uid == "foo" and dup[0].related == ["foo_steam"], dup

    # --- resolve_dependencies
    db = _FakeDB(rows=[DbMod("111", "lib.c", "Lib C", dependencies={"222": "Lib D", "1826140": "Biotech"},
                             game_versions=["1.6"]),
                       DbMod("222", "lib.d", "Lib D", game_versions=["1.6"]),
                       DbMod("333", "gone.mod", "Gone", unpublished=True),
                       DbMod("444", "", "No pid lib"),
                       DbMod("555", "lib.e", "Lib E", dependencies={"444": "No pid lib", "333": "Gone"})])
    I5 = inst(*official()[:3],
              mk("m.a", "A", deps=["m.b", "ludeon.rimworld.royalty"]), mk("m.b", "B", deps=["lib.c", "x.unknown"]),
              mk("m.f", "F", deps=[ModDependency("hinted", "Hinted", workshop_id="999"), "lib.e", "gone.mod"]),
              mk("m.g", "G", deps=["m.a"]))
    add, refs, msgs = resolve_dependencies([CORE_ID, "m.a", "m.f", "m.g"], I5, db, "1.6.4871 rev590")
    assert add == ["m.b", "ludeon.rimworld.royalty"], add
    assert [(r.package_id, r.workshop_id) for r in refs] == [
        ("hinted", "999"), ("lib.e", "555"), ("lib.c", "111"), ("", "444"), ("lib.d", "222")], refs
    joined = " | ".join(msgs)
    assert "x.unknown" in joined and "Biotech" in joined and "removed from the Steam Workshop" in joined, msgs
    assert len(msgs) == 3, msgs
    add, refs, msgs = resolve_dependencies([CORE_ID, "m.g"], I5)      # no db: recursion via About only
    assert add == ["m.a", "m.b", "ludeon.rimworld.royalty"] and refs == [] and len(msgs) == 2, (add, refs, msgs)
    assert resolve_dependencies([CORE_ID, "m.g", "m.a", "m.b", "ludeon.rimworld.royalty"], I5)[0] == []
    assert resolve_dependencies(["x"], inst(mk("x", deps=[HARMONY_ID])))[1] == [ModRef(HARMONY_ID, "Harmony", HARMONY_WORKSHOP_ID)]

    # --- alternativePackageIds, deps RimWorld does not enforce, SteamCMD-cache-only copies, '_dupN' copies
    I9 = inst(official()[1], mk("rsc", deps=[ModDependency("old.rs", "Replace Stuff", "", "1372003680", ["new.rs"])]),
              mk("new.rs"), mk("t.b", source="steamcmd", wid="777"), mk("t.foo", "Foo"), mk("t.foo", "Foo copy", uid="t.foo_dup1"),
              mk("t.a", deps=["t.b", ModDependency("t.half", enforced=False)]))
    assert resolve_dependencies([CORE_ID, "rsc"], I9)[:2] == (["new.rs"], [])      # an installed alternative beats a download
    assert not [i for i in check([CORE_ID, "new.rs", "rsc"], I9) if i.kind in ("missing_dependency", "order")]
    assert build_edges(["rsc", "new.rs"], I9)["rsc"] == {"new.rs"}
    add, refs, _ = resolve_dependencies([CORE_ID, "t.a"], I9)
    assert add == [] and [r.workshop_id for r in refs] == ["777"], (add, refs)     # a SteamCMD cache copy is never activated
    md = sorted((i.level, (i.fix or {}).get("action")) for i in check([CORE_ID, "t.a"], I9) if i.kind == "missing_dependency")
    assert md == [("error", "download"), ("warning", None)], md
    dup = [i.fix for i in check([CORE_ID, "t.foo_dup1"], I9) if i.kind == "not_in_game"]
    assert dup == [{"action": "replace", "uid": "t.foo_dup1", "ref": ModRef("t.foo", "Foo copy", "")}], dup
    assert [i.fix["action"] for i in check([CORE_ID, "t.foo", "t.foo_dup1"], I9) if i.kind == "not_in_game"] == ["remove"]
    I9s = dict(I9, **inst(mk("t.foo", "Foo (workshop)", uid="t.foo_steam", source="workshop")))
    dup = [i.fix for i in check([CORE_ID, "t.foo_steam", "t.foo_dup1"], I9s) if i.kind == "not_in_game"]
    assert dup == [{"action": "remove", "uids": ["t.foo_dup1"]}], dup    # the '_steam' copy is already active

    # --- download_closure: Steam 'required items' are hints; never walk through met, variant or superseded ones
    db = _FakeDB(rows=[DbMod("10", "ce.buff", "CE Buff", dependencies={"11": "CE Dev", "1149640": "Royalty"}),
                       DbMod("11", "", "", "Combat Extended - Dev Snapshot", dependencies={"12": "Deep"}),
                       DbMod("12", "deep.lib", "Deep lib"),
                       DbMod("20", "rs.compat", "RS Compat", dependencies={"21": "Replace Stuff"}),
                       DbMod("21", "old.rs", "Replace Stuff", game_versions=["1.5"]),
                       DbMod("30", "blood.patch", "Blood patch", dependencies={"31": "Bloody Mess", "40": "Lib"}),
                       DbMod("31", "old.blood", "Bloody Mess", game_versions=["1.5"]),
                       DbMod("32", "new.blood", "Bloody Mess (Continued)", game_versions=["1.6"], dependencies={"12": "D"}),
                       DbMod("40", "t.lib", "Lib", dependencies={"41": "Lib dep"}), DbMod("41", "t.libdep", "Lib dep"),
                       DbMod("50", "x.y", "X", dependencies={"51": "Needs"}), DbMod("51", "", "", "Needed Framework")],
                 uti={"old.rs": {"newWorkshopId": "22", "newPackageId": "New.RS", "newVersions": ["1.6"]},
                      "old.blood": {"newWorkshopId": "32", "newPackageId": "new.blood", "newVersions": ["1.6"]}})
    I10 = inst(official()[1], mk("ceteam.ce", "Combat Extended", wid="2890901044"), mk("t.lib", "Lib", wid="40"),
               mk("x.y", source="steamcmd", wid="50"))
    refs = [ModRef("ce.buff", "CE Buff", "10"), ModRef("rs.compat", "RS Compat", "20"), ModRef("new.rs", "RS", "22"),
            ModRef("blood.patch", "", "30"), ModRef("rs.compat", "again", "20"), ModRef("x.y", "X", "50")]
    out = [(r.package_id, r.workshop_id) for r in download_closure(refs, I10, db, "1.6.4871 rev590")]
    assert out == [("ce.buff", "10"), ("rs.compat", "20"), ("new.rs", "22"), ("blood.patch", "30"), ("x.y", "50"),
                   ("new.blood", "32"), ("", "51"), ("deep.lib", "12")], out
    assert _variant_of("NewRatkinPlus[1.6]", {"newratkinplus"}) and not _variant_of("Lib - Flowers", {"lib"})

    # --- a stale About.xml link (legacy '[1.0]' upload) loses to the DB upload of that package id that supports 1.6
    vef = "oskarpotocki.vanillafactionsexpanded.core"
    db = _FakeDB(rows=[DbMod("1854607105", "", "", "[1.0] Vanilla Factions Expanded - Core", game_versions=["1.0"]),
                       DbMod("2023507013", vef, "Vanilla Expanded Framework", game_versions=["1.0", "1.5", "1.6"]),
                       DbMod("2023507099", vef, "VEF fork", game_versions=["1.6"])])
    I11 = inst(official()[1], mk("vpe", deps=[ModDependency(vef, "Vanilla Expanded Framework", "u", "1854607105")]))
    want = [ModRef(vef, "Vanilla Expanded Framework", "2023507013")]
    assert resolve_dependencies([CORE_ID, "vpe"], I11, db, "1.6")[1] == want
    assert [i.fix["refs"] for i in check([CORE_ID, "vpe"], I11, db, "1.6") if i.kind == "missing_dependency"] == [want]
    assert _lookup_wid(vef, "1854607105", db, "1.0")[0] == "1854607105"                   # a link that fits is kept
    assert _lookup_wid(vef, "1854607105", db, "1.6", {"2023507099"})[0] == "2023507099"   # a copy on disk first
    assert _lookup_wid(vef, "42", db, "1.6")[0] == "42"                                   # unknown to the DB: trusted
    db = _FakeDB(rows=[DbMod("77", "x.addon", "Addon", dependencies={"88": "Old Lib"}),   # a stale Steam required item
                       DbMod("88", "old.lib", "Old Lib [1.4]", game_versions=["1.4"]),
                       DbMod("89", "old.lib", "Old Lib", game_versions=["1.6"])])
    assert download_closure([ModRef("x.addon", "Addon", "77")], I11, db, "1.6")[1:] == [ModRef("old.lib", "Old Lib", "89")]

    # --- check: every issue kind
    db = _FakeDB(rows=[DbMod("700", "dl.me", "Download Me", game_versions=["1.6"]),
                       DbMod("701", "need.me", "Need Me", game_versions=["1.6"]),
                       DbMod("702", "need.me", "Need Me (old fork)", game_versions=["1.4"])],
                 rules={"inc.b": {"incompatible_with": {"inc.a": "breaks"}}},
                 uti={"old.mod": {"oldPackageId": "Old.Mod", "newWorkshopId": "800", "newPackageId": "New.Mod",
                                  "newName": "New Mod", "newVersions": ["1.6"]}},
                 nvw={"quiet.mod"})
    I6 = inst(official()[0], official()[1], official()[2], official()[4],   # harmony, core, royalty, biotech owned
              mk("inc.a"), mk("inc.b"), mk("inc.c", incompat=["inc.a"]), mk("inc.d", incompat=["inc.e"]), mk("inc.e", before=["inc.d"]),
              mk("needs", deps=["need.me", "inactive.lib", "nowhere.lib", "ludeon.rimworld.biotech",
                                "ludeon.rimworld.ideology"]),
              mk("inactive.lib"), mk("old.mod", versions=("1.4",)), mk("noversions", versions=()),
              mk("quiet.mod", versions=("1.2",)), mk("broken", parse_error="About.xml missing packageId; used folder name"),
              mk("steamy", uid="steamy"))
    act6 = [HARMONY_ID, "ludeon.rimworld.royalty", "inc.a", "inc.b", "inc.c", "inc.d", "inc.e", "needs", "dl.me",
            "unknown.mod", "steamy_steam", "ludeon.rimworld.anomaly", "old.mod", "noversions", "quiet.mod", "broken"]
    iss = check(act6, I6, db, "1.6", owned_dlcs={"ludeon.rimworld.royalty", "ludeon.rimworld.biotech"})
    by = lambda kind, uid=None: [i for i in iss if i.kind == kind and (uid is None or i.uid == uid)]
    assert by("missing_core")[0].fix == {"action": "add", "uids": [CORE_ID]} and iss[0].kind == "missing_core"
    assert by("not_installed", "dl.me")[0].fix == {"action": "download", "refs": [ModRef("dl.me", "Download Me", "700")]}
    assert by("not_installed", "unknown.mod")[0].fix == {"action": "remove", "uids": ["unknown.mod"]}
    assert by("not_installed", "steamy_steam")[0].fix["action"] == "replace"
    assert by("dlc_missing", "ludeon.rimworld.anomaly") and by("dlc_missing", "needs")    # active DLC / dep DLC not owned
    md = {re.search(r"requires (.+?)(?:,| \(|:)", i.message).group(1): i.fix for i in by("missing_dependency", "needs")}
    assert md["Need Me"] == {"action": "download", "refs": [ModRef("need.me", "Need Me", "701")]}, md
    assert md["inactive.lib"] == {"action": "add", "uids": ["inactive.lib"]}, md
    assert md["the Biotech DLC"] == {"action": "add", "uids": ["ludeon.rimworld.biotech"]}, md
    assert md["nowhere.lib"] is None, md
    inc = {frozenset([i.uid, *i.related]) for i in by("incompatible")}
    assert inc == {frozenset(["inc.a", "inc.b"]), frozenset(["inc.a", "inc.c"]), frozenset(["inc.d", "inc.e"])}, inc
    assert all(i.fix == {"action": "remove", "uids": [i.uid]} for i in by("incompatible"))
    assert by("incompatible", "inc.b") and by("incompatible", "inc.c") and by("incompatible", "inc.e")
    assert {i.uid for i in by("version")} == {"old.mod", "noversions"}, by("version")
    r = by("replacement", "old.mod")[0]
    assert r.level == "info" and r.fix == {"action": "replace", "uid": "old.mod", "ref": ModRef("new.mod", "New Mod", "800")}
    assert by("no_packageid", "broken") and by("order", "inc.d")
    lv = [LEVEL_RANK[i.level] for i in iss]
    assert lv == sorted(lv)
    for level in ("error", "warning"):
        idx = [act6.index(i.uid) if i.uid else -1 for i in iss if i.level == level]
        assert idx == sorted(idx), (level, idx)
    assert check([CORE_ID], inst(official()[1]), version_short="1.6") == []     # Core never version-flagged
    assert not by("version", "quiet.mod")                                          # NoVersionWarning respected

    # --- property: a freshly sorted list has no 'order' issues (random graphs, cycles, tiers, rules, unknowns)
    rnd = random.Random(1234)
    for trial in range(250):
        n = rnd.randint(1, 40)
        ids = [f"m{i}" for i in range(n)]
        mods = official()[: rnd.randint(0, 7)]
        pool = ids + [x.uid for x in mods] + ["ghost"]
        for u in ids:
            k = lambda: rnd.sample(pool, min(len(pool), rnd.randint(0, 3 if trial % 3 else 1)))
            mods.append(mk(u, f"N{rnd.randint(0, 9)}", deps=k(), after=k(), before=k()))
        rr = {}
        for u in rnd.sample(pool, min(len(pool), 6)):
            rr[u] = {"load_after": dict.fromkeys(rnd.sample(pool, 2), ""), "load_before": dict.fromkeys(rnd.sample(pool, 1), ""),
                     "load_top": rnd.random() < 0.3, "load_bottom": rnd.random() < 0.3}
        ur = {rnd.choice(pool): {"load_after": {rnd.choice(pool): ""}, "load_bottom": rnd.random() < 0.2}}
        I7 = inst(*mods)
        act7 = list(I7) + ["ghost", "ghost_steam"] * (trial % 2)
        rnd.shuffle(act7)
        for tb in ("current", "alphabetical"):
            out, _ = sort_order(act7, I7, rr, ur, tb)
            assert sorted(out) == sorted(set(act7)), (trial, out)
            bad = [i for i in check(out, I7, _FakeDB(rules=rr), user_rules=ur) if i.kind == "order"]
            assert not bad, (trial, tb, bad)
            if not _plan(act7, I7, rr, ur).cycles:
                assert sort_order(out, I7, rr, ur, tb)[0] == out, (trial, tb)

    # --- performance: 1500 mods, random acyclic edges
    rnd = random.Random(7)
    ids = [f"perf.m{i}" for i in range(1500)]
    mods = official() + [mk(u, f"Mod {rnd.random():.6f}", deps=rnd.sample(ids[:i], min(i, rnd.randint(0, 3))),
                            after=rnd.sample(ids[:i], min(i, rnd.randint(0, 2))),
                            before=rnd.sample(ids[i + 1:], min(1499 - i, rnd.randint(0, 1))))
                         for i, u in enumerate(ids)]
    I8 = inst(*mods)
    act8 = list(I8)
    rnd.shuffle(act8)
    rules8 = {u: {"load_after": {rnd.choice(ids[:j]): ""}} for j, u in enumerate(ids[1:], 1) if j % 10 == 0}
    rules8.update({ids[5]: {"load_top": True}, ids[1400]: {"load_bottom": True}})
    t = time.perf_counter()
    out, warns = sort_order(act8, I8, rules8)
    dt = time.perf_counter() - t
    assert dt < 0.2, f"sort_order took {dt * 1000:.0f} ms"
    assert not [w for w in warns if "loop" in w], warns
    t = time.perf_counter()
    assert not [i for i in check(out, I8, _FakeDB(rules=rules8), "1.6") if i.kind in ("order", "cycle")]
    assert time.perf_counter() - t < 1.0
    at = {u: i for i, u in enumerate(out)}
    assert all(at[d.package_id] < at[u] for u in ids for d in I8[u].dependencies)


class _FakeDB:
    """Minimal duck-typed stand-in for rmm.database.ModDatabase used by _selftest."""

    def __init__(self, rows=(), rules=None, uti=None, nvw=()):
        self.rows = {r.workshop_id: r for r in rows}
        self._rules, self._uti, self._nvw = rules or {}, uti or {}, set(nvw)

    def get(self, wid):
        return self.rows.get(wid)

    def get_many(self, ids):
        return {i: self.rows[i] for i in ids if i in self.rows}

    def by_package_id(self, pid):
        return sorted((r for r in self.rows.values() if r.package_id == pid.lower()), key=lambda r: r.unpublished)

    def rules(self):
        return self._rules

    def replacement_for(self, workshop_id="", package_id=""):
        return self._uti.get(package_id)

    def no_version_warning(self, version_short):
        return self._nvw


if __name__ == "__main__":
    _selftest()
    print("sorting selftest OK")
