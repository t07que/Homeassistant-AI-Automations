# agent_server.py
import os
from contextvars import ContextVar
import json
import time
import re
import difflib
import math
import hashlib

from typing import Any, Dict, List, Optional, Tuple

import requests
import websockets
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query, Body, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from pathlib import Path
import datetime

load_dotenv()
app = FastAPI()

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = os.getenv("STATIC_DIR", "static")
STATIC_PATH = (BASE_DIR / STATIC_DIR).resolve()

def _clean_env_value(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    v = value.strip().strip('"').strip("'")
    # Guard against mistakenly copied Python expressions in .env
    if "os.getenv" in v or "os.environ" in v:
        return None
    return v

def _env_int(name: str, default: int) -> int:
    raw = _clean_env_value(os.getenv(name))
    if not raw:
        return default
    try:
        return int(raw)
    except Exception:
        return default

def _env_float(name: str, default: float) -> float:
    raw = _clean_env_value(os.getenv(name))
    if not raw:
        return default
    try:
        return float(raw)
    except Exception:
        return default

def _looks_like_absolute_path(path_str: str) -> bool:
    if not path_str:
        return False
    if path_str.startswith("\\\\"):
        return True
    try:
        return Path(path_str).is_absolute()
    except Exception:
        return False

def _resolve_path(path_str: str) -> Path:
    if not path_str:
        return BASE_DIR
    if _looks_like_absolute_path(path_str):
        return Path(path_str)
    return (BASE_DIR / path_str).resolve()

AUTOMATIONS_FILE_PATH = (
    _clean_env_value(os.getenv("AUTOMATIONS_FILE_PATH"))
    or _clean_env_value(os.getenv("HA_AUTOMATIONS_PATH"))
)

SCRIPTS_FILE_PATH = (
    _clean_env_value(os.getenv("SCRIPTS_FILE_PATH"))
    or _clean_env_value(os.getenv("HA_SCRIPTS_PATH"))
)

RESTORE_STATE_PATH = (
    _clean_env_value(os.getenv("RESTORE_STATE_PATH"))
    or _clean_env_value(os.getenv("HA_RESTORE_STATE_PATH"))
)

LOCAL_AUTOMATIONS_PATH = _clean_env_value(os.getenv("LOCAL_AUTOMATIONS_PATH")) or "automations.yaml"
LOCAL_SCRIPTS_PATH = _clean_env_value(os.getenv("LOCAL_SCRIPTS_PATH")) or "scripts.yaml"
AUTOMATIONS_VERSIONS_DIR = (
    _clean_env_value(os.getenv("AUTOMATIONS_VERSIONS_DIR"))
    or _clean_env_value(os.getenv("AUTOMATION_VERSIONS_DIR"))
    or "versions"
)
LOCAL_DB_FILE = _clean_env_value(os.getenv("LOCAL_DB_FILE")) or "local_automations_db.json"

print(f"[UI] Serving static from: {STATIC_PATH}")

# Debug endpoint so we can verify what the server sees
@app.get("/__debug/ui", include_in_schema=False)
def debug_ui():
    return {
        "static_path": str(STATIC_PATH),
        "exists": STATIC_PATH.is_dir(),
        "files": [p.name for p in STATIC_PATH.iterdir()] if STATIC_PATH.is_dir() else [],
    }

def _ingress_base_path(request: Request) -> str:
    # Home Assistant ingress sends the base path in headers when proxying.
    ingress = (
        request.headers.get("X-Ingress-Path")
        or request.headers.get("X-Forwarded-Path")
        or request.headers.get("X-Forwarded-Prefix")
    )
    if ingress:
        return ingress.rstrip("/")
    root_path = request.scope.get("root_path") or ""
    return root_path.rstrip("/")

@app.get("/", include_in_schema=False)
def root(request: Request):
    base = _ingress_base_path(request)
    if base:
        return RedirectResponse(url=f"{base}/ui/")
    # Use a relative redirect so ingress prefixes are preserved.
    return RedirectResponse(url="ui/")

# Mount UI
app.mount("/ui", StaticFiles(directory=str(STATIC_PATH), html=True), name="ui")

# Optional: direct file access for testing
app.mount("/static", StaticFiles(directory=str(STATIC_PATH), html=False), name="static")


# ----------------------------
# LOCAL DB + IMPORT HELPERS
# ----------------------------

def _ensure_versions_dir() -> Path:
    p = (BASE_DIR / AUTOMATIONS_VERSIONS_DIR).resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p

def _ts() -> str:
    return time.strftime("%Y%m%d-%H%M%S")

def _load_local_db() -> Dict[str, Any]:
    p = (BASE_DIR / LOCAL_DB_FILE).resolve()
    if not p.exists():
        return {"items": {}, "meta": {"created": _ts()}}
    try:
        data = json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        # if corrupted, start fresh but don't crash the API
        return {"items": {}, "meta": {"warning": "db_parse_failed", "recovered": _ts()}}

    if not isinstance(data, dict):
        return {"items": {}, "meta": {"warning": "db_format_invalid", "recovered": _ts()}}

    items = data.get("items")
    if isinstance(items, list):
        migrated: Dict[str, Any] = {}
        for i, it in enumerate(items, start=1):
            if not isinstance(it, dict):
                continue
            key = str(it.get("id") or it.get("alias") or f"imported_{i}")
            key = _slug(key)
            if not key:
                key = f"imported_{i:03d}"
            if key in migrated:
                key = f"{key}_{i:03d}"
            it["id"] = key
            migrated[key] = it
        data["items"] = migrated
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        meta["migrated_from_list"] = _ts()
        data["meta"] = meta
    elif not isinstance(items, dict):
        data["items"] = {}

    return data

def _save_local_db(db: Dict[str, Any]) -> None:
    p = (BASE_DIR / LOCAL_DB_FILE).resolve()
    p.write_text(json.dumps(db, indent=2, ensure_ascii=False), encoding="utf-8")

def _backup_file(path: Path, label: str) -> Optional[str]:
    try:
        if not path.exists():
            return None
        _ensure_versions_dir()
        dest = (BASE_DIR / AUTOMATIONS_VERSIONS_DIR / f"{label}_{_ts()}_{path.name}").resolve()
        dest.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        return str(dest)
    except Exception:
        return None

def _backup_db() -> Optional[str]:
    try:
        p = (BASE_DIR / LOCAL_DB_FILE).resolve()
        return _backup_file(p, "db_backup")
    except Exception:
        return None

def _normalize_automation_list(obj: Any) -> List[Dict[str, Any]]:
    """
    Home Assistant automations.yaml is typically a YAML list of dicts.
    We also accept a dict with an 'automation' key, just in case.
    """
    if obj is None:
        return []
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if isinstance(obj, dict):
        # sometimes people wrap it
        if isinstance(obj.get("automation"), list):
            return [x for x in obj["automation"] if isinstance(x, dict)]
        # single automation dict
        if "trigger" in obj and ("action" in obj or "sequence" in obj):
            return [obj]
    return []

def _import_automations_into_db(autos: List[Dict[str, Any]], source: str) -> Dict[str, Any]:
    db = _load_local_db()
    items = db.get("items")
    if not isinstance(items, dict):
        items = {}
        db["items"] = items

    imported = 0
    updated = 0

    for idx, a in enumerate(autos):
        if not isinstance(a, dict):
            continue
        aid = str(a.get("id") or "").strip()
        alias = str(a.get("alias") or a.get("name") or f"Imported Automation {idx+1}").strip()

        if not aid:
            aid = f"local_{_slug(alias)}_{idx+1:03d}"

        yaml_text = _yaml_dump(a)

        prev_item = items.get(aid) if isinstance(items, dict) else None
        if prev_item:
            if isinstance(prev_item.get("yaml"), str):
                _backup_write(aid, prev_item["yaml"], reason="local_before_import_overwrite")
            updated += 1
        else:
            imported += 1

        items[aid] = {
            "id": aid,
            "alias": alias,
            "description": str(a.get("description") or ""),
            "source": source,
            "ha_id": None,
            "updated": _now_stamp(),
            "yaml": yaml_text,
            "conversation_id": prev_item.get("conversation_id") if isinstance(prev_item, dict) else None,
            "conversation_history": prev_item.get("conversation_history") if isinstance(prev_item, dict) and isinstance(prev_item.get("conversation_history"), list) else [],
        }

    db["meta"] = db.get("meta") or {}
    db["meta"]["last_import"] = {"at": _now_stamp(), "source": source, "imported": imported, "updated": updated}
    _save_local_db(db)

    return {"imported": imported, "updated": updated, "total": len(items)}

# ----------------------------
# API: Import automations.yaml
# ----------------------------

@app.post("/api/automations/import")
async def api_import_automations(request: Request):
    """
    Import HA automations from either:
      - uploaded YAML file (multipart/form-data with field `file`), OR
      - local disk path (JSON: {"path":"automations.yaml"}) OR default LOCAL_AUTOMATIONS_PATH

    Also creates backups:
      - local db backup
      - source yaml backup (if importing from disk)
    """
    _backup_db()

    source = "upload"
    raw_text: Optional[str] = None
    req_path: Optional[str] = None

    content_type = (request.headers.get("content-type") or "").lower()

    # 1) Multipart upload (file or optional path in form data)
    if "multipart/form-data" in content_type:
        form = await request.form()
        file = form.get("file")
        if file is not None and hasattr(file, "read"):
            raw = await file.read()
            raw_text = raw.decode("utf-8", errors="replace")
            source = f"upload:{getattr(file, 'filename', 'upload')}"
        else:
            path_val = form.get("path")
            if isinstance(path_val, str) and path_val.strip():
                req_path = path_val.strip()

    # 2) JSON payload (disk import)
    else:
        payload: Dict[str, Any] = {}
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if isinstance(payload, dict):
            path_val = payload.get("path")
            if isinstance(path_val, str) and path_val.strip():
                req_path = path_val.strip()

    # 3) Import from disk if no upload content
    if raw_text is None:
        candidates = []
        if req_path:
            candidates.append(req_path)
        if AUTOMATIONS_FILE_PATH:
            candidates.append(AUTOMATIONS_FILE_PATH)
        candidates.append(LOCAL_AUTOMATIONS_PATH)
        # also try common singular/plural in case env is wrong
        candidates.append("automation.yaml")
        candidates.append("automations.yaml")

        chosen: Optional[Path] = None
        for c in candidates:
            p = _resolve_path(str(c))
            if p.exists() and p.is_file():
                chosen = p
                break

        if not chosen:
            raise HTTPException(status_code=404, detail=f"No automations YAML found. Tried: {candidates}")

        _backup_file(chosen, "import_source")
        raw_text = chosen.read_text(encoding="utf-8")
        source = f"disk:{chosen.name}"

    # Parse YAML
    try:
        obj = yaml.safe_load(raw_text) if raw_text is not None else None
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"YAML parse failed: {type(e).__name__}: {e}")

    autos = _normalize_automation_list(obj)
    if not autos:
        raise HTTPException(status_code=400, detail="No automations found in YAML (expected a list of automation dicts).")

    summary = _import_automations_into_db(autos, source=source)
    return {"ok": True, "source": source, **summary}


# ----------------------------
# ENV CONFIG
# ----------------------------
HA_URL = os.getenv("HA_URL", "").rstrip("/")
HA_TOKEN = os.getenv("HA_TOKEN", "")
AGENT_SECRET = os.getenv("AGENT_SECRET", "")
BUILDER_AGENT_ID = os.getenv("BUILDER_AGENT_ID", "conversation.homeassistant")
AI_EDIT_AGENT_ID = os.getenv("AI_EDIT_AGENT_ID", BUILDER_AGENT_ID)
ARCHITECT_AGENT_ID = os.getenv("ARCHITECT_AGENT_ID", "conversation.automation_architect")
SUMMARY_AGENT_ID = os.getenv("SUMMARY_AGENT_ID", "conversation.automation_summary")
CAPABILITY_MAPPER_AGENT_ID = os.getenv("CAPABILITY_MAPPER_AGENT_ID", "conversation.home_assistant_capability_mapper")
SEMANTIC_DIFF_AGENT_ID = os.getenv("SEMANTIC_DIFF_AGENT_ID", "conversation.home_assistant_semantic_diff_summarizer")
KB_SYNC_HELPER_AGENT_ID = os.getenv("KB_SYNC_HELPER_AGENT_ID", "conversation.knowledgebase_sync_helper")
DUMB_BUILDER_AGENT_ID = os.getenv("DUMB_BUILDER_AGENT_ID", "conversation.autoautomation_dumb_builder")

# Server-side "completion announce" (optional). Set via env or add-on config.
# Example:
#   CONFIRM_DOMAIN=script
#   CONFIRM_SERVICE=your_announce_script
#   CONFIRM_FIELD=message
CONFIRM_DOMAIN = os.getenv("CONFIRM_DOMAIN", "")
CONFIRM_SERVICE = os.getenv("CONFIRM_SERVICE", "")
CONFIRM_FIELD = os.getenv("CONFIRM_FIELD", "message")

# Optional completion generation via conversation agent
CONFIRM_JARVIS = os.getenv("CONFIRM_JARVIS", "0") == "1"
CONFIRM_AGENT_ID = os.getenv("CONFIRM_AGENT_ID", "conversation.chatgpt_2")

# Optional legacy TTS support (only used if you set CONFIRM_DOMAIN=tts & CONFIRM_SERVICE=speak)
TTS_ENTITY_ID = os.getenv("TTS_ENTITY_ID", "")

DEBUG = os.getenv("DEBUG", "0") == "1"
HA_REQUEST_TIMEOUT = _env_int("HA_REQUEST_TIMEOUT", 60)
HA_CONVERSATION_TIMEOUT = _env_int("HA_CONVERSATION_TIMEOUT", 180)

HELPER_MAP_FILE = "helper_map.json"
CAPABILITIES_FILE = _clean_env_value(os.getenv("CAPABILITIES_FILE")) or "capabilities.yaml"

# Local UI + versions
AUTOMATION_VERSIONS_DIR = _clean_env_value(os.getenv("AUTOMATION_VERSIONS_DIR")) or AUTOMATIONS_VERSIONS_DIR
VERSIONS_DIR = Path(AUTOMATION_VERSIONS_DIR)
VERSIONS_DIR.mkdir(parents=True, exist_ok=True)

# Helper pool naming (optional)
POOL = {
    "counter": [f"counter.ai_counter_{i:02d}" for i in range(1, 11)],
    "timer": [f"timer.ai_timer_{i:02d}" for i in range(1, 11)],
    "boolean": [f"input_boolean.ai_bool_{i:02d}" for i in range(1, 11)],
    "number": [f"input_number.ai_num_{i:02d}" for i in range(1, 11)],
    "text": [f"input_text.ai_text_{i:02d}" for i in range(1, 11)],
}

TYPE_SYNONYMS = {
    "input_boolean": "boolean",
    "boolean": "boolean",
    "bool": "boolean",
    "input_number": "number",
    "number": "number",
    "input_text": "text",
    "text": "text",
    "timer": "timer",
    "counter": "counter",
}

BUILDER_BAD_OUTPUT_PATTERNS = (
    "OpenAI response incomplete",
    "max output tokens reached",
    "problem with my template",
    "Error talking to OpenAI",
)

DUMB_BUILDER_ADDENDUM = (
    "If any detail is uncertain, keep changes minimal, avoid guesses, and use placeholders rather than inventing entity_ids. "
    "Prefer preserving the current YAML structure."
)

HELPER_MIN_CONFIDENCE = _env_float("HELPER_MIN_CONFIDENCE", 0.55)
SUMMARY_CACHE_FILE = _clean_env_value(os.getenv("SUMMARY_CACHE_FILE")) or "summary_cache.json"
SUMMARY_CACHE_MAX = _env_int("SUMMARY_CACHE_MAX", 400)
ALLOW_AI_DIFF = os.getenv("ALLOW_AI_DIFF", "0") == "1"
RUNTIME_CONFIG_FILE = _clean_env_value(os.getenv("RUNTIME_CONFIG_FILE")) or "runtime_config.json"

SCRIPT_ID_PREFIX = "script__"

# Helper agent trace (per-request)
_AGENT_TRACE: ContextVar[Optional[List[Dict[str, Any]]]] = ContextVar("agent_trace", default=None)


def _agent_trace_start():
    return _AGENT_TRACE.set([])


def _helper_name_for_agent(agent_id: str) -> str:
    if agent_id == SUMMARY_AGENT_ID:
        return "summary"
    if agent_id == CAPABILITY_MAPPER_AGENT_ID:
        return "capability_mapper"
    if agent_id == SEMANTIC_DIFF_AGENT_ID:
        return "semantic_diff"
    if agent_id == KB_SYNC_HELPER_AGENT_ID:
        return "kb_sync_helper"
    return agent_id or "agent"


def _agent_trace_record(agent_id: str, ok: bool, detail: str = "") -> None:
    trace = _AGENT_TRACE.get()
    if trace is None:
        return
    trace.append({
        "name": _helper_name_for_agent(agent_id),
        "agent_id": agent_id,
        "ok": bool(ok),
        "detail": detail or "",
    })


def _agent_trace_finish(token) -> List[Dict[str, Any]]:
    trace = _AGENT_TRACE.get() or []
    try:
        _AGENT_TRACE.reset(token)
    except Exception:
        pass
    if not trace:
        return []
    merged: Dict[str, Dict[str, Any]] = {}
    for item in trace:
        key = item.get("name") or item.get("agent_id") or "agent"
        if key not in merged:
            merged[key] = dict(item)
        else:
            merged[key]["ok"] = bool(merged[key].get("ok")) and bool(item.get("ok"))
            if not item.get("ok") and item.get("detail"):
                merged[key]["detail"] = item.get("detail")
    return list(merged.values())



class BuildReq(BaseModel):
    text: str
    source: Optional[str] = "voice"


class ArchitectChatReq(BaseModel):
    text: str
    conversation_id: Optional[str] = None
    automation_id: Optional[str] = None
    entity_id: Optional[str] = None
    entity_type: Optional[str] = None
    current_yaml: Optional[str] = None
    include_context: Optional[bool] = False
    mode: Optional[str] = None
    save_entity_hint: Optional[bool] = False


class ArchitectFinalizeReq(BaseModel):
    conversation_id: Optional[str] = None
    automation_id: Optional[str] = None
    entity_id: Optional[str] = None
    entity_type: Optional[str] = None
    current_yaml: Optional[str] = None
    include_context: Optional[bool] = False
    mode: Optional[str] = None
    text: Optional[str] = None


class AutomationUpdateReq(BaseModel):
    # UI will send the HA automation config object here
    config: Dict[str, Any]


# ----------------------------
# AUTH
# ----------------------------
def require_auth(x_ha_agent_secret: str = "") -> None:
    """
    If AGENT_SECRET is set, enforce it. If it's blank, allow local dev without headers.
    """
    if AGENT_SECRET and x_ha_agent_secret != AGENT_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ----------------------------
# UTIL
# ----------------------------
def ha_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}


def ws_url() -> str:
    if HA_URL.startswith("https://"):
        return "wss://" + HA_URL[len("https://"):] + "/api/websocket"
    if HA_URL.startswith("http://"):
        return "ws://" + HA_URL[len("http://"):] + "/api/websocket"
    return HA_URL + "/api/websocket"


def load_capabilities() -> Dict[str, Any]:
    try:
        with open(CAPABILITIES_FILE, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        if DEBUG:
            print("Failed to load capabilities.yaml:", repr(e))
        return {}


def save_capabilities(data: Dict[str, Any]) -> None:
    with open(CAPABILITIES_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)


ENTITY_ID_RE = re.compile(r"\b[a-z_]+\.[a-z0-9_]+\b")
SCRIPT_ID_RE = re.compile(r"\bscript\.[a-z0-9_]+\b", re.IGNORECASE)
SCRIPT_PURPOSE_PATTERNS = (
    re.compile(r"(?:use|run|trigger|call|start|fire)\s+(script\.[a-z0-9_]+)\s+(?:for|to)\s+([^.;\n]+)", re.IGNORECASE),
    re.compile(r"(?:for|to)\s+([^.;\n]+?)\s+(?:use|run|trigger|call|start|fire)\s+(script\.[a-z0-9_]+)", re.IGNORECASE),
    re.compile(r"(script\.[a-z0-9_]+)\s+(?:handles|does|is for|is used for|runs)\s+([^.;\n]+)", re.IGNORECASE),
)
CONTEXT_TAG_KEYWORDS = {
    "todo": ("todo", "to-do", "task list", "shopping list"),
    "calendar": ("calendar", "event", "agenda", "schedule"),
    "reminder": ("remind", "reminder", "alert me"),
}


def update_capabilities_entity_hints(note: str) -> List[str]:
    if not note:
        return []
    entity_ids = sorted(set(ENTITY_ID_RE.findall(note)))
    if not entity_ids:
        return []

    caps = load_capabilities()
    user_ctx = caps.setdefault("user_context", {})
    hints = user_ctx.setdefault("entity_hints", {})
    stamp = _now_stamp()
    for eid in entity_ids:
        hints[eid] = {"note": note[:500], "updated": stamp}
    save_capabilities(caps)
    return entity_ids


def update_capabilities_script_hints(note: str) -> List[str]:
    if not note:
        return []
    script_ids = sorted({s.lower() for s in SCRIPT_ID_RE.findall(note)})
    if not script_ids:
        return []

    purpose_map: Dict[str, str] = {}
    for pattern in SCRIPT_PURPOSE_PATTERNS:
        for match in pattern.finditer(note):
            if len(match.groups()) != 2:
                continue
            g1, g2 = match.group(1), match.group(2)
            if (g1 or "").lower().startswith("script."):
                script_id = g1.lower()
                purpose = g2
            else:
                script_id = (g2 or "").lower()
                purpose = g1
            purpose = (purpose or "").strip()
            purpose = re.sub(r"\s+", " ", purpose)
            if script_id and purpose and script_id not in purpose_map:
                purpose_map[script_id] = purpose[:160]

    caps = load_capabilities()
    scripts = caps.get("scripts")
    if not isinstance(scripts, list):
        scripts = []
        caps["scripts"] = scripts

    stamp = _now_stamp()
    updated: List[str] = []
    for script_id in script_ids:
        entry = next((s for s in scripts if isinstance(s, dict) and s.get("entity_id") == script_id), None)
        if not entry:
            entry = {"entity_id": script_id}
            scripts.append(entry)
        if purpose_map.get(script_id):
            entry["purpose"] = purpose_map[script_id]
        elif not entry.get("purpose"):
            entry["note"] = note[:220]
        entry["updated"] = stamp
        updated.append(script_id)

    save_capabilities(caps)
    return updated


def update_capabilities_context_hints(note: str) -> List[str]:
    if not note:
        return []
    text = note.lower()
    tags = set()

    for tag, keywords in CONTEXT_TAG_KEYWORDS.items():
        if any(k in text for k in keywords):
            tags.add(tag)

    entity_ids = sorted(set(ENTITY_ID_RE.findall(note)))
    domain_entities: Dict[str, List[str]] = {}
    for eid in entity_ids:
        domain = eid.split(".", 1)[0].lower()
        if domain in ("todo", "calendar"):
            tags.add(domain)
            domain_entities.setdefault(domain, [])
            if eid not in domain_entities[domain]:
                domain_entities[domain].append(eid)

    if not tags and not domain_entities:
        return []

    caps = load_capabilities()
    learned = caps.setdefault("learned_context", {})
    entities = learned.setdefault("entities", {})
    for domain, ids in domain_entities.items():
        existing = entities.get(domain)
        if not isinstance(existing, list):
            existing = []
        for eid in ids:
            if eid not in existing:
                existing.append(eid)
        entities[domain] = existing

    hints = learned.setdefault("hints", [])
    if not isinstance(hints, list):
        hints = []
        learned["hints"] = hints

    stamp = _now_stamp()
    entry = {
        "note": note[:220],
        "tags": sorted(tags),
        "updated": stamp,
    }
    if not any((h.get("note") == entry["note"] and h.get("tags") == entry["tags"]) for h in hints[-80:] if isinstance(h, dict)):
        hints.append(entry)

    if len(hints) > 120:
        learned["hints"] = hints[-120:]

    save_capabilities(caps)
    return sorted(tags)


def _context_tags_from_note(note: str) -> Tuple[List[str], Dict[str, List[str]]]:
    text = (note or "").lower()
    tags = set()
    for tag, keywords in CONTEXT_TAG_KEYWORDS.items():
        if any(k in text for k in keywords):
            tags.add(tag)

    entity_ids = sorted(set(ENTITY_ID_RE.findall(note or "")))
    domain_entities: Dict[str, List[str]] = {}
    for eid in entity_ids:
        domain = eid.split(".", 1)[0].lower()
        if domain in ("todo", "calendar"):
            tags.add(domain)
            domain_entities.setdefault(domain, [])
            if eid not in domain_entities[domain]:
                domain_entities[domain].append(eid)

    return sorted(tags), domain_entities


def preview_capabilities_note(note: str) -> Dict[str, Any]:
    entities = sorted(set(ENTITY_ID_RE.findall(note or "")))
    scripts = sorted({s.lower() for s in SCRIPT_ID_RE.findall(note or "")})
    tags, domain_entities = _context_tags_from_note(note or "")
    return {
        "entities": entities,
        "scripts": scripts,
        "tags": tags,
        "domain_entities": domain_entities,
    }


def _append_general_kb_note(note: str) -> List[str]:
    caps = load_capabilities()
    user_ctx = caps.setdefault("user_context", {})
    notes = user_ctx.get("notes")
    if not isinstance(notes, list):
        notes = []
        user_ctx["notes"] = notes

    stamp = _now_stamp()
    entry = {"note": (note or "")[:500], "updated": stamp}
    notes.append(entry)
    if len(notes) > 200:
        user_ctx["notes"] = notes[-200:]
    save_capabilities(caps)
    return [entry["note"]]


def commit_capabilities_note(note: str) -> Tuple[List[str], List[str], List[str], List[str]]:
    updated_entities = update_capabilities_entity_hints(note)
    updated_scripts = update_capabilities_script_hints(note)
    updated_context = update_capabilities_context_hints(note)
    saved_notes: List[str] = []
    if not updated_entities and not updated_scripts and not updated_context:
        saved_notes = _append_general_kb_note(note)
    return updated_entities, updated_scripts, updated_context, saved_notes


def save_learned_from_history(history: List[Dict[str, Any]], extra_note: Optional[str] = None) -> Dict[str, List[str]]:
    seen: set = set()
    saved_entities: set = set()
    saved_scripts: set = set()
    saved_context: set = set()
    saved_notes: set = set()

    def _commit(note: str) -> None:
        s = (note or "").strip()
        if not s or s in seen:
            return
        seen.add(s)
        ents, scripts, ctx, notes = commit_capabilities_note(s)
        saved_entities.update(ents or [])
        saved_scripts.update(scripts or [])
        saved_context.update(ctx or [])
        saved_notes.update(notes or [])

    for msg in history or []:
        if not isinstance(msg, dict):
            continue
        if (msg.get("role") or "").lower() != "user":
            continue
        _commit(str(msg.get("text") or msg.get("content") or ""))

    if extra_note:
        _commit(extra_note)

    return {
        "saved_entities": sorted(saved_entities),
        "saved_scripts": sorted(saved_scripts),
        "saved_context": sorted(saved_context),
        "saved_notes": sorted(saved_notes),
    }

def _script_entity_id(script_id: str) -> str:
    sid = str(script_id or "").strip()
    if not sid:
        return ""
    if "." in sid:
        return sid
    return f"script.{sid}"


def _collect_entity_ids(obj: Any, out: set) -> None:
    if isinstance(obj, dict):
        for value in obj.values():
            if isinstance(value, str):
                out.update(ENTITY_ID_RE.findall(value))
            else:
                _collect_entity_ids(value, out)
        return
    if isinstance(obj, list):
        for item in obj:
            _collect_entity_ids(item, out)

def _collect_service_names(obj: Any, out: set) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in ("service", "service_template") and isinstance(value, str):
                out.add(value.strip())
                continue
            _collect_service_names(value, out)
        return
    if isinstance(obj, list):
        for item in obj:
            _collect_service_names(item, out)


def _coerce_yaml_dict(yaml_text: str) -> Optional[Dict[str, Any]]:
    try:
        obj = _yaml_load(yaml_text)
    except Exception:
        return None
    if isinstance(obj, list):
        obj = next((x for x in obj if isinstance(x, dict)), None)
    if isinstance(obj, dict) and isinstance(obj.get("automation"), list):
        obj = next((x for x in obj.get("automation") if isinstance(x, dict)), obj)
    return obj if isinstance(obj, dict) else None


def _build_capabilities_inventory(
    entity_registry: List[Dict[str, Any]],
    device_registry: List[Dict[str, Any]],
    area_registry: List[Dict[str, Any]],
    states: List[Dict[str, Any]],
    automations: List[Dict[str, Any]],
    scripts: Dict[str, Any],
    services: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    area_name_by_id = {
        a.get("area_id"): a.get("name")
        for a in area_registry
        if isinstance(a, dict)
    }
    device_by_id = {
        d.get("id"): d
        for d in device_registry
        if isinstance(d, dict)
    }
    state_by_entity = {
        s.get("entity_id"): s
        for s in states
        if isinstance(s, dict) and s.get("entity_id")
    }

    entities: List[Dict[str, Any]] = []
    seen = set()
    for e in entity_registry:
        if not isinstance(e, dict):
            continue
        entity_id = e.get("entity_id")
        if not entity_id:
            continue
        seen.add(entity_id)
        st = state_by_entity.get(entity_id, {})
        attrs = st.get("attributes") or {}
        name = attrs.get("friendly_name") or e.get("name") or entity_id
        dev = device_by_id.get(e.get("device_id")) if e.get("device_id") else None
        area = ""
        if dev:
            area = area_name_by_id.get(dev.get("area_id")) or ""
        device_class = attrs.get("device_class") or ""
        entry = {
            "entity_id": entity_id,
            "name": name,
            "domain": entity_id.split(".")[0],
        }
        if area:
            entry["area"] = area
        if device_class:
            entry["device_class"] = device_class
        entities.append(entry)

    for st in states:
        if not isinstance(st, dict):
            continue
        entity_id = st.get("entity_id")
        if not entity_id or entity_id in seen:
            continue
        attrs = st.get("attributes") or {}
        name = attrs.get("friendly_name") or entity_id
        device_class = attrs.get("device_class") or ""
        entry = {
            "entity_id": entity_id,
            "name": name,
            "domain": entity_id.split(".")[0],
        }
        if device_class:
            entry["device_class"] = device_class
        entities.append(entry)

    entities.sort(key=lambda x: x.get("entity_id") or "")

    areas: List[Dict[str, Any]] = []
    for area in area_registry:
        if not isinstance(area, dict):
            continue
        area_id = area.get("area_id")
        name = area.get("name")
        if not area_id and not name:
            continue
        entry: Dict[str, Any] = {}
        if area_id:
            entry["area_id"] = area_id
        if name:
            entry["name"] = name
        areas.append(entry)
    areas.sort(key=lambda x: x.get("name") or "")

    scripts_out: List[Dict[str, Any]] = []
    for sid, cfg in (scripts or {}).items():
        entity_id = _script_entity_id(sid)
        if not entity_id:
            continue
        entry: Dict[str, Any] = {"entity_id": entity_id}
        if isinstance(cfg, dict):
            alias = cfg.get("alias")
            if alias:
                entry["alias"] = alias
            desc = cfg.get("description")
            if desc:
                entry["description"] = desc
        scripts_out.append(entry)
    scripts_out.sort(key=lambda x: x.get("entity_id") or "")

    automations_out: List[Dict[str, Any]] = []
    for a in automations:
        if not isinstance(a, dict):
            continue
        aid = str(a.get("id") or "").strip()
        if not aid:
            continue
        entry: Dict[str, Any] = {"id": aid}
        alias = a.get("alias")
        if alias:
            entry["alias"] = alias
        desc = a.get("description")
        if desc:
            entry["description"] = desc
        automations_out.append(entry)
    automations_out.sort(key=lambda x: x.get("alias") or x.get("id") or "")

    used_entities: set = set()
    for a in automations:
        _collect_entity_ids(a, used_entities)
    for sid, cfg in (scripts or {}).items():
        _collect_entity_ids(cfg, used_entities)
        ent_id = _script_entity_id(sid)
        if ent_id:
            used_entities.add(ent_id)

    used_list = sorted(used_entities)

    services_out: List[str] = []
    services_by_domain: Dict[str, List[str]] = {}
    if services:
        for item in services:
            if not isinstance(item, dict):
                continue
            domain = item.get("domain")
            service_map = item.get("services") or {}
            if not domain or not isinstance(service_map, dict):
                continue
            names = []
            for svc in service_map.keys():
                if not svc:
                    continue
                names.append(str(svc))
                services_out.append(f"{domain}.{svc}")
            if names:
                services_by_domain[domain] = sorted(set(names))
    services_out = sorted(set(services_out))

    return {
        "updated": _now_stamp(),
        "counts": {
            "areas": len(areas),
            "entities": len(entities),
            "scripts": len(scripts_out),
            "automations": len(automations_out),
            "used_entities": len(used_list),
            "services": len(services_out),
        },
        "areas": areas,
        "entities": entities,
        "scripts": scripts_out,
        "automations": automations_out,
        "used_entities": used_list,
        "services": services_out,
        "services_by_domain": services_by_domain,
    }


def _parse_time_value(value: Optional[str]) -> Optional[datetime.time]:
    if not value:
        return None
    try:
        parts = str(value).strip().split(":")
        if len(parts) < 2:
            return None
        hh = int(parts[0])
        mm = int(parts[1])
        ss = int(parts[2]) if len(parts) > 2 else 0
        return datetime.time(hour=hh, minute=mm, second=ss)
    except Exception:
        return None

def _parse_offset(value: Optional[str]) -> Optional[datetime.timedelta]:
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    sign = -1 if s.startswith("-") else 1
    if s[0] in "+-":
        s = s[1:]
    parts = s.split(":")
    if len(parts) < 2:
        return None
    try:
        hh = int(parts[0])
        mm = int(parts[1])
        ss = int(parts[2]) if len(parts) > 2 else 0
        return datetime.timedelta(seconds=sign * (hh * 3600 + mm * 60 + ss))
    except Exception:
        return None

def _parse_datetime(value: Optional[str]) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        v = str(value).strip()
        if v.endswith("Z"):
            v = v[:-1] + "+00:00"
        return datetime.datetime.fromisoformat(v)
    except Exception:
        return None

def _parse_date_value(value: Optional[str]) -> Optional[datetime.date]:
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(str(value).strip())
    except Exception:
        return None

def _parse_weekday_list(value: Any) -> List[int]:
    if not value:
        return []
    items = value if isinstance(value, list) else [value]
    mapping = {
        "mon": 0, "monday": 0,
        "tue": 1, "tues": 1, "tuesday": 1,
        "wed": 2, "weds": 2, "wednesday": 2,
        "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
        "fri": 4, "friday": 4,
        "sat": 5, "saturday": 5,
        "sun": 6, "sunday": 6,
    }
    out: List[int] = []
    for item in items:
        s = str(item).strip().lower()
        if not s:
            continue
        if s in mapping:
            out.append(mapping[s])
    return sorted(set(out))

def _device_expected_state(cond: Dict[str, Any]) -> Optional[str]:
    type_val = str(cond.get("type") or "").strip().lower()
    mapping = {
        "is_on": "on",
        "is_off": "off",
        "is_open": "open",
        "is_closed": "closed",
        "is_locked": "locked",
        "is_unlocked": "unlocked",
        "is_home": "home",
        "is_not_home": "not_home",
        "is_playing": "playing",
        "is_paused": "paused",
        "is_problem": "problem",
        "is_clear": "clear",
    }
    if type_val in mapping:
        return mapping[type_val]
    state_val = cond.get("state")
    if state_val is not None:
        return str(state_val).strip().lower()
    return None

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c

def _simple_slug(value: str) -> str:
    if not value:
        return ""
    s = re.sub(r"[^a-z0-9_]+", "_", value.lower())
    return s.strip("_")

def _resolve_zone_entity(zone_value: Any, state_map: Dict[str, Dict[str, Any]]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    if not zone_value:
        return None, None
    zone_id = str(zone_value).strip()
    if zone_id in state_map:
        return zone_id, state_map.get(zone_id)
    if not zone_id:
        return None, None
    if not zone_id.startswith("zone."):
        candidate = f"zone.{_simple_slug(zone_id)}"
        if candidate in state_map:
            return candidate, state_map.get(candidate)
    target = zone_id.lower()
    for eid, st in state_map.items():
        if not isinstance(eid, str) or not eid.startswith("zone."):
            continue
        attrs = st.get("attributes") or {}
        name = str(attrs.get("friendly_name") or "").lower()
        if name and name == target:
            return eid, st
    return None, None

def _entity_in_zone(entity_id: str, zone_value: Any, ctx: Dict[str, Any]) -> Optional[bool]:
    state_map = ctx.get("states") or {}
    overrides = ctx.get("overrides") or {}
    state_val, attrs = _get_state_value(str(entity_id), state_map, overrides)

    zone_id, zone_state = _resolve_zone_entity(zone_value, state_map)
    zone_attrs = zone_state.get("attributes") if isinstance(zone_state, dict) else {}

    try:
        lat = attrs.get("latitude") if isinstance(attrs, dict) else None
        lon = attrs.get("longitude") if isinstance(attrs, dict) else None
        zlat = zone_attrs.get("latitude") if isinstance(zone_attrs, dict) else None
        zlon = zone_attrs.get("longitude") if isinstance(zone_attrs, dict) else None
        radius = zone_attrs.get("radius") if isinstance(zone_attrs, dict) else None
        if lat is not None and lon is not None and zlat is not None and zlon is not None and radius is not None:
            dist = _haversine_m(float(lat), float(lon), float(zlat), float(zlon))
            return dist <= float(radius)
    except Exception:
        pass

    if state_val is None:
        return None
    sval = str(state_val).strip().lower()
    if sval in ("unknown", "unavailable", ""):
        return None
    zone_name = str(zone_attrs.get("friendly_name") or "").lower() if isinstance(zone_attrs, dict) else ""
    zone_value_str = str(zone_value).strip().lower()
    if zone_id and sval == zone_id.lower():
        return True
    if zone_id:
        zone_slug = zone_id.split(".", 1)[-1].lower()
        if sval == zone_slug:
            return True
    if zone_name and sval == zone_name:
        return True
    if zone_value_str:
        if sval == zone_value_str:
            return True
        if zone_value_str.startswith("zone.") and sval == zone_value_str.split(".", 1)[-1]:
            return True
    if zone_id or zone_name:
        return False
    return None

def _sun_times_for_today(ctx: Dict[str, Any]) -> Optional[Dict[str, datetime.datetime]]:
    now_dt = ctx.get("now_dt")
    if not isinstance(now_dt, datetime.datetime):
        return None
    st = (ctx.get("states") or {}).get("sun.sun") or {}
    attrs = st.get("attributes") or {}
    next_rising = _parse_datetime(attrs.get("next_rising"))
    next_setting = _parse_datetime(attrs.get("next_setting"))
    if not next_rising or not next_setting:
        return None
    sun_state = str(st.get("state") or "").lower()
    if sun_state == "above_horizon":
        sunrise = next_rising - datetime.timedelta(days=1)
        sunset = next_setting
    else:
        if next_rising.date() == now_dt.date():
            sunrise = next_rising
            sunset = next_setting
        else:
            sunrise = next_rising - datetime.timedelta(days=1)
            sunset = next_setting - datetime.timedelta(days=1)
    return {"sunrise": sunrise, "sunset": sunset}

def _compare_vals(left: Any, op: str, right: Any) -> Optional[bool]:
    try:
        if op in ("==", "!="):
            if left is None:
                return (right is None) if op == "==" else (right is not None)
            return (str(left) == str(right)) if op == "==" else (str(left) != str(right))
        left_num = float(left)
        right_num = float(right)
        if op == ">":
            return left_num > right_num
        if op == ">=":
            return left_num >= right_num
        if op == "<":
            return left_num < right_num
        if op == "<=":
            return left_num <= right_num
    except Exception:
        return None
    return None

def _eval_template_expr(template: str, ctx: Dict[str, Any]) -> Optional[bool]:
    if not template:
        return None
    expr = template.strip()
    if expr.startswith("{{") and expr.endswith("}}"):
        expr = expr[2:-2].strip()

    m = re.search(r"is_state\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)", expr)
    if m:
        entity_id, expected = m.group(1), m.group(2)
        state_val, _ = _get_state_value(entity_id, ctx["states"], ctx["overrides"])
        return str(state_val) == expected

    m = re.search(r"is_state_attr\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)", expr)
    if m:
        entity_id, attr, expected = m.group(1), m.group(2), m.group(3)
        state_val, attrs = _get_state_value(entity_id, ctx["states"], ctx["overrides"])
        val = attrs.get(attr)
        return str(val) == expected

    m = re.search(r"states\(\s*['\"]([^'\"]+)['\"]\s*\)\s*([=!<>]=|[<>])\s*['\"]?([^'\" ]+)['\"]?", expr)
    if m:
        entity_id, op, expected = m.group(1), m.group(2), m.group(3)
        state_val, _ = _get_state_value(entity_id, ctx["states"], ctx["overrides"])
        return _compare_vals(state_val, op, expected)

    m = re.search(r"state_attr\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)\s*([=!<>]=|[<>])\s*['\"]?([^'\" ]+)['\"]?", expr)
    if m:
        entity_id, attr, op, expected = m.group(1), m.group(2), m.group(3), m.group(4)
        state_val, attrs = _get_state_value(entity_id, ctx["states"], ctx["overrides"])
        val = attrs.get(attr)
        return _compare_vals(val, op, expected)

    return None


def _get_state_value(entity_id: str, state_map: Dict[str, Dict[str, Any]], overrides: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, Any]]:
    if entity_id in overrides:
        return str(overrides.get(entity_id)), {}
    st = state_map.get(entity_id) or {}
    attrs = st.get("attributes") or {}
    return st.get("state"), attrs


def _eval_condition(cond: Any, ctx: Dict[str, Any], logs: List[str], path: str = "") -> Tuple[Optional[bool], str]:
    if not isinstance(cond, dict):
        return None, f"{path}unsupported condition"

    cond_type = (cond.get("condition") or "").strip().lower()
    if not cond_type:
        cond_type = (cond.get("platform") or "").strip().lower()

    if cond_type in ("and", "or"):
        items = cond.get("conditions") if isinstance(cond.get("conditions"), list) else []
        results: List[Tuple[Optional[bool], str]] = []
        for idx, item in enumerate(items):
            results.append(_eval_condition(item, ctx, logs, f"{path}{cond_type}[{idx}]: "))
        trues = [r for r in results if r[0] is True]
        falses = [r for r in results if r[0] is False]
        unknowns = [r for r in results if r[0] is None]
        if cond_type == "and":
            if falses:
                return False, f"{path}and failed"
            if unknowns and not trues:
                return None, f"{path}and unknown"
            return True, f"{path}and passed"
        if trues:
            return True, f"{path}or passed"
        if unknowns:
            return None, f"{path}or unknown"
        return False, f"{path}or failed"

    if cond_type == "not":
        items = cond.get("conditions") if isinstance(cond.get("conditions"), list) else []
        if not items:
            return None, f"{path}not missing conditions"
        inner, msg = _eval_condition(items[0], ctx, logs, f"{path}not: ")
        if inner is None:
            return None, f"{path}not unknown"
        return (not inner), f"{path}not {'passed' if not inner else 'failed'}"

    if cond_type == "state":
        entity_ids = cond.get("entity_id")
        target = cond.get("state")
        match = (cond.get("match") or "any").lower()
        if not entity_ids:
            return None, f"{path}state missing entity_id"
        ids = entity_ids if isinstance(entity_ids, list) else [entity_ids]
        targets = target if isinstance(target, list) else [target]
        passed = 0
        checked = 0
        for eid in ids:
            state_val, _ = _get_state_value(str(eid), ctx["states"], ctx["overrides"])
            checked += 1
            if state_val in targets:
                passed += 1
        if match == "all":
            return (passed == checked), f"{path}state {'passed' if passed == checked else 'failed'}"
        return (passed > 0), f"{path}state {'passed' if passed > 0 else 'failed'}"

    if cond_type == "numeric_state":
        entity_id = cond.get("entity_id")
        if not entity_id:
            return None, f"{path}numeric_state missing entity_id"
        state_val, attrs = _get_state_value(str(entity_id), ctx["states"], ctx["overrides"])
        attr = cond.get("attribute")
        raw_val = attrs.get(attr) if attr else state_val
        try:
            num = float(raw_val)
        except Exception:
            return None, f"{path}numeric_state non-numeric"
        above = cond.get("above")
        below = cond.get("below")
        if above is not None and num <= float(above):
            return False, f"{path}numeric_state failed (<= above)"
        if below is not None and num >= float(below):
            return False, f"{path}numeric_state failed (>= below)"
        return True, f"{path}numeric_state passed"

    if cond_type == "template":
        template = cond.get("value_template") or cond.get("template")
        if not template:
            return None, f"{path}template missing value_template"
        res = _eval_template_expr(str(template), ctx)
        if res is None:
            return None, f"{path}template unknown"
        return res, f"{path}template {'passed' if res else 'failed'}"

    if cond_type == "time":
        now = ctx.get("now")
        now_dt = ctx.get("now_dt")
        if not isinstance(now, datetime.time) and not isinstance(now_dt, datetime.datetime):
            return None, f"{path}time missing now"

        weekdays = _parse_weekday_list(cond.get("weekday"))
        ok = True
        if weekdays:
            if not isinstance(now_dt, datetime.datetime):
                return None, f"{path}time missing date for weekday"
            if now_dt.weekday() not in weekdays:
                ok = False

        after_val = cond.get("after")
        before_val = cond.get("before")

        after_dt = _parse_datetime(after_val) if after_val else None
        before_dt = _parse_datetime(before_val) if before_val else None
        after_date = _parse_date_value(after_val) if (after_val and not after_dt) else None
        before_date = _parse_date_value(before_val) if (before_val and not before_dt) else None
        after_t = _parse_time_value(after_val) if (after_val and not after_dt and not after_date) else None
        before_t = _parse_time_value(before_val) if (before_val and not before_dt and not before_date) else None

        if after_dt or before_dt:
            if not isinstance(now_dt, datetime.datetime):
                return None, f"{path}time missing datetime"
            if after_dt and now_dt < after_dt:
                ok = False
            if before_dt and now_dt > before_dt:
                ok = False

        if after_date or before_date:
            if not isinstance(now_dt, datetime.datetime):
                return None, f"{path}time missing date"
            today = now_dt.date()
            if after_date and today < after_date:
                ok = False
            if before_date and today > before_date:
                ok = False

        if after_t or before_t:
            if not isinstance(now, datetime.time):
                return None, f"{path}time missing clock"
            if after_t and before_t:
                if after_t <= before_t:
                    if now < after_t or now > before_t:
                        ok = False
                else:
                    if not (now >= after_t or now <= before_t):
                        ok = False
            else:
                if after_t and now <= after_t:
                    ok = False
                if before_t and now >= before_t:
                    ok = False

        if not any([weekdays, after_dt, before_dt, after_date, before_date, after_t, before_t]):
            return None, f"{path}time missing constraints"
        return ok, f"{path}time {'passed' if ok else 'failed'}"

    if cond_type == "trigger":
        ids = cond.get("id") or cond.get("ids") or cond.get("trigger_id")
        if not ids:
            return None, f"{path}trigger missing id"
        id_list = ids if isinstance(ids, list) else [ids]
        trigger_id = ctx.get("trigger_id")
        if not trigger_id and isinstance(ctx.get("trigger"), dict):
            trigger_id = ctx["trigger"].get("id")
        if not trigger_id:
            return None, f"{path}trigger unknown"
        ok = str(trigger_id) in [str(x) for x in id_list]
        return ok, f"{path}trigger {'passed' if ok else 'failed'}"

    if cond_type == "device":
        entity_id = cond.get("entity_id")
        if not entity_id:
            return None, f"{path}device missing entity_id"
        expected = _device_expected_state(cond)
        if not expected:
            return None, f"{path}device unknown type"
        state_val, _ = _get_state_value(str(entity_id), ctx["states"], ctx["overrides"])
        if state_val is None:
            return None, f"{path}device missing state"
        ok = str(state_val).strip().lower() == expected
        return ok, f"{path}device {'passed' if ok else 'failed'}"

    if cond_type == "calendar":
        entity_id = cond.get("entity_id")
        if not entity_id:
            return None, f"{path}calendar missing entity_id"
        state_val, attrs = _get_state_value(str(entity_id), ctx["states"], ctx["overrides"])
        expected = cond.get("state")
        if expected is not None:
            ok = str(state_val) == str(expected)
            return ok, f"{path}calendar {'passed' if ok else 'failed'}"
        if state_val is None:
            return None, f"{path}calendar missing state"
        sval = str(state_val).strip().lower()
        if sval in ("on", "off"):
            return (sval == "on"), f"{path}calendar {'passed' if sval == 'on' else 'failed'}"
        if isinstance(attrs, dict):
            start = _parse_datetime(attrs.get("start_time") or attrs.get("start"))
            end = _parse_datetime(attrs.get("end_time") or attrs.get("end"))
            now_dt = ctx.get("now_dt")
            if start and end and isinstance(now_dt, datetime.datetime):
                ok = start <= now_dt <= end
                return ok, f"{path}calendar {'passed' if ok else 'failed'}"
        return None, f"{path}calendar unknown"

    if cond_type == "sun":
        now_dt = ctx.get("now_dt")
        if not isinstance(now_dt, datetime.datetime):
            return None, f"{path}sun missing now"
        sun_times = _sun_times_for_today(ctx)
        if not sun_times:
            return None, f"{path}sun missing sun.sun"
        after_key = str(cond.get("after") or "").strip().lower()
        before_key = str(cond.get("before") or "").strip().lower()
        after_dt = sun_times.get(after_key)
        before_dt = sun_times.get(before_key)
        after_offset = _parse_offset(cond.get("after_offset"))
        before_offset = _parse_offset(cond.get("before_offset"))
        if after_dt and after_offset:
            after_dt = after_dt + after_offset
        if before_dt and before_offset:
            before_dt = before_dt + before_offset
        if not after_dt and not before_dt:
            return None, f"{path}sun missing before/after"
        if after_dt and before_dt:
            if after_dt <= before_dt:
                ok = after_dt <= now_dt <= before_dt
            else:
                ok = now_dt >= after_dt or now_dt <= before_dt
        elif after_dt:
            ok = now_dt >= after_dt
        else:
            ok = now_dt <= before_dt
        return ok, f"{path}sun {'passed' if ok else 'failed'}"

    if cond_type == "zone":
        entity_ids = cond.get("entity_id")
        zone_val = cond.get("zone")
        if not entity_ids or not zone_val:
            return None, f"{path}zone missing entity_id or zone"
        ids = entity_ids if isinstance(entity_ids, list) else [entity_ids]
        zones = zone_val if isinstance(zone_val, list) else [zone_val]
        match = (cond.get("match") or "any").lower()
        unknown = False
        if match == "all":
            for eid in ids:
                entity_ok = False
                entity_unknown = False
                for z in zones:
                    res = _entity_in_zone(str(eid), z, ctx)
                    if res is True:
                        entity_ok = True
                        break
                    if res is None:
                        entity_unknown = True
                if not entity_ok:
                    if entity_unknown:
                        unknown = True
                        continue
                    return False, f"{path}zone failed"
            if unknown:
                return None, f"{path}zone unknown"
            return True, f"{path}zone passed"
        for eid in ids:
            entity_unknown = False
            for z in zones:
                res = _entity_in_zone(str(eid), z, ctx)
                if res is True:
                    return True, f"{path}zone passed"
                if res is None:
                    entity_unknown = True
            if entity_unknown:
                unknown = True
        if unknown:
            return None, f"{path}zone unknown"
        return False, f"{path}zone failed"

    return None, f"{path}unsupported condition '{cond_type}'"


def _eval_conditions_list(conditions: Any, ctx: Dict[str, Any], logs: List[str]) -> Tuple[bool, bool]:
    if not conditions:
        logs.append("Conditions: none")
        return True, False
    items = conditions if isinstance(conditions, list) else [conditions]
    unknown = False
    for idx, cond in enumerate(items):
        res, msg = _eval_condition(cond, ctx, logs, f"cond[{idx}]: ")
        logs.append(msg)
        if res is False:
            return False, False
        if res is None:
            unknown = True
    return True, unknown


def _summarize_action(action: Any) -> str:
    if not isinstance(action, dict):
        return "action: (unknown)"
    if "service" in action:
        service = action.get("service")
        target = action.get("target") or {}
        entity_id = target.get("entity_id") if isinstance(target, dict) else None
        if entity_id:
            return f"service {service} -> {entity_id}"
        return f"service {service}"
    if "choose" in action:
        choices = action.get("choose") or []
        return f"choose ({len(choices)} options)"
    if "delay" in action:
        return f"delay {action.get('delay')}"
    if "wait_for_trigger" in action:
        return "wait_for_trigger"
    if "wait_template" in action:
        return "wait_template"
    return "action: (unknown)"


def _simulate_actions(actions: Any, ctx: Dict[str, Any], logs: List[str]) -> List[str]:
    out: List[str] = []
    items = actions if isinstance(actions, list) else []
    for idx, action in enumerate(items):
        if isinstance(action, dict) and "choose" in action:
            choices = action.get("choose") or []
            matched = False
            for c_idx, choice in enumerate(choices):
                conds = choice.get("conditions") if isinstance(choice, dict) else None
                ok, unknown = _eval_conditions_list(conds, ctx, logs)
                if ok and not unknown:
                    logs.append(f"choose[{idx}] -> option {c_idx + 1}")
                    out.extend(_simulate_actions(choice.get("sequence") or [], ctx, logs))
                    matched = True
                    break
                if unknown:
                    logs.append(f"choose[{idx}] -> option {c_idx + 1} unknown")
            if not matched:
                default_seq = action.get("default") or []
                if default_seq:
                    logs.append(f"choose[{idx}] -> default")
                    out.extend(_simulate_actions(default_seq, ctx, logs))
            continue
        out.append(_summarize_action(action))
    return out


def normalize_area_name(area_name: Optional[str], capabilities: Dict[str, Any]) -> Optional[str]:
    if not area_name:
        return area_name
    aliases = ((capabilities.get("lights") or {}).get("area_aliases") or {})
    if area_name in aliases:
        return aliases[area_name]
    for k, v in aliases.items():
        if k.lower() == area_name.lower():
            return v
    return area_name


def _collect_alias_prefixes(capabilities: Dict[str, Any]) -> List[str]:
    conventions = capabilities.get("conventions") or {}
    prefixes: List[str] = []
    for key in (
        "automation_alias_prefix",
        "automation_alias_prefix_create",
        "automation_alias_prefix_edit",
        "script_alias_prefix",
        "script_alias_prefix_create",
        "script_alias_prefix_edit",
    ):
        val = conventions.get(key)
        if isinstance(val, str) and val.strip():
            prefixes.append(val.strip())
    prefixes.extend([
        "AUTO AI GENERATED - ",
        "AUTO AI CREATED - ",
        "AUTO AI EDITED - ",
    ])
    seen = set()
    out: List[str] = []
    for p in prefixes:
        key = p.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _strip_known_prefix(alias: str, prefixes: List[str]) -> str:
    base = (alias or "").strip()
    for prefix in prefixes:
        if base.lower().startswith(prefix.lower()):
            return base[len(prefix):].lstrip()
    return base


def _ai_alias_prefix(capabilities: Dict[str, Any], mode: str, entity_type: str) -> str:
    conventions = capabilities.get("conventions") or {}
    mode_key = "edit" if mode == "edit" else "create"
    prefix = None
    if entity_type == "script":
        if mode_key == "edit":
            prefix = conventions.get("script_alias_prefix_edit")
        else:
            prefix = conventions.get("script_alias_prefix_create")
        if not prefix:
            prefix = conventions.get("script_alias_prefix")
    else:
        if mode_key == "edit":
            prefix = conventions.get("automation_alias_prefix_edit")
        else:
            prefix = conventions.get("automation_alias_prefix_create")
        if not prefix:
            prefix = conventions.get("automation_alias_prefix")
    if prefix and str(prefix).strip().lower() == "auto ai generated -":
        prefix = None
    if not prefix:
        prefix = "AUTO AI EDITED - " if mode_key == "edit" else "AUTO AI CREATED - "
    prefix = str(prefix)
    if prefix and not prefix.endswith(" "):
        prefix = prefix + " "
    return prefix


def apply_ai_alias_prefix(
    alias: str,
    capabilities: Dict[str, Any],
    *,
    mode: str = "create",
    entity_type: str = "automation"
) -> str:
    base = _strip_known_prefix(alias, _collect_alias_prefixes(capabilities))
    if not base:
        base = "AI Script" if entity_type == "script" else "AI Automation"
    prefix = _ai_alias_prefix(capabilities, mode, entity_type)
    if prefix and base.lower().startswith(prefix.lower()):
        return base
    return f"{prefix}{base}".strip()


def enforce_alias_prefix(alias: str, capabilities: Dict[str, Any], mode: str = "create", entity_type: str = "automation") -> str:
    return apply_ai_alias_prefix(alias, capabilities, mode=mode, entity_type=entity_type)


def slim_capabilities_for_llm(capabilities: Dict[str, Any]) -> Dict[str, Any]:
    lights = capabilities.get("lights") or {}
    speech = capabilities.get("speech") or {}
    covers = capabilities.get("covers") or {}
    conventions = capabilities.get("conventions") or {}
    heating = capabilities.get("heating") or {}
    notifications = capabilities.get("notifications") or {}
    media = capabilities.get("media") or {}
    presence = capabilities.get("presence") or {}
    language = capabilities.get("language") or {}
    learned = capabilities.get("learned_context") or {}

    scripts = capabilities.get("scripts") or []
    slim_scripts = []
    if isinstance(scripts, list):
        for s in scripts[:120]:
            if isinstance(s, dict) and s.get("entity_id"):
                entry = {"entity_id": s.get("entity_id"), "fields": s.get("fields") or {}}
                if s.get("purpose"):
                    entry["purpose"] = s.get("purpose")
                slim_scripts.append(entry)

    return {
        "language": language,
        "conventions": {
            "automation_alias_prefix": conventions.get("automation_alias_prefix", "AUTO AI GENERATED - "),
            "prefer_groups_over_areas": conventions.get("prefer_groups_over_areas", True),
        },
        "lights": {
            "prefer_groups": lights.get("prefer_groups", True),
            "area_aliases": lights.get("area_aliases") or {},
            "area_group_map": lights.get("area_group_map") or {},
        },
        "speech": speech,
        "notifications": {
            "primary_phone_notify": notifications.get("primary_phone_notify"),
            "keep_notify_prefixes": notifications.get("keep_notify_prefixes") or ["notify.mobile_app_"],
            "actionable_event_type": notifications.get("actionable_event_type", "mobile_app_notification_action"),
        },
        "presence": presence,
        "media": media,
        "covers": covers,
        "heating": heating,
        "scripts": slim_scripts,
        "learned_context": {
            "entities": {
                "todo": ((learned.get("entities") or {}).get("todo") or [])[:30],
                "calendar": ((learned.get("entities") or {}).get("calendar") or [])[:30],
            },
            "hints": [
                {
                    "note": h.get("note"),
                    "tags": h.get("tags"),
                    "updated": h.get("updated"),
                }
                for h in (learned.get("hints") or [])[-40:]
                if isinstance(h, dict) and h.get("note")
            ],
        },
    }


def _looks_like_bad_builder_output(speech: str) -> bool:
    s = (speech or "").strip()
    if not s:
        return True
    return any(p.lower() in s.lower() for p in BUILDER_BAD_OUTPUT_PATTERNS)


def _extract_json_object(text: str) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    return m.group(0) if m else None


_SUMMARY_CACHE: Optional[Dict[str, Any]] = None

def _summary_cache_path() -> Path:
    return (BASE_DIR / SUMMARY_CACHE_FILE).resolve()

def _load_summary_cache() -> Dict[str, Any]:
    global _SUMMARY_CACHE
    if _SUMMARY_CACHE is not None:
        return _SUMMARY_CACHE
    path = _summary_cache_path()
    if not path.exists():
        _SUMMARY_CACHE = {}
        return _SUMMARY_CACHE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        _SUMMARY_CACHE = data if isinstance(data, dict) else {}
    except Exception:
        _SUMMARY_CACHE = {}
    return _SUMMARY_CACHE

def _save_summary_cache(cache: Dict[str, Any]) -> None:
    try:
        path = _summary_cache_path()
        path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

def _yaml_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _runtime_config_path() -> Path:
    return (BASE_DIR / RUNTIME_CONFIG_FILE).resolve()

def _load_runtime_config() -> Dict[str, Any]:
    path = _runtime_config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _save_runtime_config(cfg: Dict[str, Any]) -> None:
    try:
        path = _runtime_config_path()
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

def _apply_runtime_config(cfg: Dict[str, Any]) -> None:
    global HELPER_MIN_CONFIDENCE, ALLOW_AI_DIFF
    if not isinstance(cfg, dict):
        return
    if "helper_min_confidence" in cfg:
        try:
            val = float(cfg.get("helper_min_confidence"))
            if 0 <= val <= 1:
                HELPER_MIN_CONFIDENCE = val
        except Exception:
            pass
    if "allow_ai_diff" in cfg:
        ALLOW_AI_DIFF = bool(cfg.get("allow_ai_diff"))


_apply_runtime_config(_load_runtime_config())

def _summary_confident(summary: Dict[str, Any]) -> bool:
    if not isinstance(summary, dict):
        return False
    conf = summary.get("confidence")
    try:
        return float(conf) >= HELPER_MIN_CONFIDENCE
    except Exception:
        return False

def _get_cached_summary(yaml_text: str) -> Optional[Dict[str, Any]]:
    if not yaml_text:
        return None
    cache = _load_summary_cache()
    key = _yaml_hash(yaml_text)
    entry = cache.get(key) or {}
    summary = entry.get("summary") if isinstance(entry, dict) else None
    if isinstance(summary, dict) and _summary_confident(summary):
        return summary
    return None

def _store_summary(yaml_text: str, summary: Dict[str, Any]) -> None:
    if not yaml_text or not isinstance(summary, dict):
        return
    cache = _load_summary_cache()
    key = _yaml_hash(yaml_text)
    cache[key] = {"summary": summary, "ts": _now_stamp()}
    if SUMMARY_CACHE_MAX and len(cache) > SUMMARY_CACHE_MAX:
        items = sorted(cache.items(), key=lambda kv: kv[1].get("ts", ""))
        for k, _ in items[:-SUMMARY_CACHE_MAX]:
            cache.pop(k, None)
    _save_summary_cache(cache)


def _call_helper_agent_json(
    agent_id: str,
    payload: Dict[str, Any],
    required_keys: Optional[List[str]] = None,
    min_confidence: Optional[float] = None
) -> Optional[Dict[str, Any]]:
    if not agent_id:
        return None
    if not (HA_URL and HA_TOKEN):
        return None
    try:
        text = (
            "Return ONLY a single minified JSON object. No markdown. No commentary.\n"
            "INPUT_JSON:\n" + json.dumps(payload, ensure_ascii=False)
        )
        speech = call_conversation_agent(agent_id, text)
        data = None
        try:
            data = json.loads(speech)
        except Exception:
            extracted = _extract_json_object(speech or "")
            if extracted:
                data = json.loads(extracted)
        if not isinstance(data, dict):
            _agent_trace_record(agent_id, False, "invalid_json")
            return None
        if required_keys:
            for key in required_keys:
                if key not in data:
                    _agent_trace_record(agent_id, False, "missing_keys")
                    return None
        conf_limit = HELPER_MIN_CONFIDENCE if min_confidence is None else min_confidence
        if "confidence" in data:
            try:
                if float(data.get("confidence")) < conf_limit:
                    _agent_trace_record(agent_id, False, "low_confidence")
                    return None
            except Exception:
                _agent_trace_record(agent_id, False, "confidence_invalid")
                return None
        _agent_trace_record(agent_id, True, "")
        return data
    except Exception:
        _agent_trace_record(agent_id, False, "exception")
        return None


def _summarize_yaml_for_prompt(request_text: str, yaml_text: str) -> Optional[Dict[str, Any]]:
    if not yaml_text:
        return None
    cached = _get_cached_summary(yaml_text)
    if cached:
        return cached
    caps = slim_capabilities_for_llm(load_capabilities())
    payload = {
        "request": request_text or "",
        "current_yaml": yaml_text,
        "candidates": [],
        "capabilities": caps,
    }
    summary = _call_helper_agent_json(
        SUMMARY_AGENT_ID,
        payload,
        required_keys=["triggers", "conditions", "actions", "entities", "services"],
    )
    if summary and _summary_confident(summary):
        _store_summary(yaml_text, summary)
    return summary


def _semantic_diff_summary_ai(base_yaml: str, new_yaml: str) -> Optional[str]:
    if not SEMANTIC_DIFF_AGENT_ID:
        return None
    base = _semantic_items_from_yaml(base_yaml)
    new = _semantic_items_from_yaml(new_yaml)
    if not base or not new:
        return None
    payload = {"before_summary": base, "after_summary": new}
    out = _call_helper_agent_json(SEMANTIC_DIFF_AGENT_ID, payload, required_keys=["summary"])
    if out and isinstance(out.get("summary"), str) and out["summary"].strip():
        return out["summary"].strip()
    return None


def snapshot_automation(automation_id: str, config: Dict[str, Any], note: str = "") -> None:
    """
    Save a local version of the automation any time we update it via the UI.
    """
    try:
        ts = time.strftime("%Y%m%d-%H%M%S")
        safe_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", automation_id)
        d = VERSIONS_DIR / safe_id
        d.mkdir(parents=True, exist_ok=True)

        payload = {
            "timestamp": ts,
            "note": note,
            "automation_id": automation_id,
            "config": config,
        }
        (d / f"{ts}.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        if DEBUG:
            print("snapshot_automation failed:", repr(e))

# ----------------------------
# LOCAL STORE + BACKUPS
# ----------------------------
def _now_stamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

def _ensure_dir(path: str) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)

def _backup_write(automation_id: str, yaml_text: str, *, reason: str) -> None:
    _ensure_dir(AUTOMATIONS_VERSIONS_DIR)
    fn = f"{automation_id}__{reason}__{_now_stamp()}.yaml"
    fp = Path(AUTOMATIONS_VERSIONS_DIR) / fn
    fp.write_text(yaml_text or "", encoding="utf-8")

def _version_dir() -> Path:
    p = _resolve_path(AUTOMATIONS_VERSIONS_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p

def _sanitize_reason(reason: str) -> str:
    s = (reason or "").strip().lower()
    s = re.sub(r"[^a-z0-9_-]+", "_", s).strip("_")
    return s or "version"

VERSION_LABEL_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?$")

def _parse_version_label(label: Optional[str]) -> Optional[Tuple[int, int]]:
    if not label:
        return None
    m = VERSION_LABEL_RE.match(label.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2) or 0)

def _format_version_label(major: int, minor: int) -> str:
    if major == 1 and minor == 0:
        return "v1"
    if minor == 0:
        return f"{major}.0"
    return f"{major}.{minor}"

def _diff_line_stats(base_text: str, new_text: str) -> Tuple[int, int, int, int]:
    base_lines = (base_text or "").splitlines()
    new_lines = (new_text or "").splitlines()
    added = 0
    removed = 0
    sm = difflib.SequenceMatcher(a=base_lines, b=new_lines)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "insert":
            added += (j2 - j1)
        elif tag == "delete":
            removed += (i2 - i1)
        elif tag == "replace":
            added += (j2 - j1)
            removed += (i2 - i1)
    return added, removed, len(base_lines), len(new_lines)

def _format_diff_summary(added: int, removed: int) -> str:
    if added == 0 and removed == 0:
        return "No line changes"
    return f"+{added} / -{removed} lines"

def _as_list(val: Any) -> List[Any]:
    if not val:
        return []
    if isinstance(val, list):
        return val
    return [val]

def _summarize_trigger(t: Any) -> str:
    if not isinstance(t, dict):
        return "trigger"
    platform = (t.get("platform") or t.get("trigger") or "").strip()
    if platform == "time":
        at = t.get("at")
        if at:
            return f"time at {at}"
        after = t.get("after")
        before = t.get("before")
        return f"time after {after} before {before}".strip()
    if platform == "state":
        eid = t.get("entity_id")
        to = t.get("to")
        if eid and to is not None:
            return f"state {eid} -> {to}"
        if eid:
            return f"state {eid}"
    if platform == "numeric_state":
        eid = t.get("entity_id")
        above = t.get("above")
        below = t.get("below")
        return f"numeric {eid} {above or ''}-{below or ''}".strip()
    if platform == "event":
        return f"event {t.get('event_type') or ''}".strip()
    if platform:
        return f"{platform} trigger"
    return "trigger"

def _summarize_condition(c: Any) -> str:
    if not isinstance(c, dict):
        return "condition"
    cond = (c.get("condition") or "").strip()
    if cond == "state":
        eid = c.get("entity_id")
        state = c.get("state")
        if eid and state is not None:
            return f"state {eid} = {state}"
        if eid:
            return f"state {eid}"
    if cond == "numeric_state":
        eid = c.get("entity_id")
        above = c.get("above")
        below = c.get("below")
        return f"numeric {eid} {above or ''}-{below or ''}".strip()
    if cond == "time":
        after = c.get("after")
        before = c.get("before")
        return f"time {after or ''}-{before or ''}".strip()
    if cond:
        return f"{cond} condition"
    return "condition"

def _summarize_action_diff(a: Any) -> str:
    if not isinstance(a, dict):
        return "action"
    if "service" in a:
        svc = a.get("service")
        target = a.get("target") or {}
        eid = target.get("entity_id") if isinstance(target, dict) else None
        if eid:
            return f"service {svc} -> {eid}"
        return f"service {svc}"
    if "choose" in a:
        choices = a.get("choose") or []
        return f"choose ({len(choices)} options)"
    if "delay" in a:
        return f"delay {a.get('delay')}"
    return "action"

def _semantic_items_from_yaml(yaml_text: str) -> Optional[Dict[str, List[str]]]:
    obj = _coerce_yaml_dict(yaml_text or "")
    if not obj:
        return None
    triggers = _as_list(obj.get("trigger") or obj.get("triggers"))
    conditions = _as_list(obj.get("condition") or obj.get("conditions"))
    if triggers or conditions:
        actions = _as_list(obj.get("action") or obj.get("actions"))
    else:
        actions = _as_list(obj.get("sequence") or obj.get("action") or obj.get("actions"))
    return {
        "triggers": [ _summarize_trigger(t) for t in triggers ],
        "conditions": [ _summarize_condition(c) for c in conditions ],
        "actions": [ _summarize_action_diff(a) for a in actions ],
    }

def _diff_list(base_list: List[str], new_list: List[str]) -> Tuple[List[str], List[str]]:
    base_counts: Dict[str, int] = {}
    new_counts: Dict[str, int] = {}
    for item in base_list:
        base_counts[item] = base_counts.get(item, 0) + 1
    for item in new_list:
        new_counts[item] = new_counts.get(item, 0) + 1
    added: List[str] = []
    removed: List[str] = []
    for item, count in new_counts.items():
        diff = count - base_counts.get(item, 0)
        if diff > 0:
            added.extend([item] * diff)
    for item, count in base_counts.items():
        diff = count - new_counts.get(item, 0)
        if diff > 0:
            removed.extend([item] * diff)
    return added, removed

def _semantic_diff_summary(base_yaml: str, new_yaml: str) -> Optional[str]:
    if ALLOW_AI_DIFF:
        ai_summary = _semantic_diff_summary_ai(base_yaml, new_yaml)
        if ai_summary:
            return ai_summary
    base = _semantic_items_from_yaml(base_yaml)
    new = _semantic_items_from_yaml(new_yaml)
    if not base or not new:
        return None
    t_add, t_rem = _diff_list(base["triggers"], new["triggers"])
    c_add, c_rem = _diff_list(base["conditions"], new["conditions"])
    a_add, a_rem = _diff_list(base["actions"], new["actions"])
    parts: List[str] = []
    if t_add:
        parts.append(f"Added trigger: {t_add[0]}" if len(t_add) == 1 else f"Added {len(t_add)} triggers")
    if t_rem:
        parts.append(f"Removed trigger: {t_rem[0]}" if len(t_rem) == 1 else f"Removed {len(t_rem)} triggers")
    if c_add:
        parts.append(f"Added condition: {c_add[0]}" if len(c_add) == 1 else f"Added {len(c_add)} conditions")
    if c_rem:
        parts.append(f"Removed condition: {c_rem[0]}" if len(c_rem) == 1 else f"Removed {len(c_rem)} conditions")
    if a_add and a_rem and len(a_add) == 1 and len(a_rem) == 1:
        parts.append(f"Changed action from {a_rem[0]} -> {a_add[0]}")
    else:
        if a_add:
            parts.append(f"Added action: {a_add[0]}" if len(a_add) == 1 else f"Added {len(a_add)} actions")
        if a_rem:
            parts.append(f"Removed action: {a_rem[0]}" if len(a_rem) == 1 else f"Removed {len(a_rem)} actions")
    if not parts:
        return "No semantic changes"
    return "; ".join(parts)

def _is_major_update(added: int, removed: int, base_lines: int, new_lines: int) -> bool:
    changed = added + removed
    denom = max(base_lines, new_lines, 1)
    ratio = changed / denom
    return changed >= 60 or ratio >= 0.35

def _sanitize_version_label(label: str) -> str:
    s = (label or "").strip().lower()
    s = re.sub(r"[^a-z0-9._-]+", "_", s).strip("_")
    return s or "v1"

def _version_meta_path(fp: Path) -> Path:
    return fp.with_suffix(".meta.json")

def _read_version_meta(fp: Path) -> Dict[str, Any]:
    try:
        meta_path = _version_meta_path(fp)
        if meta_path.exists():
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}
    return {}

def _write_version_meta(fp: Path, meta: Dict[str, Any]) -> None:
    try:
        meta_path = _version_meta_path(fp)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

def _parse_version_filename(name: str) -> Optional[Tuple[str, str, str]]:
    base = name[:-5] if name.endswith(".yaml") else name
    parts = base.rsplit("__", 2)
    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2]

def _resolve_version_file(automation_id: str, version_id: str) -> Tuple[Path, str, str]:
    if not version_id.endswith(".yaml"):
        version_id = version_id + ".yaml"
    parsed = _parse_version_filename(version_id)
    if not parsed or parsed[0] != automation_id:
        raise HTTPException(status_code=404, detail="Version not found")
    fp = _version_dir() / version_id
    if not fp.exists():
        raise HTTPException(status_code=404, detail="Version not found")
    return fp, parsed[1], parsed[2]

def _collect_version_entries(automation_id: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for p in _version_dir().glob("*.yaml"):
        parsed = _parse_version_filename(p.name)
        if not parsed:
            continue
        aid, reason, ts = parsed
        if aid != automation_id:
            continue
        try:
            size = p.stat().st_size
        except Exception:
            size = None
        meta = _read_version_meta(p)
        try:
            yaml_text = p.read_text(encoding="utf-8")
        except Exception:
            yaml_text = ""
        items.append({
            "id": p.name,
            "reason": reason,
            "ts": ts,
            "size": size,
            "note": (meta.get("note") or "").strip() if isinstance(meta, dict) else "",
            "meta": meta if isinstance(meta, dict) else {},
            "path": p,
            "yaml": yaml_text,
        })
    items.sort(key=lambda x: x.get("ts", ""))
    return items

def _ensure_version_labels(automation_id: str, entries: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    if entries is None:
        entries = _collect_version_entries(automation_id)
    major: Optional[int] = None
    minor: Optional[int] = None
    prev_yaml = ""
    for entry in entries:
        meta = entry.get("meta") or {}
        label = meta.get("label") if isinstance(meta, dict) else None
        parsed = _parse_version_label(label) if label else None
        changed = False
        entry_yaml = entry.get("yaml") or ""
        added, removed, base_lines, new_lines = _diff_line_stats(prev_yaml, entry_yaml)
        summary = _semantic_diff_summary(prev_yaml, entry_yaml) or _format_diff_summary(added, removed)
        if meta.get("summary") != summary:
            meta["summary"] = summary
            changed = True
        if parsed:
            major, minor = parsed
        else:
            if major is None or minor is None:
                major, minor = 1, 0
            else:
                if _is_major_update(added, removed, base_lines, new_lines):
                    major += 1
                    minor = 0
                else:
                    minor += 1
            label = _format_version_label(major, minor)
            meta["label"] = label
            changed = True
        if meta.get("id") != entry.get("id"):
            meta["id"] = entry.get("id")
            changed = True
        if meta.get("reason") != entry.get("reason"):
            meta["reason"] = entry.get("reason")
            changed = True
        if meta.get("ts") != entry.get("ts"):
            meta["ts"] = entry.get("ts")
            changed = True
        if entry.get("note") and not meta.get("note"):
            meta["note"] = entry.get("note")
            changed = True
        if changed and entry.get("path"):
            _write_version_meta(entry["path"], meta)
        entry["label"] = label or meta.get("label") or ""
        entry["description"] = meta.get("description") or meta.get("note") or entry.get("note") or ""
        entry["summary"] = meta.get("summary") or summary
        entry["meta"] = meta
        prev_yaml = entry_yaml
    return entries

def _next_version_info(automation_id: str, yaml_text: str) -> Tuple[str, str]:
    entries = _ensure_version_labels(automation_id)
    if not entries:
        added, removed, base_lines, new_lines = _diff_line_stats("", yaml_text or "")
        summary = _semantic_diff_summary("", yaml_text or "") or _format_diff_summary(added, removed)
        return "v1", summary
    last = entries[-1]
    label = last.get("label") or ""
    parsed = _parse_version_label(label) if label else None
    if not parsed:
        parsed = (1, 0)
    major, minor = parsed
    prev_yaml = last.get("yaml") or ""
    added, removed, base_lines, new_lines = _diff_line_stats(prev_yaml, yaml_text or "")
    summary = _semantic_diff_summary(prev_yaml, yaml_text or "") or _format_diff_summary(added, removed)
    if _is_major_update(added, removed, base_lines, new_lines):
        major += 1
        minor = 0
    else:
        minor += 1
    return _format_version_label(major, minor), summary

def _write_version(automation_id: str, yaml_text: str, reason: str, note: Optional[str] = None) -> str:
    ts = _now_stamp()
    label, summary = _next_version_info(automation_id, yaml_text or "")
    reason_slug = _sanitize_reason(reason)
    label_slug = _sanitize_version_label(label)
    fn = f"{automation_id}__{reason_slug}_{label_slug}__{ts}.yaml"
    fp = _version_dir() / fn
    fp.write_text(yaml_text or "", encoding="utf-8")
    meta = {
        "id": fn,
        "reason": reason,
        "ts": ts,
        "note": (note or "").strip(),
        "description": (note or "").strip(),
        "label": label,
        "summary": summary,
    }
    _write_version_meta(fp, meta)
    _ensure_version_labels(automation_id)
    return fn

def _list_versions(automation_id: str) -> List[Dict[str, Any]]:
    entries = _ensure_version_labels(automation_id)
    items: List[Dict[str, Any]] = []
    for entry in entries:
        items.append({
            "id": entry.get("id"),
            "reason": entry.get("reason"),
            "ts": entry.get("ts"),
            "size": entry.get("size"),
            "note": entry.get("note") or "",
            "label": entry.get("label") or "",
            "description": entry.get("description") or "",
            "summary": entry.get("summary") or "",
        })
    items.sort(key=lambda x: x.get("ts", ""), reverse=True)
    return items

def _slug(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s or "automation"

def _script_key(script_id: str) -> str:
    return f"{SCRIPT_ID_PREFIX}{script_id}"

def _suggest_script_id(alias: str, existing_ids: List[str]) -> str:
    base = _slug(alias or "script")
    if base not in existing_ids:
        return base
    stamp = _now_stamp()
    return f"{base}_{stamp}"

def _yaml_dump(obj: Any) -> str:
    return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True)

def _yaml_load(text: str) -> Any:
    return yaml.safe_load(text) if text else None

def _get_automations_file_path() -> Optional[Path]:
    if AUTOMATIONS_FILE_PATH:
        return _resolve_path(AUTOMATIONS_FILE_PATH)
    if _looks_like_absolute_path(LOCAL_AUTOMATIONS_PATH):
        return _resolve_path(LOCAL_AUTOMATIONS_PATH)
    return None

def _get_scripts_file_path() -> Optional[Path]:
    if SCRIPTS_FILE_PATH:
        return _resolve_path(SCRIPTS_FILE_PATH)
    if AUTOMATIONS_FILE_PATH:
        base = _resolve_path(AUTOMATIONS_FILE_PATH)
        return base.parent / "scripts.yaml"
    if _looks_like_absolute_path(LOCAL_AUTOMATIONS_PATH):
        base = _resolve_path(LOCAL_AUTOMATIONS_PATH)
        return base.parent / "scripts.yaml"
    if _looks_like_absolute_path(LOCAL_SCRIPTS_PATH):
        return _resolve_path(LOCAL_SCRIPTS_PATH)
    return None

def _get_restore_state_path() -> Optional[Path]:
    if RESTORE_STATE_PATH:
        return _resolve_path(RESTORE_STATE_PATH)
    automations_file = _get_automations_file_path()
    if automations_file:
        return automations_file.parent / ".storage" / "core.restore_state"
    return None

def _read_scripts_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Scripts file not found: {path}")
    raw = path.read_text(encoding="utf-8")
    data = _yaml_load(raw)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="scripts.yaml must be a YAML mapping of script_id -> config")
    out: Dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, dict):
            out[str(k)] = v
    return out

def _write_scripts_file(path: Path, scripts: Dict[str, Any]) -> None:
    _backup_file(path, "ha_scripts_before_apply")
    path.write_text(_yaml_dump(scripts), encoding="utf-8")

def _read_automations_file(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Automations file not found: {path}")
    raw = path.read_text(encoding="utf-8")
    data = _yaml_load(raw)
    if data is None:
        return []
    if not isinstance(data, list):
        raise HTTPException(status_code=400, detail="automations.yaml must be a YAML list of automations")
    return [x for x in data if isinstance(x, dict)]

def _extract_restore_entities(obj: Any, out: List[Dict[str, Any]]) -> None:
    if isinstance(obj, dict):
        if "entity_id" in obj and "state" in obj:
            out.append(obj)
            return
        for v in obj.values():
            _extract_restore_entities(v, out)
        return
    if isinstance(obj, list):
        for item in obj:
            _extract_restore_entities(item, out)

def _read_restore_state_entities(path: Optional[Path]) -> List[Dict[str, Any]]:
    if not path or not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:
        return []
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    _extract_restore_entities(data, out)
    return [e for e in out if isinstance(e, dict)]

def _automation_state_maps_from_entities(
    entities: List[Dict[str, Any]]
) -> Tuple[Dict[str, Dict[str, str]], Dict[str, Dict[str, str]]]:
    by_id: Dict[str, Dict[str, str]] = {}
    by_slug: Dict[str, Dict[str, str]] = {}
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        entity_id = str(ent.get("entity_id") or "")
        if not entity_id.startswith("automation."):
            continue
        state = str(ent.get("state") or "")
        attrs = ent.get("attributes") if isinstance(ent.get("attributes"), dict) else {}
        info = {"state": state, "entity_id": entity_id}
        auto_id = str(attrs.get("id") or attrs.get("unique_id") or "").strip()
        if auto_id:
            by_id[auto_id] = info
        friendly = str(attrs.get("friendly_name") or "").strip()
        if friendly:
            by_slug[_slug(friendly)] = info
        slug = entity_id.split(".", 1)[1] if "." in entity_id else entity_id
        if slug:
            by_slug[slug] = info
    return by_id, by_slug

def _find_automation_in_file(path: Path, automation_id: str) -> Optional[Dict[str, Any]]:
    for item in _read_automations_file(path):
        if str(item.get("id") or "") == automation_id:
            return item
    return None

def _write_automations_file(path: Path, automations: List[Dict[str, Any]]) -> None:
    # Backup the current automations.yaml locally before any write
    _backup_file(path, "ha_file_before_apply")
    path.write_text(_yaml_dump(automations), encoding="utf-8")

def _ha_get_all_automations() -> List[Dict[str, Any]]:
    # HA usually supports this; if your HA build differs, we fail gracefully.
    try:
        r = requests.get(f"{HA_URL}/api/config/automation/config", headers=ha_headers(), timeout=30)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []

def _ha_get_states() -> List[Dict[str, Any]]:
    if not (HA_URL and HA_TOKEN):
        return []
    try:
        r = requests.get(f"{HA_URL}/api/states", headers=ha_headers(), timeout=30)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []

def _ha_get_services() -> List[Dict[str, Any]]:
    if not (HA_URL and HA_TOKEN):
        return []
    try:
        r = requests.get(f"{HA_URL}/api/services", headers=ha_headers(), timeout=30)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []

def _get_automation_state_maps() -> Tuple[Dict[str, Dict[str, str]], Dict[str, Dict[str, str]]]:
    # Prefer live HA state when available; fall back to restore_state file.
    states = _ha_get_states()
    if states:
        return _automation_state_maps_from_entities(states)

    restore_path = _get_restore_state_path()
    entities = _read_restore_state_entities(restore_path)
    if entities:
        return _automation_state_maps_from_entities(entities)

    return {}, {}

def _get_automation_state_info(automation_id: str, alias: Optional[str] = None) -> Dict[str, str]:
    by_id, by_slug = _get_automation_state_maps()
    info = by_id.get(str(automation_id))
    if not info and alias:
        info = by_slug.get(_slug(alias))
    return info or {}

def _ha_get_automation(automation_id: str) -> Optional[Dict[str, Any]]:
    try:
        r = requests.get(f"{HA_URL}/api/config/automation/config/{automation_id}", headers=ha_headers(), timeout=30)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json() if isinstance(r.json(), dict) else None
    except Exception:
        return None

def _ha_update_automation(automation_id: str, config: Dict[str, Any]) -> None:
    # Update existing automation
    r = requests.put(f"{HA_URL}/api/config/automation/config/{automation_id}", headers=ha_headers(), json=config, timeout=60)
    r.raise_for_status()

def _ha_create_automation(config: Dict[str, Any]) -> str:
    # Create new automation
    r = requests.post(f"{HA_URL}/api/config/automation/config", headers=ha_headers(), json=config, timeout=60)
    r.raise_for_status()
    data = r.json() or {}
    new_id = str(data.get("automation_id") or "")
    if not new_id:
        # fallback to legacy create method if no ID returned
        new_id = str(int(time.time() * 1000))
        rr = requests.post(
            f"{HA_URL}/api/config/automation/config/{new_id}",
            headers=ha_headers(),
            json={"id": new_id, **config},
            timeout=60,
        )
        rr.raise_for_status()
    reload_automations()
    return new_id

def _normalize_automation_config_from_yaml(yaml_text: str) -> Dict[str, Any]:
    obj = _yaml_load(yaml_text)
    if not isinstance(obj, dict):
        raise ValueError("YAML must be a single automation dictionary (not a list).")

    # Keep only the keys HA automation config expects
    allowed = {"id","alias","description","trigger","condition","action","mode","initial_state","variables"}
    cfg = {k: obj.get(k) for k in allowed if k in obj}

    # Ensure list types exist
    cfg.setdefault("trigger", [])
    cfg.setdefault("condition", [])
    cfg.setdefault("action", [])
    cfg.setdefault("mode", cfg.get("mode") or "single")
    cfg.setdefault("initial_state", bool(cfg.get("initial_state", True)))

    return cfg

def _local_upsert(automation_id: str, meta: Dict[str, Any], yaml_text: str) -> None:
    db = _load_local_db()
    items = db.setdefault("items", {})
    prev = items.get(automation_id) or {}
    items[automation_id] = {
        "id": automation_id,
        "alias": meta.get("alias") or meta.get("name") or automation_id,
        "description": meta.get("description") or "",
        "source": meta.get("source") or "local",
        "ha_id": meta.get("ha_id"),
        "updated": _now_stamp(),
        "yaml": yaml_text or "",
        "conversation_id": prev.get("conversation_id"),
        "conversation_history": prev.get("conversation_history") if isinstance(prev.get("conversation_history"), list) else [],
    }
    _save_local_db(db)


def _normalize_conversation_message(msg: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(msg, dict):
        return None
    role = (msg.get("role") or msg.get("type") or "assistant").strip().lower()
    text = msg.get("text") or msg.get("content") or msg.get("message")
    if not text:
        return None
    return {
        "role": role if role in ("user", "assistant", "system") else "assistant",
        "text": str(text)[:4000],
        "ts": _now_stamp(),
    }


def _local_update_conversation(key: str, conversation_id: Optional[str] = None, messages: Optional[List[Any]] = None,
                               clear: bool = False, replace: bool = False) -> Dict[str, Any]:
    db = _load_local_db()
    items = db.setdefault("items", {})
    item = items.get(key) or {
        "id": key,
        "alias": key,
        "description": "",
        "source": "local",
        "ha_id": None,
        "updated": _now_stamp(),
        "yaml": "",
    }

    history = item.get("conversation_history")
    if not isinstance(history, list):
        history = []

    if clear:
        history = []
        item["conversation_id"] = None

    if replace:
        history = []

    if messages:
        for msg in messages:
            norm = _normalize_conversation_message(msg)
            if norm:
                history.append(norm)

    if conversation_id:
        item["conversation_id"] = conversation_id

    if len(history) > 120:
        history = history[-120:]

    item["conversation_history"] = history
    item["updated"] = _now_stamp()
    items[key] = item
    _save_local_db(db)
    return item


def _get_conversation_payload(key: str) -> Dict[str, Any]:
    db = _load_local_db()
    item = (db.get("items") or {}).get(key) or {}
    history = item.get("conversation_history")
    if not isinstance(history, list):
        history = []
    return {
        "conversation_id": item.get("conversation_id"),
        "conversation_history": history,
    }

# ----------------------------
# HA WS FETCH
# ----------------------------
async def ha_ws_fetch() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    async with websockets.connect(ws_url(), max_size=None, open_timeout=30) as ws:
        msg = json.loads(await ws.recv())
        if msg.get("type") != "auth_required":
            raise RuntimeError(f"Unexpected WS pre-auth: {msg}")

        await ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
        msg = json.loads(await ws.recv())
        if msg.get("type") != "auth_ok":
            raise RuntimeError(f"WS auth failed: {msg}")

        req_id = 1

        async def call(t: str, **fields):
            nonlocal req_id
            req_id += 1
            await ws.send(json.dumps({"id": req_id, "type": t, **fields}))
            while True:
                resp = json.loads(await ws.recv())
                if resp.get("id") == req_id:
                    if not resp.get("success", False):
                        raise RuntimeError(f"WS call failed {t}: {resp}")
                    return resp.get("result")

        entity_registry = await call("config/entity_registry/list")
        device_registry = await call("config/device_registry/list")
        area_registry = await call("config/area_registry/list")
        states = await call("get_states")
        return entity_registry, device_registry, area_registry, states


DOMAIN_HINTS = {
    "light": ["light", "lights", "lamp", "lamps"],
    "switch": ["switch", "plug", "socket", "outlet"],
    "scene": ["scene"],
    "script": ["script", "routine", "announce", "announcement", "alexa", "speech", "notify"],
    "media_player": ["tv", "music", "media", "speaker", "sonos", "alexa", "echo", "spotify"],
    "notify": ["notify", "notification", "phone", "mobile", "push"],
    "climate": ["heat", "heater", "thermostat", "temperature", "hvac"],
    "cover": ["blind", "blinds", "shade", "curtain", "garage"],
    "alarm_control_panel": ["alarm", "arm", "disarm"],
    "lock": ["lock", "unlock"],
    "camera": ["camera"],
    "calendar": ["calendar", "schedule"],
    "timer": ["timer"],
    "input_boolean": ["toggle", "boolean", "input_boolean"],
    "input_number": ["input_number", "number", "level"],
    "input_select": ["input_select", "select", "mode"],
    "binary_sensor": ["motion", "door", "window", "contact", "presence", "occupancy"],
    "sensor": ["sensor", "humidity", "illuminance"],
}

def _tokenize_text(text: str) -> List[str]:
    return re.findall(r"[a-z0-9_]+", (text or "").lower())

def _infer_domains(request_text: str, summary: Optional[Dict[str, Any]] = None) -> List[str]:
    text = request_text or ""
    tokens = set(_tokenize_text(text))
    domains = set()
    for match in re.findall(r"\b([a-z_]+)\.[a-z0-9_]+\b", text.lower()):
        domains.add(match)
    if isinstance(summary, dict):
        for svc in summary.get("services") or []:
            if isinstance(svc, str) and "." in svc:
                domains.add(svc.split(".", 1)[0])
        for ent in summary.get("entities") or []:
            if isinstance(ent, str) and "." in ent:
                domains.add(ent.split(".", 1)[0])
    for domain, words in DOMAIN_HINTS.items():
        if any(w in tokens for w in words):
            domains.add(domain)
    return sorted(domains)

def _score_candidate(item: Dict[str, Any], tokens: List[str]) -> int:
    hay = f"{item.get('entity_id','')} {item.get('name','')} {item.get('area','')} {item.get('domain','')} {item.get('device_class','')}".lower()
    return sum(3 for t in tokens if t and t in hay)

def build_candidates(
    user_text: str,
    entity_registry,
    device_registry,
    area_registry,
    states,
    preferred_domains: Optional[List[str]] = None,
    include_entities: Optional[List[str]] = None,
    max_total: int = 120,
    per_domain: int = 30,
) -> List[Dict[str, Any]]:
    area_name_by_id = {a.get("area_id"): a.get("name") for a in area_registry}
    device_by_id = {d.get("id"): d for d in device_registry}
    state_by_entity = {s["entity_id"]: s for s in states}

    catalog: List[Dict[str, Any]] = []
    for e in entity_registry:
        entity_id = e.get("entity_id")
        st = state_by_entity.get(entity_id, {})
        attrs = st.get("attributes") or {}
        friendly = attrs.get("friendly_name") or e.get("name") or entity_id
        dev = device_by_id.get(e.get("device_id"), {}) if e.get("device_id") else {}
        area = area_name_by_id.get(dev.get("area_id")) or ""
        device_class = attrs.get("device_class") or ""
        catalog.append({
            "entity_id": entity_id,
            "name": friendly,
            "domain": entity_id.split(".")[0],
            "area": area,
            "device_class": device_class,
        })

    known = {c["entity_id"] for c in catalog}
    for st in states:
        eid = st["entity_id"]
        if eid in known:
            continue
        attrs = st.get("attributes") or {}
        friendly = attrs.get("friendly_name") or eid
        device_class = attrs.get("device_class") or ""
        catalog.append({"entity_id": eid, "name": friendly, "domain": eid.split(".")[0], "area": "", "device_class": device_class})

    tokens = _tokenize_text(user_text)
    catalog.sort(key=lambda item: _score_candidate(item, tokens), reverse=True)

    include_entities = include_entities or []
    preferred = [d for d in (preferred_domains or []) if d]

    if not preferred:
        selected = catalog[:max_total]
    else:
        by_domain: Dict[str, List[Dict[str, Any]]] = {}
        for item in catalog:
            dom = item.get("domain")
            if dom in preferred:
                by_domain.setdefault(dom, []).append(item)
        selected = []
        for dom in preferred:
            selected.extend(by_domain.get(dom, [])[:per_domain])
        # fill remaining slots with highest scored candidates
        if len(selected) < max_total:
            seen = {i.get("entity_id") for i in selected}
            for item in catalog:
                eid = item.get("entity_id")
                if eid in seen:
                    continue
                selected.append(item)
                if len(selected) >= max_total:
                    break

    # Ensure required entities are included
    if include_entities:
        by_id = {c.get("entity_id"): c for c in catalog}
        for eid in include_entities:
            if not eid or eid in {i.get("entity_id") for i in selected}:
                continue
            if eid in by_id:
                selected.append(by_id[eid])
    return selected[:max_total]


def _score_text(hay: str, tokens: List[str]) -> int:
    if not hay:
        return 0
    h = hay.lower()
    return sum(1 for t in tokens if t and t in h)


def build_capabilities_subset(
    capabilities: Dict[str, Any],
    request_text: str,
    summary: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    caps = slim_capabilities_for_llm(capabilities)
    tokens = _tokenize_text(request_text)
    if isinstance(summary, dict):
        tokens.extend(_tokenize_text(summary.get("intent") or ""))
        for item in (summary.get("actions") or []):
            tokens.extend(_tokenize_text(str(item)))
        for item in (summary.get("triggers") or []):
            tokens.extend(_tokenize_text(str(item)))
        for item in (summary.get("conditions") or []):
            tokens.extend(_tokenize_text(str(item)))
        for ent in (summary.get("entities") or []):
            tokens.extend(_tokenize_text(str(ent)))
        for svc in (summary.get("services") or []):
            tokens.extend(_tokenize_text(str(svc)))
    tokens = [t for t in tokens if t]
    if not tokens:
        return caps

    scripts = caps.get("scripts") or []
    ranked_scripts = []
    for s in scripts:
        if not isinstance(s, dict):
            continue
        hay = f"{s.get('entity_id','')} {s.get('purpose','')}"
        score = _score_text(hay, tokens)
        if score > 0:
            ranked_scripts.append((score, s))
    ranked_scripts.sort(key=lambda x: x[0], reverse=True)
    caps["scripts"] = [s for _, s in ranked_scripts[:40]]

    learned = caps.get("learned_context") or {}
    hints = learned.get("hints") or []
    ranked_hints = []
    for h in hints:
        if not isinstance(h, dict):
            continue
        hay = f"{h.get('note','')} {' '.join(h.get('tags') or [])}"
        score = _score_text(hay, tokens)
        if score > 0:
            ranked_hints.append((score, h))
    ranked_hints.sort(key=lambda x: x[0], reverse=True)
    if ranked_hints:
        learned["hints"] = [h for _, h in ranked_hints[:30]]
    caps["learned_context"] = learned
    return caps


def _trim_history(history: List[Dict[str, Any]], limit: int = 6, max_len: int = 280) -> List[Dict[str, Any]]:
    if not history:
        return []
    trimmed: List[Dict[str, Any]] = []
    for msg in history[-limit:]:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        text = msg.get("text") or msg.get("content") or ""
        if role not in ("user", "assistant", "system"):
            role = "user"
        s = str(text)
        if len(s) > max_len:
            s = s[:max_len] + "..."
        trimmed.append({"role": role, "text": s})
    return trimmed


def build_context_pack_from_regs(
    request_text: str,
    current_yaml: Optional[str],
    entity_type: str,
    entity_registry,
    device_registry,
    area_registry,
    states,
    history: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    capabilities = load_capabilities()
    summary = _summarize_yaml_for_prompt(request_text, current_yaml or "") if current_yaml else None
    preferred_domains = _infer_domains(request_text, summary)
    include_entities = summary.get("entities") if isinstance(summary, dict) else []
    candidates = build_candidates(
        request_text or "",
        entity_registry,
        device_registry,
        area_registry,
        states,
        preferred_domains=preferred_domains,
        include_entities=include_entities,
        max_total=80,
        per_domain=25,
    )
    caps_subset = build_capabilities_subset(capabilities, request_text or "", summary)
    return {
        "summary": summary or {},
        "capabilities": caps_subset,
        "candidates": candidates,
        "recent_history": _trim_history(history or []),
    }


# ----------------------------
# HELPER TOKENS (OPTIONAL)
# ----------------------------
def load_helper_map() -> Dict[str, str]:
    if not os.path.exists(HELPER_MAP_FILE):
        return {}
    with open(HELPER_MAP_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_helper_map(m: Dict[str, str]) -> None:
    with open(HELPER_MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2)


def _normalize_helpers_needed(helpers_needed: Any) -> List[Dict[str, Any]]:
    if helpers_needed is None:
        return []
    if isinstance(helpers_needed, dict):
        return [helpers_needed]
    if isinstance(helpers_needed, str):
        s = helpers_needed.strip()
        if not s or s.lower() in ("none", "null", "no", "n/a"):
            return []
        return [{"type": s}]
    if isinstance(helpers_needed, list):
        out = []
        for item in helpers_needed:
            if isinstance(item, dict):
                out.append(item)
            elif isinstance(item, str):
                s = item.strip()
                if s and s.lower() not in ("none", "null", "no", "n/a"):
                    out.append({"type": s})
        return out
    return []


def allocate_helpers(states: List[Dict[str, Any]], helpers_needed: Any) -> Dict[str, str]:
    existing = {s["entity_id"] for s in states}
    helper_map = load_helper_map()
    placeholder_map: Dict[str, str] = {}

    normalized = _normalize_helpers_needed(helpers_needed)
    counts = {"counter": 0, "timer": 0, "boolean": 0, "number": 0, "text": 0}

    for h in normalized:
        raw_typ = (h.get("type") or "").lower().strip()
        typ = TYPE_SYNONYMS.get(raw_typ, raw_typ)
        if typ not in POOL:
            continue

        counts[typ] += 1
        idx = counts[typ]
        placeholder = f"HELPER_{typ.upper()}_{idx}"

        slot = None
        for candidate in POOL[typ]:
            if candidate in existing and candidate not in helper_map:
                slot = candidate
                break

        if slot:
            helper_map[slot] = h.get("purpose", f"auto:{typ}:{idx}")
            placeholder_map[placeholder] = slot

    save_helper_map(helper_map)
    return placeholder_map


def replace_placeholders(obj: Any, mapping: Dict[str, str]) -> Any:
    if isinstance(obj, str):
        for k, v in mapping.items():
            obj = obj.replace(k, v)
        return obj
    if isinstance(obj, list):
        return [replace_placeholders(x, mapping) for x in obj]
    if isinstance(obj, dict):
        return {k: replace_placeholders(v, mapping) for k, v in obj.items()}
    return obj


# ----------------------------
# BUILDER CALL (HA conversation agent)
# ----------------------------
def _builder_request_text(payload: Dict[str, Any], *, minimal: bool, addendum: Optional[str] = None) -> str:
    contract = payload.get("output_contract") or ""
    hint = ("Return ONLY a single minified JSON object. No markdown. No commentary. "
            "Do NOT echo candidates/capabilities. ")
    if minimal:
        hint += "Keep it VERY short. Omit helpers_needed unless required. "
    if addendum:
        hint = "ADDENDUM:\n" + addendum.strip() + "\n\n" + hint
    return hint + "\nCONTRACT:\n" + contract + "\n\nINPUT_JSON:\n" + json.dumps(payload, ensure_ascii=False)


def call_builder(payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{HA_URL}/api/conversation/process"

    def _post(text: str, agent_id: str) -> str:
        r = requests.post(url, headers=ha_headers(), json={"agent_id": agent_id, "text": text}, timeout=180)
        r.raise_for_status()
        data = r.json()
        speech = ((((data.get("response") or {}).get("speech") or {}).get("plain") or {}).get("speech") or "").strip()
        return speech

    def _try_parse(speech: str) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(speech)
        except Exception:
            extracted = _extract_json_object(speech or "")
            if not extracted:
                return None
            try:
                return json.loads(extracted)
            except Exception:
                return None

    minimal_payload = dict(payload)
    minimal_payload["candidates"] = (payload.get("candidates") or [])[:60]
    minimal_payload["capabilities"] = slim_capabilities_for_llm(payload.get("capabilities") or {})
    minimal_payload["output_contract"] = (
        "Return ONLY minified JSON keys: alias, description, trigger, condition, action, mode, initial_state, helpers_needed. "
        "Do not include any other keys. helpers_needed must be [] or a list of objects {type, purpose}. "
        "If entity_ids or services are not specified, infer them from candidates and capabilities. "
        "Use real HA services. Keep under 1200 characters."
    )

    speech = _post(_builder_request_text(payload, minimal=False), BUILDER_AGENT_ID)
    if DEBUG:
        print("BUILDER RAW SPEECH:", repr(speech[:600]))
    if not _looks_like_bad_builder_output(speech):
        parsed = _try_parse(speech)
        if parsed is not None:
            return parsed

    speech = _post(_builder_request_text(minimal_payload, minimal=True), BUILDER_AGENT_ID)
    if DEBUG:
        print("BUILDER RETRY SPEECH:", repr(speech[:600]))
    if not _looks_like_bad_builder_output(speech):
        parsed = _try_parse(speech)
        if parsed is not None:
            return parsed

    # Fallback to dumb builder (cheaper model)
    if DUMB_BUILDER_AGENT_ID:
        speech = _post(_builder_request_text(minimal_payload, minimal=True, addendum=DUMB_BUILDER_ADDENDUM), DUMB_BUILDER_AGENT_ID)
        if DEBUG:
            print("DUMB BUILDER SPEECH:", repr(speech[:600]))
        parsed = _try_parse(speech)
        if parsed is not None:
            return parsed

    raise RuntimeError(f"Builder did not return JSON. Got: {speech[:250]}")

# ----------------------------
# API: Health
# ----------------------------
@app.get("/api/health")
def api_health():
    return {"ok": True, "ha_url_set": bool(HA_URL), "static_dir": str(STATIC_PATH)}

# ----------------------------
# API: Admin helper agent check
# ----------------------------
@app.get("/api/admin/agent-check")
def api_admin_agent_check(x_ha_agent_secret: str = Header(default="")):
    require_auth(x_ha_agent_secret)
    if not (HA_URL and HA_TOKEN):
        raise HTTPException(status_code=412, detail="HA_URL/HA_TOKEN not configured")

    caps_slim = slim_capabilities_for_llm(load_capabilities())
    yaml_test = "alias: Test\ntrigger: []\ncondition: []\naction: []\nmode: single\ninitial_state: true"
    fallback_summary = {
        "intent": "",
        "triggers": [],
        "conditions": [],
        "actions": [],
        "entities": [],
        "services": [],
        "helpers": [],
        "risks": [],
        "notes": [],
        "confidence": 0,
    }

    results: Dict[str, Any] = {}

    def record(name: str, agent_id: str, ok: bool, detail: str = ""):
        results[name] = {"agent_id": agent_id, "ok": ok, "detail": detail}

    summary_out = _call_helper_agent_json(
        SUMMARY_AGENT_ID,
        {
            "request": "health check",
            "current_yaml": yaml_test,
            "candidates": [],
            "capabilities": caps_slim,
        },
        required_keys=["triggers", "conditions", "actions", "entities", "services", "confidence"],
    )
    record("summary", SUMMARY_AGENT_ID, bool(summary_out), "" if summary_out else "invalid_json_or_missing_keys")

    mapper_out = _call_helper_agent_json(
        CAPABILITY_MAPPER_AGENT_ID,
        {
            "summary": summary_out or fallback_summary,
            "capabilities": caps_slim,
            "candidates": [],
        },
        required_keys=["missing_entities", "missing_services", "questions", "confidence"],
    )
    record("capability_mapper", CAPABILITY_MAPPER_AGENT_ID, bool(mapper_out), "" if mapper_out else "invalid_json_or_missing_keys")

    diff_out = _call_helper_agent_json(
        SEMANTIC_DIFF_AGENT_ID,
        {"before_summary": fallback_summary, "after_summary": fallback_summary},
        required_keys=["summary", "added", "removed", "changed", "confidence"],
    )
    record("semantic_diff", SEMANTIC_DIFF_AGENT_ID, bool(diff_out), "" if diff_out else "invalid_json_or_missing_keys")

    kb_out = _call_helper_agent_json(
        KB_SYNC_HELPER_AGENT_ID,
        {"user_request": "health check", "summary": summary_out or fallback_summary, "capabilities": caps_slim},
        required_keys=["questions", "confidence"],
    )
    record("kb_sync_helper", KB_SYNC_HELPER_AGENT_ID, bool(kb_out), "" if kb_out else "invalid_json_or_missing_keys")

    dumb_ok = False
    dumb_detail = ""
    if DUMB_BUILDER_AGENT_ID:
        try:
            minimal_payload = {
                "request": "Create a simple automation that does nothing.",
                "source": "admin_check",
                "entity_type": "automation",
                "candidates": [],
                "capabilities": caps_slim,
                "helper_placeholders": [
                    "HELPER_COUNTER_1",
                    "HELPER_TIMER_1",
                    "HELPER_BOOLEAN_1",
                    "HELPER_NUMBER_1",
                    "HELPER_TEXT_1",
                ],
                "output_contract": (
                    "Return ONLY minified JSON keys: alias, description, trigger, condition, action, mode, initial_state, helpers_needed. "
                    "Do not include any other keys."
                ),
            }
            test_text = _builder_request_text(minimal_payload, minimal=True, addendum=DUMB_BUILDER_ADDENDUM)
            speech = call_conversation_agent(DUMB_BUILDER_AGENT_ID, test_text)
            extracted = _extract_json_object(speech or "")
            parsed = json.loads(extracted) if extracted else None
            required = {"alias", "description", "trigger", "condition", "action", "mode", "initial_state", "helpers_needed"}
            dumb_ok = isinstance(parsed, dict) and required.issubset(parsed.keys())
            if not dumb_ok:
                dumb_detail = "invalid_json_or_missing_keys"
        except Exception as e:
            dumb_ok = False
            dumb_detail = f"error:{type(e).__name__}"
    else:
        dumb_detail = "agent_id_not_set"
    record("dumb_builder", DUMB_BUILDER_AGENT_ID, dumb_ok, dumb_detail)

    bad_agents = [k for k, v in results.items() if not v.get("ok")]
    return {
        "ok": len(bad_agents) == 0,
        "checked_at": _now_stamp(),
        "bad_agents": bad_agents,
        "results": results,
    }

# ----------------------------
# API: Admin runtime config
# ----------------------------
@app.get("/api/admin/runtime")
def api_admin_runtime_get(x_ha_agent_secret: str = Header(default="")):
    require_auth(x_ha_agent_secret)
    return {
        "ok": True,
        "helper_min_confidence": HELPER_MIN_CONFIDENCE,
        "allow_ai_diff": ALLOW_AI_DIFF,
    }

@app.post("/api/admin/runtime")
def api_admin_runtime_set(
    body: Dict[str, Any] = Body(default={}),
    x_ha_agent_secret: str = Header(default="")
):
    require_auth(x_ha_agent_secret)
    cfg = {
        "helper_min_confidence": HELPER_MIN_CONFIDENCE,
        "allow_ai_diff": ALLOW_AI_DIFF,
    }
    if "helper_min_confidence" in body:
        try:
            val = float(body.get("helper_min_confidence"))
            if 0 <= val <= 1:
                cfg["helper_min_confidence"] = val
        except Exception:
            pass
    if "allow_ai_diff" in body:
        cfg["allow_ai_diff"] = bool(body.get("allow_ai_diff"))

    _apply_runtime_config(cfg)
    _save_runtime_config(cfg)
    return {
        "ok": True,
        "helper_min_confidence": HELPER_MIN_CONFIDENCE,
        "allow_ai_diff": ALLOW_AI_DIFF,
    }

# ----------------------------
# API: Capabilities knowledgebase
# ----------------------------
@app.get("/api/capabilities")
def api_get_capabilities():
    caps = load_capabilities()
    yaml_text = yaml.safe_dump(caps, sort_keys=False, allow_unicode=True)
    return {"yaml": yaml_text}


@app.post("/api/capabilities/refresh")
async def api_refresh_capabilities():
    if not (HA_URL and HA_TOKEN):
        raise HTTPException(status_code=412, detail="HA_URL/HA_TOKEN not configured")

    entity_registry, device_registry, area_registry, states = await ha_ws_fetch()

    automations: List[Dict[str, Any]] = []
    automations_file = _get_automations_file_path()
    if automations_file and automations_file.exists():
        try:
            automations = _read_automations_file(automations_file)
        except HTTPException:
            automations = []

    scripts: Dict[str, Any] = {}
    scripts_file = _get_scripts_file_path()
    if scripts_file and scripts_file.exists():
        try:
            scripts = _read_scripts_file(scripts_file)
        except HTTPException:
            scripts = {}

    services = _ha_get_services()

    inventory = _build_capabilities_inventory(
        entity_registry,
        device_registry,
        area_registry,
        states,
        automations,
        scripts,
        services,
    )

    caps = load_capabilities()
    caps["inventory"] = inventory
    save_capabilities(caps)
    yaml_text = yaml.safe_dump(caps, sort_keys=False, allow_unicode=True)

    return {"ok": True, "summary": inventory.get("counts") or {}, "yaml": yaml_text}


@app.post("/api/capabilities/sync")
def api_capabilities_sync(body: Dict[str, Any] = Body(default={})):
    if not (HA_URL and HA_TOKEN):
        raise HTTPException(status_code=412, detail="HA_URL/HA_TOKEN not configured")

    entity_type = (body.get("entity_type") or "automation").strip().lower()
    if entity_type not in ("automation", "script"):
        entity_type = "automation"
    entity_id = (
        body.get("entity_id")
        or body.get("automation_id")
        or body.get("script_id")
        or body.get("id")
        or ""
    ).strip()
    yaml_text = (body.get("yaml") or "").strip()
    user_prompt = (body.get("prompt") or body.get("message") or body.get("notes") or "").strip()

    if not yaml_text and entity_id:
        if entity_type == "script":
            scripts_file = _get_scripts_file_path()
            if not scripts_file:
                raise HTTPException(status_code=412, detail="SCRIPTS_FILE_PATH not configured")
            scripts = _read_scripts_file(scripts_file)
            script_key = entity_id
            if script_key.startswith("script."):
                script_key = script_key.split(".", 1)[1]
            item = scripts.get(script_key) or scripts.get(entity_id)
            if not item:
                raise HTTPException(status_code=404, detail="Script not found")
            yaml_text = _yaml_dump(item)
        else:
            automations_file = _get_automations_file_path()
            if not automations_file:
                raise HTTPException(status_code=412, detail="AUTOMATIONS_FILE_PATH not configured")
            item = _find_automation_in_file(automations_file, entity_id)
            if not item:
                raise HTTPException(status_code=404, detail="Automation not found")
            yaml_text = _yaml_dump(item)

    if not yaml_text:
        raise HTTPException(status_code=400, detail="Missing automation/script yaml")

    caps = load_capabilities()
    inv = caps.get("inventory") or {}
    known_entities = set()
    for item in (inv.get("entities") or []):
        if isinstance(item, dict) and item.get("entity_id"):
            known_entities.add(item["entity_id"])
    for eid in (inv.get("used_entities") or []):
        if eid:
            known_entities.add(str(eid))
    for item in (inv.get("scripts") or []):
        if isinstance(item, dict) and item.get("entity_id"):
            known_entities.add(item["entity_id"])
    user_ctx = caps.get("user_context") or {}
    if isinstance(user_ctx.get("entity_hints"), dict):
        known_entities.update(user_ctx.get("entity_hints").keys())

    known_services = set()
    for svc in (inv.get("services") or []):
        if svc:
            known_services.add(str(svc))

    obj = _coerce_yaml_dict(yaml_text)
    used_entities: set = set()
    used_services: set = set()
    if obj:
        _collect_entity_ids(obj, used_entities)
        _collect_service_names(obj, used_services)

    missing_entities = sorted([e for e in used_entities if known_entities and e not in known_entities])
    missing_services = sorted([s for s in used_services if known_services and s not in known_services])

    summary = _summarize_yaml_for_prompt(user_prompt, yaml_text)
    caps_slim = build_capabilities_subset(caps, user_prompt, summary)

    mapper_out = _call_helper_agent_json(
        CAPABILITY_MAPPER_AGENT_ID,
        {
            "summary": summary or {},
            "capabilities": caps_slim,
            "candidates": [],
        },
        required_keys=["missing_entities", "missing_services", "questions"],
    )

    helper_out = _call_helper_agent_json(
        KB_SYNC_HELPER_AGENT_ID,
        {
            "user_request": user_prompt or "",
            "summary": summary or {},
            "capabilities": caps_slim,
        },
        required_keys=["questions"],
    )

    reply = ""
    if helper_out and isinstance(helper_out.get("questions"), list):
        qs = [str(q).strip() for q in helper_out.get("questions") if str(q).strip()]
        if qs:
            reply = "\n".join([f"{i+1}. {q}" for i, q in enumerate(qs[:6])])

    if not reply:
        caps_yaml = yaml.safe_dump(caps, sort_keys=False, allow_unicode=True)
        prompt = (
            "You are a Knowledgebase Sync Agent for Home Assistant.\n"
            "Task: review the automation and the current capabilities YAML, then ask the user what should be added.\n"
            "Focus on entities, scripts, rooms/areas, speech/notification conventions, and reliability rules.\n"
            "Ask 3-5 concise questions and mention any missing entities/services explicitly.\n"
            "Return plain text only (no markdown).\n\n"
            f"USER_REQUEST: {user_prompt or 'None'}\n"
            f"MISSING_ENTITIES: {missing_entities}\n"
            f"MISSING_SERVICES: {missing_services}\n\n"
            f"CAPABILITIES_YAML:\n{caps_yaml}\n\n"
            f"AUTOMATION_YAML:\n{yaml_text}\n"
        )
        reply = call_conversation_agent(ARCHITECT_AGENT_ID, prompt)

    if mapper_out:
        mapped_missing_entities = mapper_out.get("missing_entities")
        mapped_missing_services = mapper_out.get("missing_services")
        if isinstance(mapped_missing_entities, list) and mapped_missing_entities:
            missing_entities = sorted(set(missing_entities) | set(map(str, mapped_missing_entities)))
        if isinstance(mapped_missing_services, list) and mapped_missing_services:
            missing_services = sorted(set(missing_services) | set(map(str, mapped_missing_services)))

        mapped_questions = mapper_out.get("questions")
        if isinstance(mapped_questions, list) and mapped_questions:
            extra = [str(q).strip() for q in mapped_questions if str(q).strip()]
            if extra:
                reply = (reply + "\n\nAdditional mapping questions:\n" + "\n".join(f"- {q}" for q in extra[:3])).strip()

    return {
        "ok": True,
        "reply": reply,
        "missing_entities": missing_entities,
        "missing_services": missing_services,
        "mapper": mapper_out or {},
        "summary": summary or {},
    }


@app.post("/api/capabilities/learn")
def api_capabilities_learn(body: Dict[str, Any] = Body(default={})):
    note = (body.get("text") or body.get("note") or body.get("prompt") or "").strip()
    if not note:
        raise HTTPException(status_code=400, detail="Missing note text")

    entity_type = (body.get("entity_type") or "automation").strip().lower()
    if entity_type not in ("automation", "script"):
        entity_type = "automation"
    entity_id = (
        body.get("entity_id")
        or body.get("automation_id")
        or body.get("script_id")
        or body.get("id")
        or ""
    ).strip()
    yaml_text = (body.get("yaml") or "").strip()

    if not yaml_text and entity_id:
        if entity_type == "script":
            scripts_file = _get_scripts_file_path()
            if scripts_file:
                scripts = _read_scripts_file(scripts_file)
                script_key = entity_id
                if script_key.startswith("script."):
                    script_key = script_key.split(".", 1)[1]
                item = scripts.get(script_key) or scripts.get(entity_id)
                if item:
                    yaml_text = _yaml_dump(item)
        else:
            automations_file = _get_automations_file_path()
            if automations_file:
                item = _find_automation_in_file(automations_file, entity_id)
                if item:
                    yaml_text = _yaml_dump(item)

    summary = _summarize_yaml_for_prompt(note, yaml_text) if yaml_text else {}
    caps = load_capabilities()
    caps_subset = build_capabilities_subset(caps, note, summary) if summary or note else slim_capabilities_for_llm(caps)

    helper_out = _call_helper_agent_json(
        KB_SYNC_HELPER_AGENT_ID,
        {
            "user_request": note,
            "summary": summary or {},
            "capabilities": caps_subset,
        },
        required_keys=["questions"],
    )

    preview = preview_capabilities_note(note)
    confirm = bool(body.get("confirm") or body.get("commit") or False)

    if not confirm:
        return {
            "ok": True,
            "preview": preview,
            "questions": helper_out.get("questions") if isinstance(helper_out, dict) else [],
            "intent_summary": helper_out.get("intent_summary") if isinstance(helper_out, dict) else "",
            "confidence": helper_out.get("confidence") if isinstance(helper_out, dict) else None,
            "summary": summary or {},
        }

    updated_entities, updated_scripts, updated_context, saved_notes = commit_capabilities_note(note)
    return {
        "ok": True,
        "saved_entities": updated_entities,
        "saved_scripts": updated_scripts,
        "saved_context": updated_context,
        "saved_notes": saved_notes,
        "preview": preview,
    }

# ----------------------------
# API: Automations list (automations.yaml via UNC path)
# ----------------------------
@app.get("/api/automations")
def api_list_automations(
    q: str = Query(default="", description="Optional search query (alias/description/id)")
):
    qn = (q or "").strip().lower()

    automations_file = _get_automations_file_path()
    if not automations_file:
        raise HTTPException(status_code=412, detail="AUTOMATIONS_FILE_PATH not configured")

    autos = _read_automations_file(automations_file)
    state_by_id, state_by_slug = _get_automation_state_maps()
    items = []
    for a in autos:
        if not isinstance(a, dict):
            continue
        aid = str(a.get("id") or "").strip()
        if not aid:
            continue
        alias = a.get("alias") or aid
        state_info = state_by_id.get(aid) or state_by_slug.get(_slug(alias))
        items.append({
            "id": aid,
            "alias": alias,
            "description": a.get("description") or "",
            "enabled": a.get("enabled"),
            "initial_state": a.get("initial_state"),
            "state": state_info.get("state") if state_info else None,
            "entity_id": state_info.get("entity_id") if state_info else None,
            "source": "ha_file",
        })

    if qn:
        items = [
            it for it in items
            if qn in str(it.get("id","")).lower()
            or qn in str(it.get("alias","")).lower()
            or qn in str(it.get("description","")).lower()
        ]

    items.sort(key=lambda x: str(x.get("alias") or ""))
    return {"items": items}

# ----------------------------
# API: Get automation YAML (from automations.yaml)
# ----------------------------
@app.get("/api/automations/{automation_id}")
def api_get_automation(automation_id: str):
    automations_file = _get_automations_file_path()
    if not automations_file:
        raise HTTPException(status_code=412, detail="AUTOMATIONS_FILE_PATH not configured")

    item = _find_automation_in_file(automations_file, automation_id)
    if not item:
        raise HTTPException(status_code=404, detail="Automation not found")

    alias = item.get("alias") or automation_id
    state_info = _get_automation_state_info(automation_id, alias)
    payload = {
        "id": automation_id,
        "alias": alias,
        "description": item.get("description") or "",
        "source": "ha_file",
        "ha_id": automation_id,
        "state": state_info.get("state") if state_info else None,
        "entity_id": state_info.get("entity_id") if state_info else None,
        "yaml": _yaml_dump(item),
    }
    payload.update(_get_conversation_payload(automation_id))
    return payload


@app.get("/api/automations/{automation_id}/health")
async def api_automation_health(automation_id: str):
    if not (HA_URL and HA_TOKEN):
        raise HTTPException(status_code=412, detail="HA_URL/HA_TOKEN not configured")

    automations_file = _get_automations_file_path()
    if not automations_file:
        raise HTTPException(status_code=412, detail="AUTOMATIONS_FILE_PATH not configured")

    item = _find_automation_in_file(automations_file, automation_id)
    if not item:
        raise HTTPException(status_code=404, detail="Automation not found")

    alias = item.get("alias") or automation_id
    state_info = _get_automation_state_info(automation_id, alias)
    entity_id = state_info.get("entity_id") or f"automation.{_slug(alias)}"

    entity_registry, device_registry, area_registry, states = await ha_ws_fetch()
    state_map = {s.get("entity_id"): s for s in states if isinstance(s, dict)}
    entity_map = {e.get("entity_id"): e for e in entity_registry if isinstance(e, dict)}
    device_ids = {d.get("id") for d in device_registry if isinstance(d, dict)}

    used_entities: set = set()
    _collect_entity_ids(item, used_entities)

    missing_entities = sorted([
        eid for eid in used_entities
        if eid not in state_map and eid not in entity_map
    ])
    disabled_entities = sorted([
        eid for eid in used_entities
        if (entity_map.get(eid) or {}).get("disabled_by")
    ])
    stale_entities = sorted([
        eid for eid in used_entities
        if (entity_map.get(eid) or {}).get("device_id") and (entity_map.get(eid) or {}).get("device_id") not in device_ids
    ])

    state_obj = state_map.get(entity_id) or {}
    attrs = state_obj.get("attributes") or {}

    return {
        "ok": True,
        "automation_id": automation_id,
        "entity_id": entity_id,
        "state": state_info.get("state") or state_obj.get("state"),
        "last_triggered": attrs.get("last_triggered"),
        "last_action": attrs.get("last_action"),
        "last_error": attrs.get("last_error"),
        "missing_entities": missing_entities,
        "disabled_entities": disabled_entities,
        "stale_entities": stale_entities,
        "used_entities_count": len(used_entities),
    }

@app.get("/api/automations/{automation_id}/state")
def api_get_automation_state(automation_id: str):
    alias: Optional[str] = None
    automations_file = _get_automations_file_path()
    if automations_file:
        item = _find_automation_in_file(automations_file, automation_id)
        if item:
            alias = item.get("alias")
    state_info = _get_automation_state_info(automation_id, alias)
    return {
        "id": automation_id,
        "state": state_info.get("state") if state_info else None,
        "entity_id": state_info.get("entity_id") if state_info else None,
    }

@app.post("/api/automations/{automation_id}/state")
def api_set_automation_state(
    automation_id: str,
    body: Dict[str, Any] = Body(default={})
):
    if not (HA_URL and HA_TOKEN):
        raise HTTPException(status_code=412, detail="HA_URL/HA_TOKEN not configured")

    desired = body.get("state")
    enabled = body.get("enabled")
    if desired is None and enabled is None:
        raise HTTPException(status_code=400, detail="state or enabled is required")

    if enabled is not None:
        desired_state = "on" if bool(enabled) else "off"
    else:
        s = str(desired).strip().lower()
        if s in {"on", "off"}:
            desired_state = s
        elif s in {"true", "1"}:
            desired_state = "on"
        elif s in {"false", "0"}:
            desired_state = "off"
        else:
            raise HTTPException(status_code=400, detail="state must be 'on' or 'off'")

    entity_id = body.get("entity_id")
    if not entity_id:
        alias: Optional[str] = None
        automations_file = _get_automations_file_path()
        if automations_file:
            item = _find_automation_in_file(automations_file, automation_id)
            if item:
                alias = item.get("alias")
        state_info = _get_automation_state_info(automation_id, alias)
        entity_id = state_info.get("entity_id") if state_info else None

    if not entity_id:
        raise HTTPException(status_code=404, detail="Automation entity_id not found")

    service = "turn_on" if desired_state == "on" else "turn_off"
    r = requests.post(
        f"{HA_URL}/api/services/automation/{service}",
        headers=ha_headers(),
        json={"entity_id": entity_id},
        timeout=30,
    )
    r.raise_for_status()

    return {"ok": True, "id": automation_id, "entity_id": entity_id, "state": desired_state}


@app.post("/api/automations/{automation_id}/test")
async def api_test_automation(
    automation_id: str,
    body: Dict[str, Any] = Body(default={})
):
    if not (HA_URL and HA_TOKEN):
        raise HTTPException(status_code=412, detail="HA_URL/HA_TOKEN not configured")

    yaml_text = (body.get("yaml") or "").strip()
    overrides_raw = body.get("overrides") or {}
    overrides = overrides_raw if isinstance(overrides_raw, dict) else {}
    time_override = body.get("time") or body.get("time_of_day")
    trigger_id = body.get("trigger_id") or body.get("triggerId")
    trigger_payload = body.get("trigger") if isinstance(body.get("trigger"), dict) else None

    if not trigger_id:
        trigger_id = overrides.pop("__trigger_id", None) or overrides.pop("trigger_id", None)

    if not yaml_text:
        automations_file = _get_automations_file_path()
        if not automations_file:
            raise HTTPException(status_code=412, detail="AUTOMATIONS_FILE_PATH not configured")
        item = _find_automation_in_file(automations_file, automation_id)
        if not item:
            raise HTTPException(status_code=404, detail="Automation not found")
        yaml_text = _yaml_dump(item)

    obj = _coerce_yaml_dict(yaml_text)
    if not obj:
        raise HTTPException(status_code=400, detail="Invalid automation YAML")

    states = _ha_get_states()
    state_map = {s.get("entity_id"): s for s in states if isinstance(s, dict)}
    now_dt = datetime.datetime.now()
    if time_override:
        dt_override = _parse_datetime(time_override)
        if dt_override:
            now_dt = dt_override
        else:
            t_override = _parse_time_value(time_override)
            if t_override:
                now_dt = now_dt.replace(
                    hour=t_override.hour,
                    minute=t_override.minute,
                    second=t_override.second,
                    microsecond=0,
                )
    now_time = now_dt.time()

    ctx = {
        "states": state_map,
        "overrides": overrides,
        "now": now_time,
        "now_dt": now_dt,
        "trigger_id": trigger_id,
        "trigger": trigger_payload,
    }
    logs: List[str] = []
    conditions = obj.get("condition") or obj.get("conditions") or []
    passed, unknown = _eval_conditions_list(conditions, ctx, logs)
    actions: List[str] = []
    if passed and not unknown:
        actions = _simulate_actions(obj.get("action") or obj.get("sequence") or [], ctx, logs)
    else:
        logs.append("Actions skipped due to unmet or unknown conditions.")

    return {
        "ok": True,
        "automation_id": automation_id,
        "conditions_passed": passed,
        "conditions_unknown": unknown,
        "actions": actions,
        "logs": logs,
    }


@app.get("/api/automations/{automation_id}/conversation")
def api_get_automation_conversation(automation_id: str):
    return _get_conversation_payload(automation_id)


@app.post("/api/automations/{automation_id}/conversation")
def api_update_automation_conversation(
    automation_id: str,
    body: Dict[str, Any] = Body(default={})
):
    conversation_id = body.get("conversation_id")
    messages = body.get("messages") or body.get("append") or []
    clear = bool(body.get("clear"))
    replace = bool(body.get("replace"))
    if not isinstance(messages, list):
        messages = []
    item = _local_update_conversation(
        automation_id,
        conversation_id=conversation_id,
        messages=messages,
        clear=clear,
        replace=replace,
    )
    return {
        "ok": True,
        "conversation_id": item.get("conversation_id"),
        "conversation_history": item.get("conversation_history") or [],
    }


@app.delete("/api/automations/{automation_id}/conversation")
def api_clear_automation_conversation(automation_id: str):
    item = _local_update_conversation(automation_id, clear=True, replace=True)
    return {
        "ok": True,
        "conversation_id": item.get("conversation_id"),
        "conversation_history": item.get("conversation_history") or [],
    }

# ----------------------------
# API: Save automation YAML (local draft save + backup)
# ----------------------------
@app.put("/api/automations/{automation_id}")
def api_save_automation(
    automation_id: str,
    body: Dict[str, Any] = Body(default={})
):
    yaml_text = body.get("yaml") or ""
    note = body.get("note") or ""
    if not isinstance(yaml_text, str) or not yaml_text.strip():
        raise HTTPException(status_code=400, detail="Missing yaml")

    # Backup previous local version if present
    db = _load_local_db()
    prev = (db.get("items") or {}).get(automation_id)
    if prev and isinstance(prev.get("yaml"), str):
        _backup_write(automation_id, prev["yaml"], reason="local_before_save")

    # Extract meta from YAML if possible
    try:
        obj = _yaml_load(yaml_text)
        meta = {
            "alias": obj.get("alias") if isinstance(obj, dict) else automation_id,
            "description": obj.get("description") if isinstance(obj, dict) else "",
            "source": "local",
            "ha_id": prev.get("ha_id") if prev else None,
        }
    except Exception:
        meta = {"alias": automation_id, "description": "", "source": "local", "ha_id": prev.get("ha_id") if prev else None}

    _local_upsert(automation_id, meta, yaml_text)
    try:
        _write_version(automation_id, yaml_text, "local_save", note=note)
    except Exception:
        pass
    return {"ok": True}

# ----------------------------
# API: Apply YAML to Home Assistant (backup HA + reload)
# ----------------------------
@app.post("/api/automations/{automation_id}/apply")
def api_apply_automation(
    automation_id: str,
    body: Dict[str, Any] = Body(default={})
):
    yaml_text = body.get("yaml") or ""
    note = body.get("note") or ""
    if not isinstance(yaml_text, str) or not yaml_text.strip():
        raise HTTPException(status_code=400, detail="Missing yaml")

    # Prefer direct file mode if an automations.yaml path is configured (UNC or absolute)
    automations_file = _get_automations_file_path()
    if automations_file:
        try:
            cfg = _yaml_load(yaml_text)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"YAML parse failed: {e}")
        if not isinstance(cfg, dict):
            raise HTTPException(status_code=400, detail="YAML must be a single automation dictionary (not a list).")

        target_id = str(cfg.get("id") or automation_id).strip()
        cfg["id"] = target_id

        autos = _read_automations_file(automations_file)
        replaced = False
        for i, a in enumerate(autos):
            if not isinstance(a, dict):
                continue
            aid = str(a.get("id") or "")
            if aid == automation_id or aid == target_id:
                autos[i] = cfg
                replaced = True
                break
        if not replaced:
            autos.append(cfg)

        _write_automations_file(automations_file, autos)
        reload_automations()
        ha_id = target_id
    else:
        # Backup current HA version if exists
        current = _ha_get_automation(automation_id)
        if current:
            _backup_write(automation_id, _yaml_dump(current), reason="ha_before_apply")

        # Parse YAML -> config
        cfg = _normalize_automation_config_from_yaml(yaml_text)

        # Apply: update if exists, else create new
        if current:
            _ha_update_automation(automation_id, cfg)
            reload_automations()
            ha_id = automation_id
        else:
            ha_id = _ha_create_automation(cfg)

    # Also save to local as the "last applied draft"
    _local_upsert(
        automation_id,
        {
            "alias": (cfg.get("alias") if isinstance(cfg, dict) else None) or automation_id,
            "description": (cfg.get("description") if isinstance(cfg, dict) else "") or "",
            "source": "local",
            "ha_id": ha_id,
        },
        yaml_text,
    )
    try:
        _write_version(automation_id, yaml_text, "apply", note=note)
    except Exception:
        pass

    return {"ok": True, "automation_id": ha_id}

# ----------------------------
# API: Versions (local snapshots)
# ----------------------------
@app.get("/api/automations/{automation_id}/versions")
def api_list_versions(automation_id: str):
    items = _list_versions(automation_id)
    if not items:
        raise HTTPException(status_code=404, detail="No versions found")
    return {"ok": True, "items": items}


@app.post("/api/automations/{automation_id}/versions")
def api_create_version(
    automation_id: str,
    body: Dict[str, Any] = Body(default={})
):
    yaml_text = body.get("yaml") or ""
    note = body.get("note") or ""
    if not isinstance(yaml_text, str) or not yaml_text.strip():
        raise HTTPException(status_code=400, detail="Missing yaml")
    reason = str(body.get("reason") or "manual")
    fn = _write_version(automation_id, yaml_text, reason, note=note)
    return {"ok": True, "id": fn}


@app.get("/api/automations/{automation_id}/versions/{version_id}")
def api_get_version(automation_id: str, version_id: str):
    if not version_id.endswith(".yaml"):
        version_id = version_id + ".yaml"
    # Ensure the requested file belongs to this automation
    base = version_id[:-5]
    parts = base.rsplit("__", 2)
    if len(parts) != 3 or parts[0] != automation_id:
        raise HTTPException(status_code=404, detail="Version not found")
    fp = _version_dir() / version_id
    if not fp.exists():
        raise HTTPException(status_code=404, detail="Version not found")
    return {"ok": True, "id": version_id, "yaml": fp.read_text(encoding="utf-8")}


@app.patch("/api/automations/{automation_id}/versions/{version_id}")
def api_update_version_meta(
    automation_id: str,
    version_id: str,
    body: Dict[str, Any] = Body(default={})
):
    fp, reason, ts = _resolve_version_file(automation_id, version_id)
    meta = _read_version_meta(fp)
    if not isinstance(meta, dict):
        meta = {}
    if "description" in body:
        desc = str(body.get("description") or "").strip()
        meta["description"] = desc[:400]
    meta.setdefault("id", fp.name)
    meta.setdefault("reason", reason)
    meta.setdefault("ts", ts)
    _write_version_meta(fp, meta)
    return {
        "ok": True,
        "id": fp.name,
        "label": meta.get("label") or "",
        "description": meta.get("description") or "",
    }

# ----------------------------
# API: Scripts list (scripts.yaml via UNC path)
# ----------------------------
@app.get("/api/scripts")
def api_list_scripts(
    q: str = Query(default="", description="Optional search query (alias/description/id)")
):
    qn = (q or "").strip().lower()

    scripts_file = _get_scripts_file_path()
    if not scripts_file:
        raise HTTPException(status_code=412, detail="SCRIPTS_FILE_PATH not configured")

    scripts = _read_scripts_file(scripts_file)
    items = []
    for sid, cfg in scripts.items():
        if not isinstance(cfg, dict):
            continue
        items.append({
            "id": sid,
            "alias": cfg.get("alias") or sid,
            "description": cfg.get("description") or "",
            "source": "ha_file",
        })

    if qn:
        items = [
            it for it in items
            if qn in str(it.get("id","")).lower()
            or qn in str(it.get("alias","")).lower()
            or qn in str(it.get("description","")).lower()
        ]

    items.sort(key=lambda x: str(x.get("alias") or ""))
    return {"items": items}


# ----------------------------
# API: Get script YAML (from scripts.yaml)
# ----------------------------
@app.get("/api/scripts/{script_id}")
def api_get_script(script_id: str):
    scripts_file = _get_scripts_file_path()
    if not scripts_file:
        raise HTTPException(status_code=412, detail="SCRIPTS_FILE_PATH not configured")

    scripts = _read_scripts_file(scripts_file)
    item = scripts.get(script_id)
    if not item:
        raise HTTPException(status_code=404, detail="Script not found")

    payload = {
        "id": script_id,
        "alias": item.get("alias") or script_id,
        "description": item.get("description") or "",
        "source": "ha_file",
        "ha_id": script_id,
        "yaml": _yaml_dump(item),
    }
    payload.update(_get_conversation_payload(_script_key(script_id)))
    return payload


@app.get("/api/scripts/{script_id}/conversation")
def api_get_script_conversation(script_id: str):
    return _get_conversation_payload(_script_key(script_id))


@app.post("/api/scripts/{script_id}/conversation")
def api_update_script_conversation(
    script_id: str,
    body: Dict[str, Any] = Body(default={})
):
    conversation_id = body.get("conversation_id")
    messages = body.get("messages") or body.get("append") or []
    clear = bool(body.get("clear"))
    replace = bool(body.get("replace"))
    if not isinstance(messages, list):
        messages = []
    item = _local_update_conversation(
        _script_key(script_id),
        conversation_id=conversation_id,
        messages=messages,
        clear=clear,
        replace=replace,
    )
    return {
        "ok": True,
        "conversation_id": item.get("conversation_id"),
        "conversation_history": item.get("conversation_history") or [],
    }


@app.delete("/api/scripts/{script_id}/conversation")
def api_clear_script_conversation(script_id: str):
    item = _local_update_conversation(_script_key(script_id), clear=True, replace=True)
    return {
        "ok": True,
        "conversation_id": item.get("conversation_id"),
        "conversation_history": item.get("conversation_history") or [],
    }


# ----------------------------
# API: Save script YAML (local draft save + backup)
# ----------------------------
@app.put("/api/scripts/{script_id}")
def api_save_script(
    script_id: str,
    body: Dict[str, Any] = Body(default={})
):
    yaml_text = body.get("yaml") or ""
    note = body.get("note") or ""
    if not isinstance(yaml_text, str) or not yaml_text.strip():
        raise HTTPException(status_code=400, detail="Missing yaml")

    script_key = _script_key(script_id)

    # Backup previous local version if present
    db = _load_local_db()
    prev = (db.get("items") or {}).get(script_key)
    if prev and isinstance(prev.get("yaml"), str):
        _backup_write(script_key, prev["yaml"], reason="local_before_save")

    # Extract meta from YAML if possible
    try:
        obj = _yaml_load(yaml_text)
        meta = {
            "alias": obj.get("alias") if isinstance(obj, dict) else script_id,
            "description": obj.get("description") if isinstance(obj, dict) else "",
            "source": "local",
            "ha_id": prev.get("ha_id") if prev else None,
        }
    except Exception:
        meta = {"alias": script_id, "description": "", "source": "local", "ha_id": prev.get("ha_id") if prev else None}

    _local_upsert(script_key, meta, yaml_text)
    try:
        _write_version(script_key, yaml_text, "local_save", note=note)
    except Exception:
        pass
    return {"ok": True}


# ----------------------------
# API: Apply script YAML to Home Assistant (backup + reload)
# ----------------------------
@app.post("/api/scripts/{script_id}/apply")
def api_apply_script(
    script_id: str,
    body: Dict[str, Any] = Body(default={})
):
    yaml_text = body.get("yaml") or ""
    note = body.get("note") or ""
    if not isinstance(yaml_text, str) or not yaml_text.strip():
        raise HTTPException(status_code=400, detail="Missing yaml")

    scripts_file = _get_scripts_file_path()
    if not scripts_file:
        raise HTTPException(status_code=412, detail="SCRIPTS_FILE_PATH not configured")

    try:
        cfg = _yaml_load(yaml_text)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"YAML parse failed: {e}")
    if not isinstance(cfg, dict):
        raise HTTPException(status_code=400, detail="YAML must be a single script dictionary (not a list).")

    scripts = _read_scripts_file(scripts_file)
    scripts[script_id] = cfg
    _write_scripts_file(scripts_file, scripts)
    reload_scripts()

    script_key = _script_key(script_id)
    _local_upsert(
        script_key,
        {
            "alias": (cfg.get("alias") if isinstance(cfg, dict) else None) or script_id,
            "description": (cfg.get("description") if isinstance(cfg, dict) else "") or "",
            "source": "local",
            "ha_id": script_id,
        },
        yaml_text,
    )
    try:
        _write_version(script_key, yaml_text, "apply", note=note)
    except Exception:
        pass

    return {"ok": True, "script_id": script_id}


# ----------------------------
# API: Script Versions (local snapshots)
# ----------------------------
@app.get("/api/scripts/{script_id}/versions")
def api_list_script_versions(script_id: str):
    items = _list_versions(_script_key(script_id))
    if not items:
        raise HTTPException(status_code=404, detail="No versions found")
    return {"ok": True, "items": items}


@app.post("/api/scripts/{script_id}/versions")
def api_create_script_version(
    script_id: str,
    body: Dict[str, Any] = Body(default={})
):
    yaml_text = body.get("yaml") or ""
    note = body.get("note") or ""
    if not isinstance(yaml_text, str) or not yaml_text.strip():
        raise HTTPException(status_code=400, detail="Missing yaml")
    reason = str(body.get("reason") or "manual")
    fn = _write_version(_script_key(script_id), yaml_text, reason, note=note)
    return {"ok": True, "id": fn}


@app.get("/api/scripts/{script_id}/versions/{version_id}")
def api_get_script_version(script_id: str, version_id: str):
    if not version_id.endswith(".yaml"):
        version_id = version_id + ".yaml"
    base = version_id[:-5]
    parts = base.rsplit("__", 2)
    if len(parts) != 3 or parts[0] != _script_key(script_id):
        raise HTTPException(status_code=404, detail="Version not found")
    fp = _version_dir() / version_id
    if not fp.exists():
        raise HTTPException(status_code=404, detail="Version not found")
    return {"ok": True, "id": version_id, "yaml": fp.read_text(encoding="utf-8")}


@app.patch("/api/scripts/{script_id}/versions/{version_id}")
def api_update_script_version_meta(
    script_id: str,
    version_id: str,
    body: Dict[str, Any] = Body(default={})
):
    fp, reason, ts = _resolve_version_file(_script_key(script_id), version_id)
    meta = _read_version_meta(fp)
    if not isinstance(meta, dict):
        meta = {}
    if "description" in body:
        desc = str(body.get("description") or "").strip()
        meta["description"] = desc[:400]
    meta.setdefault("id", fp.name)
    meta.setdefault("reason", reason)
    meta.setdefault("ts", ts)
    _write_version_meta(fp, meta)
    return {
        "ok": True,
        "id": fp.name,
        "label": meta.get("label") or "",
        "description": meta.get("description") or "",
    }

# ----------------------------
# API: AI edit current automation (YAML + prompt -> updated YAML)
# ----------------------------
def _clean_quoted_value(val: str) -> str:
    v = (val or "").strip()
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        v = v[1:-1].strip()
    return v

def _ensure_list(val: Any) -> List[Any]:
    if val is None:
        return []
    return val if isinstance(val, list) else [val]

def _apply_local_edit_rules(prompt: str, obj: Dict[str, Any], entity_type: str) -> Tuple[bool, List[str]]:
    if not prompt or not isinstance(obj, dict):
        return False, []
    p = prompt.strip()
    lower = p.lower()
    changes: List[str] = []

    alias_patterns = [
        r"^\s*(rename|name|alias)\s+(this\s+)?(automation|script)?\s*(to|as)?\s*['\"]?(.+?)['\"]?\s*$",
        r"^\s*set\s+alias\s+(to|as)\s*['\"]?(.+?)['\"]?\s*$",
        r"^\s*change\s+alias\s+(to|as)\s*['\"]?(.+?)['\"]?\s*$",
    ]
    for pat in alias_patterns:
        m = re.match(pat, p, re.I)
        if m:
            new_alias = _clean_quoted_value(m.group(m.lastindex))
            if new_alias:
                obj["alias"] = new_alias
                changes.append(f"alias -> {new_alias}")
            break

    desc_patterns = [
        r"^\s*set\s+description\s+(to|as)\s*['\"]?(.+?)['\"]?\s*$",
        r"^\s*change\s+description\s+(to|as)\s*['\"]?(.+?)['\"]?\s*$",
    ]
    for pat in desc_patterns:
        m = re.match(pat, p, re.I)
        if m:
            new_desc = _clean_quoted_value(m.group(m.lastindex))
            obj["description"] = new_desc
            changes.append("description updated")
            break

    m = re.search(r"\bmode\s*(to|=)?\s*(single|restart|queued|parallel)\b", lower)
    if m:
        mode = m.group(2)
        obj["mode"] = mode
        changes.append(f"mode -> {mode}")

    if re.search(r"\b(start|initial_state)\s*(is\s*)?(disabled|off|false)\b", lower):
        obj["initial_state"] = False
        changes.append("initial_state -> false")
    elif re.search(r"\b(start|initial_state)\s*(is\s*)?(enabled|on|true)\b", lower):
        obj["initial_state"] = True
        changes.append("initial_state -> true")

    m = re.search(r"\bchange\s+service\s+([a-z_]+\.[a-z0-9_]+)\s*(to|->)\s*([a-z_]+\.[a-z0-9_]+)\b", lower)
    if m:
        src = m.group(1)
        dst = m.group(3)
        key = "sequence" if entity_type == "script" else "action"
        seq = _ensure_list(obj.get(key))
        replaced = 0
        for step in seq:
            if isinstance(step, dict) and str(step.get("service", "")).lower() == src:
                step["service"] = dst
                replaced += 1
        if replaced:
            obj[key] = seq
            changes.append(f"service {src} -> {dst}")

    if entity_type == "automation":
        m = re.search(r"\b(only if|if)\s+([a-z_]+\.[a-z0-9_]+)\s+(is|=)\s+([a-z0-9_]+)\b", lower)
        if m:
            entity_id = m.group(2)
            state_val = m.group(4)
            cond = {"condition": "state", "entity_id": entity_id, "state": state_val}
            conditions = _ensure_list(obj.get("condition") or obj.get("conditions"))
            exists = any(isinstance(c, dict) and c.get("condition") == "state" and c.get("entity_id") == entity_id and str(c.get("state")) == state_val for c in conditions)
            if not exists:
                conditions.append(cond)
                obj["condition"] = conditions
                changes.append(f"add condition {entity_id} is {state_val}")

    return bool(changes), changes

@app.post("/api/automations/{automation_id}/ai_update")
def api_ai_update_automation(
    automation_id: str,
    body: Dict[str, Any] = Body(default={})
):
    prompt = (body.get("prompt") or "").strip()
    yaml_text = (body.get("yaml") or "").strip()
    local_only = bool(body.get("local_only", False))
    if not prompt or not yaml_text:
        raise HTTPException(status_code=400, detail="Missing prompt or yaml")

    try:
        obj = _yaml_load(yaml_text)
        if isinstance(obj, dict):
            changed, notes = _apply_local_edit_rules(prompt, obj, "automation")
            if changed:
                capabilities = load_capabilities()
                obj["alias"] = apply_ai_alias_prefix(
                    obj.get("alias") or automation_id,
                    capabilities,
                    mode="edit",
                    entity_type="automation",
                )
                return {"ok": True, "yaml": _yaml_dump(obj), "message": f"Applied local edit: {', '.join(notes)}"}
            if local_only:
                return {"ok": False, "local_only": True, "message": "No local edit matched."}
    except Exception:
        pass

    # Tell the existing agent to do an EDIT, not a new build
    ai_text = (
        "You are an expert Home Assistant automation editor.\n"
        "Task: modify the PROVIDED automation YAML according to the user request.\n"
        "Rules:\n"
        "- Return ONLY valid YAML for ONE automation dict (not a list), no markdown.\n"
        "- Preserve intent and structure unless user asked otherwise.\n"
        "- Do not invent entity_ids; only use ones that already exist in the YAML unless user explicitly provided them.\n"
        "- Keep alias and description unless user wants them changed.\n"
        f"\nUSER REQUEST:\n{prompt}\n"
        f"\nCURRENT AUTOMATION YAML:\n{yaml_text}\n"
    )

    try:
        updated = call_conversation_agent(AI_EDIT_AGENT_ID, ai_text)
    except requests.exceptions.ReadTimeout:
        raise HTTPException(status_code=504, detail="Home Assistant conversation timed out. Try again or increase HA_CONVERSATION_TIMEOUT.")

    # Basic sanity: must parse to dict
    try:
        obj = _yaml_load(updated)
        if not isinstance(obj, dict):
            raise ValueError("AI did not return a dict YAML")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI returned invalid YAML: {e}")

    capabilities = load_capabilities()
    obj["alias"] = apply_ai_alias_prefix(
        obj.get("alias") or automation_id,
        capabilities,
        mode="edit",
        entity_type="automation",
    )
    return {"ok": True, "yaml": _yaml_dump(obj)}


# ----------------------------
# API: AI edit current script (YAML + prompt -> updated YAML)
# ----------------------------
@app.post("/api/scripts/{script_id}/ai_update")
def api_ai_update_script(
    script_id: str,
    body: Dict[str, Any] = Body(default={})
):
    prompt = (body.get("prompt") or "").strip()
    yaml_text = (body.get("yaml") or "").strip()
    local_only = bool(body.get("local_only", False))
    if not prompt or not yaml_text:
        raise HTTPException(status_code=400, detail="Missing prompt or yaml")

    try:
        obj = _yaml_load(yaml_text)
        if isinstance(obj, dict):
            changed, notes = _apply_local_edit_rules(prompt, obj, "script")
            if changed:
                capabilities = load_capabilities()
                obj["alias"] = apply_ai_alias_prefix(
                    obj.get("alias") or script_id,
                    capabilities,
                    mode="edit",
                    entity_type="script",
                )
                return {"ok": True, "yaml": _yaml_dump(obj), "message": f"Applied local edit: {', '.join(notes)}"}
            if local_only:
                return {"ok": False, "local_only": True, "message": "No local edit matched."}
    except Exception:
        pass

    ai_text = (
        "You are an expert Home Assistant script editor.\n"
        "Task: modify the PROVIDED script YAML according to the user request.\n"
        "Rules:\n"
        "- Return ONLY valid YAML for ONE script dict (not a list), no markdown.\n"
        "- Preserve intent and structure unless user asked otherwise.\n"
        "- Do not invent entity_ids; only use ones that already exist in the YAML unless user explicitly provided them.\n"
        "- Keep alias and description unless user wants them changed.\n"
        f"\nUSER REQUEST:\n{prompt}\n"
        f"\nCURRENT SCRIPT YAML:\n{yaml_text}\n"
    )

    try:
        updated = call_conversation_agent(AI_EDIT_AGENT_ID, ai_text)
    except requests.exceptions.ReadTimeout:
        raise HTTPException(status_code=504, detail="Home Assistant conversation timed out. Try again or increase HA_CONVERSATION_TIMEOUT.")

    try:
        obj = _yaml_load(updated)
        if not isinstance(obj, dict):
            raise ValueError("AI did not return a dict YAML")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI returned invalid YAML: {e}")

    capabilities = load_capabilities()
    obj["alias"] = apply_ai_alias_prefix(
        obj.get("alias") or script_id,
        capabilities,
        mode="edit",
        entity_type="script",
    )
    return {"ok": True, "yaml": _yaml_dump(obj)}


# ----------------------------
# API: Architect chat + builder handoff
# ----------------------------
@app.post("/api/architect/chat")
async def api_architect_chat(
    req: ArchitectChatReq,
    x_ha_agent_secret: str = Header(default="")
):
    require_auth(x_ha_agent_secret)
    trace_token = _agent_trace_start()

    if not (HA_URL and HA_TOKEN):
        raise HTTPException(status_code=500, detail="Server not configured")

    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Missing text")

    entity_type = (req.entity_type or "automation").strip().lower()
    if entity_type not in ("automation", "script"):
        entity_type = "automation"
    entity_id = req.entity_id or req.automation_id
    mode = (req.mode or ("edit" if entity_id else "create")).lower()
    label = "script" if entity_type == "script" else "automation"
    label_upper = label.upper()

    prompt_parts: List[str] = []
    context_pack: Optional[Dict[str, Any]] = None
    if not req.conversation_id:
        try:
            entity_registry, device_registry, area_registry, states = await ha_ws_fetch()
            history = []
            if entity_id:
                payload = _get_conversation_payload(_script_key(entity_id) if entity_type == "script" else entity_id)
                history = payload.get("conversation_history") if isinstance(payload, dict) else []
            context_pack = build_context_pack_from_regs(
                text,
                req.current_yaml if req.include_context else None,
                entity_type,
                entity_registry,
                device_registry,
                area_registry,
                states,
                history=history if isinstance(history, list) else [],
            )
        except Exception:
            context_pack = None
        if context_pack:
            prompt_parts.append("CONTEXT_PACK_JSON:\n" + json.dumps(context_pack, ensure_ascii=False))
        else:
            caps_json = json.dumps(slim_capabilities_for_llm(load_capabilities()), ensure_ascii=False)
            prompt_parts.append("CAPABILITIES_JSON:\n" + caps_json)

    if req.include_context and req.current_yaml and entity_id:
        summary = (context_pack or {}).get("summary") if context_pack else _summarize_yaml_for_prompt(text, req.current_yaml)
        if context_pack and summary:
            prompt_parts.append(f"Context: You are discussing edits to {label} {entity_id}. Summary is in CONTEXT_PACK_JSON.")
        elif summary:
            prompt_parts.append(
                f"Context: You are discussing edits to {label} {entity_id}.\n"
                f"CURRENT {label_upper} SUMMARY JSON:\n{json.dumps(summary, ensure_ascii=False)}"
            )
        else:
            prompt_parts.append(
                f"Context: You are discussing edits to {label} {entity_id}.\n"
                f"CURRENT {label_upper} YAML:\n{req.current_yaml}"
            )
    elif mode == "create":
        prompt_parts.append(f"Context: You are planning a new {label}.")

    prompt_parts.append(
        "Guidance: If the user does not specify exact entity_ids or services, infer them from the provided context "
        "(CONTEXT_PACK_JSON or CAPABILITIES_JSON) and known candidates; make reasonable assumptions and proceed."
    )
    prompt_parts.append(
        "FORMAT: Use markdown-style hints for readability: start sections with '### Heading', use bullet lists "
        "with '-' or numbered lists '1.', and use ##bold## for emphasis. Keep it concise."
    )

    prompt_parts.append(f"USER MESSAGE:\n{text}")
    prompt = "\n\n".join(prompt_parts)

    try:
        reply, conv_id = call_conversation_agent_full(ARCHITECT_AGENT_ID, prompt, req.conversation_id)
    except requests.exceptions.ReadTimeout:
        raise HTTPException(status_code=504, detail="Home Assistant conversation timed out. Try again or increase HA_CONVERSATION_TIMEOUT.")

    updated_entities: List[str] = []
    updated_scripts: List[str] = []
    updated_context: List[str] = []
    if entity_id:
        try:
            key = _script_key(entity_id) if entity_type == "script" else entity_id
            _local_update_conversation(
                key,
                conversation_id=conv_id,
                messages=[
                    {"role": "user", "text": text},
                    {"role": "assistant", "text": reply},
                ],
            )
        except Exception as e:
            if DEBUG:
                print("CONVERSATION_SAVE_FAILED:", repr(e))
    if req.save_entity_hint:
        try:
            updated_entities = update_capabilities_entity_hints(text)
            updated_scripts = update_capabilities_script_hints(text)
            updated_context = update_capabilities_context_hints(text)
        except Exception as e:
            if DEBUG:
                print("CAPABILITIES_UPDATE_FAILED:", repr(e))

    agent_status = _agent_trace_finish(trace_token)
    return {
        "ok": True,
        "reply": reply,
        "conversation_id": conv_id,
        "saved_entities": updated_entities,
        "saved_scripts": updated_scripts,
        "saved_context": updated_context,
        "agent_status": agent_status,
    }


@app.post("/api/architect/finalize")
async def api_architect_finalize(
    req: ArchitectFinalizeReq,
    x_ha_agent_secret: str = Header(default="")
):
    require_auth(x_ha_agent_secret)
    trace_token = _agent_trace_start()

    if not (HA_URL and HA_TOKEN):
        raise HTTPException(status_code=500, detail="Server not configured")

    entity_type = (req.entity_type or "automation").strip().lower()
    if entity_type not in ("automation", "script"):
        entity_type = "automation"
    entity_id = req.entity_id or req.automation_id
    mode = (req.mode or ("edit" if entity_id else "create")).lower()
    label = "script" if entity_type == "script" else "automation"
    label_upper = label.upper()
    context_pack = None

    finalize_parts = [
        "We are ready to hand off to the builder agent.",
        f"Write the FINAL BUILDER PROMPT as direct instructions to the builder agent for a Home Assistant {label}.",
        "Do NOT address the user. Do NOT ask questions. No commentary.",
        "Assume clarifications are complete. If details are missing, make the best reasonable assumptions and proceed.",
        "Return ONLY the prompt text (plain instructions). Use readable formatting: '### Heading', bullet lists with '-', numbered lists '1.', and ##bold## for emphasis.",
    ]
    summary_for_finalize = None
    if mode == "edit" and entity_id:
        finalize_parts.append(f"This is an EDIT of existing {label} id: {entity_id}.")
        if req.include_context and req.current_yaml:
            summary_for_finalize = _summarize_yaml_for_prompt(req.text or "", req.current_yaml)
            if summary_for_finalize:
                finalize_parts.append(f"Use the PROVIDED {label_upper} SUMMARY JSON as the base and modify it.")
            else:
                finalize_parts.append(f"Use the PROVIDED {label_upper} YAML as the base and modify it.")
    if not req.conversation_id:
        try:
            entity_registry, device_registry, area_registry, states = await ha_ws_fetch()
            history = []
            if entity_id:
                payload = _get_conversation_payload(_script_key(entity_id) if entity_type == "script" else entity_id)
                history = payload.get("conversation_history") if isinstance(payload, dict) else []
            context_pack = build_context_pack_from_regs(
                req.text or "",
                req.current_yaml if req.include_context else None,
                entity_type,
                entity_registry,
                device_registry,
                area_registry,
                states,
                history=history if isinstance(history, list) else [],
            )
        except Exception:
            context_pack = None
        if context_pack:
            finalize_parts.append("CONTEXT_PACK_JSON:\n" + json.dumps(context_pack, ensure_ascii=False))
        else:
            caps_json = json.dumps(slim_capabilities_for_llm(load_capabilities()), ensure_ascii=False)
            finalize_parts.append("CAPABILITIES_JSON:\n" + caps_json)

    if summary_for_finalize and not (context_pack and context_pack.get("summary")):
        finalize_parts.append(f"{label_upper}_SUMMARY_JSON:\n{json.dumps(summary_for_finalize, ensure_ascii=False)}")
    elif req.include_context and req.current_yaml and not summary_for_finalize:
        finalize_parts.append(f"{label_upper}_YAML:\n{req.current_yaml}")
    finalize_parts.append(
        "Guidance: If the user does not specify exact entity_ids or services, infer them from the provided context "
        "(CONTEXT_PACK_JSON or CAPABILITIES_JSON) and known candidates; make reasonable assumptions."
    )
    if req.text:
        finalize_parts.append(f"USER MESSAGE:\n{req.text}")
    finalize_prompt = "\n".join(finalize_parts)

    try:
        final_text, conv_id = call_conversation_agent_full(ARCHITECT_AGENT_ID, finalize_prompt, req.conversation_id)
    except requests.exceptions.ReadTimeout:
        raise HTTPException(status_code=504, detail="Home Assistant conversation timed out. Try again or increase HA_CONVERSATION_TIMEOUT.")

    if not final_text:
        raise HTTPException(status_code=500, detail="Architect did not return a final prompt")

    # Build config via builder agent
    ha_config, _ = await build_ha_config_from_text(
        final_text,
        source="architect",
        current_yaml=req.current_yaml if mode == "edit" else None,
        entity_type=entity_type,
    )

    note = f"architect:{final_text[:200]}".strip()

    agent_status = _agent_trace_finish(trace_token)
    history_payload = None
    if entity_id:
        history_payload = _get_conversation_payload(_script_key(entity_id) if entity_type == "script" else entity_id)
    history_items = (history_payload or {}).get("conversation_history") if isinstance(history_payload, dict) else []
    saved_info = save_learned_from_history(history_items or [], req.text)

    if entity_type == "script":
        if mode == "edit" and entity_id:
            yaml_text = _yaml_dump(ha_config)
            out = api_apply_script(entity_id, body={"yaml": yaml_text, "note": note})
            return {
                "ok": True,
                "conversation_id": conv_id,
                "final_prompt": final_text,
                "entity_type": "script",
                "entity_id": out.get("script_id", entity_id),
                "script_id": out.get("script_id", entity_id),
                "alias": ha_config.get("alias") or entity_id,
                "yaml": yaml_text,
                "agent_status": agent_status,
                **saved_info,
            }

        scripts_file = _get_scripts_file_path()
        if not scripts_file:
            raise HTTPException(status_code=412, detail="SCRIPTS_FILE_PATH not configured")
        scripts = _read_scripts_file(scripts_file)
        new_id = _suggest_script_id(ha_config.get("alias") or "script", list(scripts.keys()))
        yaml_text = _yaml_dump(ha_config)
        out = api_apply_script(new_id, body={"yaml": yaml_text, "note": note})
        return {
            "ok": True,
            "conversation_id": conv_id,
            "final_prompt": final_text,
            "entity_type": "script",
            "entity_id": out.get("script_id", new_id),
            "script_id": out.get("script_id", new_id),
            "alias": ha_config.get("alias") or new_id,
            "yaml": yaml_text,
            "agent_status": agent_status,
            **saved_info,
        }

    if mode == "edit" and entity_id:
        ha_config["id"] = entity_id
        yaml_text = _yaml_dump(ha_config)
        out = api_apply_automation(entity_id, body={"yaml": yaml_text, "note": note})
        return {
            "ok": True,
            "conversation_id": conv_id,
            "final_prompt": final_text,
            "entity_type": "automation",
            "entity_id": out.get("automation_id", entity_id),
            "automation_id": out.get("automation_id", entity_id),
            "yaml": yaml_text,
            "agent_status": agent_status,
            **saved_info,
        }

    # Create new automation
    automations_file = _get_automations_file_path()
    if automations_file:
        new_id = str(int(time.time() * 1000))
        ha_config["id"] = new_id
        yaml_text = _yaml_dump(ha_config)
        out = api_apply_automation(new_id, body={"yaml": yaml_text, "note": note})
        return {
            "ok": True,
            "conversation_id": conv_id,
            "final_prompt": final_text,
            "entity_type": "automation",
            "entity_id": out.get("automation_id", new_id),
            "automation_id": out.get("automation_id", new_id),
            "alias": ha_config.get("alias") or new_id,
            "yaml": yaml_text,
            "agent_status": agent_status,
            **saved_info,
        }

    automation_id = create_or_update_automation(ha_config)
    ha_config["id"] = automation_id
    yaml_text = _yaml_dump(ha_config)

    announce(f"Automation created: {ha_config['alias']}. {ha_config.get('description','')}".strip())

    return {
        "ok": True,
        "conversation_id": conv_id,
        "final_prompt": final_text,
        "entity_type": "automation",
        "entity_id": automation_id,
        "automation_id": automation_id,
        "alias": ha_config.get("alias") or automation_id,
        "yaml": yaml_text,
    }

# ----------------------------
# NORMALIZATION: enforce your "house style"
# ----------------------------
def resolve_light_target(area_name: str, capabilities: Dict[str, Any], area_registry: List[Dict[str, Any]]) -> Dict[str, Any]:
    lights_cfg = (capabilities.get("lights") or {})
    area_group_map = (lights_cfg.get("area_group_map") or {})
    canonical = normalize_area_name(area_name, capabilities)

    if lights_cfg.get("prefer_groups", True) and canonical in area_group_map:
        return {"entity_id": area_group_map[canonical]}

    area_name_to_id = {a.get("name"): a.get("area_id") for a in area_registry}
    if canonical in area_name_to_id:
        return {"area_id": area_name_to_id[canonical]}

    return {}


def normalize_actions(actions: List[Any], area_registry: List[Dict[str, Any]], candidates: List[Dict[str, Any]], capabilities: Dict[str, Any]) -> List[Any]:
    out: List[Any] = []
    for a in actions or []:
        if isinstance(a, dict) and a.get("service") in ("HassTurnOn", "HassTurnOff"):
            data = a.get("data") or {}
            area_name = data.get("area")
            domains = data.get("domain") or []
            service = "homeassistant.turn_on" if a["service"] == "HassTurnOn" else "homeassistant.turn_off"

            if area_name and "light" in domains:
                target = resolve_light_target(area_name, capabilities, area_registry)
                out.append({"service": service, "target": target} if target else {"service": service})
                continue

            entity_ids: List[str] = []
            if area_name:
                canon = normalize_area_name(area_name, capabilities)
                for c in candidates:
                    if c.get("area") == canon and (not domains or c.get("domain") in domains):
                        eid = c.get("entity_id")
                        if eid:
                            entity_ids.append(eid)

            target: Dict[str, Any] = {"entity_id": entity_ids} if entity_ids else {}
            out.append({"service": service, "target": target} if target else {"service": service})
            continue

        out.append(a)
    return out


def _speech_cfg(capabilities: Dict[str, Any]) -> Dict[str, Any]:
    speech = capabilities.get("speech") or {}
    if "say_script" in speech or "prompt_script" in speech:
        return speech

    normal = speech.get("normal_announce") or {}
    prompted = speech.get("prompted_jarvis") or {}
    return {
        "say_script": normal.get("entity_id"),
        "say_field": normal.get("field", "message"),
        "prompt_script": prompted.get("entity_id"),
        "prompt_field": prompted.get("field", "prompt"),
        "default_mode": "say",
    }


def normalize_speech_actions(actions: List[Any], capabilities: Dict[str, Any]) -> List[Any]:
    """
    Rewrite non-mobile notify/tts into your preferred speech scripts,
    BUT NEVER rewrite mobile_app notifications (or actionable phone notifications).
    """
    speech_cfg = _speech_cfg(capabilities)
    say_script = speech_cfg.get("say_script")
    say_field = speech_cfg.get("say_field", "message")
    prompt_script = speech_cfg.get("prompt_script")
    prompt_field = speech_cfg.get("prompt_field", "prompt")
    default_mode = speech_cfg.get("default_mode", "say")

    notifications = capabilities.get("notifications") or {}
    keep_prefixes = tuple(notifications.get("keep_notify_prefixes") or ["notify.mobile_app_"])
    primary_phone_notify = notifications.get("primary_phone_notify") or ""
    speech_policy = (capabilities.get("speech") or {}).get("rewrite_non_mobile_notify_to_speech", True)

    out: List[Any] = []
    for a in actions or []:
        if not isinstance(a, dict) or not isinstance(a.get("service"), str):
            out.append(a)
            continue

        svc = a["service"]
        data = a.get("data") or {}

        if svc in (say_script, prompt_script):
            out.append(a)
            continue

        if svc.startswith("notify."):
            # KEEP mobile app notifies
            if svc == primary_phone_notify or svc.startswith(keep_prefixes):
                out.append(a)
                continue

            # KEEP actionable-looking notifications
            inner = data.get("data") if isinstance(data.get("data"), dict) else {}
            if isinstance(inner.get("actions"), list) or "tag" in inner or "clickAction" in inner:
                out.append(a)
                continue

            if not speech_policy:
                out.append(a)
                continue

            msg = data.get("message") or ""
            use_prompt = bool(data.get("use_prompt", False))
            mode = "prompt" if use_prompt else default_mode

            if mode == "prompt" and prompt_script:
                out.append({"service": prompt_script, "data": {prompt_field: msg}})
            elif say_script:
                out.append({"service": say_script, "data": {say_field: msg}})
            else:
                out.append(a)
            continue

        if svc.startswith("tts.") and speech_policy:
            msg = data.get("message") or data.get("text") or ""
            use_prompt = bool(data.get("use_prompt", False))
            mode = "prompt" if use_prompt else default_mode

            if mode == "prompt" and prompt_script:
                out.append({"service": prompt_script, "data": {prompt_field: msg}})
            elif say_script:
                out.append({"service": say_script, "data": {say_field: msg}})
            else:
                out.append(a)
            continue

        out.append(a)

    return out


def normalize_tv_power_actions(actions: List[Any], capabilities: Dict[str, Any]) -> List[Any]:
    """
    If a device uses a power toggle script, rewrite generic on/off calls to that script.
    """
    media = (capabilities.get("media") or {})
    raw_rules = media.get("power_toggle_rules") or []
    if not isinstance(raw_rules, list) or not raw_rules:
        return actions

    default_services = (
        "media_player.turn_off",
        "media_player.turn_on",
        "switch.turn_off",
        "switch.turn_on",
        "homeassistant.turn_off",
        "homeassistant.turn_on",
    )

    def _coerce_str_list(val: Any) -> List[str]:
        if isinstance(val, str):
            return [val]
        if isinstance(val, list):
            return [x for x in val if isinstance(x, str)]
        return []

    def _target_entities(target: Any) -> List[str]:
        if not isinstance(target, dict):
            return []
        eid = target.get("entity_id")
        if isinstance(eid, str):
            return [eid]
        if isinstance(eid, list):
            return [x for x in eid if isinstance(x, str)]
        return []

    out: List[Any] = []
    for a in actions or []:
        if not isinstance(a, dict):
            out.append(a)
            continue

        svc = a.get("service")
        if not isinstance(svc, str):
            out.append(a)
            continue

        target = a.get("target") or {}
        ents = _target_entities(target)
        if not ents:
            out.append(a)
            continue

        rewritten = False
        for rule in raw_rules:
            if not isinstance(rule, dict):
                continue
            script = rule.get("script") or rule.get("power_toggle_script")
            if not isinstance(script, str) or not script.strip():
                continue

            match_services = _coerce_str_list(rule.get("match_services") or rule.get("services"))
            if not match_services:
                match_services = list(default_services)
            if svc not in match_services:
                continue

            match_entities = _coerce_str_list(rule.get("match_entities") or rule.get("entity_id"))
            for key in ("state_proxy", "tv_entity"):
                match_entities.extend(_coerce_str_list(rule.get(key)))
            match_entities = [m for m in match_entities if isinstance(m, str)]

            match_substrings = _coerce_str_list(rule.get("match_entity_substrings"))

            if match_entities and any(e in match_entities for e in ents):
                out.append({"service": "script.turn_on", "target": {"entity_id": script}})
                rewritten = True
                break

            if match_substrings:
                lowered = [s.lower() for s in match_substrings if s]
                for e in ents:
                    if any(s in e.lower() for s in lowered):
                        out.append({"service": "script.turn_on", "target": {"entity_id": script}})
                        rewritten = True
                        break
            if rewritten:
                break

        if not rewritten:
            out.append(a)

    return out


def _get_cover_position_rules(capabilities: Dict[str, Any]) -> List[Dict[str, Any]]:
    covers = (capabilities or {}).get("covers") or {}
    raw_rules = covers.get("position_rules") if isinstance(covers, dict) else None
    if raw_rules is None:
        raw_rules = covers if isinstance(covers, list) else []
    if not isinstance(raw_rules, list):
        return []

    def _coerce_str_list(val: Any) -> List[str]:
        if isinstance(val, str):
            return [val]
        if isinstance(val, list):
            return [x for x in val if isinstance(x, str)]
        return []

    rules: List[Dict[str, Any]] = []
    for rule in raw_rules:
        if not isinstance(rule, dict):
            continue
        entities = _coerce_str_list(rule.get("match_entities") or rule.get("entity_id"))
        if not entities:
            continue
        open_pos = int(rule.get("open_position", 50))
        closed_pos = int(rule.get("closed_position", 1))
        rules.append({
            "entities": entities,
            "open_position": open_pos,
            "closed_position": closed_pos,
        })
    return rules


def normalize_cover_actions(actions: List[Any], capabilities: Dict[str, Any]) -> List[Any]:
    rules = _get_cover_position_rules(capabilities)
    if not rules:
        return actions

    def _target_entities(target: Any) -> List[str]:
        if not isinstance(target, dict):
            return []
        eid = target.get("entity_id")
        if isinstance(eid, str):
            return [eid]
        if isinstance(eid, list):
            return [x for x in eid if isinstance(x, str)]
        return []

    out: List[Any] = []
    for a in actions or []:
        if not isinstance(a, dict):
            out.append(a)
            continue

        svc = a.get("service")
        target = a.get("target") or {}
        targets = _target_entities(target)
        if not targets:
            out.append(a)
            continue

        rewritten = False
        for rule in rules:
            matched = [t for t in targets if t in rule.get("entities", [])]
            if not matched:
                continue
            if svc in ("cover.open_cover", "cover.close_cover"):
                pos = rule["open_position"] if svc == "cover.open_cover" else rule["closed_position"]
                out.append({
                    "service": "cover.set_cover_position",
                    "target": {"entity_id": matched if len(matched) > 1 else matched[0]},
                    "data": {"position": pos},
                })
                rewritten = True
                break
        if not rewritten:
            out.append(a)

    return out


def call_conversation_agent(agent_id: str, text: str) -> str:
    url = f"{HA_URL}/api/conversation/process"
    r = requests.post(
        url,
        headers=ha_headers(),
        json={"agent_id": agent_id, "text": text},
        timeout=HA_CONVERSATION_TIMEOUT,
    )
    r.raise_for_status()
    data = r.json() or {}
    speech = (
        ((((data.get("response") or {}).get("speech") or {}).get("plain") or {}).get("speech"))
        or (data.get("response") or {}).get("text")
        or ""
    )
    return (speech or "").strip()


def call_conversation_agent_full(agent_id: str, text: str, conversation_id: Optional[str] = None) -> Tuple[str, Optional[str]]:
    url = f"{HA_URL}/api/conversation/process"
    payload: Dict[str, Any] = {"agent_id": agent_id, "text": text}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    r = requests.post(
        url,
        headers=ha_headers(),
        json=payload,
        timeout=HA_CONVERSATION_TIMEOUT,
    )
    r.raise_for_status()
    data = r.json() or {}
    speech = (
        ((((data.get("response") or {}).get("speech") or {}).get("plain") or {}).get("speech"))
        or (data.get("response") or {}).get("text")
        or ""
    )
    conv_id = (
        data.get("conversation_id")
        or (data.get("response") or {}).get("conversation_id")
        or conversation_id
    )
    return (speech or "").strip(), conv_id


# ----------------------------
# CREATE/UPDATE AUTOMATION + RELOAD
# ----------------------------
def update_automation(automation_id: str, config: Dict[str, Any]) -> str:
    url = f"{HA_URL}/api/config/automation/config/{automation_id}"
    r = requests.put(url, headers=ha_headers(), json=config, timeout=60)
    r.raise_for_status()
    return automation_id


def reload_automations() -> None:
    rr = requests.post(f"{HA_URL}/api/services/automation/reload", headers=ha_headers(), json={}, timeout=60)
    rr.raise_for_status()

def reload_scripts() -> None:
    if not (HA_URL and HA_TOKEN):
        return
    try:
        rr = requests.post(f"{HA_URL}/api/services/script/reload", headers=ha_headers(), json={}, timeout=60)
        rr.raise_for_status()
    except Exception:
        pass


def create_or_update_automation(config: Dict[str, Any]) -> str:
    """
    Creates an automation via:
      POST /api/config/automation/config   -> returns {automation_id}
    Updates via:
      PUT  /api/config/automation/config/{automation_id}
    Falls back to legacy explicit-ID POST if needed.
    """
    create_url = f"{HA_URL}/api/config/automation/config"

    r = requests.post(create_url, headers=ha_headers(), json=config, timeout=60)
    if r.status_code < 400:
        data = r.json() or {}
        automation_id = str(data.get("automation_id") or "")
        if not automation_id:
            automation_id = str(int(time.time() * 1000))
            _ = update_automation(automation_id, config)
        reload_automations()
        return automation_id

    automation_id = str(int(time.time() * 1000))
    legacy_url = f"{HA_URL}/api/config/automation/config/{automation_id}"
    rr = requests.post(legacy_url, headers=ha_headers(), json={"id": automation_id, **config}, timeout=60)
    rr.raise_for_status()
    reload_automations()
    return automation_id


def announce(msg: str) -> None:
    """
    Server-side confirmation that an automation was created.
    Routes via whatever CONFIRM_* points at.
    Optional two-sentence style (describe + playful comment).
    """
    if not (CONFIRM_DOMAIN and CONFIRM_SERVICE):
        return

    final_msg = msg

    if CONFIRM_JARVIS:
        try:
            prompt = (
                "You are a helpful assistant.\n"
                "Create EXACTLY TWO sentences.\n"
                "Sentence 1: In ONE sentence, describe what the newly created Home Assistant automation does, "
                "based ONLY on the provided context.\n"
                "Sentence 2: Add a short, playful, non-personal comment. Teasing is fine, but do not target anyone.\n"
                "Rules: sound human, no swearing, no emojis, no slurs, no personal details.\n"
                f"CONTEXT:\n{msg}"
            )
            generated = call_conversation_agent(CONFIRM_AGENT_ID, prompt)
            if generated:
                final_msg = generated
        except Exception:
            pass

    url = f"{HA_URL}/api/services/{CONFIRM_DOMAIN}/{CONFIRM_SERVICE}"
    payload: Dict[str, Any] = {CONFIRM_FIELD: final_msg}

    if CONFIRM_DOMAIN == "tts" and CONFIRM_SERVICE == "speak" and TTS_ENTITY_ID:
        payload = {"entity_id": TTS_ENTITY_ID, "message": final_msg}

    try:
        requests.post(url, headers=ha_headers(), json=payload, timeout=30)
    except Exception:
        pass


# ----------------------------
# BUILDER PIPELINE (shared)
# ----------------------------
async def build_ha_config_from_text(
    request_text: str,
    source: str = "ui",
    current_yaml: Optional[str] = None,
    entity_type: str = "automation",
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    capabilities = load_capabilities()

    entity_registry, device_registry, area_registry, states = await ha_ws_fetch()
    context_pack = build_context_pack_from_regs(
        request_text,
        current_yaml,
        entity_type,
        entity_registry,
        device_registry,
        area_registry,
        states,
        history=None,
    )
    candidates = context_pack.get("candidates") or []
    caps_subset = context_pack.get("capabilities") or slim_capabilities_for_llm(capabilities)

    entity_type = (entity_type or "automation").strip().lower()
    if entity_type not in ("automation", "script"):
        entity_type = "automation"
    mode = "edit" if current_yaml else "create"

    prompt = {
        "request": request_text,
        "source": source,
        "entity_type": entity_type,
        "candidates": candidates,
        "capabilities": caps_subset,
        "helper_placeholders": [
            "HELPER_COUNTER_1",
            "HELPER_TIMER_1",
            "HELPER_BOOLEAN_1",
            "HELPER_NUMBER_1",
            "HELPER_TEXT_1",
        ],
    }
    if entity_type == "script":
        prompt["output_contract"] = (
            "Return ONLY minified JSON keys: alias, description, sequence, mode, helpers_needed. "
            "No other keys. helpers_needed must be [] or list of objects {type, purpose}. "
            "sequence must be a list of valid HA action objects. "
            "If entity_ids or services are not specified, infer them from candidates and capabilities. "
            "Use real HA services. Prefer light groups from capabilities.lights.area_group_map over areas. "
            "If user asks to send a phone notification, use capabilities.notifications.primary_phone_notify and keep it as notify.mobile_app_* "
            "(do NOT route through speech scripts). "
            "For actionable phone notifications, include data.actions and wait for event type capabilities.notifications.actionable_event_type. "
            "If capabilities.media.power_toggle_rules is provided, use script.turn_on with the rule's script "
            "when power on/off targets any rule.match_entities (avoid media_player/switch power services)."
        )
    else:
        prompt["output_contract"] = (
            "Return ONLY minified JSON keys: alias, description, trigger, condition, action, mode, initial_state, helpers_needed. "
            "No other keys. helpers_needed must be [] or list of objects {type, purpose}. "
            "If entity_ids or services are not specified, infer them from candidates and capabilities. "
            "Use real HA services. Prefer light groups from capabilities.lights.area_group_map over areas. "
            "If user asks to send a phone notification, use capabilities.notifications.primary_phone_notify and keep it as notify.mobile_app_* "
            "(do NOT route through speech scripts). "
            "For actionable phone notifications, include data.actions and wait for event type capabilities.notifications.actionable_event_type. "
            "If capabilities.media.power_toggle_rules is provided, use script.turn_on with the rule's script "
            "when power on/off targets any rule.match_entities (avoid media_player/switch power services)."
        )
    if current_yaml:
        prompt["current_yaml"] = current_yaml

    out = call_builder(prompt)
    if not isinstance(out, dict):
        raise RuntimeError(f"Builder returned non-object JSON: {type(out).__name__}")

    placeholder_map = allocate_helpers(states, out.get("helpers_needed"))

    if entity_type == "script":
        seq = out.get("sequence")
        if seq is None:
            seq = out.get("action", [])
        if not isinstance(seq, list):
            seq = []
        script_config: Dict[str, Any] = {
            "alias": apply_ai_alias_prefix(out.get("alias") or "AI Script", capabilities, mode=mode, entity_type="script"),
            "description": out.get("description", ""),
            "sequence": seq,
            "mode": out.get("mode", "single"),
        }
        script_config = replace_placeholders(script_config, placeholder_map)
        script_config["sequence"] = normalize_actions(script_config.get("sequence", []), area_registry, candidates, capabilities)
        script_config["sequence"] = normalize_speech_actions(script_config.get("sequence", []), capabilities)
        script_config["sequence"] = normalize_cover_actions(script_config.get("sequence", []), capabilities)
        script_config["sequence"] = normalize_tv_power_actions(script_config.get("sequence", []), capabilities)
        return script_config, placeholder_map

    ha_config = {
        "alias": apply_ai_alias_prefix(out.get("alias", "AI Automation"), capabilities, mode=mode, entity_type="automation"),
        "description": out.get("description", ""),
        "trigger": out.get("trigger", []),
        "condition": out.get("condition", []),
        "action": out.get("action", []),
        "mode": out.get("mode", "single"),
        "initial_state": bool(out.get("initial_state", True)),
    }

    ha_config = replace_placeholders(ha_config, placeholder_map)

    # Enforce house style even if builder drifts
    ha_config["action"] = normalize_actions(ha_config.get("action", []), area_registry, candidates, capabilities)
    ha_config["action"] = normalize_speech_actions(ha_config.get("action", []), capabilities)
    ha_config["action"] = normalize_cover_actions(ha_config.get("action", []), capabilities)
    ha_config["action"] = normalize_tv_power_actions(ha_config.get("action", []), capabilities)

    return ha_config, placeholder_map

# ----------------------------
# FASTAPI ENDPOINT: builder
# ----------------------------
@app.post("/ha/automation-builder")
async def automation_builder(req: BuildReq, x_ha_agent_secret: str = Header(default="")):
    require_auth(x_ha_agent_secret)

    if not (HA_URL and HA_TOKEN):
        raise HTTPException(status_code=500, detail="Server not configured")

    try:
        ha_config, placeholder_map = await build_ha_config_from_text(req.text, source=req.source or "ui")

        automation_id = create_or_update_automation(ha_config)

        announce(f"Automation created: {ha_config['alias']}. {ha_config.get('description','')}".strip())

        return {
            "ok": True,
            "automation_id": automation_id,
            "alias": ha_config["alias"],
            "helpers_allocated": placeholder_map,
        }

    except Exception as e:
        msg = f"{type(e).__name__}: {str(e)}"
        if DEBUG:
            print("AUTOMATION_BUILDER_ERROR:", msg)
        return {"ok": False, "error": msg}
