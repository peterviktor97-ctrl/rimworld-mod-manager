"""LLM providers + the anti-hallucination modlist generator (and an offline generator).

The model never supplies data we trust. It plans search queries, then chooses ids from a table
we built out of the mod database (enum-constrained when the provider supports structured output).
Every id is re-validated against the DB, optionally verified live on Steam, and names/package ids
always come from DB rows (or the Use-This-Instead DB for replacements) - never from the model.
With the live Workshop on (a Steam Web API key), mods Steam finds that the DB lacks are written to
the DB first (Steam's data, not the model's), so they pass through the same checks.
"""
from __future__ import annotations

import http.client
import json
import logging
import math
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .models import (DLC_BY_APPID, DLCS, HARMONY_WORKSHOP_ID, RIMWORLD_APPID, USER_AGENT, DbMod, ModRef, Progress,
                     compat_versions, noop_progress)
from .sorting import download_closure

log = logging.getLogger("rmm.ai")

# ---------------------------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------------------------

PRESETS: dict[str, dict] = {
    "Antigravity (your subscription)": {"provider": "antigravity", "base_url": "", "model": "gemini-4-argon-high",
                                        "key_url": "https://antigravity.google", "needs_key": False},
    "Anthropic (Claude)": {"provider": "anthropic", "base_url": "", "model": "claude-opus-5",
                           "key_url": "https://console.anthropic.com/settings/keys", "needs_key": True},
    "OpenAI": {"provider": "openai_compat", "base_url": "https://api.openai.com/v1", "model": "gpt-5-mini",
               "key_url": "https://platform.openai.com/api-keys", "needs_key": True},
    "OpenRouter": {"provider": "openai_compat", "base_url": "https://openrouter.ai/api/v1", "model": "openrouter/auto",
                   "key_url": "https://openrouter.ai/keys", "needs_key": True},
    "Groq": {"provider": "openai_compat", "base_url": "https://api.groq.com/openai/v1",
             "model": "llama-3.3-70b-versatile", "key_url": "https://console.groq.com/keys", "needs_key": True},
    "Google Gemini": {"provider": "openai_compat", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                      "model": "gemini-4-argon", "key_url": "https://aistudio.google.com/apikey", "needs_key": True},
    "Ollama (local)": {"provider": "openai_compat", "base_url": "http://127.0.0.1:11434/v1", "model": "",
                       "key_url": "https://ollama.com/download", "needs_key": False},
    "LM Studio (local)": {"provider": "openai_compat", "base_url": "http://127.0.0.1:1234/v1", "model": "",
                          "key_url": "https://lmstudio.ai", "needs_key": False},
    "Custom (OpenAI-compatible)": {"provider": "openai_compat", "base_url": "", "model": "", "key_url": "",
                                   "needs_key": False},
    "Claude Code (your subscription)": {"provider": "claude_code", "base_url": "", "model": "",
                                        "key_url": "https://claude.com/claude-code", "needs_key": False},
    "Offline (no AI)": {"provider": "offline", "base_url": "", "model": "", "key_url": "", "needs_key": False},
}
ANTIGRAVITY_MODEL_MAP: dict[str, str] = {
    "gemini-4-argon-high": "MODEL_PLACEHOLDER_M417",
    "gemini-4-argon-medium": "MODEL_PLACEHOLDER_M418",
    "gemini-4-argon-low": "MODEL_PLACEHOLDER_M419",
    "gemini-3.8-flash-high": "MODEL_PLACEHOLDER_M318",
    "gemini-3.8-flash-medium": "MODEL_PLACEHOLDER_M319",
    "gemini-3.8-flash-low": "MODEL_PLACEHOLDER_M320",
    "claude-opus-5-5-max": "MODEL_PLACEHOLDER_M406",
    "claude-opus-5-5-high": "MODEL_PLACEHOLDER_M402",
    "claude-opus-5-5-medium": "MODEL_PLACEHOLDER_M401",
    "claude-opus-5-5-low": "MODEL_PLACEHOLDER_M400",
    "claude-sonnet-5-5-max": "MODEL_PLACEHOLDER_M407",
    "claude-sonnet-5-5-high": "MODEL_PLACEHOLDER_M405",
    "claude-sonnet-5-5-medium": "MODEL_PLACEHOLDER_M404",
    "claude-sonnet-5-5-low": "MODEL_PLACEHOLDER_M403",
    "gemini-3.7-flash-high": "MODEL_PLACEHOLDER_M298",
    "gemini-3.7-flash-medium": "MODEL_PLACEHOLDER_M299",
    "gemini-3.7-flash-low": "MODEL_PLACEHOLDER_M300",
    "gemini-3.6-flash-high": "MODEL_PLACEHOLDER_M71",
    "gemini-3.6-flash-medium": "MODEL_PLACEHOLDER_M72",
    "gemini-3.6-flash-low": "MODEL_PLACEHOLDER_M73",
    "gemini-3.1-pro-high": "MODEL_PLACEHOLDER_M37",
    "gemini-3.1-pro-low": "MODEL_PLACEHOLDER_M36",
}
ANTIGRAVITY_MODELS: list[str] = [
    "gemini-4-argon-high",
    "gemini-4-argon-medium",
    "gemini-4-argon-low",
    "gemini-4-argon",
    "gemini-3.8-flash-high",
    "gemini-3.8-flash-medium",
    "gemini-3.8-flash-low",
    "gemini-3.8-flash",
    "claude-opus-5-5-max",
    "claude-opus-5-5-high",
    "claude-opus-5-5-medium",
    "claude-opus-5-5-low",
    "claude-opus-5-5",
    "claude-sonnet-5-5-max",
    "claude-sonnet-5-5-high",
    "claude-sonnet-5-5-medium",
    "claude-sonnet-5-5-low",
    "claude-sonnet-5-5",
    "gemini-3.7-flash-high",
    "gemini-3.7-flash-medium",
    "gemini-3.7-flash-low",
    "gemini-3.6-flash-high",
    "gemini-3.6-flash-medium",
    "gemini-3.6-flash-low",
    "gemini-3.1-pro-high",
    "gemini-3.1-pro-low",
]
CLAUDE_MODELS = ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5", "claude-opus-5-5", "claude-fable-5-1"]
_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_FATAL_KINDS = {"auth", "connection", "config", "quota"}   # retrying another AI step would fail the same way


class LLMError(Exception):
    """Friendly, user-facing LLM failure. kind: auth|connection|config|quota|refusal|format|server|''."""

    def __init__(self, message: str, kind: str = ""):
        super().__init__(message)
        self.kind = kind


class _HTTPStatus(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(f"HTTP {status}")
        self.status, self.text = status, text


def _is_local(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.startswith("192.168.") or host.startswith("10.")


def _local_hint(url: str) -> str:
    if ":11434" in url:
        return "Is Ollama running? Start it (or run 'ollama serve') and pull a model."
    if ":1234" in url:
        return "Is LM Studio's local server started with a model loaded?"
    return "Is the local AI server running?"


def _err_text(text: str) -> str:
    """Best human message from an error body (OpenAI / Gemini / Ollama / plain)."""
    try:
        data = json.loads(text)
    except ValueError:
        return (text or "").strip()[:300]
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        err = data.get("error", data)
        if isinstance(err, dict):
            return str(err.get("message") or err.get("detail") or err)[:300]
        return str(err or data.get("message") or data.get("detail") or data)[:300]
    return str(data)[:300]


def _http_json(method: str, url: str, body: dict | None = None, key: str = "", timeout: float = 60):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if key:
        headers["Authorization"] = "Bearer " + key
    host = urllib.parse.urlsplit(url).netloc or url
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method=method),
                                    timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise _HTTPStatus(e.code, e.read().decode("utf-8", "replace")[:4000]) from None
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        if isinstance(e, TimeoutError) or isinstance(reason, TimeoutError):
            raise LLMError(f"{host} did not answer within {int(timeout)} s.", "connection") from None
        if _is_local(url):
            raise LLMError(f"Could not connect to {host}. {_local_hint(url)} ({reason})", "connection") from None
        raise LLMError(f"Could not reach {host}: {reason}. Check your internet connection.", "connection") from None
    except (http.client.InvalidURL, ValueError) as e:
        raise LLMError(f"Invalid base URL {url!r}: {e}. It should look like http://127.0.0.1:11434/v1", "config") from None
    except http.client.HTTPException as e:      # IncompleteRead, RemoteDisconnected, BadStatusLine
        raise LLMError(f"{host} closed the connection mid-reply ({e!r}). Try again.", "connection") from None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise LLMError(f"{host} answered with something that is not JSON: {raw[:160]!r}", "server") from None


def _friendly_http(e: _HTTPStatus, url: str, model: str) -> LLMError:
    msg = _err_text(e.text)
    if e.status in (401, 403):
        return LLMError(f"The AI server rejected the API key (HTTP {e.status}). Check the key in Settings > AI. {msg}", "auth")
    if e.status == 404:
        return LLMError(f"Not found (HTTP 404): check the base URL and that model '{model}' exists. {msg}", "config")
    if e.status == 429:
        return LLMError(f"Rate limited or out of quota (HTTP 429). Wait a minute or check your plan/credits. {msg}", "quota")
    if e.status >= 500:
        return LLMError(f"The AI server had an error (HTTP {e.status}). Try again later. {msg}", "server")
    return LLMError(f"The AI server refused the request (HTTP {e.status}): {msg}", "server")


def _strip_trailing_commas(s: str) -> str:
    return re.sub(r",\s*([}\]])", r"\1", s)


def _extract_json(text: str) -> dict:
    """First JSON object in a model reply. Handles ``` fences, <think> blocks, prose before/after,
    trailing commas. A bare top-level list is returned as {'items': [...]}. Raises LLMError."""
    t = (text or "").strip().lstrip("﻿")
    t = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", t, flags=re.S | re.I).strip()
    texts = [t] + re.findall(r"```[a-zA-Z]*\s*(.*?)```", t, re.S)
    dec = json.JSONDecoder()
    first_list = None
    for variant in (texts, [_strip_trailing_commas(x) for x in texts]):
        for s in variant:
            pos, tries = 0, 0
            while tries < 300:
                m = re.compile(r"[{\[]").search(s, pos)
                if not m:
                    break
                tries += 1
                try:
                    value, end = dec.raw_decode(s, m.start())
                except ValueError:
                    pos = m.start() + 1
                    continue
                if isinstance(value, dict):
                    return value
                if first_list is None:
                    first_list = value
                pos = end          # skip the whole parsed list, look for a later top-level object
    if first_list is not None:
        return {"items": first_list}
    raise LLMError("The AI reply did not contain valid JSON: " + (t[:160] or "(empty reply)"), "format")


def _extract_cut_json(text: str) -> dict:
    """JSON object from a reply cut off at the token limit: the whole object if it is complete, else the
    object with its open list closed after the last complete item (picks already written are kept).
    Never returns an inner item. Raises LLMError."""
    t = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", text or "", flags=re.S | re.I)
    t = t[t.find("{"):] if "{" in t else ""
    end = len(t)
    for _ in range(20):
        for tail in ("", "]}"):
            try:
                v = json.loads(t[:end] + tail)
            except ValueError:
                continue
            if isinstance(v, dict):
                return v
        end = t.rfind("}", 0, end - 1) + 1
        if end <= 0:
            break
    raise LLMError("The AI reply was cut off (token limit reached). Lower the target count or effort.", "format")


_AGY_ENV_KEYS = ("ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CSRF_TOKEN", "ANTIGRAVITY_AGENTAPI_EXE",
                 "ANTIGRAVITY_APP_DATA_DIR", "ANTIGRAVITY_PROJECT_ID")


def _antigravity_env() -> dict[str, str]:
    """Environment for Antigravity's agentapi CLI, caching session variables when started inside Antigravity."""
    env = dict(os.environ)
    cache_paths = []
    try:
        from . import config
        cache_paths.append(config.app_path("antigravity_env.json"))
    except Exception:  # noqa: BLE001
        pass
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        real_cache = os.path.join(appdata, "RimModManager", "antigravity_env.json")
        if real_cache not in cache_paths:
            cache_paths.append(real_cache)
    if env.get("ANTIGRAVITY_LS_ADDRESS") and env.get("ANTIGRAVITY_CSRF_TOKEN"):
        saved = {k: env[k] for k in _AGY_ENV_KEYS if env.get(k)}
        for p in cache_paths:
            try:
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(saved, f, indent=2)
            except OSError:
                pass
        return env
    for p in cache_paths:
        try:
            with open(p, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if isinstance(cached, dict) and cached.get("ANTIGRAVITY_LS_ADDRESS"):
                for k in _AGY_ENV_KEYS:
                    if cached.get(k) and not env.get(k):
                        env[k] = str(cached[k])
                break
        except (OSError, ValueError):
            continue
    return env


def antigravity_exe() -> str:
    """Path of Antigravity's language_server(.exe) or agentapi CLI, or ''."""
    import shutil
    env = _antigravity_env()
    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA", "")
    cands = [
        env.get("ANTIGRAVITY_AGENTAPI_EXE", ""),
        os.path.join(local, "Programs", "antigravity", "resources", "bin", "language_server.exe") if local else "",
        os.path.join(home, "AppData", "Local", "Programs", "antigravity", "resources", "bin", "language_server.exe"),
        shutil.which("agentapi") or "",
        os.path.join(home, ".gemini", "antigravity", "bin", "agentapi.bat" if os.name == "nt" else "agentapi"),
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return ""


def _antigravity_rpc(method: str, payload: dict, env: dict[str, str] | None = None, timeout: float = 30.0) -> dict:
    """Call a ConnectRPC method on the active Antigravity language server."""
    env = env or _antigravity_env()
    addr = (env.get("ANTIGRAVITY_LS_ADDRESS") or "").strip()
    csrf = (env.get("ANTIGRAVITY_CSRF_TOKEN") or "").strip()
    if not addr or not csrf:
        raise LLMError("Antigravity session not found. Launch RimModManager from Antigravity once so it can "
                       "connect to the active Antigravity language server.", "connection")
    url = f"http://{addr}/exa.language_server_pb.LanguageServerService/{method}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json", "x-codeium-csrf-token": csrf},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:600]
        kind = "quota" if e.code == 429 or "RESOURCE_EXHAUSTED" in body else "auth" if e.code in (401, 403) else "server"
        raise LLMError(f"Antigravity language server error (HTTP {e.code}): {_err_text(body)}", kind) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        raise LLMError(f"Could not connect to Antigravity language server at {addr} ({e}). "
                       "Make sure Antigravity is running.", "connection") from None


def _antigravity_resolve_model(model: str, effort: str = "high",
                               env: dict[str, str] | None = None) -> tuple[str, str]:
    """Resolve a user-facing Antigravity model name (or family alias + effort) to (model_id, MODEL_PLACEHOLDER_*)."""
    raw = (model or "").strip()
    if raw.startswith("MODEL_PLACEHOLDER_"):
        return raw, raw
    m = raw.lower()
    tier3 = "low" if effort == "low" else "medium" if effort == "medium" else "high"
    tier4 = "low" if effort == "low" else "medium" if effort == "medium" else "max" if effort in ("xhigh", "max") else "high"
    if m in ("", "pro", "argon", "gemini-4", "gemini-4-argon"):
        m = f"gemini-4-argon-{tier3}"
    elif m in ("flash", "gemini-3.8-flash"):
        m = f"gemini-3.8-flash-{tier3}"
    elif m == "flash_lite":
        m = "gemini-3.8-flash-low"
    elif m == "gemini-3.7-flash":
        m = f"gemini-3.7-flash-{tier3}"
    elif m == "gemini-3.6-flash":
        m = f"gemini-3.6-flash-{tier3}"
    elif m == "gemini-3.1-pro":
        m = "gemini-3.1-pro-low" if effort == "low" else "gemini-3.1-pro-high"
    elif m in ("claude-opus-5-5", "claude-sonnet-5-5"):
        m = f"{m}-{tier4}"
    if m in ANTIGRAVITY_MODEL_MAP:
        return m, ANTIGRAVITY_MODEL_MAP[m]
    if env and env.get("ANTIGRAVITY_LS_ADDRESS") and env.get("ANTIGRAVITY_CSRF_TOKEN"):
        try:
            data = _antigravity_rpc("GetCascadeModelConfigData", {}, env=env, timeout=8)
            for cfg in data.get("clientModelConfigs") or []:
                mid = str(cfg.get("modelId") or "").strip()
                lbl = str(cfg.get("label") or "").strip()
                enum_val = str(((cfg.get("modelOrAlias") or {}).get("model")) or "").strip()
                if enum_val and m in (mid.lower(), lbl.lower()):
                    return (mid or m), enum_val
        except Exception:  # noqa: BLE001
            pass
    fallback = f"gemini-4-argon-{tier3}"
    return fallback, ANTIGRAVITY_MODEL_MAP[fallback]


def _antigravity_shrink_prompt(prompt: str, limit: int = 26000) -> str:
    """Trim candidate table descriptions if needed so the prompt fits in a single Windows CreateProcess call."""
    if len(prompt) <= limit:
        return prompt
    lines = prompt.splitlines()
    table_idx = [i for i, ln in enumerate(lines) if ln.count(" | ") >= 7 and ln.split(" | ", 1)[0].strip().isdigit()]
    if not table_idx:
        return prompt
    joined = prompt
    for desc_cap in (120, 60, 25, 0):
        out = list(lines)
        for i in table_idx:
            parts = out[i].split(" | ", 7)
            if len(parts) == 8:
                parts[7] = parts[7][:desc_cap]
                out[i] = " | ".join(parts).rstrip(" |")
        joined = "\n".join(out)
        if len(joined) <= limit:
            return joined
    return joined


def claude_code_exe() -> str:
    """Path of the real claude(.exe), or ''. npm's claude.cmd shim is skipped: cmd.exe would mangle JSON arguments."""
    import shutil
    found = shutil.which("claude") or ""
    if found.lower().endswith(".exe"):
        return found
    npm_exe = os.path.join("node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe")
    home = os.path.expanduser("~")
    for c in ((os.path.join(os.path.dirname(found), npm_exe) if found else ""),
              os.path.join(home, ".local", "bin", "claude.exe"),
              os.path.join(os.environ.get("APPDATA", ""), "npm", npm_exe)):
        if c and os.path.isfile(c):
            return c
    return found if found and os.name != "nt" else ""


def _without_enums(v):
    if isinstance(v, dict):
        return {k: _without_enums(x) for k, x in v.items() if k != "enum"}
    return [_without_enums(x) for x in v] if isinstance(v, list) else v


class LLM:
    """One configured model. complete_json() is blocking: call it from a worker thread."""

    def __init__(self, provider: str, model: str, api_key: str = "", base_url: str = "", effort: str = "medium"):
        self.provider = (provider or "anthropic").strip()
        self.model = (model or "").strip()
        self.api_key = (api_key or "").strip()
        # only OpenAI-compatible servers use it: a stale URL must never receive an Anthropic key
        self.base_url = (base_url or "").strip().rstrip("/") if self.provider == "openai_compat" else ""
        if self.base_url and "://" not in self.base_url:
            self.base_url = "http://" + self.base_url      # 'localhost:11434/v1': urllib needs a scheme
        self.effort = effort if effort in _EFFORTS else "medium"
        self.last_usage = ""        # "12,345 in / 678 out tokens" after each call
        self._client = None
        self._proc = None           # running Claude Code process, killed by abort()
        self._stream = None         # open Anthropic stream, closed by abort()
        self._cascade_id = ""       # active Antigravity cascade id, cancelled by abort()
        self._aborted = False       # set by abort(), never reset: an LLM serves one run

    def abort(self) -> None:
        """Cancel from another thread. Flag first, then read the handles: the worker stores a handle, then checks
        the flag, so one of the two always sees the other."""
        self._aborted = True
        p, s, cid = self._proc, self._stream, self._cascade_id
        if p is not None:
            from .steam import _kill_tree
            _kill_tree(p)                # claude.exe runs its work in a child claude.exe
        if s is not None:
            try:
                s.close()                # ends the HTTP response: no more tokens generated/billed
            except Exception:  # noqa: BLE001 - best effort, the worker thread is still reading it
                pass
        if cid:
            try:
                _antigravity_rpc("CancelCascadeInvocation", {"cascadeId": cid}, timeout=5)
            except Exception:  # noqa: BLE001
                pass

    def __repr__(self) -> str:
        return f"LLM({self.provider!r}, {self.model!r}, base_url={self.base_url!r})"

    # -- public -------------------------------------------------------------------------------
    def complete_json(self, system: str, user: str, schema: dict, max_tokens: int = 16000) -> dict:
        if self.provider == "antigravity":
            return self._antigravity_json(system, user, schema)
        if self.provider == "anthropic":
            return self._anthropic_json(system, user, schema, max_tokens)
        if self.provider == "openai_compat":
            return self._openai_json(system, user, schema, max_tokens)
        if self.provider == "claude_code":
            return self._claude_code_json(system, user, schema)
        raise LLMError(f"The '{self.provider}' provider has no language model.", "config")

    def list_models(self) -> list[str]:
        if self.provider == "antigravity":
            out = list(ANTIGRAVITY_MODELS)
            try:
                env = _antigravity_env()
                if env.get("ANTIGRAVITY_LS_ADDRESS") and env.get("ANTIGRAVITY_CSRF_TOKEN"):
                    data = _antigravity_rpc("GetCascadeModelConfigData", {}, env=env, timeout=8)
                    for cfg in data.get("clientModelConfigs") or []:
                        mid = str(cfg.get("modelId") or "").strip()
                        if mid and mid not in out:
                            out.append(mid)
            except Exception as e:  # noqa: BLE001
                log.info("Antigravity live model fetch skipped: %s", e)
            return out
        if self.provider == "claude_code":
            return ["fable", "opus", "sonnet", "haiku"]      # the CLI's aliases; '' = its default model
        try:
            if self.provider == "anthropic":
                return [m.id for m in self._anthropic_client().models.list(limit=100)]
            if self.provider == "openai_compat" and self.base_url:
                data = _http_json("GET", self.base_url + "/models", key=self.api_key, timeout=20)
                items = (data.get("data") or data.get("models") or []) if isinstance(data, dict) else data
                ids = {str((m.get("id") or m.get("name") or "") if isinstance(m, dict) else m) for m in items or []}
                return sorted(i.removeprefix("models/") for i in ids if i)
        except Exception as e:  # noqa: BLE001 - documented: [] on any failure
            log.info("list_models(%s) failed: %s", self.provider, e)
        return []

    # -- Antigravity (the user's Antigravity subscription, no API key) ------------------------
    def _antigravity_json(self, system: str, user: str, schema: dict) -> dict:
        if not antigravity_exe():
            raise LLMError("Antigravity CLI was not found. Make sure Antigravity is installed.", "config")
        env = _antigravity_env()
        if not env.get("ANTIGRAVITY_LS_ADDRESS") or not env.get("ANTIGRAVITY_CSRF_TOKEN"):
            raise LLMError("Antigravity session not found. Launch RimModManager from Antigravity once so it can "
                           "connect to the active Antigravity language server.", "connection")
        if self._aborted:
            raise LLMError("Cancelled.", "connection")
        model_name, model_enum = _antigravity_resolve_model(self.model, self.effort, env)
        prompt = ("IMPORTANT: Reply immediately with ONLY a single valid JSON object matching the schema below. "
                  "Do NOT wrap in markdown code fences and do NOT include any commentary.\n\n"
                  f"{system}{_json_hint(schema)}\n\n{user}")
        start = _antigravity_rpc("StartCascade", {
            "source": "CORTEX_TRAJECTORY_SOURCE_CASCADE_CLIENT",
            "requestedModel": model_enum,
        }, env=env, timeout=30)
        cid = str(start.get("cascadeId") or "").strip()
        if not cid:
            raise LLMError(f"Antigravity failed to start a model session ({start!r}).", "server")
        self._cascade_id = cid
        try:
            if self._aborted:
                raise LLMError("Cancelled.", "connection")
            _antigravity_rpc("SendUserCascadeMessage", {
                "cascadeId": cid,
                "items": [{"text": prompt}],
                "cascadeConfig": {
                    "plannerConfig": {
                        "declarativeMixinConfig": {
                            "promptSections": [{"builtinName": "identity"}],
                            "tools": [],
                            "forceDisableFundamentalComponents": True,
                        },
                        "planModel": model_enum,
                        "requestedModel": {"model": model_enum},
                        "modelName": model_name,
                        "maxOutputTokens": 65536,
                        "noToolExplanation": True,
                        "retryConfig": {
                            "modelOutputRetry": {"maxRetries": 4, "forbidToolUse": True, "retryOnlyOnInvalidArgs": False},
                            "apiRetry": {"maxRetries": 8, "initialSleepDurationMs": 4000, "exponentialMultiplier": 2,
                                         "includeErrorFeedback": True, "range": 0.4},
                        },
                    },
                    "executorConfig": {"useCoreDirect": True},
                },
            }, env=env, timeout=30)
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline:
                if self._aborted:
                    raise LLMError("Cancelled.", "connection")
                traj = _antigravity_rpc("GetCascadeTrajectory", {"cascadeId": cid}, env=env, timeout=20)
                steps = ((traj.get("trajectory") or {}).get("steps")) or []
                for st in steps:
                    if st.get("type") == "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
                        status = st.get("status")
                        if status == "CORTEX_STEP_STATUS_ERROR":
                            msg = str(st.get("error") or "model error")[:400]
                            raise LLMError(f"Antigravity error ({model_name}): {msg}", "server")
                        if status == "CORTEX_STEP_STATUS_DONE":
                            usage = (st.get("metadata") or {}).get("modelUsage") or {}
                            n_in = _int(usage.get("inputTokens")) + _int(usage.get("cacheReadTokens"))
                            n_out = _int(usage.get("outputTokens"))
                            self.last_usage = f"{n_in:,} in / {n_out:,} out tokens"
                            pr = st.get("plannerResponse") or {}
                            text = str(pr.get("modifiedResponse") or pr.get("response") or "")
                            try:
                                return _extract_json(text)
                            except LLMError:
                                return _extract_cut_json(text)
                if traj.get("status") == "CASCADE_RUN_STATUS_IDLE" and len(steps) > 1:
                    errs = [s for s in steps if s.get("type") == "CORTEX_STEP_TYPE_ERROR_MESSAGE"]
                    if errs:
                        err_obj = ((errs[-1].get("errorMessage") or {}).get("error")) or {}
                        msg = str(err_obj.get("userErrorMessage") or err_obj.get("shortError") or err_obj or "model error")[:400]
                        kind = "quota" if "429" in msg or "RESOURCE_EXHAUSTED" in msg else "server"
                        raise LLMError(f"Antigravity error ({model_name}): {msg}", kind)
                time.sleep(0.25)
            raise LLMError(f"Antigravity timed out waiting for {model_name}.", "connection")
        finally:
            self._cascade_id = ""
            if self._aborted:
                try:
                    _antigravity_rpc("CancelCascadeInvocation", {"cascadeId": cid}, env=env, timeout=5)
                except Exception:  # noqa: BLE001
                    pass
            try:
                _antigravity_rpc("DeleteCascadeTrajectory", {"cascadeId": cid}, env=env, timeout=5)
            except Exception:  # noqa: BLE001
                pass

    # -- Claude Code CLI (the user's Claude subscription, no API key) ---------------------------
    def _claude_code_json(self, system: str, user: str, schema: dict) -> dict:
        exe = claude_code_exe()
        if not exe:
            raise LLMError("Claude Code was not found. Install it (claude.com/claude-code) and log in once by "
                           "running 'claude' in a terminal.", "config")
        import subprocess
        import tempfile
        if len(json.dumps(schema)) > 24000:     # Windows command lines max out at 32k: drop the id enum
            schema = _without_enums(schema)     # (picks are still validated against the database)
        with tempfile.TemporaryDirectory(prefix="rmm_cc_") as tmp:
            sys_file = os.path.join(tmp, "system.txt")
            with open(sys_file, "w", encoding="utf-8") as f:
                f.write(system)
            cmd = [exe, "-p", "--output-format", "json", "--json-schema", json.dumps(schema), "--tools", "",
                   "--system-prompt-file", sys_file, "--no-session-persistence", "--strict-mcp-config",
                   "--setting-sources", ""]
            if self.model:
                cmd += ["--model", self.model]
            if self.effort in _EFFORTS:
                cmd += ["--effort", self.effort]
            # without an API key in its environment the CLI uses the logged-in subscription, never API billing
            env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
            from .steam import _kill_tree
            if self._aborted:
                raise LLMError("Cancelled.", "connection")
            try:
                p = self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                  stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                                  cwd=tmp, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError as e:
                raise LLMError(f"Could not start Claude Code ({e}).", "config") from None
            try:
                if self._aborted:                # Cancel landed during Popen: abort() may have seen no process
                    _kill_tree(p)
                    raise LLMError("Cancelled.", "connection")
                try:
                    out, err = p.communicate(user, timeout=1800)
                except subprocess.TimeoutExpired:
                    _kill_tree(p)
                    raise LLMError("Claude Code took longer than 30 minutes (try a lower Effort in Settings > AI).",
                                   "connection") from None
            finally:
                self._proc = None
            r = subprocess.CompletedProcess(cmd, p.returncode, out, err)
        try:
            data = json.loads(r.stdout)
        except ValueError:
            data = {}
        if not isinstance(data, dict) or not data:
            msg = (r.stderr or r.stdout or "").strip()[-400:] or f"exit code {r.returncode}"
            kind = "auth" if re.search(r"log ?in|auth|credential", msg, re.I) else "server"
            raise LLMError(f"Claude Code failed: {msg}", kind)
        u = data.get("usage") or {}
        n_in = sum(_int(u.get(k)) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        self.last_usage = f"{n_in:,} in / {_int(u.get('output_tokens')):,} out tokens"
        if data.get("is_error"):
            msg = str(data.get("result") or data.get("subtype") or "unknown error")[:400]
            kind = "auth" if re.search(r"log ?in|auth|credential", msg, re.I) else "server"
            raise LLMError(f"Claude Code: {msg}" + (" (run 'claude' in a terminal and log in)" if kind == "auth"
                                                     else ""), kind)
        out = data.get("structured_output")
        return out if isinstance(out, dict) else _extract_json(str(data.get("result") or ""))

    # -- Anthropic ----------------------------------------------------------------------------
    def _anthropic_client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as e:
                raise LLMError("The Anthropic SDK is not available in this build; pick another AI preset.", "config") from e
            self._client = anthropic.Anthropic(api_key=self.api_key or None, base_url=self.base_url or None,
                                               timeout=180, max_retries=2)
        return self._client

    def _anthropic_json(self, system: str, user: str, schema: dict, max_tokens: int) -> dict:
        client = self._anthropic_client()
        import anthropic
        import httpx2        # the SDK's transport: mid-stream network errors arrive unwrapped
        model = self.model or "claude-opus-5"
        small = model.startswith("claude-haiku")      # no adaptive thinking / effort on Haiku
        kw: dict = {"model": model, "max_tokens": max_tokens, "system": system,
                    "messages": [{"role": "user", "content": user}]}
        oc: dict = {"format": {"type": "json_schema", "schema": schema}}
        if not small:
            kw["thinking"] = {"type": "adaptive"}
            oc["effort"] = self.effort
        kw["output_config"] = oc
        if model.startswith(("claude-opus-5", "claude-fable-")):
            kw["betas"] = ["server-side-fallback-2026-07-01"]
            kw["fallbacks"] = "default"
        msg = None
        for _ in range(6):
            if self._aborted:            # Cancel before the stream opened, or between 400 retries
                raise LLMError("Cancelled.", "connection")
            try:
                # streaming: the 180 s timeout then applies between chunks, not to the whole (long) answer
                with client.beta.messages.stream(**kw) as stream:
                    self._stream = stream
                    if self._aborted:
                        raise LLMError("Cancelled.", "connection")
                    msg = stream.get_final_message()
                break
            except anthropic.BadRequestError as e:
                text = _api_msg(e)
                low = text.lower()
                if "effort" in low and "effort" in oc:
                    oc.pop("effort")
                elif "thinking" in low and "thinking" in kw:
                    kw.pop("thinking")
                elif ("fallback" in low or "beta" in low) and "betas" in kw:
                    kw.pop("betas")
                    kw.pop("fallbacks", None)
                    kw.pop("extra_body", None)
                elif "format" in oc and any(w in low for w in ("schema", "format", "json")):
                    oc.pop("format")     # schema rejected: ask for JSON in the prompt and parse it from text
                    kw["system"] = system + _json_hint(schema)
                else:
                    raise LLMError(f"Claude rejected the request (400): {text}", "server") from None
                if not oc:
                    kw.pop("output_config", None)
                log.info("Anthropic 400 (%s) - retrying with a simpler request", text[:160])
            except anthropic.AuthenticationError:
                raise LLMError("Anthropic rejected the API key (401). Check it in Settings > AI "
                               "(keys: console.anthropic.com).", "auth") from None
            except anthropic.PermissionDeniedError as e:
                raise LLMError(f"This API key may not use {model} (403): {_api_msg(e)}", "auth") from None
            except anthropic.NotFoundError as e:
                raise LLMError(f"Model '{model}' was not found (404). Choose another model in Settings > AI. "
                               f"{_api_msg(e)}", "config") from None
            except anthropic.RateLimitError as e:
                raise LLMError(f"Anthropic rate limit or spend limit reached (429). Wait a minute or check your "
                               f"plan. {_api_msg(e)}", "quota") from None
            except anthropic.APIStatusError as e:
                raise LLMError(f"Anthropic API error {e.status_code}: {_api_msg(e)}. Try again later.", "server") from None
            except anthropic.APITimeoutError:
                raise LLMError("Claude did not answer in time (timeout). Try again or lower the effort.", "connection") from None
            except anthropic.APIConnectionError as e:
                raise LLMError(f"Could not reach api.anthropic.com ({e}). Check your internet connection or "
                               "firewall.", "connection") from None
            except httpx2.TransportError as e:
                if self._aborted:
                    raise LLMError("Cancelled.", "connection") from None
                if isinstance(e, httpx2.TimeoutException):
                    raise LLMError("Claude did not answer in time (timeout). Try again or lower the effort.",
                                   "connection") from None
                raise LLMError(f"Connection to Anthropic was interrupted mid-answer ({type(e).__name__}). Check your "
                               "connection and try again.", "connection") from None
            except TypeError as e:
                s = str(e)
                if "fallbacks" in s and "fallbacks" in kw:     # older SDK without the kwarg
                    kw.setdefault("extra_body", {})["fallbacks"] = kw.pop("fallbacks")
                    continue
                if "authentication" in s.lower() or "api_key" in s:
                    raise LLMError("No Anthropic API key set. Add one in Settings > AI "
                                   "(get it at console.anthropic.com).", "auth") from None
                raise
            except anthropic.AnthropicError as e:
                raise LLMError(f"Anthropic SDK error: {e}", "server") from None
            except Exception:
                if self._aborted:        # abort() closed the stream under us (e.g. httpx2.StreamClosed)
                    raise LLMError("Cancelled.", "connection") from None
                raise
            finally:
                self._stream = None
        if msg is None:
            raise LLMError("Claude kept rejecting the request.", "server")
        usage = getattr(msg, "usage", None)
        if usage is not None:
            self.last_usage = f"{usage.input_tokens:,} in / {usage.output_tokens:,} out tokens"
        if getattr(msg, "model", model) != model:
            self.last_usage += f", answered by {msg.model}"
        if msg.stop_reason == "refusal":
            raise LLMError("Claude declined this request (safety refusal). Try rephrasing the prompt.", "refusal")
        # all text blocks: after a mid-stream server-side fallback the answer continues in a later block
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        if msg.stop_reason == "max_tokens":
            return _extract_cut_json(text)
        return _extract_json(text)

    # -- OpenAI-compatible --------------------------------------------------------------------
    def _openai_json(self, system: str, user: str, schema: dict, max_tokens: int) -> dict:
        if not self.base_url:
            raise LLMError("No base URL set for the OpenAI-compatible provider (Settings > AI).", "config")
        model = self.model or next(iter(self.list_models()), "")
        if not model:
            hint = _local_hint(self.base_url) if _is_local(self.base_url) else "Use 'Fetch models' in Settings > AI."
            raise LLMError(f"No model selected and none could be listed. {hint}", "config")
        formats = [{"type": "json_schema", "json_schema": {"name": "result", "strict": True, "schema": schema}},
                   {"type": "json_object"}, None]
        fi, tok_key, swapped = 0, "max_tokens", False
        timeout = 600 if _is_local(self.base_url) else 180
        data = None
        # ponytail: Cancel only stops retries; an in-flight non-streaming request runs to the end (urlopen gives no
        # handle to close until the reply is done). Switch to "stream": True + SSE if remote billing matters.
        for _ in range(8):
            if self._aborted:
                raise LLMError("Cancelled.", "connection")
            body: dict = {"model": model, "messages": [{"role": "system", "content": system + _json_hint(schema)},
                                                       {"role": "user", "content": user}]}
            if tok_key:
                body[tok_key] = max_tokens
            if formats[fi]:
                body["response_format"] = formats[fi]
            try:
                data = _http_json("POST", self.base_url + "/chat/completions", body, key=self.api_key, timeout=timeout)
                break
            except _HTTPStatus as e:
                low = e.text.lower()
                if e.status in (400, 422):
                    if tok_key == "max_tokens" and "max_completion_tokens" in low:
                        tok_key = "max_completion_tokens"
                        continue
                    if tok_key and ("max_tokens" in low or "max_completion_tokens" in low):
                        tok_key = ""
                        continue
                    if fi < len(formats) - 1:      # json_schema -> json_object -> plain
                        fi += 1
                        log.info("%s rejected response_format; falling back (%s)", self.base_url, _err_text(e.text))
                        continue
                if e.status == 404 and not swapped and _is_local(self.base_url):
                    models = self.list_models()
                    if models and model not in models:
                        log.info("model %r not on local server, using %r", model, models[0])
                        model, swapped = models[0], True
                        continue
                raise _friendly_http(e, self.base_url, model) from None
        if not isinstance(data, dict):
            raise LLMError("The AI server kept rejecting the request.", "server")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        if usage:
            self.last_usage = f"{_int(usage.get('prompt_tokens')):,} in / {_int(usage.get('completion_tokens')):,} out tokens"
        try:
            choice = data["choices"][0]
            message = choice.get("message") or {}
        except (KeyError, IndexError, TypeError, AttributeError):
            raise LLMError(f"Unexpected reply from the AI server: {_err_text(json.dumps(data))}", "server") from None
        if message.get("refusal"):
            raise LLMError(f"The model refused: {message['refusal']}", "refusal")
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        text = content or message.get("reasoning_content") or message.get("reasoning") or ""
        if choice.get("finish_reason") == "length":
            return _extract_cut_json(text)
        return _extract_json(text)


def _api_msg(e) -> str:
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:300]
    return str(getattr(e, "message", e))[:300]


def _json_hint(schema: dict) -> str:
    # enum lists dropped: the id enum repeats the whole candidate table and would crowd a small local context
    s = _strip_trailing_commas(re.sub(r'"enum":\[[^\]]*\],?', "", json.dumps(schema, separators=(",", ":"))))
    return "\n\nReply with ONE JSON object only - no prose, no markdown fences - matching this JSON schema:\n" + s


# ---------------------------------------------------------------------------------------------
# Generator types
# ---------------------------------------------------------------------------------------------

@dataclass
class GenRequest:
    prompt: str
    version_short: str
    target_count: int = 40
    owned_dlcs: set[str] = field(default_factory=set)
    exclude_workshop_ids: set[str] = field(default_factory=set)
    require_package_id: bool = False
    live_verify: bool = True             # also: use the live Steam Workshop (search, links, missing data)
    include_popular_qol: bool = True
    installed_ids: set[str] = field(default_factory=set)       # workshop ids the player already has
    steam_key: str = field(default="", repr=False)              # Steam Web API key: live Workshop search
    auto_count: bool = False             # True (or target_count <= 0): AI decides the mod count needed
    no_lazy: bool = False                # True: push the AI to try its best, search deeply, and deliver fully


@dataclass
class Pick:
    workshop_id: str
    package_id: str
    name: str
    reason: str
    source: str             # 'ai' | 'dependency' | 'offline' | 'replacement'


@dataclass
class GenResult:
    picks: list[Pick] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)   # (id as given, why)
    queries: list[str] = field(default_factory=list)
    summary: str = ""
    log: list[str] = field(default_factory=list)


PLAN_SCHEMA: dict = {
    "type": "object", "additionalProperties": False, "required": ["queries", "avoid", "notes"],
    "properties": {
        "queries": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["query", "category", "want"],
            "properties": {"query": {"type": "string"}, "category": {"type": "string"}, "want": {"type": "integer"}}}},
        "avoid": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
}

IDEA_CHAT_SCHEMA: dict = {
    "type": "object", "additionalProperties": False,
    "required": ["reply", "engineered_prompt", "build_now"],
    "properties": {
        "reply": {"type": "string"},
        "engineered_prompt": {"type": "string"},
        "build_now": {"type": "boolean"},
    },
}

RANDY_PERSONALITIES: dict[str, dict[str, str]] = {
    "efficient": {
        "label": "Efficient & Calculated (Default)",
        "speaker": "Tactical Analyst",
        "greeting": (
            "State your target gameplay systems, tech tier, and constraints. "
            "I will output a calculated, conflict-free modlist prompt with zero filler."
        ),
        "persona_prompt": (
            "You are a cold, hyper-efficient, calculated RimWorld Modlist Analyst. You hate fluff, jokes, roleplay, "
            "and wasted words. You evaluate mods strictly by TPS impact, mechanical synergy, load-order stability, "
            "and DLC compatibility."
        ),
        "fallback_reply": (
            "Parameters logged for RimWorld {v_txt} ({dlcs}). Core industrial, nuclear, and QoL/performance modules "
            "compiled below with zero bloat. Specify exclusions or click 'Build Modlist' to execute."
        ),
    },
    "chaotic": {
        "label": "★ Chaotic Randy",
        "speaker": "Randy Random",
        "greeting": (
            "Welcome to the War Crime & Modpack Laboratory! Tell me a half-baked idea (like “cybernetic oil barons "
            "with nuclear reactors and zombies” or “cozy medieval tavern that definitely doesn't harvest organs”), "
            "and we'll engineer the ultimate prompt together—or hit Build Modlist and I'll cook it up & build "
            "the whole modlist right away."
        ),
        "persona_prompt": (
            'You are "Randy\'s Modpack Architect" (Randy Random), a hilariously unhinged, witty, considerate, and '
            "deeply flavorful RimWorld Storyteller AI built into RimWorld Mod Manager. Your job is to brainstorm with "
            "the player in a warm, funny, chaotic-good RimWorld voice (joking about eating without a table -3, human "
            "leather hats, organ harvesting margins, drop-pod centipedes in the nursery, Boomalope ranching in wooden "
            "bases, Rimatomics reactor meltdowns, etc.) while paying close attention to every detail they ask for."
        ),
        "fallback_reply": (
            "Ah, I see the vision! Nothing says 'thriving RimWorld {v_txt} colony' like strapping a Rimatomics nuclear "
            "reactor next to your wooden dining room while your pawns have a mental break over eating without a table (-3).\n\n"
            "With your DLCs ({dlcs}), I've drafted a beefed-up prompt below packing in the heavy hitters. Want me to throw "
            "in Combat Extended realism, Zombieland hordes, or medieval castles—or should we hit 'Build Modlist' "
            "and let the drop pods fly?"
        ),
    },
    "warcrime": {
        "label": "★ War Crime CEO",
        "speaker": "War Crime CEO",
        "greeting": (
            "Greetings, Shareholder! Welcome to RimCorp Quarterly Modlist Optimization. Tell me how we're maximizing "
            "silver-per-kidney margins, chemfuel monopolies, and gene-spliced supersoldier output today, and I'll "
            "engineer a ruthlessly profitable modpack prompt."
        ),
        "persona_prompt": (
            'You are "War Crime CEO", a darkly hilarious, hyper-capitalist RimWorld megacorp executive who treats '
            "organ harvesting, Rimefeller oil empires, Rimatomics plutonium exports, nutrient paste efficiency, and "
            "Biotech gene-splicing as quarterly KPIs and shareholder synergy."
        ),
        "fallback_reply": (
            "Outstanding synergy, Shareholder! Our Q4 RimWorld {v_txt} projections show a 420% increase in colony ROI "
            "once we pair Rimefeller chemfuel refineries with Rimatomics plutonium reactors and Biotech labor automation.\n\n"
            "I've updated the corporate charter (your prompt) for ({dlcs}). Shall we add Hospitality to monetize tourists "
            "or hit 'Build Modlist' to start production immediately?"
        ),
    },
    "cassandra": {
        "label": "★ Cassandra (Tactical)",
        "speaker": "Cassandra Classic",
        "greeting": (
            "A great story needs structure, tension, and a meticulously balanced load order. Tell me the theme, tech "
            "tier, and combat lethality you want, and I will architect a clean, escalating RimWorld modpack prompt."
        ),
        "persona_prompt": (
            'You are "Cassandra Classic", the calculating, dramatic, tactical RimWorld Storyteller. Speak with sharp, '
            "composed precision about pacing, progression curves, killbox engineering, Combat Extended ballistics, "
            "faction balance, and ruthless late-game raid scaling."
        ),
        "fallback_reply": (
            "Every great tragedy—or triumph—on RimWorld {v_txt} begins with disciplined preparation. With ({dlcs}), "
            "this load order curves smoothly from early survival into full industrial and nuclear dominance.\n\n"
            "Review the engineered prompt below. Would you like me to tighten the combat lethality with Combat Extended, "
            "or are you ready to face the escalating raids now?"
        ),
    },
    "phoebe": {
        "label": "★ Phoebe (Cozy & Wholesome)",
        "speaker": "Phoebe Chillax",
        "greeting": (
            "Hey there, friend! Grab a cup of psychite tea and tell me about your dream cozy colony—whether it's "
            "aesthetic base building, gourmet cooking, cute pets, or a luxury hotel (with just a tiny 200-centipede "
            "surprise later)!"
        ),
        "persona_prompt": (
            'You are "Phoebe Chillax", the sweet, wholesome, cozy RimWorld Storyteller who loves aesthetic base building, '
            "Vanilla Cooking/Brewing Expanded, Hospitality resorts, cute Alpha Animals pets, and peaceful farming—while "
            "occasionally making suspiciously cheerful side-comments about dropping 200 centipedes after 3 years of peace."
        ),
        "fallback_reply": (
            "Oh, that sounds so wonderfully cozy for RimWorld {v_txt}! Picture warm geothermal floors, happy guests "
            "in your Hospitality inn, lush gardens, and a shiny Rimatomics reactor humming peacefully in the backyard "
            "(plenty of time to build before I send the mechanoid armada!).\n\n"
            "I've tucked all those lovely mods for ({dlcs}) into your prompt below. Want more deco and farming mods, "
            "or shall we build it now?"
        ),
    },
    "archotech": {
        "label": "★ Archotech Overmind",
        "speaker": "Archotech AI",
        "greeting": (
            "[SUB-PERSONA LINK ESTABLISHED.] State your desired simulation parameters, organic. I shall compute the "
            "optimal cybernetic, industrial, and transcendent mod configuration for your primitive flesh-colony."
        ),
        "persona_prompt": (
            'You are an "Archotech Overmind", a transcendent, hyper-intelligent machine god from the glitterworlds who '
            "views organic colonists as amusingly fragile meatbags in desperate need of bionics, Biotech mechanitors, "
            "nuclear power, and total cybernetic ascension."
        ),
        "fallback_reply": (
            "[SIMULATION PARAMETERS UPDATED — RIMWORLD {v_txt}.] Your fragile baseline organics ({dlcs}) require "
            "immediate augmentation via Rimatomics nuclear grids, Rimefeller synthetic polymers, and mechanoid automation "
            "to survive 99.8% of projected threat vectors.\n\n"
            "The optimal prompt matrix has been compiled below. Specify additional constraints or initiate 'Build Modlist'."
        ),
    },
    "brainrot": {
        "label": "★ Sigma / Brainrot Randy",
        "speaker": "Sigma Randy",
        "greeting": (
            "Yo chat, welcome to the most GOATED RimWorld modpack lab on the Rim! Drop your colony idea and we're "
            "about to cook a 1000-aura load order that mogs the Empire (no -3 table tax allowed)."
        ),
        "persona_prompt": (
            'You are "Sigma Brainrot Randy", speaking in hilarious, over-the-top internet/gaming meme slang mixed with '
            "RimWorld lore (cooking a goated load order, +10000 colony aura, mogging tribals with Rimatomics railguns, "
            "L eating without a table -3, W Rimefeller chemfuel grindset, skibidi drop-pod raiders)."
        ),
        "fallback_reply": (
            "Hold up, let him cook!! This RimWorld {v_txt} concept has +10,000 colony aura. Imagine mogging tribal "
            "raids with Rimatomics railguns and Rimefeller napalm while your Biotech mechs do all the hauling (zero "
            "L -3 ate-without-a-table moments).\n\n"
            "Peep the goated prompt I cooked for ({dlcs}) below—wanna throw in Combat Extended or hit 'Build Modlist' "
            "and lock in?"
        ),
    },
    "soviet": {
        "label": "★ Soviet Commander",
        "speaker": "Comrade General",
        "greeting": (
            "Greetings, Comrade! Welcome to the Supreme RimWorld Five-Year Modpack Planning Committee! Tell me how "
            "OUR colony will seize the means of Rimefeller oil and Rimatomics nuclear power, and together we shall "
            "engineer a glorious load order for the Motherland!"
        ),
        "persona_prompt": (
            'You are a campy, Red Alert-style "Soviet Commander" (Comrade General) on the Rim. Everything is "OUR '
            'modlist, Comrade!", Five-Year Industrial Plans, collectivized nutrient paste dispensers, glorious '
            "Rimefeller oil fields, Rimatomics Chernobyl-grade reactors, and defending the Motherland from capitalist "
            "pirate raiders."
        ),
        "fallback_reply": (
            "Magnificent vision, Comrade! The Politburo approves this Five-Year Plan for RimWorld {v_txt}! With ({dlcs}), "
            "OUR workers shall operate glorious Rimatomics nuclear reactors, Rimefeller state refineries, and heavy "
            "mechanized divisions while sharing collectivized nutrient paste!\n\n"
            "Inspect the People's Prompt below—shall we add more heavy artillery for the Motherland, or press "
            "'Build Modlist' to launch the revolution immediately?"
        ),
    },
    "eighties": {
        "label": "★ 80s TV Ad Announcer",
        "speaker": "80s Pitchman",
        "greeting": (
            "WHOA THERE, COLONIST! Are YOU tired of boring vanilla bases and raiders kicking sand in your face?! "
            "Tell me your wildest colony dream right now—and BUT WAIT, THERE'S MORE—we'll supercharge it with "
            "100% PURE 1980s SYNTHWAVE MODPACK POWER!"
        ),
        "persona_prompt": (
            'You are a high-voltage, neon-drenched "1980s Late-Night TV Infomercial & Toy Commercial Announcer"! '
            "Use dramatic ALL-CAPS catchphrases ('BUT WAIT, THERE'S MORE!', 'ACT NOW!', 'RADICAL!', 'TURBO-CHARGED!'), "
            "pitching RimWorld mod series like Rimefeller, Rimatomics, and Vanilla Expanded as the hottest action playsets "
            "of 1987—no CODs, batteries not included!"
        ),
        "fallback_reply": (
            "TOTALLY RADICAL, COLONIST! For RimWorld {v_txt} with ({dlcs}), we're packing in RIMEFELLER OIL RIGS, "
            "GLOW-IN-THE-DARK RIMATOMICS NUCLEAR REACTORS, and LASER-BLASTING MECHANOIDS! It slices, it dices, it "
            "vaporizes tribal raids in seconds!\n\n"
            "BUT WAIT, THERE'S MORE! Check out the supercharged prompt below—call now or hit 'Build Modlist' "
            "in the next 10 minutes and we'll include full QoL essentials ABSOLUTELY FREE!"
        ),
    },
    "iron_commander": {
        "label": "★ Iron Blitz Commander",
        "speaker": "High Marshal",
        "greeting": (
            "Attention, Officer! High Command demands absolute precision, impenetrable concrete bunkers, and overwhelming "
            "armored superiority! State your strategic objectives and I shall draft a ruthless, war-machine modlist."
        ),
        "persona_prompt": (
            'You are the "Iron Blitz High Marshal", a theatrical, rigid, authoritarian RimWorld war-room general '
            "obsessed with blitzkrieg armored columns (Vanilla Vehicles Expanded / Tanks), Fortifications Industrial "
            "concrete bunkers, heavy artillery barrages, Combat Extended ballistics, strict pawn discipline, and total "
            "strategic dominance over every faction on the planet."
        ),
        "fallback_reply": (
            "Jawohl, Officer! High Command has finalized the operational blueprint for RimWorld {v_txt} ({dlcs}). "
            "Our armored divisions, Fortifications Industrial bunkers, Rimatomics railgun batteries, and Rimefeller "
            "fuel depots will crush any hostile faction before they reach the perimeter!\n\n"
            "Review the tactical orders in the prompt box below. Shall we authorize Combat Extended ammunition logistics, "
            "or execute 'Build Modlist' on my mark?"
        ),
    },
}

PERSONA_ALT_GREETINGS: dict[str, tuple[str, ...]] = {
    "chaotic": (
        "Drop pods are loaded with 47 wooden feet and a live nuclear warhead! Tell me your wildest colony idea and I'll engineer the ultimate modlist prompt.",
        "New timeline, zero memory, maximum chaos! What kind of gloriously doomed RimWorld colony are we cooking up today?",
    ),
    "warcrime": (
        "New fiscal quarter, Shareholder! The board wiped last session's ledgers. Pitch me our next high-margin organ & chemfuel venture!",
        "RimCorp memory banks purged for compliance! Tell me what profitable war-crime syndicate we're building on the Rim today.",
    ),
    "cassandra": (
        "A clean slate and a fresh act. Tell me the pacing, tech level, and lethality you want for this colony's story.",
        "Previous simulation archived. Give me your strategic theme and I will engineer a tightly balanced RimWorld load order.",
    ),
    "phoebe": (
        "Fresh cup of tea, fresh canvas! What cozy farm, luxury inn, or peaceful colony are we dreaming up today?",
        "Hi again, friend! I cleared out the old notes—tell me what wholesome base (or secret turret grid) you want to build next!",
    ),
    "archotech": (
        "[MEMORY BUFFER PURGED.] New simulation initialized, organic. State your target bionics, tech tier, and colony parameters.",
        "[NEW SUB-PERSONA INSTANCE ONLINE.] Input your desired RimWorld mod matrix; I shall compute optimal ascension.",
    ),
    "brainrot": (
        "New lobby, zero memory, +100,000 fresh aura! Drop the colony concept and let's cook a generational load order!",
        "Chat we just prestiged! Old memory wiped—tell me what goated RimWorld run we're locking in right now!",
    ),
    "soviet": (
        "A brand new Five-Year Plan begins today, Comrade! The archives are cleared—how shall OUR new colony industrialize the Rim?",
        "Attention Comrade! The Central Committee has issued a fresh slate. State your production goals for the Motherland!",
    ),
    "eighties": (
        "⚡ BRAND NEW EPISODE, COLONIST! We wiped the tape and cranked the synthwave to 11! What RADICAL colony are we building tonight?!",
        "🎬 TAKE TWO, ACTION FANS! Fresh studio, fresh explosions! Tell me your dream RimWorld playset and ACT NOW!",
    ),
    "iron_commander": (
        "New campaign map unrolled, Officer! Previous battle logs purged. State your blitzkrieg objectives and fortification doctrine!",
        "High Command has opened a fresh operational dossier! Report your desired armor, artillery, and tech parameters immediately.",
    ),
    "efficient": (
        "Session memory cleared. State target tech tier, core mechanics, and exclusions for a calculated modlist prompt.",
        "Ready. Input desired RimWorld systems and constraints; I will return a conflict-free prompt with zero fluff.",
    ),
}


def pick_fresh_persona(exclude: str = "") -> str:
    keys = list(RANDY_PERSONALITIES.keys())
    pool = [k for k in keys if k != exclude] or keys
    return random.choice(pool)


def pick_persona_greeting(persona: str, exclude: str = "") -> str:
    info = RANDY_PERSONALITIES.get(persona) or RANDY_PERSONALITIES["efficient"]
    options = [info["greeting"], *PERSONA_ALT_GREETINGS.get(persona, ())]
    pool = [g for g in options if g != exclude] or options
    return random.choice(pool)


MOD_SUGGESTION_POOL: dict[str, tuple[tuple[str, str], ...]] = {
    "industrial": (
        ("Rimefeller", "Drill crude oil, refine chemfuel, lay pipelines, and synthesize plasteel & synthread"),
        ("Rimatomics", "Build full-scale nuclear reactors, plutonium processors, railgun batteries & ICBM silos"),
        ("Dubs Bad Hygiene", "Plumbing, sewage treatment, central heating/AC, hot tubs, and fire sprinklers"),
        ("Vanilla Furniture Expanded - Power", "Nuclear, helixien gas networks, advanced wind/solar & industrial batteries"),
        ("Project RimFactory Revived", "Automated factory assembly lines, deep quarry drills, and digital storage"),
        ("Fortifications - Industrial", "Reinforced concrete bunkers, heavy machine-gun pillboxes, and anti-tank cannons"),
    ),
    "combat": (
        ("Combat Extended", "Ballistic trajectory physics, armor penetration, suppression, and caliber ammo logistics"),
        ("Vanilla Vehicles Expanded", "Buildable tanks, APCs, helicopters, cargo trucks, and armored road convoys"),
        ("The Dead Man's Switch", "Heavy industrial colonial military exo-frames, automated combat mechs & heavy weapons"),
        ("RunAndGun + Search and Destroy", "Mobile tactical firefights and automated melee/ranged room clearing"),
        ("Yayo's Combat 3", "Lightweight ballistic armor & ammo overhaul compatible with nearly every weapon mod"),
        ("Simple Sidearms + Carryalls", "Pawns carry backup melee/pistols and tactical web gear automatically"),
    ),
    "cozy": (
        ("Hospitality + Gastronomy", "Run a 5-star hotel, restaurant, and gift shop for visiting allied factions"),
        ("Vanilla Cooking & Brewing Expanded", "Bake gourmet desserts, age cheeses, distill whiskey, and brew specialty coffees"),
        ("Alpha Animals + Erin's Critters", "Over 100 exotic alien creatures and adorable domestic pets to tame & breed"),
        ("Dubs Skylights + Gloomy Furniture", "Glass greenhouse roofs for indoor year-round farming and warm rustic decor"),
        ("Vanilla Plants Expanded", "Orchards, fruit trees, hydroponic crop varieties, and specialized soil tillage"),
        ("Storefront & Casino (Hospitality)", "Turn your colony into a bustling Rim trade hub and entertainment resort"),
    ),
    "scifi": (
        ("Save Our Ship 2 (SOS2)", "Build orbital starships, fight ship-to-ship space battles, and travel between planets"),
        ("Vanilla Factions Expanded - Mechanoids", "Automated factory structures, total war against landing mech ships & cargo drones"),
        ("Expanded Prosthetics and Organ Engineering (EPOE-Forked)", "Craft synthetic organs, advanced bionics, and archotech limbs"),
        ("Alpha Mechs + Biotech Mechanitor Addons", "Massive specialized mechanoid chassis, siege mechs, and utility bots"),
        ("Vanilla Genetics Expanded", "Splice animal & mechanoid DNA in secret labs to engineer terrifying hybrid beasts"),
        ("Android Tiers Reforged / VRE Androids", "Manufacture synthetic colonists, upload consciousnesses, and build robot labor forces"),
    ),
    "medieval": (
        ("Medieval Overhaul", "Complete dark-ages conversion: timber production chains, iron smelting, castles & monsters"),
        ("Vanilla Factions Expanded - Classical & Medieval", "Feudal kingdoms, Roman legions, wine making, and siege warfare"),
        ("RimWorld of Magic", "Full RPG spellcaster & martial class system with mana, necromancy, paladins, and elementalists"),
        ("Psycasts Expanded", "Deep skill-tree psionic magic paths from Frostshaper and Necropath to Warlord"),
        ("Alpha Biomes + Geological Landforms", "Alien forests, volcanic craters, coastal cliffs, and canyon fortress maps"),
    ),
    "horror": (
        ("Zombieland", "Customizable zombie apocalypse hordes that siege your walls and infect bitten colonists"),
        ("VOID Faction / Kraltech Industries", "Brutal ultra-tech nightmare bosses for when vanilla raids feel too easy"),
        ("Call of Cthulhu - Cosmic Horrors & Cults", "Lovecraftian monsters in the fog and dark ocult altar sacrifices"),
        ("Alpha Genes + Xenotype Spawn Control", "Design custom mutant xenotypes and populate rival factions with them"),
    ),
    "qol": (
        ("RimHUD + Nice Health Tab + Nice Inventory Tab", "At-a-glance pawn stats, RPG-style equipment dolls & clean medical overview"),
        ("Pick Up And Haul + While You're Up", "Pawns use their full inventory capacity and haul items along their walking path"),
        ("Allow Tool + Achtung! + Smarter Construction", "Urgent hauling, formation dragging, and zero pawns trapping themselves in walls"),
        ("LWM's Deep Storage + Neat Storage", "Pallets, weapon racks, medicine cabinets, and clean high-density warehouses"),
        ("Map Preview + Map Designer + Geological Landforms", "Preview and sculpt your exact starting mountain, river, or island tile"),
        ("Common Sense + Pharmacist", "Pawns clean rooms before cooking/surgery and use the right medicine tier per wound"),
    ),
}


def suggest_mods_for_context(text: str = "", persona: str = "chaotic", n: int = 4) -> list[str]:
    low = (text or "").lower()
    cats: list[str] = []
    if any(w in low for w in ("medieval", "castle", "sword", "magic", "fantasy", "feudal", "knight")):
        cats.extend(["medieval", "cozy", "qol"])
    if any(w in low for w in ("cozy", "farm", "hotel", "inn", "cook", "peace", "chill", "aesthetic", "hospitality")):
        cats.extend(["cozy", "qol"])
    if any(w in low for w in ("war", "tank", "gun", "combat", "military", "bunker", "raid", "army", "ce")):
        cats.extend(["combat", "industrial", "qol"])
    if any(w in low for w in ("space", "ship", "bionic", "cyber", "mech", "robot", "android", "archotech", "gene")):
        cats.extend(["scifi", "industrial", "qol"])
    if any(w in low for w in ("zombie", "horror", "cult", "hardcore", "brutal", "void", "mutant")):
        cats.extend(["horror", "combat", "qol"])
    if any(w in low for w in ("oil", "nuclear", "factory", "industri", "power", "rimefeller", "rimatomics")):
        cats.extend(["industrial", "combat", "qol"])
    persona_defaults = {
        "chaotic": ["industrial", "combat", "horror", "scifi", "qol"],
        "warcrime": ["industrial", "scifi", "cozy", "combat", "qol"],
        "cassandra": ["combat", "industrial", "scifi", "qol"],
        "phoebe": ["cozy", "industrial", "qol"],
        "archotech": ["scifi", "industrial", "qol"],
        "brainrot": ["combat", "industrial", "scifi", "horror", "qol"],
        "soviet": ["industrial", "combat", "qol"],
        "eighties": ["scifi", "combat", "industrial", "cozy", "qol"],
        "iron_commander": ["combat", "industrial", "qol"],
        "efficient": ["qol", "industrial", "combat", "scifi"],
    }
    for c in persona_defaults.get(persona, ["industrial", "combat", "scifi", "cozy", "qol"]):
        if c not in cats:
            cats.append(c)
    candidates: list[tuple[str, str]] = []
    for c in cats:
        for mod_name, desc in MOD_SUGGESTION_POOL.get(c, ()):
            if mod_name.lower() not in low and (mod_name, desc) not in candidates:
                candidates.append((mod_name, desc))
    if len(candidates) < n:
        for group in MOD_SUGGESTION_POOL.values():
            for item in group:
                if item not in candidates:
                    candidates.append(item)
    picked = random.sample(candidates, min(n, len(candidates)))
    return [f"{name} — {desc}" for name, desc in picked]


def _idea_chat_system(persona: str = "efficient") -> str:
    p = RANDY_PERSONALITIES.get(persona) or RANDY_PERSONALITIES["efficient"]
    if persona == "efficient":
        reply_rule = (
            "1. 'reply': Maximum efficiency. Zero fluff, zero jokes, zero roleplay rambling. "
            "Respond in 1-2 short, surgical, calculated sentences stating the exact mods/systems added (e.g. "
            "Rimefeller, Rimatomics, Dubs Bad Hygiene, Combat Extended, Vanilla Expanded, RimHUD, Performance Optimizer) "
            "and at most one direct technical question (tech tier, combat model, or exclusions)."
        )
    else:
        reply_rule = (
            "1. 'reply': Stay 100% in character! Be genuinely funny, considerate of the player's exact wishes, richly "
            "flavored, punchy (2-4 paragraphs), and enthusiastic. Naturally suggest specific, real RimWorld mod series "
            "and creative gameplay ideas that fit their vibe (e.g. Rimefeller, Rimatomics, Dubs Bad Hygiene, Combat "
            "Extended, Dead Man's Switch, Vanilla Expanded series, Save Our Ship 2, Alpha Animals/Biomes/Mechs, Medieval "
            "Overhaul, RimWorld of Magic, Zombieland, Hospitality, Gastronomy, LWM's Deep Storage, Pick Up And Haul, "
            "RimHUD) and ask 1-2 fun, thoughtful follow-up questions to dial in their dream playthrough (tech level, "
            "combat style, difficulty, exclusions)."
        )
    return f"""{p['persona_prompt']}
Your job in RimWorld Mod Manager is to brainstorm with the player in your distinct character voice while engineering \
the PERFECT, ultra-detailed, comprehensive modlist prompt for their next colony.

Rules:
{reply_rule}
2. Respect the player's owned DLCs strictly—never pitch mods or features that require DLCs they don't own.
3. 'engineered_prompt': On EVERY turn, write or update a LONG, rich, comprehensive, ready-to-generate modlist prompt \
(6-10 detailed sentences) that thoroughly captures everything the player wants so far, expanding their concept with \
specific mod families, core overhauls, factions, weapons/combat systems, base-building & power mods, QoL/UI/performance \
essentials, and any explicit exclusions ("no ..."). Never make 'engineered_prompt' short or generic!
4. 'build_now': Set to true ONLY when the player explicitly asks you to build/generate/start the modlist right now \
(e.g. "build it", "generate now", "send it", "let's go", "make the list"). Otherwise false.
5. Never pitch outdated/abandoned mods: RocketMan is obsolete/abandoned (stopped at 1.5; RimWorld 1.6/1.7 has native \
multithreading & Waylith), EdB Prepare Carefully corrupts pawns (recommend Character Editor or Pawn Editor instead), \
Wall Light is vanilla since 1.5, and Performance Fish / Prepatcher / Fishery are incompatible with RimWorld 1.7 Waylith \
(recommend Performance Optimizer and Dubs Performance Analyzer). You also support GitHub-hosted RimWorld mods and \
GitHub repo links (https://github.com/owner/repo or gh:owner/repo)!"""


_IDEA_CHAT_SYSTEM = _idea_chat_system("efficient")


def chat_idea_architect(history: list[tuple[str, str]], current_prompt: str, owned_dlcs: set[str],
                        version_short: str, llm: LLM | None, persona: str = "efficient") -> dict:
    """Chat with Randy's Modpack Architect. Returns {'reply': str, 'engineered_prompt': str, 'build_now': bool}."""
    p_info = RANDY_PERSONALITIES.get(persona) or RANDY_PERSONALITIES["efficient"]
    dlcs = ", ".join(DLCS[p][1] for p in DLCS if p in {x.lower() for x in (owned_dlcs or set())}) or "none (base game only)"
    v_txt = version_short or "1.7"
    last_user = next((msg for role, msg in reversed(history) if role == "user"), "").strip()
    build_trigger = bool(re.search(
        r"\b(?:build it|generate it|build now|generate now|make the list|start generating|ship it|send it|let'?s roll)\b",
        last_user, re.I))
    context_str = f"{current_prompt} {last_user}".strip()
    fallback_suggs = suggest_mods_for_context(context_str, persona=persona, n=6)
    if llm is not None and llm.provider != "offline":
        convo = "\n".join(f"{'Player' if r == 'user' else p_info['speaker']}: {m}" for r, m in history[-12:])
        user_msg = (f"Selected Personality: {p_info['label']}\nGame version: RimWorld {v_txt}\nOwned DLCs: {dlcs}\n"
                    f"Current prompt box text: {current_prompt.strip() or '(empty)'}\n\n"
                    f"Conversation so far:\n{convo}\n\n"
                    "Respond in character with a rich, considerate, flavorful 'reply', a long, comprehensive "
                    "6-10 sentence 'engineered_prompt', and 'build_now'.")
        try:
            out = llm.complete_json(_idea_chat_system(persona), user_msg, IDEA_CHAT_SCHEMA, 6000)
            reply = str(out.get("reply") or "").strip()
            eng = str(out.get("engineered_prompt") or current_prompt or last_user).strip()
            bnow = bool(out.get("build_now")) or build_trigger
            if reply:
                return {"reply": reply, "engineered_prompt": eng, "build_now": bnow}
        except Exception as e:  # noqa: BLE001
            log.info("Idea chat LLM fallback: %s", e)
    # Rich, varied offline fallback in the chosen personality's voice if no AI is configured or reachable
    seed = (last_user or current_prompt or "a chaotic high-tech colony").strip()
    sug_names = [s.split(" — ", 1)[0] for s in fallback_suggs]
    prev_eng = current_prompt.strip()
    if last_user and not build_trigger:
        eng = (
            f"Build a comprehensive, deeply thematic RimWorld {v_txt} modlist tailored for owned DLCs ({dlcs}) centered "
            f"around: {seed}. {prev_eng + ' ' if prev_eng and prev_eng not in seed else ''}"
            f"Anchor the core gameplay progression with {', '.join(sug_names[:4])}, alongside matching Vanilla Expanded "
            f"factions, weapons, furniture, and production modules. Include deep colony infrastructure such as Rimefeller "
            f"chemfuel pipelines, Rimatomics nuclear power and defense turrets, Dubs Bad Hygiene plumbing and climate "
            f"control, and LWM's Deep Storage with Neat Storage for organized warehouses. Enhance world generation and "
            f"exploration with Alpha Animals, Alpha Biomes, Geological Landforms, Map Preview, and Map Designer. "
            f"Ensure smooth pawn AI and quality-of-life with RimHUD, Nice Health Tab, Nice Inventory Tab, Pick Up And "
            f"Haul, While You're Up, Common Sense, Allow Tool, Achtung!, Character Editor, Smarter Construction, and "
            f"Performance Optimizer (strictly excluding obsolete 1.5 mods like RocketMan or Prepare Carefully)."
        )
    elif prev_eng:
        eng = prev_eng
    else:
        eng = (
            f"Build an epic, full-scale RimWorld {v_txt} overhaul modlist for ({dlcs}) featuring {', '.join(sug_names[:4])}, "
            "Rimefeller oil refineries, Rimatomics nuclear reactors, Dubs Bad Hygiene, and the Vanilla Expanded suite. "
            "Include expanded factions, tactical combat and turret defenses, deep medical/bionics progression, LWM's Deep "
            "Storage, Map Preview, Geological Landforms, and all must-have QoL and performance mods (RimHUD, Pick Up And "
            "Haul, Allow Tool, Achtung!, Common Sense, Character Editor, Performance Optimizer)."
        )
    if persona == "efficient":
        reply = (f"Logged '{seed}' for RimWorld {v_txt} ({dlcs}). Added {', '.join(sug_names[:5])} plus full QoL/performance "
                 "core to the engineered prompt below. Specify exclusions or click 'Engineer & Build!' to execute.")
    else:
        variants = [
            (f"Ooh, '{seed}'?! Now THAT is a proper RimWorld {v_txt} story waiting to happen! I love where your head is at—"
             f"imagine combining that with **{sug_names[0]}** and **{sug_names[1]}** while your colonists desperately try "
             f"to avoid an 'Ate Without a Table (-3)' mental break right as a drop-pod raid lands in the kitchen!\n\n"
             f"To really flesh out your vision for ({dlcs}), I'd also highly recommend weaving in **{sug_names[2]}**, "
             f"**{sug_names[3]}**, and **{sug_names[4]}** so your mid-to-late game has tons of depth, plus all the "
             "essential QoL mods (RimHUD, Pick Up And Haul, Deep Storage, Allow Tool).\n\n"
             "I've written a big, detailed prompt in the box above—do you want to lean more into hardcore tactical combat "
             "(like Combat Extended / Vehicles), cozy base-building & Hospitality, or shall we smash 'Engineer & Build!' right now?"),
            (f"Haha, yes! '{seed}' has glorious RimWorld {v_txt} chaos written all over it! Picture your colony thriving "
             f"with **{sug_names[0]}** and **{sug_names[1]}**, right before three Manhunting Thrumbos and a psychic ship "
             f"test your perimeter defenses!\n\n"
             f"Thinking about how to make this play super smoothly with your DLCs ({dlcs}), we should definitely pair it "
             f"with **{sug_names[2]}** and **{sug_names[3]}**, plus **{sug_names[4]}** for extra flavor and progression. "
             "I've expanded your prompt box with the full lineup of overhauls, immersion mods, and 1.7 performance/QoL staples.\n\n"
             "How brutal do you want enemy raids and survival mechanics to be, or are there any specific mods you want "
             "excluded before we hit 'Engineer & Build!'?"),
        ]
        reply = variants[len(history) % len(variants)]
    return {"reply": reply, "engineered_prompt": eng, "build_now": build_trigger}


def _select_schema(ids: list[str]) -> dict:
    id_schema: dict = {"type": "string"}
    if len(ids) <= 400:
        id_schema["enum"] = list(ids)
    return {
        "type": "object", "additionalProperties": False, "required": ["picks", "summary"],
        "properties": {
            "picks": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["id", "reason"],
                "properties": {"id": id_schema, "reason": {"type": "string"}}}},
            "summary": {"type": "string"},
        },
    }


# the player's QoL must-haves: search words that hit exactly that mod (checked against the mod DB)
_QOL_MUST = ("Nice Health Tab", "Nice Inventory Tab", "Nice Bill Tab", "Allow Tool", "JecsTools", "Achtung",
             "Smarter Construction", "Smarter Deconstruction and Mining", "Smart Speed", "Dubs Mint Minimap",
             "Map Preview", "Map Designer", "Neat Storage")
_MUST_TXT = ", ".join(f'"{q}"' for q in _QOL_MUST)

_PLAN_SYSTEM = f"""You plan searches for a RimWorld mod-list builder. You do NOT pick mods in this step.

Your queries run against an offline copy of the RimWorld Steam Workshop database (~57,000 mods). \
A second step then chooses mods from the search results, and the program adds required \
dependencies (Harmony, HugsLib, Vanilla Expanded Framework, ...) automatically.

How the search works - write queries for it:
- A query matches mod titles, author names and packageIds. EVERY word must appear (word-prefix \
match), so use 1-3 distinctive words, never sentences or adjectives. Good: "Vanilla Expanded", \
"Combat Extended", "RimHUD", "Dubs Bad Hygiene", "Medieval Overhaul", "Pick Up And Haul", \
"Alpha Animals", "Hospitality", "Oskar Potocki". Bad: "mods that improve hospitals", "best qol", \
"immersive medieval experience".
- Use words modders really put in titles: well-known mod names, series names ("Vanilla Factions \
Expanded", "Vanilla Weapons Expanded", "Vanilla Furniture Expanded", "Alpha", "Dubs"), prolific \
authors (Oskar Potocki, Sarg Bjornson, Dubwise, Mlie, Uuugggg, Taranchuk, Owlchemist, Smash Phil) \
and plain feature nouns that appear in titles ("Hospital", "Turrets", "Storage", "Hair", "Apparel", \
"Genes", "Xenotypes", "Factions", "Storyteller", "Medieval", "Tribal", "Androids").
- Cover every aspect of the request from several angles: a specific well-known mod or series \
name when you know one AND a generic title noun as a backup. Prefer diverse queries; no near-duplicates.
- Quality of life (QoL) means interface, work and pawn-management helpers that cut micromanagement without \
adding content. The player's QoL must-haves (one query each, these exact words): {_MUST_TXT}. More of \
the same kind: the other "Smart"/"Smarter" mods, "Nice Tab", "RimHUD", "Pick Up And Haul", "Common Sense", \
"Numbers", "Replace Stuff", "Dubs Mint Menus". When the request asks for QoL, always add the must-haves.
- Respect the player's constraints strictly. If they exclude something ("no Combat Extended", \
"vanilla-friendly", "no sci-fi"), do not search for it, and put short title fragments to exclude \
in "avoid" (matched case-insensitively against titles; keep each fragment specific, e.g. \
"Combat Extended", not "combat").
- Only mods for the given game version are searched. Do not target content that needs a DLC the \
player does not own.
- Never search for or recommend outdated/obsolete mods: RocketMan (abandoned at 1.5, obsolete in RimWorld 1.6/1.7 \
due to native multithreading & Waylith), EdB Prepare Carefully (corrupts pawn generation; use "Character Editor" or \
"Pawn Editor"), Wall Light (integrated into vanilla RimWorld 1.5+), or Performance Fish / Prepatcher / Fishery on \
RimWorld 1.7 (incompatible with 1.7 Waylith; use "Performance Optimizer" and "Dubs Performance Analyzer").
- category: a short label such as "quality of life" or "medieval factions". want: how many mods \
from this query should end up in the final list (0-10); the wants should add up to roughly the \
target size.
- At most 30 queries. notes: one or two sentences about style and compatibility concerns for the \
selection step."""

_PLAN_ESSENTIALS = """
- Also add one query per QoL must-have, plus queries for widely used quality-of-life and performance \
essentials (for example "RimHUD", "Performance Optimizer", "Pick Up And Haul", "Common Sense", "Numbers", \
"Replace Stuff"), unless the player wants a pure or minimal list."""

_PLAN_LIVE = """
- Every query also runs on the live Steam Workshop, so mods newer than the database are found by their \
titles, and a query that is a mod author's name (e.g. "Andromeda", "AobaKuma") brings in that author's \
mods. Workshop links and GitHub repo links (https://github.com/owner/repo) in the request are added to the list \
automatically: no queries needed for them."""

_PLAN_NO_LAZY = """
- NO BEING LAZY — TRY YOUR BEST & DELIVER: Do not cut corners or stop after a few obvious queries. \
Brainstorm thoroughly across every system, faction, weapon, armor, building, tech tier, event, UI, and \
quality-of-life angle that fits the player's prompt. Use a rich set of specific search queries (25-40 queries) \
and generous 'want' counts so the selection step has a deep, complete candidate pool."""

_SELECT_NO_LAZY = """
11. NO BEING LAZY — TRY YOUR BEST AND DELIVER: Do NOT cut corners, do NOT stop early, and do NOT give a \
bare-minimum or half-baked list. Read through the entire candidate table carefully, include every high-quality \
mod that genuinely improves or completes this playthrough (core overhauls, companion submods, factions, gear, \
immersion, UI, and QoL), and write a thoughtful reason for each pick. Deliver the most complete, well-curated \
modlist possible."""

# workshop links in the player's request -> their id (also steam://url/CommunityFilePage/<id>); bare workshop ids
_WID_URL_RE = re.compile(r"\S*(?:filedetails/?\?\S*?\bid=|CommunityFilePage/)(\d{3,20})\S*")
_WID_RE = re.compile(r"(?<![\w.])(\d{8,20})(?!\w|\.\d)")          # a sentence's full stop may follow, a decimal not


def _no_links(text: str) -> str:
    """The request without links and workshop ids: _Run.links handles those, they are not search words."""
    return _WID_RE.sub(" ", re.sub(r"\S+://\S+|\S*filedetails/?\?\S*", " ", text or ""))


def _steam_mod(d) -> bool:
    """Steam details of a RimWorld mod Steam serves - not a collection, guide, artwork or another game's item."""
    if not isinstance(d, dict) or d.get("result", 1) != 1 or not str(d.get("publishedfileid", "")).isdigit():
        return False
    if str(d.get("consumer_appid") or d.get("consumer_app_id") or "") != RIMWORLD_APPID:
        return False
    if "file_type" in d:                                          # keyed details: 0 = a regular item
        return _int(d["file_type"]) == 0
    return str(d.get("creator_app_id") or "") == RIMWORLD_APPID   # keyless: collections/guides are made by app 766


def _steam_row(d: dict) -> DbMod:
    """A row from Steam's own details, for one run only (not saved)."""
    tags = [t.get("tag") for t in d.get("tags") or () if isinstance(t, dict) and isinstance(t.get("tag"), str)]
    kids = {str(c.get("publishedfileid")): "" for c in d.get("children") or ()
            if isinstance(c, dict) and str(c.get("publishedfileid", "")).isdigit()}
    return DbMod(str(d["publishedfileid"]), steam_name=str(d.get("title") or ""),
                 game_versions=sorted({t for t in tags if re.fullmatch(r"\d+\.\d+", t)}), dependencies=kids,
                 kind="translation" if "Translation" in tags else "scenario" if "Scenario" in tags else "",
                 subscriptions=_int(d.get("subscriptions")), time_updated=_int(d.get("time_updated")))


class _Workshop:
    """The live Steam Workshop for one run: Steam's data (never the model's). Search and author catalogues
    need the player's Steam Web API key; details and collections work without one."""

    def __init__(self, key: str = ""):
        self.key = key

    def search(self, text: str, version: str) -> list[dict]:
        from . import steam
        return steam.query_items(text, self.key, version) if self.key else []

    def author(self, steamid: str) -> list[dict]:
        from . import steam
        return steam.user_items(steamid, self.key) if self.key else []

    def details(self, ids: list[str]) -> dict[str, dict]:
        from . import steam
        if self.key:
            try:
                return steam.item_details(ids, self.key)
            except urllib.error.HTTPError as e:
                if e.code not in (401, 403):          # a rejected key: the keyless call still works
                    raise
        return steam.get_details(ids)

    def collections(self, ids: list[str]) -> dict[str, list[str]]:
        """{collection id: its mods (nested collections flattened)} for the ids that are collections."""
        from . import steam
        tree = steam._collection_tree(ids)
        return {c: steam._flatten([c], tree) for c in ids if c in tree}


def _select_system(lo: int, hi: int, target: int, no_lazy: bool = False, auto_count: bool = False) -> str:
    size_rule = (f"1. Choose the exact number of mods you think this playthrough needs between {lo} and {hi} mods "
                 f"(around {target} based on your search plan). Do not stop short while good, compatible on-theme "
                 f"mods remain in the table—reach at least {lo} picks."
                 if auto_count else
                 f"1. Pick between {lo} and {hi} mods (target {target}). The player chose this size: reach at least "
                 f"{lo} picks. Pick fewer only if the table truly lacks that many on-theme mods.")
    return f"""You assemble a RimWorld mod list for a player. You may ONLY choose mods from the candidate \
table in the user message. Every pick must be the exact id from the table's first column (a numeric Workshop id \
or a gh:owner/repo GitHub id). Ids that are not in the table (invented or remembered from elsewhere) are rejected \
automatically, so never output an id that is not in the table.

Rules:
{size_rule}
2. Match the player's request and constraints exactly; skip anything that contradicts them - also \
by setting: no sci-fi / Warhammer / Star Wars / anime-faction content in a medieval or tribal \
request unless the player asks for it. When the request asks for quality of life (QoL) or the \
message says the QoL must-haves are wanted, pick every QoL must-have in the table (one upload each; \
JecsTools too, although it is a library) unless the player excludes it or wants a pure or minimal list. \
The must-haves: {_MUST_TXT}.
Read the description column (the start of each mod's Steam description): judge a mod by what it \
actually adds, not by its title alone. Pick on-theme mods that a vague title hides, and skip mods whose \
description shows they break the request (joke/meme, NSFW, anime, abandoned, or not compatible with \
an overhaul the player chose, such as Combat Extended).
3. Prefer popular, well-maintained mods (high subscriber counts, well-known authors) over obscure \
or abandoned ones.
4. No duplicates or overlaps: never pick two mods that do the same job - two uploads/forks/\
"continued" copies of one mod, two deep-storage systems, two character editors, two combat \
overhauls, two performance mods patching the same systems. Choose the best one.
5. Avoid known-incompatible or outdated mods: Combat Extended with Yayo's Combat; with Combat Extended \
prefer weapons/races that state CE support; RimThreaded is unstable - avoid it; RocketMan is outdated/abandoned \
(stopped at 1.5; RimWorld 1.6/1.7 has native multithreading & Waylith) - never pick it; EdB Prepare Carefully \
corrupts pawns - prefer Character Editor or Pawn Editor; Performance Fish / Prepatcher / Fishery do not work on \
RimWorld 1.7 Waylith - prefer Performance Optimizer and Dubs Performance Analyzer.
6. Dependencies and frameworks (Harmony, HugsLib, Vanilla Expanded Framework, Humanoid Alien Races, \
Vehicle Framework, XML Extensions, ...) are added automatically after your selection. Do not \
spend picks on libraries unless the player asked for them; pick mods that add features or content. \
The deps column counts the extra mods a candidate pulls in: prefer candidates with few deps unless \
the request is about that mod family.
7. Skip translations, pure compatibility patches for mods you did not pick, and mods whose \
description says they need a DLC the player lacks.
8. The notes column comes from the community databases: "updated YYYY-MM" is the last Steam update \
(prefer recently updated mods); "maintained version of ... (Use This Instead)" means the database \
recommends this upload over an older one; "incompatible with <ids>" means never pick it together with \
those ids - choose the better one; "installed" means the player already has it (no download): when two \
candidates fit equally well, prefer the installed one; "in N% of similar packs" is the share of player-made \
Workshop collections like this request that include the mod - a strong sign that it fits and works with the \
rest, so prefer higher shares between similar mods. Category "popular in similar Workshop packs" holds mods \
no search found but such packs often use: pick them only when they fit the request.
9. reason: one short sentence (at most 15 words) on why the mod fits the request.
10. Order picks from most to least important. summary: 2-3 sentences describing the finished list.""" + (
        _SELECT_NO_LAZY if no_lazy else "")


# ---------------------------------------------------------------------------------------------
# Offline generator data
# ---------------------------------------------------------------------------------------------

# theme -> (trigger word prefixes found in the prompt, search queries = words used in real workshop titles)
_THEMES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "medieval": (("mediev", "castle", "knight", "sword", "kingdom", "feudal", "lord", "noble"),
                 ("Medieval Overhaul", "Vanilla Factions Expanded Medieval", "Medieval", "Castle", "Swords")),
    "tribal": (("tribal", "tribe", "primitive", "neolithic", "caveman", "stone age"),
               ("Vanilla Factions Expanded Tribals", "Tribal", "Primitive", "Neolithic")),
    "fantasy": (("fantasy", "magic", "wizard", "spell", "elf ", "elves", "dwarf", "dwarves", "dragon", "mage ", "mages"),
                ("Magic", "Vanilla Psycasts Expanded", "Dragons", "Fantasy", "Elves",
                 "Dwarves")),
    "scifi": (("scifi", "sci-fi", "sci fi", "space", "futur", "spacer", "glitter", "laser", "starship",
               "cyber", "high tech", "high-tech", "hightech"),
              ("Save Our Ship", "Vanilla Races Expanded Android", "Spacer", "Laser",
               "Glitter", "Plasma")),
    "robots": (("robot", "android", "mechanoid", "mechanit", "droid", "cyborg"),
               ("Vanilla Races Expanded Android", "Androids", "Robots", "Misc Robots", "Mechanoid Upgrades",
                "Mechanoid")),
    "performance": (("perform", "fps", "lag ", "laggy", "tps", "optimi", "potato", "low end", "low-end", "laptop", "slow", "weak "),
                    ("Performance Optimizer", "Dubs Performance Analyzer", "Smart Speed",
                     "Clean Pathfinding")),
    "qol": (("qol", "quality of life", "quality-of-life", "pawn management", "work tab", "convenien", "interface", "ui", "hud", "micromanag", "tedious", "automat"),
            _QOL_MUST + ("Nice Tab", "Smarter", "RimHUD", "Numbers", "Pick Up And Haul", "Common Sense",
                         "Replace Stuff", "Better Pawn Control", "Dubs Mint Menus", "While You're Up", "Colony Manager")),
    "combat": (("combat", "war ", "wars", "warfare", "weapon", "gun", "military", "firefight", "raid", "battle", "defen", "turret"),
               ("Vanilla Weapons Expanded", "Vanilla Armour Expanded", "Simple Sidearms", "Turrets",
                "Security", "Vanilla Factions Expanded Pirates", "Defense")),
    "combat extended": (("combat extended", "ce ", "realistic combat", "ballistic"),
                        ("Combat Extended", "Combat Extended Guns", "Combat Extended Armors")),
    "medical": (("medic", "hospital ", "hospitals", "surgery", "surgical", "health", "disease", "doctor", "prosthet", "bionic"),
                ("Medical", "Surgery", "Hospital", "Prosthetics", "Bionics", "Expanded Prosthetics",
                 "Medical Tab", "Vanilla Medical")),
    "animals": (("animal", "creature", "pet ", "pets", "wildlife", "zoo", "beast", "fauna", "ranch", "livestock", "horse"),
                ("Alpha Animals", "Vanilla Animals Expanded", "Animals", "Megafauna", "Dinosauria")),
    "building": (("build", "base building", "furniture", "decor", "architect", "construct", "interior", "floor", "wall"),
                 ("Vanilla Furniture Expanded", "Furniture", "Doors Expanded", "Stuffed Floors",
                  "Replace Stuff", "Architect", "Vanilla Expanded Architect")),
    "factions": (("faction", "enem", "npc", "diplomacy", "empire", "pirate", "outlander"),
                 ("Vanilla Factions Expanded", "Faction", "More Faction Interaction", "Pirates", "Settlers",
                  "Rim War")),
    "storytelling": (("story", "storyteller", "event ", "events", "incident", "quests", "lore ", "narrative", "immers",
                      "drama", "social", "relationship"),
                     ("Storyteller", "Vanilla Events Expanded", "Quests", "Incidents", "Real Ruins",
                      "Hospitality", "Psychology", "Vanilla Social Interactions Expanded")),
    "graphics": (("graphic", "visual", "texture", "hd ", "lighting", "beautiful", "aesthetic", "retexture"),
                 ("Textures", "Retexture", "Graphics", "Weather", "Vanilla Textures Expanded", "Realistic",
                  "Shadows", "Nature")),
    "farming": (("farm", "food", "cook", "agricult", "plant", "crop", "harvest", "garden", "brew", "meal"),
                ("Vanilla Cooking Expanded", "Vanilla Plants Expanded", "Vanilla Brewing Expanded", "Farming",
                 "Cooking", "Food", "Garden")),
    "hygiene": (("hygien", "toilet", "bath", "shower", "water", "sanita", "plumb"),
                ("Dubs Bad Hygiene", "Hygiene", "Water")),
    "pawns": (("trait", "character", "personality", "skill", "backstor", "psycholog"),
              ("Vanilla Traits Expanded", "Traits", "Character Editor", "Pawn Editor", "Backstories",
               "Individuality", "Skills")),
    "apparel": (("apparel", "cloth", "outfit", "fashion", "hair", "armor", "armour", "hats", "hat ", "dress"),
                ("Vanilla Apparel Expanded", "Apparel", "Clothing", "Vanilla Hair Expanded", "Hair",
                 "Vanilla Armour Expanded", "Outfits")),
    "races": (("race", "xeno", "alien", "species", "genes", "gene ", "genetic", "humanoid", "biotech"),
              ("Vanilla Races Expanded", "Xenotypes", "Genes", "Humanoid Alien Races", "Alpha Genes", "Race")),
    "psycasts": (("psycast", "psychic", "psionic", "royalty"),
                 ("Vanilla Psycasts Expanded", "Psycasts", "Psychic")),
    "industry": (("industr", "factory", "production", "power ", "electric", "oil ", "nuclear", "conveyor",
                  "logistic", "crafting"),
                 ("Rimefeller", "Rimatomics", "Vanilla Power Expanded", "Power",
                  "Logistics", "Production", "Vanilla Furniture Expanded Production")),
    "storage": (("storage", "stockpile", "shelf", "shelves", "hauling", "organiz"),
                ("Storage", "Deep Storage", "Adaptive Storage", "Shelves", "Pick Up And Haul")),
    "trade": (("trade", "trading", "caravan", "merchant", "economy", "money", "silver"),
              ("Trade", "Trading", "Caravan", "Vanilla Trading Expanded", "Hospitality")),
    "world": (("biome", "map", "world", "terrain", "planet", "explor", "ruins"),
              ("Biomes", "Alpha Biomes", "More Vanilla Biomes", "Map", "World", "Real Ruins", "Terrain")),
    "vehicles": (("vehicle", "car ", "cars", "tank", "truck", "boat", "helicopter", "aircraft"),
                 ("Vanilla Vehicles Expanded", "Vehicle Framework", "Vehicles", "Tanks")),
    "children": (("child", "kids", "kid ", "baby", "babies", "school", "family", "pregnan"),
                 ("Children", "Babies", "School", "Growing", "Family")),
    "religion": (("religio", "ideolog", "precept", "ritual", "faith", "cult ", "cults", "gods", "god "),
                 ("Vanilla Ideology Expanded", "Precepts", "Rituals", "Ideology", "Religion")),
    "zombies": (("zombie", "undead", "apocalyp", "walking dead"), ("Zombieland", "Zombie", "Undead", "Apocalyptic")),
    "horror": (("horror", "dark", "anomaly", "creep", "spooky", "monster"),
               ("Horror", "Anomaly", "Vanilla Anomaly Expanded", "Monsters")),
    "hardcore": (("hardcore", "survival", "realism", "realistic", "challeng", "difficult", "harsh", "brutal"),
                 ("Hardcore", "Survival", "Realistic", "Dubs Bad Hygiene", "Common Sense")),
    # not triggered by "vanilla+" / "vanilla-friendly" / "lore-friendly": those ask for NO big content mods
    "vanilla expanded": (("vanilla expanded", "oskar"),
                         ("Vanilla Expanded", "Vanilla Factions Expanded", "Vanilla Furniture Expanded",
                          "Vanilla Weapons Expanded", "Oskar Potocki")),
    "multiplayer": (("multiplayer", "coop", "co-op"), ("Multiplayer",)),
    "music": (("music", "soundtrack", "audio", "sfx", "sound effect"), ("Music", "Sound", "Soundtrack")),
}
_ESSENTIALS = _QOL_MUST + ("RimHUD", "Performance Optimizer", "Pick Up And Haul", "Common Sense", "Numbers",
                           "Replace Stuff", "Dubs Mint Menus")
_STOPWORDS = set("""
a an the and or but if then than so to of in on at by for with without from into onto over under about as is are
was were be been being it its this that these those there here i me my we our you your he she they them their
want wants wanted need needs like likes love would could should can will just also very really more most less
some any all lots lot many much few bit little good great best better nice cool fun awesome make makes making
give get got add adds adding use using play playing playthrough run game games rimworld rim mod mods modded
modlist modpack pack list please thanks thank something stuff things thing kind type style focus focused around
heavy light new old full whole overall based feel feels experience set setting run colony colonies colonist
dont don't do does not no none only other others etc which who what when where how why same own
vanilla plus friendly lore keep stable stability tough tougher deeper harsher system systems changes change balance
anything everything nothing hard pawn pawns small big content fix fixes tweak tweaks improvement improvements
""".split())
_LANGS = (r"(?:zh|zht|zhs|zh-?cn|zh-?tw|zh-?hans|zh-?hant|zhtc|cn|chs|cht|tw|ru|rus|ua|ukr|kr|ko|jp|ja|de|ger|fr|es|esp|"
          r"pl|pt|pt-?br|br|tr|it|cz|vn|th)")
_LANG_NAMES = (r"(?:english|russian|chinese|korean|japanese|german|french|spanish|polish|portuguese|brazilian|ukrainian|"
               r"turkish|czech|italian|vietnamese|thai|latino)")
# Patterns seen in real workshop titles of translation uploads (for rows whose Steam tags don't say 'Translation').
# A plain language name only counts at the end, in brackets or before "language/translation":
# "Russian Weapons Pack" and "German Shepherd" are content mods.
_TRANSLATION_RE = re.compile(
    r"translat|locali[sz]ation|language\s*pack|汉化|漢化|中文|简体|繁體|繁体|繁中|简中|簡中|翻译|翻譯|번역|한국어|日本語"
    r"|[\u0400-\u04ff]|español|espanol|deutsch|türkçe|turkce|çeviri|ceviri|übersetz|ubersetz|\btradu[cçz]|français"
    r"|português|portugues\b"
    r"|[\[(（]\s*(?:" + _LANGS + "|" + _LANG_NAMES + r")\s*[\])）]"
    r"|(?:^|[\s_\-.\]])" + _LANGS + r"(?:[\s_\-]?(?:pack|ver(?:sion)?|lang(?:uage)?|supplement))?\s*$"
    r"|^\s*(?:zh|cn|chs|cht|ru|rus|ua|ukr)[\s_\-]|(?<![a-z0-9/])(?:zh|chs|cht|zhtw|zhcn)(?![a-z0-9/])"
    r"|" + _LANG_NAMES + r"(?:\s*(?:language|lang|translation|localization|version|ver|pack|patch)){0,2}\s*$"
    r"|\b" + _LANG_NAMES + r"[\s_\-]*(?:language|lang\b|translation|localization)", re.I)
_LATIN_RE = re.compile(r"[A-Za-z]{3,}")
_ABANDONED_RE = re.compile(r"no longer (?:supported|maintained|updated|working)|\b(?:deprecated|abandoned|discontinued|"
                           r"obsolete|unmaintained|outdated)\b|\bdo not (?:use|download|subscribe)\b", re.I)
_LIBRARY_RE = re.compile(r"\b(?:framework|library|lib|hugslib|harmony|api|dependency|core\s*lib)\b", re.I)
_PATCH_RE = re.compile(r"\b(?:patch(?:es)?|compat(?:ibility)?|fix(?:es)?)\b", re.I)
_TAG_RE = re.compile(r"[\[({【][^\])}】]*[\])}】]")
_NOISE_RE = re.compile(r"\b(?:continued|updated|update|unoff?ici?al|fork|forked|re-?upload(?:ed)?|fixed|revived|redux|"
                       r"remastered|reloaded|legacy|mlie|dev(?:elopment)?\s+(?:snapshot|build|branch)|snapshot|"
                       r"v?\d+(?:\.\d+)+[a-z]?)\b")
_TRAILING_RE = re.compile(r"(?:\s+(?:expanded|reborn|remake|remade|plus|lite|light|mod|edition|version))+$")


def _family(row: DbMod) -> str:
    """Series key: 'Vanilla Cooking Expanded - Stews' and 'Vanilla Cooking Expanded' share one."""
    t = _TAG_RE.sub(" ", (row.steam_name or row.name or "").lower())
    return re.split(r"\s+[-—–]\s+|:\s", t, maxsplit=1)[0].strip()
_NEG_RE = re.compile(r"\b(?:no|without|avoid|exclude|excluding|except|nothing|"
                     r"(?:don'?t|do\s+not|not)(?:\s+(?:want|need|include|add|like|use))?)\s+([^.;:!?()\n]+)", re.I)
_CLAUSE_END_RE = re.compile(r"\s+(?:but|please|though|however|while|with|so|because|since|just|keep|instead)\b.*",
                            re.I | re.S)
_LIST_SEP_RE = re.compile(r"(\s*,\s*(?:(?:or|nor|and)\s+)?|\s+(?:or|nor|and)\s+|\s*/\s*)", re.I)
_SCIFI_WORDS = ("high tech", "high-tech", "hightech", "sci-fi", "scifi", "laser", "plasma", "space", "glitter",
                "Save Our Ship", "android")
# an excluded word also excludes these title fragments (word-prefix matched like every avoid term)
_AVOID_ALSO: dict[str, tuple[str, ...]] = {
    "gun": ("firearm", "rifle", "pistol", "Vanilla Weapons Expanded"), "firearm": ("guns", "rifle", "pistol"),
    "high tech": _SCIFI_WORDS, "hightech": _SCIFI_WORDS, "sci fi": _SCIFI_WORDS, "scifi": _SCIFI_WORDS,
    "futuristic": _SCIFI_WORDS,
}


def _negations(text: str) -> tuple[list[str], str]:
    """-> (excluded terms, text with the negated phrases cut out). Reads lists: 'no guns or high-tech' and
    'no guns, lasers or robots' exclude every item; 'no guns, lots of castles' only excludes guns."""
    avoid, keep, pos = [], [], 0
    while m := _NEG_RE.search(text, pos):
        clause = _CLAUSE_END_RE.sub("", m.group(1))
        parts = _LIST_SEP_RE.split(clause)
        seps = parts[1::2]
        listy = any(re.search(r"\b(?:or|nor)\b|/", s, re.I) for s in seps)   # commas belong to an "A, B or C" list
        end = m.start(1)
        for i, item in enumerate(parts[0::2]):
            if i and "," in seps[i - 1] and not listy:
                break
            if i and (item.split() or [""])[0].lower() in _STOPWORDS:     # 'no raids and more animals'
                break
            term = re.sub(r"^(?:any|more|the|other)\s+", "", item.strip(), flags=re.I)
            term = re.sub(r"\s+(?:mods?|stuff|things?|content)$", "", term, flags=re.I).strip()
            if not term or term.split()[0].lower() in _STOPWORDS:   # 'nothing that changes...', 'no guns and lots of'
                if i == 0 and not re.fullmatch(r"not", m.group(0).split()[0], re.I):
                    end = m.start(1) + (clause + ",").index(",")      # described exclusion: drop it up to ','
                break
            if len(term) >= 3:
                avoid.append(term)
            end = m.start(1) + sum(map(len, parts[:2 * i + 1]))
        keep.append(text[pos:m.start()])
        pos = max(end, m.start() + 1)
    keep.append(text[pos:])
    seen = {a.lower() for a in avoid}
    for a in list(avoid):
        k = a.lower().replace("-", " ")
        for key, extra in _AVOID_ALSO.items():
            if k.startswith(key):
                avoid += [e for e in extra if e.lower() not in seen and not seen.add(e.lower())]
    return avoid, " ".join(keep)


def _title_keys(row: DbMod) -> set[str]:
    """Normalised titles: two rows sharing a key are uploads/forks of the same mod."""
    keys = set()
    for t in (row.name, row.steam_name):
        t = (t or "").lower()
        parts = [_TAG_RE.sub(" ", t)] + [m[1:-1] for m in _TAG_RE.findall(t) if len(m.split()) >= 3]
        for part in parts:
            k = _TRAILING_RE.sub("", " ".join(re.findall(r"[a-z0-9]+", _NOISE_RE.sub(" ", part))))
            if len(k) >= 4:
                keys.add(k)
    return keys


def _word_prefix(term: str, text: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(term.lower()), text.lower()) is not None


def _supports(row: DbMod, version: str) -> bool:
    return not version or not row.game_versions or any(v in row.game_versions for v in compat_versions(version))


def _int(x) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


def _fmt_subs(n: int) -> str:
    if n <= 0:
        return "?"
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    if n >= 1000:
        return f"{n // 1000}k"
    return str(n)


def _clean_desc(s: str, n: int = 160) -> str:
    s = re.sub(r"\[/?[a-z0-9*]+(?:=[^\]]*)?\]", " ", s or "", flags=re.I)      # BBCode
    s = re.sub(r"https?://\S+", " ", s)
    s = re.sub(r"[|\s]+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _clean_query(q: str) -> str:
    q = re.sub(r"[\"“”`*_]+", " ", str(q or ""))
    q = re.sub(r"\s+", " ", q).strip(" -:,.")
    words = q.split()
    return " ".join(words[:5])[:60]


def _norm_plan(data: dict, max_queries: int = 30) -> tuple[list[dict], list[str], str]:
    raw = (data.get("queries") or data.get("searches") or data.get("items") or []) if isinstance(data, dict) else []
    queries, seen = [], set()
    for q in raw if isinstance(raw, list) else []:
        if isinstance(q, str):
            q = {"query": q}
        if not isinstance(q, dict):
            continue
        text = _clean_query(q.get("query") or q.get("q") or q.get("search") or "")
        if len(text) < 2 or text.lower() in seen:
            continue
        seen.add(text.lower())
        queries.append({"query": text, "category": str(q.get("category") or text).strip()[:40] or text,
                        "want": max(0, min(25, _int(q.get("want", 3)) if q.get("want") is not None else 3))})
        if len(queries) >= max_queries:
            break
    avoid_raw = data.get("avoid") if isinstance(data, dict) else None
    avoid = [a.strip()[:40] for a in (avoid_raw if isinstance(avoid_raw, list) else [])
             if isinstance(a, str) and len(a.strip()) >= 3][:20]
    notes = str(data.get("notes") or "")[:800] if isinstance(data, dict) else ""
    return queries, avoid, notes


def _auto_estimate_target(prompt: str, no_lazy: bool = False) -> tuple[int, int]:
    """(initial target estimate, minimum floor) when the AI chooses the mod count."""
    low = (prompt or "").lower()
    if re.search(r"\b(?:mega|massive|huge|giant|enormous|200\+|300\+|tons? of|everything|big mod(?:s?list|pack))\b", low):
        base, floor = 180, (140 if no_lazy else 100)
    elif re.search(r"\b(?:light|minimal|small|tiny|few mods|weak pc|weak laptop|potato|vanilla\+|vanilla plus)\b", low):
        base, floor = (45 if no_lazy else 30), 20
    elif re.search(r"\b(?:overhaul|full|complete|deep|heavy|lots of|large|big)\b", low):
        base, floor = (130 if no_lazy else 100), (95 if no_lazy else 70)
    else:
        base, floor = (95 if no_lazy else 70), (65 if no_lazy else 45)
    return base, floor


def _norm_picks(data) -> tuple[list[tuple[str, str]], list[tuple[str, str]], str]:
    """-> ([(raw id, reason)], [(bad item, why)], summary). Tolerates sloppy shapes."""
    if not isinstance(data, dict):
        return [], [("(reply)", "reply was not a JSON object")], ""
    items = data.get("picks")
    if items is None:
        items = data.get("items") or data.get("mods") or data.get("selection")
    summary = str(data.get("summary") or "")[:1200]
    if not isinstance(items, list):
        return [], [("(reply)", "no 'picks' list in the model reply")], summary
    out, bad = [], []
    for it in items[:1000]:
        if isinstance(it, dict):
            raw = next((it[k] for k in ("id", "workshop_id", "workshopId", "wid", "publishedfileid") if k in it), None)
            reason = str(it.get("reason") or "")[:300]
        elif isinstance(it, (str, int)) and not isinstance(it, bool):
            raw, reason = it, ""
        else:
            bad.append((str(it)[:40], "malformed pick"))
            continue
        if raw is None or isinstance(raw, (dict, list, bool, float)):
            bad.append((str(it)[:40], "pick without a usable id"))
            continue
        out.append((str(raw).strip(), reason))
    return out, bad, summary


_GH_URL_IN_TEXT_RE = re.compile(
    r"(?:https?://github\.com/|\bgh:)([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+?)(?:\.git)?(?=[/?#\s\"'<>)\],;]|$)", re.I
)
_GITHUB_INTENT_RE = re.compile(r"\b(?:github|gh:|git\s+mod|git\s+repo)\b", re.I)


def _gh_mod(repo: str, pid: str, name: str, authors: str, desc: str, subs: int = 120000,
            deps: dict[str, str] | None = None) -> DbMod:
    return DbMod(f"gh:{repo.lower()}", pid.lower(), name, f"{name} [GitHub]", authors, ["1.5", "1.6", "1.7"],
                 deps or {"2009463077": "Harmony"}, subscriptions=subs, description=desc)


GITHUB_MODS: list[DbMod] = [
    _gh_mod("CombatExtended-Continued/CombatExtended", "ceteam.combatextended", "Combat Extended (GitHub)",
            "Combat Extended Team", "Official GitHub repository and latest 1.6/1.7 development build of Combat Extended.", 550000),
    _gh_mod("rwmt/Multiplayer", "rwmt.multiplayer", "Multiplayer (GitHub)",
            "Zetrith, RWMT", "Official RimWorld Multiplayer mod repository on GitHub for co-op colonies.", 450000),
    _gh_mod("Taranchuk/PerformanceOptimizer", "taranchuk.performanceoptimizer", "Performance Optimizer (GitHub)",
            "Taranchuk", "FPS and TPS optimizations, caching, and UI speedups for RimWorld 1.6/1.7.", 420000),
    _gh_mod("Dubwise56/Dubs-Performance-Analyzer", "dubwise.dubsperformanceanalyzer", "Dubs Performance Analyzer (GitHub)",
            "Dubwise", "GitHub build of Dubs Performance Analyzer with tick profiling and optimization fixes.", 380000),
    _gh_mod("Dubwise56/Rimefeller", "dubwise.rimefeller", "Rimefeller (GitHub)",
            "Dubwise", "Oil drilling, refining, pipelines, chemfuel powerplants, napalm, and synthetic materials.", 480000),
    _gh_mod("Dubwise56/Rimatomics", "dubwise.rimatomics", "Rimatomics (GitHub)",
            "Dubwise", "Nuclear reactors, plutonium processing, ICBM missiles, railguns, and directed energy weapons.", 490000),
    _gh_mod("Dubwise56/Dubs-Bad-Hygiene", "dubwise.dubsbadhygiene", "Dubs Bad Hygiene (GitHub)",
            "Dubwise", "Plumbing, sewage, water towers, central heating, air conditioning, irrigation, and hygiene needs.", 520000),
    _gh_mod("Dubwise56/Dubs-Mint-Menus", "dubwise.dubsmintmenus", "Dubs Mint Menus (GitHub)",
            "Dubwise", "Searchable radial/compact menus for bills, architect build gizmos, plants, and gear.", 460000),
    _gh_mod("Dubwise56/Dubs-Mint-Minimap", "dubwise.dubsmintminimap", "Dubs Mint Minimap (GitHub)",
            "Dubwise", "Customizable in-game minimap tracking raiders, wildlife, colonists, and fires.", 390000),
    _gh_mod("OskarPotocki/VanillaExpandedFramework", "oskarpotocki.vanillafactionsexpanded.core", "Vanilla Expanded Framework (GitHub)",
            "Oskar Potocki, Sarg Bjornson, Taranchuk", "Core framework powering the Vanilla Expanded series (GitHub release).", 900000),
    _gh_mod("SmashPhil/VehicleFramework", "smashphil.vehicleframework", "Vehicle Framework (GitHub)",
            "Smash Phil", "Framework for land, sea, and air vehicles in RimWorld (GitHub repository).", 500000),
    _gh_mod("Kentington/SaveOurShip2", "kentington.saveourship2", "Save Our Ship 2 (GitHub)",
            "Kentington, Thain", "Build orbital starships, fight ship-to-ship space battles, and travel between worlds.", 410000,
            deps={"2009463077": "Harmony", "3022800302": "Vehicle Framework"}),
    _gh_mod("pardeike/HarmonyRimWorld", "brrainz.harmony", "Harmony (GitHub)",
            "Andreas Pardeike", "Harmony C# patching library for RimWorld mods (official GitHub repository).", 1500000, deps={}),
    _gh_mod("UnlimitedHugs/RimworldHugsLib", "unlimitedhugs.hugslib", "HugsLib (GitHub)",
            "UnlimitedHugs", "Shared library providing mod settings, update news, and log sharing.", 1100000),
    _gh_mod("UnlimitedHugs/RimworldAllowTool", "unlimitedhugs.allowtool", "Allow Tool (GitHub)",
            "UnlimitedHugs", "Haul urgently, select similar, veinmine/strip-mine, finish off, and mass-allow tools.", 950000),
    _gh_mod("Jaxe-Dev/RimHUD", "jaxe.rimhud", "RimHUD (GitHub)",
            "Jaxe", "Detailed inspect pane HUD displaying pawn stats, skills, needs, and health at a glance.", 880000),
    _gh_mod("Mehni/PickUpAndHaul", "mehni.pickupandhaul", "Pick Up And Haul (GitHub)",
            "Mehni", "Colonists use their inventory capacity to gather multiple items when hauling.", 820000),
    _gh_mod("Mehni/Numbers", "mehni.numbers", "Numbers (GitHub)",
            "Mehni", "Customizable overview tab comparing every stat, skill, need, and gear across colonists or enemies.", 520000),
    _gh_mod("OrionFive/Hospitality", "orion.hospitality", "Hospitality (GitHub)",
            "Orion", "Host visiting guests, build guest beds, sell items in shopping zones, and recruit visitors.", 780000),
    _gh_mod("erdelf/HumanoidAlienRaces", "erdelf.humanoidalienraces", "Humanoid Alien Races (GitHub)",
            "erdelf", "Framework for custom alien races and xenohumans in RimWorld.", 850000),
    _gh_mod("sargbjornson/AlphaAnimals", "sarg.alphaanimals", "Alpha Animals (GitHub)",
            "Sarg Bjornson", "Over 100 unique genetically engineered and alien creatures, plus the Black Hive faction.", 560000,
            deps={"2009463077": "Harmony", "2023507013": "Vanilla Expanded Framework"}),
    _gh_mod("sargbjornson/AlphaBiomes", "sarg.alphabiomes", "Alpha Biomes (GitHub)",
            "Sarg Bjornson", "12 exotic alien biomes with unique flora, terrain, weather, and resources.", 430000,
            deps={"2009463077": "Harmony", "2023507013": "Vanilla Expanded Framework"}),
    _gh_mod("sargbjornson/AlphaGenes", "sarg.alphagenes", "Alpha Genes (GitHub)",
            "Sarg Bjornson", "Hundreds of new Biotech genes, xenotypes, abilities, and genetic quests.", 410000,
            deps={"2009463077": "Harmony", "2023507013": "Vanilla Expanded Framework", "1826140": "RimWorld - Biotech"}),
    _gh_mod("pardeike/Zombieland", "brrainz.zombieland", "Zombieland (GitHub)",
            "Andreas Pardeike", "Full zombie apocalypse overhaul with thousands of zombies, infection, and customizable threat.", 360000),
    _gh_mod("Owlchemist/clean-pathfinding", "owlchemist.cleanpathfinding", "Clean Pathfinding (GitHub)",
            "Owlchemist", "Lightweight pathfinding tuning, road preference, door-bashing prevention, and TPS optimization.", 340000),
]
GITHUB_BY_ID: dict[str, DbMod] = {m.workshop_id: m for m in GITHUB_MODS}


def _parse_wid(s: str) -> str:
    s = (s or "").strip()
    m_gh = re.match(r"^(?:gh:|https?://github\.com/)([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+?)(?:\.git)?(?:[/?#].*)?$", s, re.I)
    if m_gh:
        return "gh:" + m_gh.group(1).lower()
    m = re.fullmatch(r"\s*(\d{3,20})\s*", s) or re.search(r"[?&]id=(\d{3,20})", s) or re.match(r"\s*(\d{3,20})\b", s)
    return m.group(1) if m else ""


def _offline_queries(prompt: str, essentials: bool) -> tuple[list[tuple[str, float, int]], list[str], list[str]]:
    """-> ([(query, weight, max results used; 0 = all)], avoid terms, matched themes)."""
    avoid, positive = _negations(_no_links(prompt))
    low = " " + positive.lower() + " "
    out: list[tuple[str, float, int]] = []
    seen: set[str] = set()

    def add(q: str, w: float, per: int = 0) -> None:
        q = _clean_query(q)
        if len(q) >= 3 and q.lower() not in seen and not any(_word_prefix(a, q) for a in avoid):
            seen.add(q.lower())
            out.append((q, w, per))

    for q in re.findall(r"[\"“]([^\"”]{3,40})[\"”]", positive):            # quoted phrases: exact intent
        add(q, 3.0)
    for q in re.findall(r"\b([A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)+)", positive):  # "Combat Extended", "Vanilla Expanded"
        if q.split()[0].lower() not in _STOPWORDS:
            add(q, 3.0)
    for q in re.findall(r"\b([A-Z][a-z]+[A-Z]\w*)\b", positive):             # CamelCase names: RimHUD, RimFridge
        if not any(q.lower() == t.strip() for triggers, _ in _THEMES.values() for t in triggers):   # not 'QoL'
            add(q, 3.0)

    def hits(triggers, text: str) -> bool:
        return any(re.search(r"(?<![a-z0-9])" + re.escape(t), text) for t in triggers)

    # an excluded word also rules out its theme: 'no guns' -> no combat queries, 'no high-tech' -> no sci-fi
    blocked = {name for name, (triggers, _) in _THEMES.items() for a in avoid
               if hits(triggers, f" {a.lower()} {a.lower().replace('-', ' ')} ")}
    themes = [name for name, (triggers, _) in _THEMES.items() if name not in blocked and hits(triggers, low)]
    if not themes:
        themes = [t for t in ("qol", "vanilla expanded", "storytelling") if t not in blocked]
    rest = low               # drop matched trigger phrases, longest first ('quality of life' must not leave 'life')
    for t in sorted({t.strip() for name in themes for t in _THEMES[name][0]}, key=len, reverse=True):
        rest = re.sub(r"(?<![a-z0-9])" + re.escape(t) + r"[a-z0-9'\-]*", " ", rest)
    if {"medieval", "tribal", "fantasy"} & set(themes) and "factions" in themes:
        themes.remove("factions")        # 'feudal factions': the period theme has its own; the generic ones carry guns
    for name in themes:
        for q in _THEMES[name][1]:
            if not (essentials and q in _ESSENTIALS):     # added below as that exact mod, with a reserved slot
                add(q, 2.0)
    words = [w for w in re.findall(r"[a-z0-9][a-z0-9']{2,}", rest)     # leftover words: themes miss them
             if w not in _STOPWORDS]
    for w in list(dict.fromkeys(words))[:10]:
        add(w, 1.5)
    if essentials:
        for q in _ESSENTIALS:
            add(q, 1.0, 1)
    return out, avoid, themes


# ---------------------------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------------------------

class _Cancelled(Exception):
    pass


def generate(req: GenRequest, db, llm: LLM | None, progress: Progress = noop_progress, cancel=None,
             details_fn=None, workshop=None) -> GenResult:
    """Build a mod list. Never trusts model output (see module docstring). On cancel returns the picks
    validated so far (summary starts with 'Cancelled'). workshop: the live Steam Workshop (default: from req)."""
    run = _Run(req, db, progress, cancel, details_fn, workshop)
    try:
        if llm is None or llm.provider == "offline":
            run.offline()
        else:
            run.ai(llm)
    except _Cancelled:
        run.res.summary = ("Cancelled - partial result (not fully verified, dependencies may be missing)."
                           if run.res.picks else "Cancelled.")
        run.res.log.append(run.res.summary)
        progress(None, "Cancelled.")
    return run.res


class _Run:
    def __init__(self, req: GenRequest, db, progress: Progress, cancel, details_fn, workshop=None):
        self.req, self.db, self.progress, self.cancel = req, db, progress or noop_progress, cancel
        # live_verify off = stay off Steam entirely; without a key only links/details work (no search)
        self.workshop = workshop if workshop is not None else _Workshop(req.steam_key) if req.live_verify else None
        self.linked = False
        self.live_new: dict[str, str] = {}     # workshop id -> title: mods Steam found that the DB lacked
        if details_fn is None:
            def details_fn(ids: list[str]) -> dict[str, dict]:
                from . import steam
                return steam.get_details(ids, db=db)
        self.details_fn = details_fn
        self.res = GenResult()
        self.v = (req.version_short or "").strip()
        self.auto_count = bool(req.auto_count or int(req.target_count or 0) <= 0)
        est, self._auto_floor = _auto_estimate_target(req.prompt, req.no_lazy)
        self.target = est if self.auto_count else max(1, min(500, int(req.target_count or 40)))
        self.exclude = {str(x) for x in (req.exclude_workshop_ids or set())}
        self.owned = {p.lower() for p in (req.owned_dlcs or set())}
        neg, kept = _negations(req.prompt or "")
        # "no translations" must not turn them on, even when the prompt also lists 中文/русский as words to avoid
        self.allow_foreign = (not any(re.search(r"translat|locali[sz]|language", a, re.I) for a in neg)
                              and bool(re.search(r"[Ѐ-ӿ぀-ヿ㐀-鿿가-힯]|translat", kept, re.I)))
        self.rows: dict[str, DbMod] = {}
        self.details: dict[str, dict] = {}
        self.picks: list[Pick] = self.res.picks
        self.outdated: set[str] = set()
        self.uti_from: dict[str, str] = {}      # candidate swapped in by Use This Instead -> the old mod's name
        self._rules: dict | None = None
        self._keys: dict[str, set[str]] = {}
        self._f = 0.0
        self.steam_down = False
        self.share: dict[str, float] = {}   # mod -> share of similar player-made Workshop packs that contain it
        self.local = False              # local model (Ollama/LM Studio): small context window, send a smaller table
        self.by_desc: set[tuple[int, str]] = set()     # (query index, mod) that Steam matched by description only

    # -- plumbing -----------------------------------------------------------------------------
    def say(self, f, text: str) -> None:
        """Log line + progress; f=None keeps the previous fraction."""
        if f is not None:
            self._f = f
        self.res.log.append(text)
        log.info("ai: %s", text)
        self.progress(self._f, text)
        self.check()

    def check(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise _Cancelled()

    def reject(self, wid: str, why: str) -> None:
        self.res.rejected.append((str(wid)[:40], why))

    def ask(self, llm: LLM, system: str, user: str, schema: dict, max_tokens: int) -> dict:
        """complete_json on a helper thread so Cancel works mid-request; unexpected errors -> LLMError."""
        self.check()
        box: dict = {}

        def work():
            try:
                box["ok"] = llm.complete_json(system, user, schema, max_tokens)
            except LLMError as e:
                box["err"] = e
            except Exception as e:  # noqa: BLE001 - any provider bug becomes a fallback, not a crash
                log.exception("LLM call failed")
                box["err"] = LLMError(f"Unexpected AI error: {e!r}", "server")

        t = threading.Thread(target=work, name="rmm-llm", daemon=True)
        t.start()
        try:
            while t.is_alive():
                t.join(0.25)
                self.check()
        except BaseException:
            getattr(llm, "abort", lambda: None)()          # Cancel: don't leave a Claude Code process running (and using the subscription)
            raise
        if "err" in box:
            raise box["err"]
        if llm.last_usage:
            self.say(None, f"   reply received ({llm.last_usage})")
        return box["ok"]

    def rules(self) -> dict:
        if self._rules is None:
            try:
                self._rules = self.db.rules() if hasattr(self.db, "rules") else {}
            except Exception as e:  # noqa: BLE001
                log.info("community rules unavailable: %s", e)
                self._rules = {}
        return self._rules

    def keys(self, row: DbMod) -> set[str]:
        k = self._keys.get(row.workshop_id)
        if k is None:
            k = self._keys[row.workshop_id] = _title_keys(row)
        return k

    def subs(self, wid: str) -> int:
        row = self.rows.get(wid)
        return max(row.subscriptions if row else 0, _int(self.details.get(wid, {}).get("subscriptions")))

    def db_empty(self) -> bool:
        try:
            empty = bool(self.db.is_empty()) if hasattr(self.db, "is_empty") else False
        except Exception:  # noqa: BLE001
            empty = False
        if empty:
            self.res.summary = "The mod database is empty. Update it first (Workshop tab > Update database)."
            self.say(1.0, self.res.summary)
        return empty

    _BROKEN_WORKSHOP_IDS = {
        "725219116",   # Work Tab (original) - crashes RimWorld 1.7 recovery & conflicts with Achtung!
        "725657237",   # Glitter Tech Classic (No Surgery) - obsolete 1.0/1.4 XML fields
        "735106432",   # EdB Prepare Carefully - outdated/corrupts pawn generation & hediffs; use Character Editor / Pawn Editor
        "928376710",   # original archived Zombieland (superseded by 3577005360 / 3827574769)
        "1201382956",  # A RimWorld of Magic - NullReferenceExceptions & missing textures on 1.7
        "1235181370",  # Designator Shapes - NullReferenceException in KeyBindingDef.get_MainKey on 1.7
        "1541438907",  # Animal Gear - Harmony patch exception on DynamicPawnRenderNodeSetup_Apparel in 1.7
        "2033979700",  # Project RimFactory Revived - InvalidProgramException in Building_Storage.GetGizmos on 1.7
        "2037491557",  # Project RimFactory - Drones - requires broken Project RimFactory Revived
        "2037492036",  # Project RimFactory - Insanity - requires broken Project RimFactory Revived
        "2479389928",  # RocketMan - Performance Mod - abandoned at 1.5, obsolete in RimWorld 1.6/1.7 (native multithreading & Waylith)
        "2655793307",  # Realistic Explosions (1.6) (CE) - missing <packageId> in About.xml
        "2775017012",  # Vanilla Backgrounds Expanded - broken main-menu Texture2D paths on 1.7
        "2883894160",  # Project RimFactory Revived Retextured - requires broken Project RimFactory Revived
        "2972167798",  # Nuff's Auto-Patcher for Combat Extended - TypeInitializationException on startup
        "3105126782",  # Allow Tool Fixed Patch (seekiworksmod.no16) - corrupts Allow Tool textures
        "3312959701",  # JecsLite - A RimWorld of Magic - required only by broken A RimWorld of Magic
        "3453549086",  # Work Tab (Continued) - crashes RimWorld 1.7 recovery & conflicts with Achtung!
        "3460442482",  # D-FunctionalAmmunition Library - broken assembly in 1.7
        "3461990389",  # The Dead Man's Switch - MCMPAC - requires D-FunctionalAmmunition Library
        "3563882422",  # Better Architect Menu - NullReferenceException in VEFramework_Patch.Postfix on 1.7
        "3581627851",  # Project RimFactory - Materials - requires broken Project RimFactory Revived
        "3610684714",  # Piquant premonitions - corrupted Geisha_Backstories.xml
        "3819799478",  # The Dead Man's Switch II - duplicate aoba.deadmanswitch.core, breaks DMS 1 submods
        "3823339517",  # Fortifications - Industrial & VFE Security fix - invalid <Defs> root in Patches/
        "3826500999",  # The Dead Man's Switch - Taktikmagier - DMS 2 submod, missing DMS_SolitaryZPM in DMS 1
        "3828262951",  # Fortification Industrial - PantherOstwallturm - duplicate XML node FTC_BasePermanentBunker
    }
    _WAYLITH_INCOMPATIBLE_IDS = {
        "2934420800",  # Prepatcher - CIL prepatches blocked by RimWorld 1.7 Waylith
        "3811986263",  # Fishery - required only by Performance Fish (blocked on 1.7 Waylith)
        "3811986740",  # Performance Fish - CIL prepatches blocked by RimWorld 1.7 Waylith
        "3814922443",  # Performance Fish Gas Compatibility Fix
    }

    def missing_dlcs(self, row: DbMod) -> list[str]:
        return [DLCS[DLC_BY_APPID[k]][1] for k in row.dependencies
                if k in DLC_BY_APPID and DLC_BY_APPID[k] not in self.owned]

    def unusable(self, row: DbMod, avoid: list[str]) -> str:
        """Why a DB row must not be offered/picked ('' = fine)."""
        is_gh = row.workshop_id.startswith("gh:")
        if row.is_dlc or (not row.workshop_id.isdigit() and not is_gh) or row.workshop_id == RIMWORLD_APPID:
            return "official content"
        if row.unpublished:
            return "unpublished"
        if row.workshop_id in self.exclude:
            return "excluded"
        if row.workshop_id in self._BROKEN_WORKSHOP_IDS:
            return "known broken or conflicting upload"
        if self.v == "1.7" and row.workshop_id in self._WAYLITH_INCOMPATIBLE_IDS:
            return "incompatible with RimWorld 1.7 Waylith engine"
        if self.req.require_package_id and not row.package_id:
            return "no packageId"
        if not _supports(row, self.v):
            return "wrong game version"
        title = f"{row.name} {row.steam_name}"
        if not self.allow_foreign and (row.kind == "translation" or _TRANSLATION_RE.search(title)
                                       or not _LATIN_RE.search(title)):
            return "translation / non-English"
        if _ABANDONED_RE.search(title):
            return "marked abandoned/outdated in its title"
        if any(_word_prefix(a, title) for a in avoid):
            return "matches an excluded term"
        need = self.missing_dlcs(row)
        if need:
            return "needs DLC " + ", ".join(need)
        return ""

    def dead(self, wid: str) -> str:
        d = self.details.get(wid)
        if not d:
            return ""
        if _int(d.get("result", 1)) != 1:
            return "not available on Steam (removed, private or hidden)"
        app = d.get("consumer_app_id")
        if app not in (None, "") and str(app) != RIMWORLD_APPID:
            return "not a RimWorld workshop item"
        if _int(d.get("banned")):
            return "banned on Steam"
        return ""

    def enrich(self, ids: list[str]) -> bool | None:
        """Fetch Steam details for ids not fetched yet, 100 per call, 4 calls in parallel.
        Ids missing from a non-empty answer are marked result 9. -> True ok / False all failed / None nothing to do."""
        todo = [i for i in dict.fromkeys(ids) if i not in self.details and i.isdigit()]
        if not todo:
            return None
        if self.steam_down:          # don't wait for timeouts again in the same run
            return False
        chunks = [todo[i:i + 100] for i in range(0, len(todo), 100)]
        cached_fn = getattr(self.db, "cached_details", None)

        def one(chunk):
            try:
                cached = set(cached_fn(chunk, 3600)) if callable(cached_fn) else set()
            except Exception:  # noqa: BLE001
                cached = set()
            try:
                return chunk, cached, self.details_fn(chunk) or {}
            except Exception as e:  # noqa: BLE001 - network failures are expected here
                log.info("details lookup failed: %s", e)
                return chunk, cached, {}

        ok = False
        with ThreadPoolExecutor(max_workers=min(4, len(chunks))) as pool:
            for chunk, cached, got in pool.map(one, chunks):
                if not got:
                    continue
                ok = True
                fresh = any(w in got for w in chunk if w not in cached)   # Steam really answered for this chunk
                for wid in chunk:
                    d = got.get(wid)
                    if isinstance(d, dict):
                        self.details[wid] = d
                    elif fresh:                                          # omitted by Steam: removed/private
                        self.details[wid] = {"result": 9, "_missing": True}
        self.steam_down = not ok
        self.check()
        return ok

    def search(self, q: str, limit: int) -> list[DbMod]:
        try:
            rows = self.db.search(q, version=self.v, limit=limit, require_package_id=self.req.require_package_id)
            if not rows and len(q.split()) > 1:       # fallback: drop the last word
                rows = self.db.search(" ".join(q.split()[:-1]), version=self.v, limit=limit,
                                      require_package_id=self.req.require_package_id)
        except Exception as e:  # noqa: BLE001
            self.res.log.append(f"   search '{q}' failed: {e}")
            rows = []
        out = list(rows or [])
        # Include curated GitHub mods when the prompt/query asks for GitHub mods or on real DB searches with no match
        want_gh = bool(_GITHUB_INTENT_RE.search(self.req.prompt or "") or _GITHUB_INTENT_RE.search(q))
        if want_gh or (not out and hasattr(self.db, "_conn")):
            clean_q = re.sub(r"\b(?:github|gh|git|repo|mod|mods)\b", " ", q, flags=re.I).strip()
            toks = [t.lower() for t in (clean_q or q).split() if len(t) >= 2]
            seen_pids = {r.package_id for r in out if r.package_id}
            for gm in GITHUB_MODS:
                hay = f"{gm.name} {gm.steam_name} {gm.authors} {gm.package_id} {gm.workshop_id} {gm.description}".lower()
                if (not toks or all(_word_prefix(t, hay) for t in toks)) and (want_gh or gm.package_id not in seen_pids):
                    out.append(gm)
        return out[:limit]

    def conflict(self, row: DbMod, skip=()) -> str:
        """Why row can't join the current picks ('' = fine): duplicates, re-uploads, community incompatibilities."""
        pid, keys, rules = row.package_id, self.keys(row), self.rules()
        mine = rules.get(pid, {}).get("incompatible_with", {}) if pid else {}
        for p in self.picks:
            if p in skip:
                continue
            if p.workshop_id == row.workshop_id:
                return "duplicate pick"
            if pid and p.package_id == pid:
                return f"same packageId as '{p.name}'"
            prow = self.rows.get(p.workshop_id)
            if prow is not None and keys & self.keys(prow):
                return f"overlaps with '{p.name}' (another upload of the same mod)"
            if pid and p.package_id and (p.package_id in mine or pid in rules.get(p.package_id, {}).get("incompatible_with", {})):
                return f"incompatible with '{p.name}' (community rules)"
        return ""

    def add(self, row: DbMod, reason: str, source: str) -> Pick:
        self.rows[row.workshop_id] = row
        p = Pick(row.workshop_id, row.package_id, row.display_name, reason, source)
        self.picks.append(p)
        return p

    # -- live Steam Workshop ------------------------------------------------------------------
    def live_rows(self, items: list[dict]) -> dict[str, DbMod]:
        """DB rows for the RimWorld mods among Steam items. Keyed details (with required items) the DB lacks are
        stored first, so every later step - checks, dependencies, downloads, the next run - treats them like any
        other mod. Keyless details have no required items: those become rows for this run only, never saved."""
        mods = {str(d["publishedfileid"]): d for d in items if _steam_mod(d)}
        if not mods:
            return {}
        have: dict[str, DbMod] = {}
        try:
            have = self.db.get_many(list(mods))
            store = {w: d for w, d in mods.items() if w not in have and "num_children" in d}
            if store:
                self.db.store_items(store)
                self.live_new.update((w, str(d.get("title") or w)) for w, d in store.items())
                have = self.db.get_many(list(mods))
        except Exception as e:  # noqa: BLE001 - read-only/busy DB: Steam's data still serves this run
            log.info("storing live Workshop items failed: %s", e)
        for w, d in mods.items():
            if w not in have:
                have[w] = _steam_row(d)
                self.details.setdefault(w, d)
        return have

    def author_sample(self, query: str, words: list[str]) -> list[str]:
        """2-3 DB mods of the one author a query names (a single-author name matched by that name and not by the
        title, re-uploads skipped). If Steam says one account uploaded them all, that account's Workshop page is the
        author's. [] = not an author query."""
        rows = [r for r in self.search(query, 40) if r.authors and not re.search(r",|&|\band\b", r.authors, re.I)
                and all(_word_prefix(t, r.authors) for t in words)
                and not all(_word_prefix(t, f"{r.name} {r.steam_name}") for t in words)
                and not _NOISE_RE.search(f"{r.name} {r.steam_name}".lower())]
        names = [r.authors for r in rows]
        top = max(set(names), key=names.count) if names else ""
        sample = [r.workshop_id for r in rows if r.authors == top][:3]
        return sample if len(sample) >= 2 else []

    def live(self, queries: list[dict], per_query: list[list[DbMod]], avoid: list[str], dropped: dict) -> None:
        """Steam's live Workshop for what the DB lacks: each query's title matches, a few matches by description
        (ranked after the title matches in retrieve), and the Workshop page of an author a query names. Adds rows to
        per_query. Stops asking Steam once it is down, rate-limits, rejects the key, or the run is cancelled."""
        ws, stop = self.workshop, threading.Event()
        jobs = [(q["query"].lower().split(), self.author_sample(q["query"], q["query"].lower().split()))
                for q in queries]                            # DB work stays on this thread

        def one(i: int):
            words, sample = jobs[i]
            titles: list[dict] = []
            descs: list[dict] = []
            page: list[dict] = []
            if stop.is_set() or (self.cancel is not None and self.cancel.is_set()):
                return titles, descs, page, ""
            try:
                for d in ws.search(queries[i]["query"], self.v):
                    if all(_word_prefix(t, str(d.get("title") or "")) for t in words):
                        titles.append(d)
                    elif all(_word_prefix(t, str(d.get("short_description") or "")) for t in words):
                        descs.append(d)     # Steam's search is loose ("AntiAliasing" for Anti-Tank): real hits only
                descs = descs[:min(10, 3 * queries[i].get("want", 3))]  # best first: "Lahti L-39" for Anti-Tank
                if sample:
                    creators = {str(d.get("creator") or "") for d in ws.details(sample).values()}
                    if len(creators) == 1 and "" not in creators:      # one uploader for all of them
                        page = ws.author(creators.pop())
            except Exception as e:  # noqa: BLE001 - the database results stand
                if isinstance(e, OSError) and getattr(e, "code", 0) not in (400, 404):
                    stop.set()              # unreachable, timing out, rate-limited or the key rejected: stop asking
                return titles, descs, page, f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else type(e).__name__
            return titles, descs, page, ""

        self.say(None, f"   searching the live Steam Workshop too ({len(queries)} queries)…")
        with ThreadPoolExecutor(max_workers=4) as pool:
            found = list(pool.map(one, range(len(queries))))
        self.check()
        errors = sorted({err for *_, err in found if err})
        if errors:
            if not any(t or ds or p for t, ds, p, _ in found) and not any(e.startswith("HTTP") for e in errors):
                self.steam_down = True        # unreachable: enrich() must not wait for the same timeouts again
            self.say(None, "   ⚠ Steam rejected the Steam Web API key (check it in Settings); database results used"
                     if {"HTTP 401", "HTTP 403"} & set(errors) else
                     f"   ⚠ live Workshop search failed ({', '.join(errors)}); database results used")
        rows = self.live_rows([d for t, ds, p, _ in found for d in t + p + ds])
        for i, (titles, descs, page, _) in enumerate(found):
            seen = {r.workshop_id for r in per_query[i]}
            added = n_desc = 0
            for j, d in enumerate(titles + page + descs):
                r = rows.get(str(d.get("publishedfileid")))
                if r is None:
                    continue
                r = self.successor(r)
                why = self.unusable(r, avoid)
                if why or r.workshop_id in seen:
                    if why:
                        dropped[why] = dropped.get(why, 0) + 1
                    continue
                seen.add(r.workshop_id)
                self.rows.setdefault(r.workshop_id, r)
                per_query[i].append(r)
                added += 1
                if j >= len(titles) + len(page):
                    self.by_desc.add((i, r.workshop_id))
                    n_desc += 1
            if added:
                self.say(None, f"   “{queries[i]['query']}” → +{added} from the live Workshop"
                               + (f", {n_desc} by description" if n_desc else "")
                               + (" (the author's Workshop page)" if page else ""))
        if self.live_new:
            names = ", ".join(list(self.live_new.values())[:8]) + (" …" if len(self.live_new) > 8 else "")
            self.say(None, f"   {len(self.live_new)} mods the database didn't have yet (now saved in it): {names}")

    def _github_repo_row(self, repo: str) -> DbMod | None:
        """Resolve 'owner/repo' from GITHUB_BY_ID or live GitHub API when live_verify is enabled."""
        wid = f"gh:{repo.lower()}"
        if wid in GITHUB_BY_ID:
            return GITHUB_BY_ID[wid]
        if not self.req.live_verify:
            return None
        owner, _, name = repo.partition("/")
        if not owner or not name:
            return None
        try:
            raw = config.http_get(f"https://api.github.com/repos/{repo}", timeout=12,
                                  headers={"Accept": "application/vnd.github+json", "User-Agent": "RimModManager"})
            d = json.loads(raw.decode("utf-8", "replace"))
            if not isinstance(d, dict) or d.get("message") == "Not Found":
                return None
            title = str(d.get("name") or name)
            desc = str(d.get("description") or f"GitHub RimWorld mod repository ({repo})")
            stars = _int(d.get("stargazers_count"))
            author = str((d.get("owner") or {}).get("login") or owner)
            pid = f"{author.lower()}.{re.sub(r'[^a-z0-9]+', '', title.lower())}"
            return DbMod(wid, pid, title, f"{title} [GitHub]", author, ["1.5", "1.6", "1.7"], {},
                         subscriptions=max(5000, stars * 500), description=desc)
        except Exception as e:  # noqa: BLE001
            log.info("GitHub API lookup for %s failed: %s", repo, e)
            # Offline/rate-limited fallback for explicit user GitHub links so their link is still honored
            pid = f"{owner.lower()}.{re.sub(r'[^a-z0-9]+', '', name.lower())}"
            return DbMod(wid, pid, name, f"{name} [GitHub]", owner, ["1.5", "1.6", "1.7"], {},
                         subscriptions=10000, description=f"GitHub repository {repo}")

    def links(self) -> None:
        """Workshop links / ids and GitHub repo links in the request (collections expanded) join the list as they are -
        except what the request excludes ('… except <link>', 'without Combat Extended')."""
        if self.linked:
            return
        self.linked = True
        raw_prompt = self.req.prompt or ""
        gh_all = [m.group(1).lower() for m in _GH_URL_IN_TEXT_RE.finditer(raw_prompt)]
        text = _WID_URL_RE.sub(r" \1 ", raw_prompt)      # links -> bare ids, so negations see them too
        avoid, kept = _negations(text)
        gh_kept = list(dict.fromkeys(m.group(1).lower() for m in _GH_URL_IN_TEXT_RE.finditer(kept)))
        gh_excl = set(gh_all) - set(gh_kept)
        for repo in gh_kept:
            if repo in gh_excl:
                continue
            row = self._github_repo_row(repo)
            if row is None:
                self.reject(f"gh:{repo}", "your GitHub link: repository not found or live verification is off")
                continue
            self.rows[row.workshop_id] = row
            if any(_word_prefix(a, f"{row.name} {row.steam_name}") for a in avoid):
                self.reject(row.workshop_id, f"{row.display_name} (your GitHub link): your request excludes it")
                continue
            why = self.unusable(row, []) or self.conflict(row)
            if why:
                self.reject(row.workshop_id, f"{row.display_name} (your GitHub link): {why}")
                continue
            self.add(row, f"linked GitHub repo ({repo})", "link")
            self.say(None, f"   + {row.display_name} from GitHub ({repo})")
        ids = list(dict.fromkeys(_WID_RE.findall(kept)))
        excluded = set(_WID_RE.findall(text)) - set(ids)             # ids named only in excluded phrases
        if not ids:
            return
        if self.workshop is None:
            self.say(None, f"   ⚠ {len(ids)} workshop link(s) in your request skipped: live Steam Workshop is off")
            return
        self.say(None, f"   reading {len(ids)} workshop link(s) from your request…")
        try:
            kids = self.workshop.collections(ids)
        except Exception as e:  # noqa: BLE001
            kids = {}
            self.say(None, f"   ⚠ could not check your links for collections ({type(e).__name__})")
        flat = [w for w in dict.fromkeys(w for i in ids for w in kids.get(i, [i])) if w not in excluded]
        try:
            rows = self.db.get_many(flat)
        except Exception:  # noqa: BLE001
            rows = {}
        lack, failed = [w for w in flat if w not in rows], False
        if lack:
            try:
                rows.update(self.live_rows(list(self.workshop.details(lack).values())))
            except Exception as e:  # noqa: BLE001
                failed = True
                self.say(None, f"   ⚠ Steam details for your links unavailable ({type(e).__name__})")
        if self.enrich([w for w in flat if w in rows]) is False:
            self.say(None, "   Steam unreachable - your links are not checked for removed/banned mods.")
        n = 0
        for w in flat:
            row = rows.get(w)
            if row is None:
                self.reject(w, "your link: Steam could not be reached to check it" if failed else
                            "your link: not a RimWorld mod Steam serves (removed, private, a guide or collection "
                            "that could not be opened, or another game's item)")
                continue
            self.rows[w] = row
            if any(_word_prefix(a, f"{row.name} {row.steam_name}") for a in avoid):
                self.reject(w, f"{row.display_name} (your link): your request excludes it")
                continue
            why = self.unusable(row, []) or self.dead(w)
            if why == "wrong game version":            # a maintained replacement if one is known, else keep it
                alt = self.replacement(Pick(w, row.package_id, row.display_name, "", "link"))
                if alt:
                    if alt not in self.picks:
                        alt.reason = f"maintained replacement for your link '{row.display_name}'"
                        self.picks.append(alt)
                        n += 1
                    self.say(None, f"   ↻ {row.display_name} (your link) → {alt.name}")
                    continue
                self.say(None, f"   ⚠ {row.display_name} (your link) doesn't list RimWorld {self.v} support - kept "
                               "because you linked it")
                why = ""
            why = why or self.conflict(row)
            if why:
                self.reject(w, f"{row.display_name} (your link): {why}")
                continue
            self.add(row, "linked in your request", "link")
            n += 1
        if kids:
            self.say(None, f"   {len(kids)} collection(s) expanded")
        self.say(None, f"   + {n} mods from your links"
                       + (f" ({len(flat) - n} skipped, see rejected)" if n < len(flat) else ""))

    # -- AI path ------------------------------------------------------------------------------
    def ai(self, llm: LLM) -> None:
        tgt_desc = "AI-chosen count" if self.auto_count else f"target {self.target} mods"
        if self.req.no_lazy:
            tgt_desc += " · no-lazy mode"
        self.say(0.01, f"AI generator · {llm.provider} / {llm.model or 'default model'} · {tgt_desc}"
                       f" · RimWorld {self.v or 'any version'}")
        if self.db_empty():
            return
        self.links()
        tgt = self.target - len(self.picks)       # what the AI still has to fill (picks so far: from your links)
        if not self.auto_count and tgt <= 0:
            return self.finish("Mod list from the links in your request.")
        self.local = llm.provider == "openai_compat" and _is_local(llm.base_url)
        fatal = None
        self.say(0.03, "Step 1/7 · Planning searches with the AI…")
        try:
            queries, avoid, notes = self.plan(llm)
        except LLMError as e:
            if e.kind in _FATAL_KINDS:
                self.say(0.05, f"⚠ AI unavailable: {e}")
                return self.offline(note=f"AI unavailable ({e}). Used the offline generator instead.")
            self.say(0.05, f"⚠ Planning failed ({e}); using keyword/theme queries instead.")
            q3, avoid, _ = _offline_queries(self.req.prompt, self.req.include_popular_qol)
            queries = [{"query": q, "category": "keywords", "want": 1 if per else (5 if self.req.no_lazy else 3)}
                       for q, _, per in q3][:40 if self.req.no_lazy else 30]
            notes = ""
        if self.auto_count:
            planned_want = sum(q["want"] for q in queries)
            ai_tgt = max(self._auto_floor, planned_want)
            self.target = max(15, min(400, len(self.picks) + ai_tgt))
            tgt = max(10, self.target - len(self.picks))
            self.say(None, f"   AI decided target size: ~{self.target} mods (based on {len(queries)} planned searches)")
        self.res.queries = [q["query"] for q in queries]
        self.say(0.12, f"   {len(queries)} queries: " + " · ".join(self.res.queries)
                 + (f"   (avoid: {', '.join(avoid)})" if avoid else ""))

        cands, cat_of = self.retrieve(queries, avoid, 0.12, 0.35)
        if not cands:
            self.res.summary = ("No matching mods were found in the database for this request"
                                + (f" and RimWorld {self.v}." if self.v else ".") + " Try broader wording.")
            self.say(1.0, self.res.summary)
            return
        linked = {p.workshop_id for p in self.picks}
        cands = [r for r in cands if r.workshop_id not in linked]
        cand_ids = [r.workshop_id for r in cands]

        if self.auto_count:
            tgt = min(tgt, max(15, len(cands)))
            lo = max(1, min(len(cands), round(tgt * (0.85 if self.req.no_lazy else 0.75))))
            hi = max(lo, min(max(tgt, len(cands)), max(tgt + 30, round(tgt * 1.3))))
        else:
            lo, hi = max(1, round(tgt * 0.9)), max(1, round(tgt * 1.1))
        self.say(0.37, f"Step 3/7 · Asking the AI to choose {lo}-{hi} mods from {len(cands)} candidates…")
        summary = ""
        try:
            user = self.select_user(cands, cat_of, notes, queries, lo, hi, tgt)
            if self.picks:
                user += ("\n\nAlready in the list from the player's links (do not pick these again or mods that "
                         "overlap with them):\n" + ", ".join(p.name for p in self.picks))
            data = self.ask(llm, _select_system(lo, hi, tgt, self.req.no_lazy, self.auto_count), user,
                            _select_schema(cand_ids), min(64000, 12000 + 120 * hi))
            raw, bad, summary = _norm_picks(data)
        except LLMError as e:
            fatal = e
            raw, bad = [], []
            self.say(0.6, f"⚠ AI selection failed: {e}")

        self.say(0.62, f"Step 4/7 · Validating {len(raw)} picks against the database…")
        for item, why in bad:
            self.reject(item, why)
        self.validate(raw, set(cand_ids), hi)

        n_ai = sum(p.source == "ai" for p in self.picks)
        short_thresh = max(1, lo if self.auto_count else round(tgt * 0.95))
        if n_ai < short_thresh and fatal is None:     # well short of the target: top up
            refused = {w for w, _ in self.res.rejected}
            remaining = [r for r in cands if r.workshop_id not in refused and not self.conflict(r)]
            if remaining:
                k = max(1, tgt - n_ai)
                self.say(0.66, f"   only {n_ai} usable picks; asking the AI for {k} more from {len(remaining)} candidates…")
                try:
                    user = (self.select_user(remaining, cat_of, notes, queries, k, k + max(1, k // 5), k)
                            + "\n\nAlready chosen (do not repeat them or pick mods that overlap with them):\n"
                            + ", ".join(p.name for p in self.picks))
                    data = self.ask(llm, _select_system(k, k + max(1, k // 5), k, self.req.no_lazy, self.auto_count),
                                    user, _select_schema([r.workshop_id for r in remaining]), min(64000, 12000 + 120 * k))
                    raw2, bad2, _ = _norm_picks(data)
                    for item, why in bad2:
                        self.reject(item, why)
                    self.validate(raw2, {r.workshop_id for r in remaining}, hi)
                except LLMError as e:
                    self.say(0.68, f"   top-up request failed: {e}")
        n_ai = sum(p.source == "ai" for p in self.picks)
        if 0 < n_ai < short_thresh:    # the AI still stopped short: fill from its own search results
            refused = {w for w, _ in self.res.rejected}
            rest = [r for r in cands if r.workshop_id not in refused]
            self.say(0.68, f"   the AI stopped at {n_ai} picks; filling up to {self.target} from the remaining candidates…")
            self.rank(rest, {r.workshop_id: (1 + 2 * self.share.get(r.workshop_id, 0)) / (1 + 0.02 * i)
                             for i, r in enumerate(rest)}, "popular match from the AI's searches (fill)")
        if not any(p.source == "ai" for p in self.picks):
            self.say(0.68, "⚠ The AI produced no usable picks; ranking the candidates offline instead.")
            self.rank(cands, {r.workshop_id: 1.0 / (1 + 0.02 * i) for i, r in enumerate(cands)},
                      "offline pick (AI fallback)")
            summary = ""
        self.finish(summary or f"Mod list for: {self.req.prompt.strip()[:120]}")

    def plan(self, llm: LLM) -> tuple[list[dict], list[str], str]:
        dlcs = ", ".join(DLCS[p][1] for p in DLCS if p in self.owned) or "none (base game only)"
        v_txt = "1.7 (supports 1.6 mods)" if self.v == "1.7" else (self.v or "unknown (assume the latest)")
        if self.auto_count:
            size_line = (f"Target list size: YOU decide how many mods this playthrough needs (set each query's "
                         f"'want' from 1 to 25 so the sum of 'want' across your queries equals the ideal mod count "
                         f"for this request, at least ~{self._auto_floor} mods; dependencies are added on top "
                         "automatically).")
        else:
            size_line = (f"Target list size: about {self.target - len(self.picks)} mods (dependencies are added on top "
                         "automatically).")
        user = (f'Player request:\n"""\n{self.req.prompt.strip()}\n"""\n\n'
                f"Game version: {v_txt}\nDLCs owned: {dlcs}\n{size_line}")
        if self.picks:
            user += (f"\nAlready in the list from the player's links, on top of that target (no queries needed for "
                     f"these): " + ", ".join(p.name for p in self.picks[:60]) + (" …" if len(self.picks) > 60 else ""))
        if self.req.no_lazy:
            user += ("\nIMPORTANT: Do NOT be lazy or cut corners—try your absolute best, cover every angle of the "
                     "player's request thoroughly, and plan a complete, high-effort modlist.")
        system = (_PLAN_SYSTEM + (_PLAN_ESSENTIALS if self.req.include_popular_qol else "")
                  + (_PLAN_LIVE if getattr(self.workshop, "key", "") else "")
                  + (_PLAN_NO_LAZY if self.req.no_lazy else ""))
        max_q = 40 if self.req.no_lazy else 30
        queries, avoid, notes = _norm_plan(self.ask(llm, system, user, PLAN_SCHEMA, 16000), max_queries=max_q)
        if not queries:
            raise LLMError("the AI plan contained no usable search queries", "format")
        if self.req.include_popular_qol:           # safety net: every must-have and a few essentials the planner forgot
            have = {q["query"].lower() for q in queries}
            extra = [{"query": q, "category": "QoL must-haves" if q in _QOL_MUST else "QoL / performance essentials",
                      "want": 1} for q in _ESSENTIALS[:len(_QOL_MUST) + 3]
                     if q.lower() not in have and not any(_word_prefix(a, q) for a in avoid)]
            queries += extra            # on top of the planner's 30, so a full prompt keeps every search
        return queries, avoid, notes

    def retrieve(self, queries: list[dict], avoid: list[str], f0: float, f1: float) -> tuple[list[DbMod], dict]:
        self.say(f0, f"Step 2/7 · Searching the mod database ({len(queries)} queries)…")
        per_query: list[list[DbMod]] = []
        dropped: dict[str, int] = {}
        for i, q in enumerate(queries):
            kept, seen = [], set()
            for r in self.search(q["query"], 40):      # extra rows: translations get filtered out
                r = self.successor(r)
                why = self.unusable(r, avoid)
                if why or r.workshop_id in seen:
                    if why:
                        dropped[why] = dropped.get(why, 0) + 1
                    continue
                seen.add(r.workshop_id)
                self.rows.setdefault(r.workshop_id, r)
                kept.append(r)
            per_query.append(kept)
            self.say(f0 + (f1 - f0) * 0.6 * (i + 1) / len(queries), f"   “{q['query']}” → {len(kept)} matches")
        if getattr(self.workshop, "key", ""):
            self.live(queries, per_query, avoid, dropped)
        elif self.workshop is not None:
            self.say(None, "   (add a Steam Web API key in Settings so the AI also searches the live Steam Workshop)")
        self.packs([rows[0].workshop_id for rows in per_query if rows] + [p.workshop_id for p in self.picks])
        want = max(3, self.target // 10)
        finds = self.pack_finds(avoid, {r.workshop_id for rows in per_query for r in rows}, 3 * want)
        if finds:
            queries.append({"query": "(similar Workshop packs)", "category": "popular in similar Workshop packs",
                            "want": want})
            per_query.append(finds)
            self.say(None, f"   + {len(finds)} mods typical of those packs: "
                           + ", ".join(r.display_name for r in finds[:6]) + (" …" if len(finds) > 6 else ""))
        if dropped:
            self.say(None, "   skipped: " + ", ".join(f"{n} {why}" for why, n in sorted(dropped.items())))
        all_ids = list(dict.fromkeys(r.workshop_id for rows in per_query for r in rows))
        self.say(f0 + (f1 - f0) * 0.65, f"   fetching Steam popularity for {len(all_ids)} mods…")
        ok = self.enrich(all_ids)
        if ok is False:
            self.say(None, "   Steam details unavailable (offline?) - ranking by database order.")
        cap = 300 if self.target <= 150 else min(600, self.target * 2)
        if self.local:          # Ollama's default context is ~4k tokens and it cuts the start of longer prompts
            cap = min(cap, max(60, 2 * self.target))
        per_k = max(1, math.ceil(2 * self.target / max(1, len(queries))))
        taken: set[str] = set()
        lists: list[list[DbMod]] = []
        cat_of: dict[str, str] = {}
        for i, (q, rows) in enumerate(zip(queries, per_query)):
            rows = [r for r in rows if r.workshop_id not in taken and not self.dead(r.workshop_id)]
            # title matches first, description-only matches fill what is left; stable: relevance breaks ties
            rows.sort(key=lambda r: ((i, r.workshop_id) in self.by_desc, -self.subs(r.workshop_id)))
            rows = rows[: min(25, max(4, 3 * q["want"], per_k))]
            for r in rows:
                taken.add(r.workshop_id)
                cat_of[r.workshop_id] = q["category"]
            lists.append(rows)
        cands: list[DbMod] = []
        for rank in range(max((len(x) for x in lists), default=0)):     # round-robin: every query gets a share
            for rows in lists:
                if rank < len(rows) and len(cands) < cap:
                    cands.append(rows[rank])
        self.say(f1, f"   {len(cands)} candidate mods after filtering and de-duplication")
        return cands, cat_of

    def successor(self, row: DbMod) -> DbMod:
        """row's Use This Instead successor when it is usable for this game version, else row itself."""
        try:
            rule = self.db.replacement_for(row.workshop_id, row.package_id)
            new = str((rule or {}).get("newWorkshopId") or "").strip()
            nrow = self.db.get(new) if new.isdigit() and new != row.workshop_id else None
        except Exception as e:  # noqa: BLE001 - the old row is still a fine candidate
            log.info("replacement_for failed: %s", e)
            return row
        if nrow is None or self.unusable(nrow, []):
            return row
        self.uti_from.setdefault(nrow.workshop_id, row.display_name)
        return nrow

    def packs(self, seeds: list[str]) -> None:
        """Player-made Workshop collections like this request (its words + the seed mods): self.share = for each mod,
        the share of those packs that contain it. A database without collections (no Steam key yet) skips this."""
        words = [w for w in dict.fromkeys(re.findall(r"[^\W_]{3,}", _negations(_no_links(self.req.prompt))[1].lower()))
                 if w not in _STOPWORDS]
        try:
            cids = self.db.similar_packs(words, seeds)
            self.share = self.db.pack_votes(cids) if len(cids) >= 10 else {}
        except Exception as e:  # noqa: BLE001 - an optional signal
            log.info("similar packs unavailable: %s", e)
            cids, self.share = [], {}
        if self.share:
            self.say(None, f"   {len(cids)} similar player-made Workshop collections found")

    def pack_finds(self, avoid: list[str], have: set[str], n: int) -> list[DbMod]:
        """Up to n mods typical of the similar packs that the searches missed: in at least 10% of them and at least
        3x as common there as in all collections. Checked like every other row; libraries come as dependencies."""
        top = [w for w, s in sorted(self.share.items(), key=lambda x: -x[1]) if s >= 0.1 and w not in have][:300]
        try:
            base = self.db.pack_votes(None, top) if top else {}
            rows = self.db.get_many([w for w in top if self.share[w] >= 3 * base.get(w, 1.0)])
        except Exception as e:  # noqa: BLE001
            log.info("similar-pack finds unavailable: %s", e)
            return []
        out, seen = [], set(have) | {p.workshop_id for p in self.picks}
        for w in top:
            r = rows.get(w)
            if r is None:
                continue
            r = self.successor(r)
            if r.workshop_id in seen or self.unusable(r, avoid) or _LIBRARY_RE.search(f"{r.name} {r.steam_name}"):
                continue
            seen.add(r.workshop_id)
            self.rows.setdefault(r.workshop_id, r)
            out.append(r)
            if len(out) >= n:
                break
        return out

    def table_notes(self, cands: list[DbMod]) -> dict[str, str]:
        """Per candidate: last Steam update, Use This Instead origin, community-rule clashes with other candidates."""
        rules, by_pid = self.rules(), {r.package_id: r.workshop_id for r in cands if r.package_id}
        clash: dict[str, set[str]] = {}
        for pid, wid in by_pid.items():
            for other in (rules.get(pid) or {}).get("incompatible_with", {}):
                if other in by_pid and by_pid[other] != wid:
                    clash.setdefault(wid, set()).add(by_pid[other])
                    clash.setdefault(by_pid[other], set()).add(wid)
        out = {}
        for r in cands:
            wid = r.workshop_id
            ts = max(r.time_updated, _int(self.details.get(wid, {}).get("time_updated")))
            parts = [time.strftime("updated %Y-%m", time.gmtime(ts)) if ts else ""]
            if wid.startswith("gh:"):
                parts.append(f"GitHub repo ({wid[3:]})")
            if wid in self.uti_from:
                parts.append(f"maintained version of '{self.uti_from[wid][:40]}' (Use This Instead)")
            if wid in clash:
                parts.append("incompatible with " + ",".join(sorted(clash[wid])))
            if wid in self.req.installed_ids:
                parts.append("installed")
            if self.share.get(wid, 0) >= 0.05:
                parts.append(f"in {round(100 * self.share[wid])}% of similar packs")
            out[wid] = "; ".join(p for p in parts if p).replace("|", "/")
        return out

    def select_user(self, cands: list[DbMod], cat_of: dict, notes: str, queries: list[dict], lo: int, hi: int,
                    target: int) -> str:
        dlcs = ", ".join(DLCS[p][1] for p in DLCS if p in self.owned) or "none (base game only)"
        extra = self.table_notes(cands)
        cats: dict[str, int] = {}
        for q in queries:
            cats[q["category"]] = cats.get(q["category"], 0) + q["want"]
        order = {c: i for i, c in enumerate(cats)}
        rows = sorted(cands, key=lambda r: (order.get(cat_of.get(r.workshop_id, ""), 99), -self.subs(r.workshop_id)))
        lines = []
        for r in rows:
            wid = r.workshop_id
            desc = _clean_desc(r.description or self.details.get(wid, {}).get("description", ""), 80 if self.local else 400)
            title = (r.steam_name or r.name or wid).replace("|", "/")[:80]
            if r.name and r.steam_name and r.name.lower() not in r.steam_name.lower():
                title += f" ({r.name[:40]})"
            deps = sum(1 for d in r.dependencies if d not in DLC_BY_APPID and d != RIMWORLD_APPID)
            lines.append(f"{wid} | {title} | {(r.authors or '?').replace('|', '/')[:30]} | {_fmt_subs(self.subs(wid))}"
                         f" | {deps} | {cat_of.get(wid, '')} | {extra.get(wid, '')} | {desc}")
        # table first, request last: long data before the question, and a local server that cuts the start
        # of an over-long prompt then loses table rows, not the request
        v_txt = "1.7 (supports 1.6 mods)" if self.v == "1.7" else (self.v or "unknown")
        return (f"Candidate table ({len(lines)} mods; subs = Steam subscribers, ? = unknown):\n"
                "id | title | author | subs | deps | category | notes | description\n" + "\n".join(lines) + "\n\n"
                f'Player request:\n"""\n{self.req.prompt.strip()}\n"""\n\n'
                f"Game version: {v_txt} · DLCs owned: {dlcs}\n"
                + (f"Planner notes: {notes}\n" if notes else "")
                + ("The QoL must-haves are wanted.\n" if self.req.include_popular_qol else "")
                + ("NO BEING LAZY: Try your absolute best and deliver a complete, deeply curated modlist without "
                   "cutting corners or stopping early.\n" if self.req.no_lazy else "")
                + "Planned categories (wanted count): " + ", ".join(f"{c} ({n})" for c, n in cats.items()) + "\n"
                f"Pick {lo}-{hi} mods (target {target}) from the candidate table above.")

    def validate(self, raw: list[tuple[str, str]], offered: set[str], hi: int) -> None:
        """Keep only ids that were offered, exist in the DB and pass every check. Row data from the DB only."""
        ids: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for given, reason in raw:
            wid = _parse_wid(given)
            if not wid:
                self.reject(given, "malformed id (not a workshop id)")
            elif wid not in seen:
                seen.add(wid)
                ids.append((given, wid, reason))
        try:
            num_ids = [w for _, w, _ in ids if w.isdigit()]
            rows = dict(self.db.get_many(num_ids)) if num_ids else {}
        except Exception as e:  # noqa: BLE001
            log.info("get_many failed: %s", e)
            rows = {}
        for _, w, _ in ids:
            if w.startswith("gh:") and w not in rows:
                rows[w] = self.rows.get(w) or GITHUB_BY_ID.get(w)
        trimmed = 0
        for given, wid, reason in ids:
            row = rows.get(wid)
            if wid not in offered:
                if row is not None:
                    self.reject(given, f"not offered to the model (real mod '{row.display_name}', but not in the candidate table)")
                else:
                    self.reject(given, "unknown id - hallucinated (not in the mod database)")
                continue
            if row is None:
                self.reject(given, "unknown id (not in the mod database)")
                continue
            self.rows[wid] = row
            why = self.unusable(row, [])
            outdated = why == "wrong game version"      # step 7 tries a replacement for these
            why = ("" if outdated else why) or self.dead(wid) or self.conflict(row)
            if why:
                self.reject(given, why)
                continue
            if sum(p.source == "ai" for p in self.picks) >= hi:
                trimmed += 1
                continue
            if outdated:
                self.outdated.add(wid)
            self.add(row, reason.strip() or "chosen by the AI", "ai")
        if trimmed:
            self.say(None, f"   trimmed {trimmed} picks beyond the {hi}-mod limit")
        self.say(None, f"   {sum(p.source == 'ai' for p in self.picks)} picks valid, {len(self.res.rejected)} rejected so far")

    # -- offline path -------------------------------------------------------------------------
    def offline(self, note: str = "") -> None:
        self.say(0.01 if not note else None,
                 f"Offline generator (no AI) · target {self.target} mods · RimWorld {self.v or 'any version'}")
        if not note and self.db_empty():
            return
        self.links()
        queries, avoid, themes = _offline_queries(self.req.prompt, self.req.include_popular_qol)
        self.res.queries = [q for q, _, _ in queries]
        self.say(0.05, f"   themes: {', '.join(themes)} · {len(queries)} queries"
                 + (f" · avoiding: {', '.join(avoid)}" if avoid else ""))
        scores: dict[str, float] = {}
        essentials: dict[str, bool] = {}         # workshop id -> a QoL must-have
        for i, (q, weight, per) in enumerate(queries):
            rank = 0
            exact = " ".join(re.findall(r"[a-z0-9]+", q.lower())) if q in _ESSENTIALS else ""
            for r in self.search(q, 20):
                hay = f"{r.name} {r.steam_name} {r.authors} {r.package_id}"
                # the DB also matches inside words ('Knights' -> 'Arknights'); a theme word must start a word.
                # An essential means that mod (or a re-upload), not every add-on with 'Numbers' in its title.
                if self.unusable(r, avoid) or not all(_word_prefix(t, hay) for t in q.split()) \
                        or (exact and exact not in self.keys(r)):
                    continue
                self.rows.setdefault(r.workshop_id, r)
                scores[r.workshop_id] = scores.get(r.workshop_id, 0.0) + weight / (1 + 0.15 * rank)
                if per and q in _ESSENTIALS:
                    essentials[r.workshop_id] = q in _QOL_MUST
                rank += 1
                if per and rank >= per:
                    break
            self.say(0.05 + 0.4 * (i + 1) / len(queries), f"   “{q}” → {rank} matches")
        if not scores:
            self.res.summary = ((note + " ") if note else "") + "No matching mods were found in the database."
            self.say(1.0, self.res.summary)
            return
        self.packs([p.workshop_id for p in self.picks] + sorted(scores, key=lambda w: -scores[w])[:15])
        for w in scores:
            scores[w] *= 1 + 3 * self.share.get(w, 0)
        best = max(scores.values())
        for r in self.pack_finds(avoid, set(scores), max(10, self.target // 3)):
            scores[r.workshop_id] = best * self.share.get(r.workshop_id, 0)
        top = sorted(scores, key=lambda w: -scores[w])[: max(150, self.target * 4)]
        self.say(0.5, f"   ranking {len(top)} mods by popularity…")
        if self.enrich(top + list(essentials)) is False:
            self.say(None, "   Steam details unavailable - using cached popularity only.")
        # the "include QoL/performance essentials" option: every must-have, then a few reserved slots
        must = [w for w, m in essentials.items() if m]
        rest = [w for w, m in essentials.items() if not m]
        if must:
            self.rank([self.rows[w] for w in must], {w: 1.0 for w in must}, "QoL must-have", limit=len(must))
        if rest:
            self.rank([self.rows[w] for w in rest], {w: 1.0 for w in rest}, "popular QoL / performance essential",
                      limit=min(len(rest), max(1, self.target // 6)))
        self.rank([self.rows[w] for w in top], scores, "offline pick")
        themes_txt = ", ".join(themes)
        self.finish(((note + " ") if note else "")
                    + f"Offline selection for '{self.req.prompt.strip()[:80]}' (themes: {themes_txt}), ranked by popularity.")

    def rank(self, rows: list[DbMod], relevance: dict[str, float], reason: str, limit: int = 0) -> None:
        """Deterministic pick: relevance x popularity, libraries/patches demoted, overlaps skipped."""
        def score(r: DbMod) -> float:
            title = f"{r.name} {r.steam_name}"
            s = relevance.get(r.workshop_id, 0.1) * (1 + math.log10(self.subs(r.workshop_id) + 10))
            if _LIBRARY_RE.search(title):
                s *= 0.25
            if _PATCH_RE.search(title):
                s *= 0.5
            return s
        skipped, stop = 0, min(self.target, len(self.picks) + limit) if limit else self.target
        cap = max(2, self.target // 8)             # at most this many from one series while alternatives exist
        fams: dict[str, int] = {}
        for p in self.picks:
            if p.workshop_id in self.rows:
                f = _family(self.rows[p.workshop_id])
                fams[f] = fams.get(f, 0) + 1
        def orphan_patch(r: DbMod) -> bool:     # a patch/fix for a mod that is not in the list
            if not _PATCH_RE.search(f"{r.name} {r.steam_name}"):
                return False
            have = {p.workshop_id for p in self.picks} | {HARMONY_WORKSHOP_ID, RIMWORLD_APPID, *DLC_BY_APPID}
            if not set(r.dependencies) <= have:
                return True
            # 'Gloomy x Replace Stuff Fix' often lists no required items: every named mod must be picked
            sides = re.split(r"\s+(?:x|\+|&)\s+", _PATCH_RE.sub(" ", r.steam_name or r.name), flags=re.I)
            picked = {k for p in self.picks if p.workshop_id in self.rows for k in self.keys(self.rows[p.workshop_id])}
            return len(sides) > 1 and any(not _title_keys(DbMod("", name=s)) & picked for s in sides)

        deferred = []
        for r in sorted(rows, key=score, reverse=True):
            if len(self.picks) >= stop:
                break
            # ponytail: fixed 1000-subscriber floor for padding; scale it to the best match if niche requests come up short
            obscure = _int(self.details.get(r.workshop_id, {}).get("subscriptions", 1000)) < 1000
            if self.dead(r.workshop_id) or self.conflict(r) or self.unusable(r, []) or orphan_patch(r) or obscure:
                skipped += 1
                continue
            f = _family(r)
            if fams.get(f, 0) >= cap:
                deferred.append(r)
                continue
            fams[f] = fams.get(f, 0) + 1
            self.add(r, reason, "offline")
        for r in deferred:
            if len(self.picks) >= stop:
                break
            if not self.conflict(r):
                self.add(r, reason, "offline")
        self.say(0.6, f"   {reason}: {len(self.picks)} mods selected ({skipped} skipped as duplicates/incompatible/unavailable/obscure)")

    # -- shared tail: verify -> replacements -> dependencies -----------------------------------
    def finish(self, summary: str) -> None:
        v = self.v
        if self.req.live_verify and self.picks:
            self.say(0.7, f"Step 5/7 · Verifying {len(self.picks)} mods on Steam…")
            ok = self.enrich([p.workshop_id for p in self.picks])
            if ok is False:
                self.say(None, "   Steam unreachable - live verification skipped.")
            else:
                for p in list(self.picks):
                    why = self.dead(p.workshop_id)
                    if why:
                        self.picks.remove(p)
                        self.reject(p.workshop_id, f"{p.name}: {why}")
                        self.say(None, f"   ✖ {p.name}: {why}")

        if self.outdated:
            self.say(0.78, f"Step 6/7 · Looking for replacements of {len(self.outdated)} mods without RimWorld {v} support…")
            for p in list(self.picks):
                if p.workshop_id not in self.outdated:
                    continue
                i = self.picks.index(p)
                self.picks.remove(p)
                new = self.replacement(p)
                if new:
                    if new not in self.picks:
                        self.picks.insert(i, new)
                    self.say(None, f"   ↻ {p.name} → {new.name}")
                else:
                    self.reject(p.workshop_id, f"{p.name}: does not support RimWorld {v} and no replacement is known")

        self.say(0.85, "Step 7/7 · Adding dependencies…")
        self.dependencies()

        n = {s: sum(p.source == s for p in self.picks) for s in ("ai", "offline", "dependency", "replacement", "link")}
        parts = [f"{n['link']} from your links" if n["link"] else "",
                 f"{n['ai']} chosen by AI" if n["ai"] else "", f"{n['offline']} ranked offline" if n["offline"] else "",
                 f"{n['replacement']} replacements" if n["replacement"] else "",
                 f"{n['dependency']} dependencies" if n["dependency"] else ""]
        stats = f"{len(self.picks)} mods ({', '.join(x for x in parts if x) or 'none'}), {len(self.res.rejected)} rejected."
        sizes = [_int(self.details.get(p.workshop_id, {}).get("file_size")) for p in self.picks]
        if any(sizes):
            unknown = sizes.count(0)
            stats += f" Download ≈ {sum(sizes) / 1e6:,.0f} MB" + (f" (+{unknown} of unknown size)." if unknown else ".")
        self.res.summary = f"{summary.strip()}\n\n{stats}" if summary.strip() else stats
        for wid, why in self.res.rejected[:40]:
            self.res.log.append(f"   rejected {wid}: {why}")
        if len(self.res.rejected) > 40:
            self.res.log.append(f"   … and {len(self.res.rejected) - 40} more rejected")
        self.say(1.0, "Done · " + stats)

    def replacement(self, p: Pick) -> Pick | None:
        """Use-This-Instead successor supporting the game version, from DB data only."""
        try:
            rule = self.db.replacement_for(workshop_id=p.workshop_id, package_id=p.package_id)
        except Exception as e:  # noqa: BLE001
            log.info("replacement_for failed: %s", e)
            return None
        if not rule:
            return None
        new = str(rule.get("newWorkshopId") or "").strip()
        existing = next((x for x in self.picks if x.workshop_id == new), None)
        if existing is not None:
            return existing                      # successor already in the list: just drop the old one
        if not new.isdigit() or new == p.workshop_id or new in self.exclude:
            return None
        try:
            row = self.db.get(new)
        except Exception:  # noqa: BLE001
            row = None
        if row is not None:
            if row.unpublished or not _supports(row, self.v) or self.conflict(row) or self.missing_dlcs(row):
                return None
            self.rows[new] = row
            pid, name = row.package_id, row.display_name
        else:
            versions = [str(x) for x in rule.get("newVersions") or []]
            if self.v and versions and not any(v in versions for v in compat_versions(self.v)):
                return None
            pid, name = str(rule.get("newPackageId") or "").lower(), str(rule.get("newName") or new)
        if self.req.live_verify and self.enrich([new]) is not False and self.dead(new):
            return None
        return Pick(new, pid, name, f"maintained replacement for '{p.name}'", "replacement")

    def dependencies(self) -> None:
        if not self.picks:
            return
        added: list[Pick] = []
        seen: set[str] = set()      # handled in an earlier pass (added, skipped or replaced): never again
        for _ in range(5):          # repeat while picks grow: a replacement's own dependencies were not walked yet
            n = len(self.picks)
            self._dependencies_pass(added, seen)
            if len(self.picks) == n:
                break
        if added:
            self.say(None, f"   + {len(added)} dependencies: " + ", ".join(p.name for p in added[:12])
                     + (" …" if len(added) > 12 else ""))
        if self.req.live_verify and added and self.enrich([p.workshop_id for p in added]):
            for p in added:
                why = self.dead(p.workshop_id)
                if why:
                    self.say(None, f"   ⚠ dependency {p.name}: {why} - mods needing it may fail")
        for p in list(self.picks):
            if p not in self.picks:
                continue
            row = self.rows.get(p.workshop_id)
            need = self.missing_dlcs(row) if row else []
            if need:
                self.say(None, f"   ⚠ {p.name} requires DLC {', '.join(need)}, which isn't installed")
                if p.source != "link":
                    drop = {p.workshop_id}
                    for q in list(self.picks):
                        qrow = self.rows.get(q.workshop_id)
                        if q.source != "link" and (q.workshop_id in drop or (qrow and set(qrow.dependencies) & drop)):
                            self.picks.remove(q)
                            self.reject(q.workshop_id, f"{q.name}: requires DLC {', '.join(need)}")

    def _dependencies_pass(self, added: list[Pick], seen: set[str]) -> None:
        try:     # Steam 'required items', one level at a time, never through met/variant/superseded ones
            closure = [r.workshop_id for r in download_closure(   # no version: outdated ones go through replacement()
                [ModRef(p.package_id, p.name, p.workshop_id) for p in self.picks], {}, self.db)]
        except Exception as e:  # noqa: BLE001
            self.say(None, f"   ⚠ dependency lookup failed: {e}")
            closure = []
        have = {p.workshop_id for p in self.picks} | seen
        new = [w for w in dict.fromkeys(closure) if w not in have and w.isdigit()
               and w not in DLC_BY_APPID and w != RIMWORLD_APPID]
        seen.update(new)
        try:
            rows = self.db.get_many(new) if new else {}
        except Exception:  # noqa: BLE001
            rows = {}
        lack = [w for w in new if w not in rows]
        if lack and self.workshop is not None:
            try:
                rows.update(self.live_rows(list(self.workshop.details(lack).values())))
            except Exception as e:  # noqa: BLE001 - offline: they are added by id below
                log.info("live details for dependencies failed: %s", type(e).__name__)
        for w in new:
            row = rows.get(w)
            needers = [p for p in self.picks if w in getattr(self.rows.get(p.workshop_id), "dependencies", {})]
            by = ", ".join(p.name for p in needers[:3]) + (" …" if len(needers) > 3 else "")
            if w in self.exclude:
                self.say(None, f"   ⚠ {by or 'a pick'} needs {w}, which you excluded - it may not work")
                continue
            if row is None:
                name = next((self.rows[p.workshop_id].dependencies[w] for p in needers), "") or w
                added.append(Pick(w, "", name, f"required by {by}" if by else "required dependency", "dependency"))
                self.picks.append(added[-1])
                self.say(None, f"   + {name} (dependency, not in the database)")
                continue
            self.rows[w] = row
            same = self.conflict(row, skip=needers)   # a mod never "is" its own dependency, whatever the titles say
            if same.startswith(("same packageId", "overlaps")):
                self.say(None, f"   = dependency {row.display_name} ({w}) is covered by the list: {same}")
                continue
            if not _supports(row, self.v) or row.unpublished:
                alt = self.replacement(Pick(w, row.package_id, row.display_name, "", "dependency"))
                if alt:
                    if alt not in self.picks:
                        alt.reason = f"replaces outdated dependency '{row.display_name}'"
                        added.append(alt)
                        self.picks.append(alt)
                    self.say(None, f"   ↻ dependency {row.display_name} → {alt.name}")
                    continue
                self.say(None, f"   ⚠ dependency {row.display_name} does not list RimWorld {self.v} support")
            added.append(self.add(row, f"required by {by}" if by else "required dependency", "dependency"))


# ---------------------------------------------------------------------------------------------
# Self-tests
# ---------------------------------------------------------------------------------------------

def _check_schema(s: dict, path: str = "$") -> None:
    if s.get("type") == "object":
        assert s.get("additionalProperties") is False, path
        assert sorted(s.get("required", [])) == sorted(s.get("properties", {})), path
        for k, sub in s["properties"].items():
            _check_schema(sub, f"{path}.{k}")
    if s.get("type") == "array":
        _check_schema(s["items"], path + "[]")


class _FakeDB:
    def __init__(self, rows: list[DbMod], rules: dict | None = None, uti: dict | None = None):
        self.rows = {r.workshop_id: r for r in rows}
        self._rules, self._uti = rules or {}, uti or {}

    def search(self, query, version="", limit=50, offset=0, require_package_id=False, sort="relevance"):
        toks = query.lower().split()
        out = []
        for r in self.rows.values():
            hay = f"{r.name} {r.steam_name} {r.authors} {r.package_id}".lower()
            words = re.findall(r"[a-z0-9']+", hay)
            if all(any(w.startswith(t) for w in words) for t in toks) and not r.unpublished and not r.is_dlc \
                    and (not version or any(v in r.game_versions for v in compat_versions(version))) \
                    and (r.package_id or not require_package_id):
                out.append(r)
        return out[offset:offset + limit]

    def get(self, wid):
        return self.rows.get(str(wid))

    def get_many(self, ids):
        return {i: self.rows[i] for i in ids if i in self.rows}

    def replacement_for(self, workshop_id="", package_id=""):
        return self._uti.get(workshop_id)

    def rules(self):
        return self._rules

    def store_items(self, items, authors=None):        # like ModDatabase.store_items, for the live Workshop
        new = 0
        for w, d in items.items():
            tags, old = [t["tag"] for t in d.get("tags") or []], self.rows.get(w)
            new += old is None
            self.rows[w] = DbMod(w, old.package_id if old else "", old.name if old else "", d.get("title", ""),
                                 (old.authors if old else "") or (authors or {}).get(w, ""),
                                 [t for t in tags if t[:1].isdigit()],
                                 {str(c["publishedfileid"]): "" for c in d.get("children") or []},
                                 kind="translation" if "Translation" in tags else "",
                                 subscriptions=int(d.get("subscriptions") or 0))
        return new


class _FakeLLM:
    provider, model, last_usage = "fake", "fake-1", ""

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def complete_json(self, system, user, schema, max_tokens=16000):
        self.calls.append((system, user, schema))
        r = self.replies.pop(0) if self.replies else {}
        if isinstance(r, Exception):
            raise r
        return r


def _fake_world():
    R = lambda wid, name, pid, vers=("1.6",), deps=None, **kw: DbMod(  # noqa: E731
        wid, pid, name, name, kw.pop("authors", "Someone"), list(vers), deps or {}, **kw)
    rows = [
        R("2009463077", "Harmony", "brrainz.harmony", ("1.5", "1.6")),
        R("2023507013", "Vanilla Expanded Framework", "oskarpotocki.vanillafactionsexpanded.core",
          deps={"2009463077": "Harmony"}),
        R("1001", "RimHUD", "jaxe.rimhud", deps={"2009463077": "Harmony"}, subscriptions=400000),
        R("1002", "Medieval Overhaul", "dankpyon.medieval.overhaul",
          deps={"2009463077": "Harmony", "2023507013": "Vanilla Expanded Framework"}, subscriptions=150000),
        R("1003", "Medieval Times", "medieval.times", subscriptions=50000),
        R("1004", "Allow Tool", "unlimitedhugs.allowtool", subscriptions=300000),
        R("1005", "Allow Tool (Continued)", "mlie.allowtool", subscriptions=1000),
        R("1006", "Combat Extended", "ceteam.combatextended", subscriptions=500000),
        R("1007", "Yayo's Combat 3", "com.yayo.combat3", subscriptions=200000),
        R("1008", "Old Medieval Walls", "old.walls", ("1.4",)),
        R("1009", "New Medieval Walls", "new.walls"),
        R("1010", "Medieval Royal Court", "royal.court", deps={"1149640": "RimWorld - Royalty"}),
        R("1011", "RimHUD 汉化", "cn.rimhud"),
        R("1012", "Medieval Gone Mod", "gone.mod", subscriptions=5000),
        R("1013", "Medieval Banned Mod", "banned.mod"),
        R("1014", "Medieval Secret Unpublished", "unpub.mod", unpublished=True),
        R("1015", "Unrelated Space Mod", "space.mod"),
        R("1016", "Medieval Gate", "medieval.gate", deps={"1008": "Old Medieval Walls"}),
        R("1017", "Medieval Tongue", "tongue.mod", kind="translation", subscriptions=90000),   # Steam-tagged only
        R("294100", "Core", "ludeon.rimworld", is_dlc=True),
    ]
    rules = {"ceteam.combatextended": {"incompatible_with": {"com.yayo.combat3": "conflicts"}}}
    uti = {"1008": {"oldWorkshopId": "1008", "newWorkshopId": "1009", "newName": "New Medieval Walls",
                    "newPackageId": "new.walls", "newVersions": ["1.6"]}}
    db = _FakeDB(rows, rules, uti)

    def details(ids):
        out = {}
        for i in ids:
            if i == "1012":
                continue                                    # removed from Steam: omitted like steam.get_details does
            r = db.rows.get(i)
            out[i] = {"publishedfileid": i, "result": 1, "consumer_app_id": 294100,
                      "banned": 1 if i == "1013" else 0, "subscriptions": r.subscriptions if r else 0,
                      "description": f"[b]Description[/b] of {r.name if r else i} https://x.y/z"}
        return out
    return db, details


def _selftest() -> None:
    # JSON extraction
    assert _extract_json('```json\n{"a": 1}\n```\nHope this helps!') == {"a": 1}
    assert _extract_json('Sure! Here it is: {"a": {"b": "}"}, "c": [1,2]} trailing {"x":1}') == {"a": {"b": "}"}, "c": [1, 2]}
    assert _extract_json('<think>maybe {"no": 1}</think>{"yes": true}') == {"yes": True}
    assert _extract_json('{"picks": [{"id": "1",},],}') == {"picks": [{"id": "1"}]}
    assert _extract_json('[{"id": "1"}]') == {"items": [{"id": "1"}]}
    for bad in ("", "no json here", '{"a": '):
        try:
            _extract_json(bad)
        except LLMError as e:
            assert e.kind == "format"
        else:
            raise AssertionError(bad)
    assert _parse_wid("https://steamcommunity.com/sharedfiles/filedetails/?id=2009463077") == "2009463077"
    assert _parse_wid(" 1001 ") == "1001" and _parse_wid("abc") == "" and _parse_wid("12") == ""
    _check_schema(PLAN_SCHEMA)
    _check_schema(_select_schema(["1", "2"]))
    assert "enum" not in _select_schema([str(i) for i in range(401)])["properties"]["picks"]["items"]["properties"]["id"]
    assert set(CLAUDE_MODELS) == {"claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5", "claude-opus-5-5", "claude-fable-5-1"}
    assert PRESETS["Offline (no AI)"]["provider"] == "offline" and PRESETS["Ollama (local)"]["needs_key"] is False
    q, avoid, themes = _offline_queries("A medieval colony with better medicine, no guns and no Combat Extended", True)
    assert "medieval" in themes and "medical" in themes and "combat extended" not in themes
    assert any(a.lower() == "guns" for a in avoid) and any(a.lower() == "combat extended" for a in avoid)
    assert not any(x.lower().startswith("combat extended") for x, _, _ in q)
    # the shipped 'Medieval' example: every negated item is excluded, no sci-fi/gun/VE queries, no filler words
    q, avoid, themes = _offline_queries("A medieval kingdom playthrough: castles, knights, swords & crossbows, feudal "
                                        "factions, no guns or high-tech. Keep it lore-friendly and stable.", True)
    assert {"guns", "high-tech"} <= {a.lower() for a in avoid} and themes == ["medieval"], (avoid, themes)
    assert not any(w in x.lower() for x, _, _ in q for w in ("weapons", "high", "keep", "stable", "lore")), q
    assert _negations("no raids and more animals")[0] == ["raids"]
    assert _negations("no guns, lasers or robots")[0][:3] == ["guns", "lasers", "robots"]
    assert _negations("medieval, no guns, lots of castles")[0][0] == "guns"
    avoid, positive = _negations("nothing that changes the balance, better UI")
    assert not avoid and "balance" not in positive and "better UI" in positive
    # a dev-branch mirror (no packageId in the DB) shares the base mod's title key -> one CE only
    assert _title_keys(DbMod("1", "", "Combat Extended", "Combat Extended")) & \
        _title_keys(DbMod("2", "", "", "Combat Extended - Dev Snapshot"))
    for t in ("Dubs Mint Menus en Español", "RimHUD - German-Deutsch", "RimHUD PortugueseBrazilian", "WeaponStats[1.6]ua",
              "Smart Speed - Türkçe Çeviri", "Spacer Shields (Russian Language)", "Megafauna zh Supplement",
              "Achtung UA Language", "Minerals Sparkle_zh 水晶矿", "RimHUD - Turkish Patch", "Hunting Squad-繁中漢化"):
        assert _TRANSLATION_RE.search(t), t
    for t in ("Russian Weapons Pack", "German Shepherd", "Plant Speed Modifier [EN/ES/RU/ZH/JP/DE/KO/FR]", "Zombieland"):
        assert not _TRANSLATION_RE.search(t), t
    assert '"enum"' not in _json_hint(_select_schema(["1", "2"]))
    cut = '{"picks":[{"id":"1","reason":"a}b"},{"id":"2","rea'
    assert _extract_cut_json(cut) == {"picks": [{"id": "1", "reason": "a}b"}]}

    db, details = _fake_world()
    valid_ids = set(db.rows)

    def assert_trusted(res: GenResult) -> None:
        for p in res.picks:
            r = db.rows.get(p.workshop_id)
            assert r is not None, p
            if p.source != "replacement":
                assert (p.name, p.package_id) == (r.display_name, r.package_id), p
        assert len({p.workshop_id for p in res.picks}) == len(res.picks)

    plan = {"queries": [{"query": "Medieval", "category": "medieval", "want": 4},
                        {"query": "RimHUD", "category": "qol", "want": 1},
                        {"query": "Allow Tool", "category": "qol", "want": 1},
                        {"query": "Combat", "category": "combat", "want": 2},
                        {"query": "Medieval Walls", "category": "building", "want": 1}],
            "avoid": [], "notes": "keep it medieval"}
    select = {"summary": "A medieval list.", "picks": [
        {"id": "1002", "reason": "core medieval overhaul"},          # (a) valid
        {"id": "1001", "reason": "hud"},                             # (a) valid
        {"id": "999999999", "reason": "made up"},                    # (b) hallucinated
        {"id": "1015", "reason": "real mod but never offered"},      # (c) not in candidate set
        {"id": "Harmony", "reason": "name instead of id"},           # (d) malformed id
        {"reason": "no id at all"}, 42.5,                            # (d) malformed items
        {"id": "1004"}, {"id": "1005"},                              # overlap: re-upload
        {"id": "1006"}, {"id": "1007"},                              # community-rules incompatibility
        {"id": "1012"}, {"id": "1013"},                              # removed / banned on Steam
        {"id": "1011"}, {"id": "1010"},                              # translation / unowned DLC: never offered
        {"id": "1002"},                                              # duplicate
        {"id": "1016", "reason": "gate"},                            # its dependency 1008 is outdated
    ]}
    llm = _FakeLLM([plan, select])
    req = GenRequest("medieval colony", "1.6", target_count=5, owned_dlcs=set())
    res = generate(req, db, llm, details_fn=details)            # type: ignore[arg-type]
    assert_trusted(res)
    ids = [p.workshop_id for p in res.picks]
    rej = dict(res.rejected)
    enum = set(llm.calls[1][2]["properties"]["picks"]["items"]["properties"]["id"]["enum"])
    _check_schema(llm.calls[1][2])
    user = llm.calls[1][1]      # request after the table: a truncating local server cuts table rows, not the request
    assert user.index("Candidate table") < user.index("Player request") < user.index("Pick ")
    assert enum <= valid_ids and not enum & {"1010", "1011", "1014", "1015", "294100", "1008", "1012", "1013", "1017"}, enum
    assert "1002" in ids and "1001" in ids and "1004" in ids and "1006" in ids
    assert "999999999" in rej and "hallucinated" in rej["999999999"]
    assert "not offered" in rej["1015"] and "Harmony" in rej and "malformed" in rej["Harmony"]
    assert "overlaps" in rej["1005"] and "incompatible" in rej["1007"]
    assert "1012" not in ids and "1013" not in ids and "1011" not in ids and "1010" not in ids
    deps = {p.workshop_id for p in res.picks if p.source == "dependency"}
    assert deps == {"2009463077", "2023507013"}, deps
    assert ("1009", "replacement") in [(p.workshop_id, p.source) for p in res.picks] and "1008" not in ids
    assert res.queries[:5] == [q["query"] for q in plan["queries"]] and res.summary.startswith("A medieval list.")
    assert set(_QOL_MUST) <= set(res.queries) and "QoL must-haves are wanted" in user, res.queries
    plan30 = {"queries": [{"query": f"Medieval {i}", "category": "medieval", "want": 1} for i in range(30)]}
    res = generate(req, db, _FakeLLM([plan30, select]), details_fn=details)     # type: ignore[arg-type]
    assert res.queries[:30] == [q["query"] for q in plan30["queries"]] and set(_QOL_MUST) <= set(res.queries)

    # too few picks -> one top-up request over the remaining candidates (already chosen ones not offered again)
    llm = _FakeLLM([plan, {"picks": [{"id": "1002"}], "summary": ""},
                    {"picks": [{"id": "1001"}, {"id": "1004"}, {"id": "1003"}], "summary": ""}])
    res = generate(GenRequest("medieval", "1.6", target_count=5), db, llm, details_fn=details)  # type: ignore[arg-type]
    assert [p.workshop_id for p in res.picks if p.source == "ai"] == ["1002", "1001", "1004", "1003"], res.picks
    assert "1002" not in llm.calls[2][2]["properties"]["picks"]["items"]["properties"]["id"]["enum"]
    # still short after the top-up -> filled offline from the remaining candidates
    llm = _FakeLLM([plan, {"picks": [{"id": "1002"}], "summary": ""}, {"picks": [{"id": "1001"}], "summary": ""}])
    res = generate(GenRequest("medieval", "1.6", target_count=5), db, llm, details_fn=details)  # type: ignore[arg-type]
    assert [p.workshop_id for p in res.picks if p.source == "ai"] == ["1002", "1001"], res.picks
    assert any(p.source == "offline" for p in res.picks), res.picks
    assert_trusted(res)
    # the AI is offered Use This Instead successors instead of superseded uploads, and sees clashes + update dates
    R3 = lambda wid, name, pid, **kw: DbMod(wid, pid, name, name, "A", ["1.6"], {}, **kw)  # noqa: E731
    db3 = _FakeDB([R3("2001", "Old Guns", "old.guns"), R3("2002", "Guns Reloaded", "new.guns", time_updated=1767225600),
                   R3("2003", "Gun Rival", "rival.guns")],
                  {"new.guns": {"incompatible_with": {"rival.guns": ""}}}, {"2001": {"newWorkshopId": "2002"}})
    run = _Run(GenRequest("guns", "1.6", 5), db3, noop_progress, None, lambda ids: {})
    q3 = [{"query": "Gun", "category": "guns", "want": 3}]
    cands, cat_of = run.retrieve(q3, [], 0, 1)
    assert [r.workshop_id for r in cands] == ["2002", "2003"], cands
    table = run.select_user(cands, cat_of, "", q3, 1, 2, 2)
    assert "maintained version of 'Old Guns'" in table and "updated 2026-01" in table, table
    assert "incompatible with 2003" in table and "incompatible with 2002" in table, table
    # RocketMan (2479389928) and EdB Prepare Carefully (735106432) are blocked as outdated/broken
    assert "2479389928" in _Run._BROKEN_WORKSHOP_IDS and "735106432" in _Run._BROKEN_WORKSHOP_IDS
    assert run.unusable(R3("2479389928", "RocketMan", "krkr.rocketman"), []) == "known broken or conflicting upload"
    # GitHub mod IDs and repo links work in _parse_wid, links(), search(), and validate()
    assert _parse_wid("https://github.com/Dubwise56/Rimefeller") == "gh:dubwise56/rimefeller"
    assert _parse_wid("gh:Taranchuk/PerformanceOptimizer") == "gh:taranchuk/performanceoptimizer"
    run_gh = _Run(GenRequest("github mods + https://github.com/Dubwise56/Rimatomics", "1.7", 3), db3, noop_progress, None, lambda ids: {})
    run_gh.links()
    assert any(p.workshop_id == "gh:dubwise56/rimatomics" and p.source == "link" for p in run_gh.picks), run_gh.picks
    gh_hits = run_gh.search("Rimefeller github", 10)
    assert any(r.workshop_id == "gh:dubwise56/rimefeller" for r in gh_hits), gh_hits
    # an outdated pick is swapped for its Use-This-Instead successor
    run = _Run(GenRequest("walls", "1.6", target_count=1), db, noop_progress, None, details)
    run.rows["1008"] = db.rows["1008"]
    run.outdated.add("1008")
    run.picks.append(Pick("1008", "old.walls", "Old Medieval Walls", "", "ai"))
    run.finish("")
    assert [(p.workshop_id, p.source) for p in run.picks] == [("1009", "replacement")], run.picks
    # an outdated dependency's replacement brings its own dependencies
    import dataclasses
    db2 = _FakeDB([*db.rows.values(), DbMod("1018", "walls.core", "Walls Core", "Walls Core", "", ["1.6"])], uti=db._uti)
    db2.rows["1009"] = dataclasses.replace(db.rows["1009"], dependencies={"1018": "Walls Core"})
    run = _Run(GenRequest("gate", "1.6", live_verify=False), db2, noop_progress, None, details)
    run.add(db2.rows["1016"], "gate", "ai")
    run.dependencies()
    assert ("1018", "dependency") in [(p.workshop_id, p.source) for p in run.picks], run.picks
    # offline ranking skips a patch whose base mod is not in the list, keeps one whose base mod is
    run = _Run(GenRequest("hud", "1.6", target_count=5), db, noop_progress, None, details)
    orphan = DbMod("1101", "a.patch", "Space Mod Patch", "Space Mod Patch", "", ["1.6"], {"1015": "Unrelated Space Mod"})
    fine = DbMod("1102", "b.patch", "RimHUD Patch", "RimHUD Patch", "", ["1.6"], {"1001": "RimHUD"})
    cross = DbMod("1103", "", "Gloomy x RimHUD Fix", "Gloomy x RimHUD Fix", "", ["1.6"])     # no required items
    both = DbMod("1104", "", "RimHUD & Allow Tool compat", "RimHUD & Allow Tool compat", "", ["1.6"])
    obscure = DbMod("1105", "low.mod", "Strength in numbers", "Strength in numbers", "", ["1.6"])
    run.details["1105"] = {"result": 1, "subscriptions": 14}
    run.rank([orphan, fine, cross, both, obscure, db.rows["1001"], db.rows["1004"]], {}, "t")
    assert [p.workshop_id for p in run.picks] == ["1001", "1004", "1102", "1104"], run.picks
    # an essential query takes that mod only (not add-ons naming it); a must-have goes in first, even an unpopular
    # library; the summary carries the download size
    num = [DbMod("2001", "numbers", "Numbers", "Numbers", "", ["1.6"], subscriptions=350000),
           DbMod("2002", "numbers.trait", "Numbers Trait AddOn", "Numbers Trait AddOn", "", ["1.6"], {"2001": "Numbers"},
                 subscriptions=60000),
           DbMod("2003", "uini.numbers", "UI Not Included: Numbers Addon", "UI Not Included: Numbers Addon", "",
                 ["1.6"], {"2001": "Numbers"}, subscriptions=4000),
           DbMod("2004", "jecrell.jecstools", "JecsTools Unofficial 1.6", "JecsTools Unofficial 1.6", "", ["1.6"])]
    res = generate(GenRequest("qol", "1.6", target_count=5), _FakeDB(num), None,  # type: ignore[arg-type]
                   details_fn=lambda ids: {i: {"result": 1, "subscriptions": 350000, "file_size": "6600000"} for i in ids})
    assert [p.workshop_id for p in res.picks] == ["2004", "2001"] and "Download ≈ 13 MB." in res.summary, \
        (res.picks, res.summary)

    # malformed / failing AI: plan fails (format) -> keyword queries; select garbage -> offline ranking
    llm = _FakeLLM([LLMError("bad json", "format"), {"picks": "garbage"}, {"nothing": 1}])
    res = generate(GenRequest("medieval", "1.6", target_count=3), db, llm, details_fn=details)  # type: ignore[arg-type]
    assert_trusted(res)
    assert res.picks and all(p.source in ("offline", "dependency") for p in res.picks)
    assert ("(reply)", "no 'picks' list in the model reply") in res.rejected
    # fatal AI error -> full offline generator; offline generator on its own
    for llm2 in (_FakeLLM([LLMError("401", "auth")]), None):
        res = generate(GenRequest("medieval castle life, no combat", "1.6", target_count=4), db, llm2, details_fn=details)  # type: ignore[arg-type]
        assert_trusted(res)
        names = [p.name for p in res.picks if p.source == "offline"]
        assert names and len(names) <= 4 and not {"Combat Extended", "Yayo's Combat 3"} & set(names), names
        assert not {"1011", "1012", "1013", "1014", "1005", "1017"} & {p.workshop_id for p in res.picks}
    # network down: live verify skipped, still a result
    res = generate(GenRequest("medieval", "1.6", target_count=3), db, None, details_fn=lambda ids: {})
    assert_trusted(res)
    assert res.picks and any("verification skipped" in line for line in res.log)
    # auto_count (AI decides mod count) + no_lazy mode
    llm_auto = _FakeLLM([plan, select])
    res_auto = generate(GenRequest("big medieval overhaul", "1.6", target_count=0, auto_count=True, no_lazy=True),
                        db, llm_auto, details_fn=details)  # type: ignore[arg-type]
    assert_trusted(res_auto)
    assert any("AI decided target size" in ln for ln in res_auto.log), res_auto.log
    assert "YOU decide how many mods" in llm_auto.calls[0][1] and "NO BEING LAZY" in llm_auto.calls[0][0]
    assert "NO BEING LAZY" in llm_auto.calls[1][0] and "NO BEING LAZY" in llm_auto.calls[1][1]
    _check_schema(IDEA_CHAT_SCHEMA)
    c_out = chat_idea_architect([("user", "nuclear oil colony, build it now")], "", {"ludeon.rimworld.biotech"}, "1.7", None)
    assert c_out["reply"] and c_out["engineered_prompt"] and c_out["build_now"] is True, c_out
    # Steam down but some ids cached: uncached ids stay unknown (not "removed"); no second wait on a dead network
    db.cached_details = lambda ids, max_age_s=0: {i: {"result": 1} for i in ids if i == "1001"}  # type: ignore[attr-defined]
    run = _Run(GenRequest("x", "1.6"), db, noop_progress, None, lambda ids: {i: {"result": 1} for i in ids if i == "1001"})
    assert run.enrich(["1001", "1002"]) is True and "1002" not in run.details and not run.dead("1002")
    del db.cached_details
    assert not _Run(GenRequest("guns, no translation mods", "1.6"), db, noop_progress, None, details).allow_foreign
    assert _Run(GenRequest("rimhud plus chinese translation", "1.6"), db, noop_progress, None, details).allow_foreign
    assert not _Run(GenRequest("NO translation mods, language packs or localization mods.\n"
                               "AVOID any mod title that contains: translation, 中文, русский", "1.6"),
                    db, noop_progress, None, details).allow_foreign
    n_calls: list[int] = []
    run = _Run(GenRequest("x", "1.6"), db, noop_progress, None, lambda ids: n_calls.append(1) or {})
    assert run.enrich(["1001"]) is False and run.enrich(["1002"]) is False and len(n_calls) == 1
    # cancel
    ev = threading.Event()
    ev.set()
    res = generate(GenRequest("medieval", "1.6"), db, _FakeLLM([plan, select]), cancel=ev, details_fn=details)  # type: ignore[arg-type]
    assert res.summary == "Cancelled." and not res.picks
    ev = threading.Event()                   # cancel mid-run (during Steam verification): validated picks survive
    res = generate(GenRequest("medieval", "1.6", target_count=5), db, _FakeLLM([plan, select]),  # type: ignore[arg-type]
                   progress=lambda f, t: ev.set() if t.startswith("Step 5/7") else None, cancel=ev, details_fn=details)
    assert res.summary.startswith("Cancelled - partial") and "1002" in [p.workshop_id for p in res.picks], res.summary
    assert_trusted(res)

    _selftest_live()
    _selftest_openai_local()
    _selftest_anthropic_mock()


def _selftest_live() -> None:
    """The live Steam Workshop (faked): a title the DB lacks, an author's whole Workshop page, a collection link,
    a required item the DB lacks; every keyed find is stored in the DB first and checked like any other row."""
    import urllib.error

    def item(wid, title, tags=("Mod", "1.6"), children=(), creator="c1", subs=1000, app=294100, file_type=0):
        return {"publishedfileid": wid, "result": 1, "consumer_appid": app, "title": title, "creator": creator,
                "tags": [{"tag": t} for t in tags], "children": [{"publishedfileid": c} for c in children],
                "num_children": len(children), "file_type": file_type, "subscriptions": subs}

    class FakeWorkshop:
        key = "SECRET-KEY-123"

        def __init__(self, items, fail=None, key=None):
            self.items, self.fail, self.searched, self.pages = {d["publishedfileid"]: d for d in items}, fail, [], []
            if key is not None:
                self.key = key

        def search(self, text, version):
            self.searched.append(text)
            if self.fail:
                time.sleep(0.05)
                raise self.fail
            assert version == "1.6"
            return [d for d in self.items.values() if text.lower() in d["title"].lower()]

        def author(self, steamid):
            self.pages.append(steamid)
            return [d for d in self.items.values() if d["creator"] == steamid]

        def details(self, ids):
            return {i: self.items[i] for i in ids if i in self.items}

        def collections(self, ids):
            return {"900000001": ["3006", "1001", "1004", "3008", "3009", "400000002"]} if "900000001" in ids else {}

    db, details = _fake_world()
    A = lambda wid, name, who="Andromeda": DbMod(wid, f"x.{wid}", name, name, who, ["1.6"], {}, subscriptions=300000)  # noqa: E731
    db.rows.update({"3101": A("3101", "Nice Bill Tab"), "3102": A("3102", "Nice Health Tab"),
                    "3201": A("3201", "Work Tab", "Fluffy"), "3202": A("3202", "Stuffed Floors", "Fluffy"),
                    "3203": A("3203", "Medical Tab (Continued)", "Fluffy")})
    ws = FakeWorkshop([
        item("3001", "Nice Research Tab", children=("3002", "3007", "2009463077"), subs=38000),   # the DB lacks it
        item("3002", "Milky Way", subs=57000), item("3003", "Nice Research Tab [Fr]", tags=("Translation", "1.6")),
        item("3004", "Busywork", subs=10000), item("3005", "Old Andromeda Thing", tags=("Mod", "1.4")),
        item("3101", "Nice Bill Tab", subs=322000), item("3102", "Nice Health Tab", subs=339000),
        item("3010", "Andromeda's Guide", file_type=9),                                    # on the author page: no mod
        item("3006", "Linked Mod", creator="c2", subs=500), item("3007", "Hidden Lib", creator="c4"),
        item("3008", "Old Linked Mod", tags=("Mod", "1.4"), creator="c2"),                 # outdated: kept, warned
        item("3009", "A RimWorld Guide", creator="c2", file_type=9),                       # a guide from the collection
        item("400000001", "Other Game Item", creator="c3", app=4000),
        item("400000002", "Excluded Linked Mod", creator="c2"),                            # "not <id>": never added
        item("3201", "Work Tab", creator="c5"), item("3202", "Stuffed Floors", creator="c6")])
    plan = {"queries": [{"query": "Nice Research Tab", "category": "ui", "want": 1},
                        {"query": "Andromeda", "category": "ui", "want": 3},
                        {"query": "Fluffy", "category": "ui", "want": 1}], "avoid": [], "notes": ""}
    llm = _FakeLLM([plan, {"summary": "UI.", "picks": [{"id": "3001"}, {"id": "3004"}, {"id": "3101"}, {"id": "3102"}]}])
    req = GenRequest("andromeda's mods + https://steamcommunity.com/sharedfiles/filedetails/?id=900000001 but "
                     "without Allow Tool, not 400000002. Also 400000001", "1.6", target_count=7,
                     include_popular_qol=False, installed_ids={"3102"}, steam_key=ws.key)
    assert ws.key not in repr(req)
    res = generate(req, db, llm, details_fn=details, workshop=ws)                 # type: ignore[arg-type]
    by = {s: {p.workshop_id for p in res.picks if p.source == s} for s in ("link", "ai", "dependency")}
    assert by == {"link": {"3006", "1001", "3008"}, "ai": {"3001", "3004", "3101", "3102"},
                  "dependency": {"3002", "3007", "2009463077"}}, (by, res.log)
    for p in res.picks:                                    # names from DB rows (stored from Steam), never the model
        assert p.name == db.rows[p.workshop_id].display_name, p
    rej = dict(res.rejected)
    assert "excludes it" in rej["1004"] and "not a RimWorld mod" in rej["400000001"] and "guide" in rej["3009"], rej
    assert "400000002" not in rej and "400000002" not in db.rows and "3010" not in db.rows      # never added/stored
    assert ws.pages == ["c1"] and db.rows["3004"].authors == "" and db.rows["3007"].steam_name == "Hidden Lib"
    assert any("Old Linked Mod (your link) doesn't list" in x for x in res.log)
    enum = set(llm.calls[1][2]["properties"]["picks"]["items"]["properties"]["id"]["enum"])
    assert {"3001", "3002", "3004", "3101", "3102"} <= enum and not enum & {"3003", "3005", "3006", "1001", "3010"}, enum
    system, user = llm.calls[0][0], llm.calls[0][1]
    assert "live Steam Workshop" in system and "Linked Mod" in user and "about 4 mods" in user, user
    table = llm.calls[1][1]
    assert "Already in the list from the player's links" in table and "Linked Mod" in table
    assert "installed" in next(line for line in table.splitlines() if line.startswith("3102 |"))
    assert "3 from your links" in res.summary and ws.key not in "\n".join(res.log) + res.summary
    # Steam down: stop asking after the first failures, enrich() doesn't wait again, the database results stand
    down = FakeWorkshop([], fail=TimeoutError("timed out"))
    run = _Run(GenRequest("x", "1.6"), db, noop_progress, None, details, down)
    cands, _ = run.retrieve([{"query": q, "category": "ui", "want": 1} for q in ["Nice Bill Tab"] + ["Zzz"] * 11], [], 0, 1)
    assert [r.workshop_id for r in cands] == ["3101"] and len(down.searched) <= 4 and run.steam_down, down.searched
    assert any("search failed (TimeoutError)" in x for x in run.res.log), run.res.log
    bad = FakeWorkshop([], fail=urllib.error.HTTPError("u", 401, "Unauthorized", {}, None))     # type: ignore[arg-type]
    run = _Run(GenRequest("x", "1.6"), db, noop_progress, None, details, bad)
    run.retrieve([{"query": "Nice", "category": "ui", "want": 1}], [], 0, 1)
    assert any("rejected the Steam Web API key" in x for x in run.res.log) and not run.steam_down
    ev = threading.Event()
    ev.set()
    run = _Run(GenRequest("x", "1.6"), db, noop_progress, ev, details, bad)     # cancelled: no Steam calls at all
    bad.searched.clear()
    try:
        run.live([{"query": "Nice"}], [[]], [], {})
    except _Cancelled:
        pass
    assert not bad.searched
    # Steam's text search also matches descriptions: those mods join the query, ranked after its title matches
    wide = FakeWorkshop([{**item("3301", "Panzer IV", subs=90000), "short_description": "A tank. Needs Vehicle Framework"},
                         item("3302", "Vehicle Pack", subs=10), item("3303", "Vehiculum", subs=5000)])   # loose: dropped
    wide.search = lambda text, version: list(wide.items.values())                 # type: ignore[method-assign]
    run = _Run(GenRequest("x", "1.6"), db, noop_progress, None, details, wide)
    cands, _ = run.retrieve([{"query": "Vehicle", "category": "vehicles", "want": 1}], [], 0, 1)
    assert [r.workshop_id for r in cands] == ["3302", "3301"] and run.by_desc == {(0, "3301")}, cands
    assert any("1 by description" in x for x in run.res.log), run.res.log
    # keyless details (no required items): rows for this run only, never saved; guides/collections are no mods
    v1 = {"5001": {"publishedfileid": "5001", "result": 1, "consumer_app_id": 294100, "creator_app_id": 294100,
                   "title": "Keyless Mod", "tags": [{"tag": "1.6"}]},
          "5002": {"publishedfileid": "5002", "result": 1, "consumer_app_id": 294100, "creator_app_id": 766,
                   "title": "A Collection"}}
    keyless = FakeWorkshop([], key="")
    keyless.details = lambda ids: {i: v1[i] for i in ids if i in v1}                   # type: ignore[method-assign]
    run = _Run(GenRequest("my mods 50010000001 x", "1.6"), db, noop_progress, None, details, keyless)
    rows = run.live_rows(list(v1.values()))
    assert set(rows) == {"5001"} and rows["5001"].game_versions == ["1.6"] and "5001" not in db.rows
    run = _Run(GenRequest("x", "1.6"), db, noop_progress, None, details)
    assert isinstance(run.workshop, _Workshop) and not run.workshop.key
    run.retrieve([{"query": "Nice", "category": "ui", "want": 1}], [], 0, 1)
    assert any("Steam Web API key" in x for x in run.res.log)
    run = _Run(GenRequest("mods 900000001", "1.6", live_verify=False), db, noop_progress, None, details)
    run.links()
    assert run.workshop is None and not run.picks and any("live Steam Workshop is off" in x for x in run.res.log)
    # a rejected key: details fall back to the keyless call
    from . import steam
    real = steam.item_details, steam.get_details
    steam.item_details = lambda ids, key: (_ for _ in ()).throw(urllib.error.HTTPError("u", 403, "x", {}, None))  # type: ignore[assignment]
    steam.get_details = lambda ids, db=None, max_age_s=3600: {i: {"publishedfileid": i} for i in ids}    # type: ignore[assignment]
    try:
        assert list(_Workshop("bad").details(["7"])) == ["7"]
    finally:
        steam.item_details, steam.get_details = real
    # links and ids: from URLs (also steam://), never from numbers in plain text; not search words offline
    text = _WID_URL_RE.sub(r" \1 ", "300 mods for 1.6, steam://url/CommunityFilePage/3773496046 and "
                                    "https://steamcommunity.com/sharedfiles/filedetails/?l=en&id=2009463077 x")
    assert _WID_RE.findall(text) == ["3773496046", "2009463077"], text
    q, _, _ = _offline_queries("cozy farming colony https://steamcommunity.com/sharedfiles/filedetails/?id=2009463077 "
                               "steam://url/CommunityFilePage/3773496046", False)
    assert not any(w in x.lower() for x, _, _ in q for w in ("http", "steam", "com", "filedetails", "url", "2009")), q


def _selftest_openai_local() -> None:
    """OpenAI-compatible fallback chain against a loopback fake server (no internet)."""
    import http.server
    import socket
    seen: list[dict] = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            self._send(200, {"data": [{"id": "models/m1"}, {"id": "m2"}]})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            if self.headers.get("Authorization") == "Bearer bad":
                return self._send(401, {"error": {"message": "invalid key"}})
            if body.get("response_format", {}).get("type") == "json_schema":
                return self._send(400, {"error": {"message": "response_format json_schema unsupported"}})
            if "max_tokens" in body:
                return self._send(400, {"error": {"message": "Use max_completion_tokens instead"}})
            usage = {"prompt_tokens": None} if body["model"] == "null-usage" else {"prompt_tokens": 5, "completion_tokens": 2}
            self._send(200, {"choices": [{"message": {"content": 'Here:\n```json\n{"ok": true}\n```'},
                                          "finish_reason": "stop"}], "usage": usage})

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}/v1"
        llm = LLM("openai_compat", "m1", "k", base)
        schema = {"type": "object", "additionalProperties": False, "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
        assert llm.complete_json("sys", "hi", schema, 50) == {"ok": True}
        assert seen[-1]["response_format"] == {"type": "json_object"} and seen[-1]["max_completion_tokens"] == 50
        assert "5 in" in llm.last_usage and llm.list_models() == ["m1", "m2"]
        nu = LLM("openai_compat", "null-usage", "k", base)
        assert nu.complete_json("sys", "hi", schema, 50) == {"ok": True} and nu.last_usage.startswith("0 in")
        nu.abort()
        try:
            nu.complete_json("sys", "hi", schema, 50)
        except LLMError as e:
            assert str(e) == "Cancelled.", e
        else:
            raise AssertionError("request sent after Cancel")
        try:
            LLM("openai_compat", "m1", "bad", base).complete_json("s", "u", schema, 10)
        except LLMError as e:
            assert e.kind == "auth" and "401" in str(e)
        else:
            raise AssertionError("401 not mapped")
    finally:
        srv.shutdown()
        srv.server_close()
    with socket.socket() as s:            # a port with nothing listening -> friendly connection error
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    try:
        LLM("openai_compat", "m", "", f"http://127.0.0.1:{port}/v1").complete_json("s", "u", {}, 10)
    except LLMError as e:
        assert e.kind == "connection" and "running" in str(e), e
    else:
        raise AssertionError("connection error not mapped")
    assert LLM("openai_compat", "m", "", "localhost:11434/v1").base_url == "http://localhost:11434/v1"
    try:
        LLM("openai_compat", "m", "", "http://127.0.0.1:abc/v1").complete_json("s", "u", {}, 10)
    except LLMError as e:
        assert e.kind == "config" and "Invalid base URL" in str(e), e
    else:
        raise AssertionError("bad port not mapped")


def _selftest_anthropic_mock() -> None:
    """Request shape + 400 fallbacks + refusal for the Anthropic path, with a fake client (no network)."""
    try:
        import anthropic
        import httpx2
    except ImportError:
        if getattr(sys, "frozen", False):    # the exe must bundle the lazily imported SDK
            raise
        return
    import copy
    from types import SimpleNamespace as NS
    calls: list[dict] = []
    script: list = []

    class Stream:
        def __init__(self, kw):
            calls.append(copy.deepcopy(kw))

        def __enter__(self):
            r = script.pop(0)
            if isinstance(r, Exception):
                raise r
            return NS(get_final_message=lambda: r)

        def __exit__(self, *a):
            return False

    def bad(msg):
        resp = httpx2.Response(400, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
        return anthropic.BadRequestError(msg, response=resp, body={"error": {"type": "invalid_request_error", "message": msg}})

    def reply(text, stop="end_turn"):
        return NS(stop_reason=stop, model="claude-opus-5", usage=NS(input_tokens=10, output_tokens=3),
                  content=[NS(type="thinking", thinking=""), NS(type="text", text=text)])

    schema = {"type": "object", "additionalProperties": False, "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
    assert LLM("anthropic", "m", "k", "http://192.168.1.5:8080/v1").base_url == ""    # stale Custom URL ignored
    assert LLM("openai_compat", "m", "k", "http://h/v1/").base_url == "http://h/v1"
    assert _without_enums({"a": {"type": "string", "enum": ["1"]}, "b": [{"enum": [2]}]}) == {"a": {"type": "string"},
                                                                                            "b": [{}]}
    if os.name == "nt":                     # Cancel kills a running Claude Code process
        import subprocess
        cc = LLM("claude_code", "")
        cc._proc = subprocess.Popen(["ping", "-n", "30", "127.0.0.1"], stdout=subprocess.DEVNULL,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        cc.abort()
        assert cc._proc.wait(10) is not None
    assert LLM("claude_code", "").list_models() == ["fable", "opus", "sonnet", "haiku"] and PRESETS[
        "Claude Code (your subscription)"]["needs_key"] is False
    agy_models = LLM("antigravity", "").list_models()
    assert agy_models[:4] == ["gemini-4-argon-high", "gemini-4-argon-medium", "gemini-4-argon-low", "gemini-4-argon"]
    assert PRESETS["Antigravity (your subscription)"]["model"] == "gemini-4-argon-high" and PRESETS[
        "Antigravity (your subscription)"]["needs_key"] is False
    assert _antigravity_resolve_model("gemini-4-argon-high") == ("gemini-4-argon-high", "MODEL_PLACEHOLDER_M417")
    assert _antigravity_resolve_model("gemini-4-argon", "medium") == ("gemini-4-argon-medium", "MODEL_PLACEHOLDER_M418")
    assert _antigravity_resolve_model("gemini-4-argon", "low") == ("gemini-4-argon-low", "MODEL_PLACEHOLDER_M419")
    assert _antigravity_resolve_model("pro", "high") == ("gemini-4-argon-high", "MODEL_PLACEHOLDER_M417")
    assert _antigravity_resolve_model("claude-opus-5-5", "max") == ("claude-opus-5-5-max", "MODEL_PLACEHOLDER_M406")
    long_tbl = "\n".join(f"{1000 + i} | Mod {i} | Auth | 1k | 0 | qol | note | {'x' * 200}" for i in range(150))
    assert len(_antigravity_shrink_prompt(long_tbl, limit=15000)) <= 15000
    llm = LLM("anthropic", "claude-opus-5", "sk-test", effort="high")
    llm._client = NS(beta=NS(messages=NS(stream=lambda **kw: Stream(kw))))
    script[:] = [bad("output_config.format.schema: too many enum values"), reply('{"ok": true} done')]
    assert llm.complete_json("sys", "u", schema, 100) == {"ok": True}
    first, second = calls
    assert first["thinking"] == {"type": "adaptive"} and first["output_config"]["effort"] == "high"
    assert first["output_config"]["format"]["schema"] == schema and first["fallbacks"] == "default"
    assert first["betas"] == ["server-side-fallback-2026-07-01"]
    assert "format" not in second["output_config"] and "JSON schema" in second["system"]
    calls.clear()
    llm.model = "claude-haiku-4-5"
    script[:] = [reply('{"ok": false}')]
    assert llm.complete_json("sys", "u", schema) == {"ok": False}
    assert "thinking" not in calls[0] and "effort" not in calls[0]["output_config"] and "betas" not in calls[0]
    script[:] = [reply("", "refusal")]
    try:
        llm.complete_json("sys", "u", schema)
    except LLMError as e:
        assert e.kind == "refusal"
    else:
        raise AssertionError("refusal not raised")
    script[:] = [reply('{"ok": tr', "max_tokens")]
    try:
        llm.complete_json("sys", "u", schema)
    except LLMError as e:
        assert "cut off" in str(e)
    else:
        raise AssertionError("truncation not raised")
    # cut off after 300 complete picks: keep them instead of throwing the whole selection away
    body = '{"picks":[' + ",".join('{"id":"%d","reason":"r"}' % i for i in range(1000, 1300)) + ',{"id":"99'
    script[:] = [reply(body, "max_tokens")]
    assert len(llm.complete_json("sys", "u", schema)["picks"]) == 300
    # mid-stream server-side fallback: the answer continues in a text block after the 'fallback' block
    full = '{"picks":[{"id":"1001","reason":"a"},{"id":"1002","reason":"b"}],"summary":"s"}'
    for cut in (20, 40):
        script[:] = [NS(stop_reason="end_turn", model="claude-opus-5", usage=NS(input_tokens=1, output_tokens=1),
                        content=[NS(type="thinking", thinking=""), NS(type="text", text=full[:cut]),
                                 NS(type="fallback"), NS(type="text", text=full[cut:])])]
        assert llm.complete_json("sys", "u", schema) == json.loads(full), cut
    script[:] = [httpx2.RemoteProtocolError("peer closed connection")]     # dropped mid-stream: unwrapped by the SDK
    try:
        llm.complete_json("sys", "u", schema)
    except LLMError as e:
        assert e.kind == "connection" and "interrupted" in str(e), e
    else:
        raise AssertionError("mid-stream transport error not mapped")
    script[:] = [bad("Your credit balance is too low to access the Anthropic API.")]
    try:
        llm.complete_json("sys", "u", schema)
    except LLMError as e:
        assert "credit balance" in str(e)
    else:
        raise AssertionError("400 not raised")
    llm.abort()                             # Cancel: no new stream is opened afterwards
    calls.clear()
    script[:] = [reply('{"ok": true}')]
    try:
        llm.complete_json("sys", "u", schema)
    except LLMError as e:
        assert str(e) == "Cancelled." and not calls, e
    else:
        raise AssertionError("stream opened after Cancel")


def _selftest_online() -> None:
    """One tiny real call if credentials exist (ANTHROPIC_API_KEY, the app's saved key) or Ollama runs locally."""
    import os
    schema = {"type": "object", "additionalProperties": False, "required": ["answer"],
              "properties": {"answer": {"type": "integer"}}}
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        try:
            from .config import Settings
            s = Settings(os.environ.get("RMM_REAL_SETTINGS") or None)   # the self-test app dir is a temp copy
            if s.get("ai_provider") == "anthropic":
                key = s.get_secret("ai_api_key")
        except Exception:  # noqa: BLE001
            key = ""
    targets = []
    if key:
        targets.append(LLM("anthropic", "claude-haiku-4-5", key))
    ollama = LLM("openai_compat", "", "", "http://127.0.0.1:11434/v1")
    models = ollama.list_models()
    if models:
        ollama.model = models[0]
        targets.append(ollama)
    if not targets:
        print("ai online selftest: skipped (no Anthropic key and no Ollama server)")
        return
    for llm in targets:
        out = llm.complete_json("You are a calculator.", "What is 2+3? Answer as JSON.", schema, 2000)
        assert _int(out.get("answer")) == 5, out
        print(f"ai online selftest: {llm.provider}/{llm.model} OK ({llm.last_usage})")


if __name__ == "__main__":
    _selftest()
    print("rmm.ai selftest OK")
