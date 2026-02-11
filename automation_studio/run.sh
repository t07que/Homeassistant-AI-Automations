#!/usr/bin/env bash
set -e

OPTIONS_FILE="/data/options.json"
if [ -f "$OPTIONS_FILE" ]; then
  eval "$(
    python - "$OPTIONS_FILE" <<'PY'
import json
import shlex
import sys

path = sys.argv[1]
try:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
except Exception:
    data = {}

keys = [
    "static_dir",
    "ha_url",
    "ha_token",
    "agent_secret",
    "builder_agent_id",
    "architect_agent_id",
    "summary_agent_id",
    "capability_mapper_agent_id",
    "semantic_diff_agent_id",
    "kb_sync_helper_agent_id",
    "dumb_builder_agent_id",
    "automations_path",
    "scripts_path",
    "restore_state_path",
    "versions_dir",
    "local_db_file",
    "summary_cache_file",
    "runtime_config_file",
    "capabilities_file",
]

for key in keys:
    val = data.get(key, "")
    if isinstance(val, (dict, list)):
        val = ""
    print(f"OPT_{key.upper()}={shlex.quote(str(val))}")
PY
  )"
fi

STATIC_DIR="${OPT_STATIC_DIR:-/app/static}"
HA_URL="${OPT_HA_URL:-http://supervisor/core}"

HA_TOKEN="${OPT_HA_TOKEN:-}"
SUPERVISOR_TOKEN_VALUE="${SUPERVISOR_TOKEN:-}"
if [ -z "$HA_TOKEN" ] && [ -n "$SUPERVISOR_TOKEN_VALUE" ]; then
  HA_TOKEN="$SUPERVISOR_TOKEN_VALUE"
fi

AUTOMATIONS_FILE_PATH="${OPT_AUTOMATIONS_PATH:-/config/automations.yaml}"
SCRIPTS_FILE_PATH="${OPT_SCRIPTS_PATH:-/config/scripts.yaml}"
RESTORE_STATE_PATH="${OPT_RESTORE_STATE_PATH:-/config/.storage/core.restore_state}"
AUTOMATIONS_VERSIONS_DIR="${OPT_VERSIONS_DIR:-/data/versions}"
LOCAL_DB_FILE="${OPT_LOCAL_DB_FILE:-/data/local_automations_db.json}"
SUMMARY_CACHE_FILE="${OPT_SUMMARY_CACHE_FILE:-/data/summary_cache.json}"
RUNTIME_CONFIG_FILE="${OPT_RUNTIME_CONFIG_FILE:-/data/runtime_config.json}"
CAPABILITIES_FILE="${OPT_CAPABILITIES_FILE:-/data/capabilities.yaml}"
AGENT_SECRET="${OPT_AGENT_SECRET:-}"

BUILDER_AGENT_ID="${OPT_BUILDER_AGENT_ID:-}"
ARCHITECT_AGENT_ID="${OPT_ARCHITECT_AGENT_ID:-}"
SUMMARY_AGENT_ID="${OPT_SUMMARY_AGENT_ID:-}"
CAPABILITY_MAPPER_AGENT_ID="${OPT_CAPABILITY_MAPPER_AGENT_ID:-}"
SEMANTIC_DIFF_AGENT_ID="${OPT_SEMANTIC_DIFF_AGENT_ID:-}"
KB_SYNC_HELPER_AGENT_ID="${OPT_KB_SYNC_HELPER_AGENT_ID:-}"
DUMB_BUILDER_AGENT_ID="${OPT_DUMB_BUILDER_AGENT_ID:-}"

export STATIC_DIR
export HA_URL
export HA_TOKEN
export AGENT_SECRET
export AUTOMATIONS_FILE_PATH
export SCRIPTS_FILE_PATH
export RESTORE_STATE_PATH
export AUTOMATIONS_VERSIONS_DIR
export LOCAL_DB_FILE
export SUMMARY_CACHE_FILE
export RUNTIME_CONFIG_FILE
export CAPABILITIES_FILE
export BUILDER_AGENT_ID
export ARCHITECT_AGENT_ID
export SUMMARY_AGENT_ID
export CAPABILITY_MAPPER_AGENT_ID
export SEMANTIC_DIFF_AGENT_ID
export KB_SYNC_HELPER_AGENT_ID
export DUMB_BUILDER_AGENT_ID

cd /app

mkdir -p "$AUTOMATIONS_VERSIONS_DIR"
if [ ! -f "$LOCAL_DB_FILE" ]; then
  echo '{}' > "$LOCAL_DB_FILE"
fi
if [ ! -f "$SUMMARY_CACHE_FILE" ]; then
  echo '{}' > "$SUMMARY_CACHE_FILE"
fi
if [ ! -f "$RUNTIME_CONFIG_FILE" ]; then
  echo '{}' > "$RUNTIME_CONFIG_FILE"
fi

echo "[INFO] Starting Automation Studio on port 8124"
exec uvicorn agent_server:app --host 0.0.0.0 --port 8124
