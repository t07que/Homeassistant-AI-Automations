#!/usr/bin/with-contenv bashio
set -e

STATIC_DIR="$(bashio::config 'static_dir')"
if [ -z "$STATIC_DIR" ]; then
  STATIC_DIR="/app/static"
fi

HA_URL="$(bashio::config 'ha_url')"
if [ -z "$HA_URL" ]; then
  HA_URL="http://supervisor/core"
fi

HA_TOKEN="$(bashio::config 'ha_token')"
if [ -z "$HA_TOKEN" ] && [ -n "$SUPERVISOR_TOKEN" ]; then
  HA_TOKEN="$SUPERVISOR_TOKEN"
fi

AUTOMATIONS_FILE_PATH="$(bashio::config 'automations_path')"
if [ -z "$AUTOMATIONS_FILE_PATH" ]; then
  AUTOMATIONS_FILE_PATH="/config/automations.yaml"
fi

SCRIPTS_FILE_PATH="$(bashio::config 'scripts_path')"
if [ -z "$SCRIPTS_FILE_PATH" ]; then
  SCRIPTS_FILE_PATH="/config/scripts.yaml"
fi

RESTORE_STATE_PATH="$(bashio::config 'restore_state_path')"
if [ -z "$RESTORE_STATE_PATH" ]; then
  RESTORE_STATE_PATH="/config/.storage/core.restore_state"
fi

AUTOMATIONS_VERSIONS_DIR="$(bashio::config 'versions_dir')"
if [ -z "$AUTOMATIONS_VERSIONS_DIR" ]; then
  AUTOMATIONS_VERSIONS_DIR="/data/versions"
fi

LOCAL_DB_FILE="$(bashio::config 'local_db_file')"
if [ -z "$LOCAL_DB_FILE" ]; then
  LOCAL_DB_FILE="/data/local_automations_db.json"
fi

SUMMARY_CACHE_FILE="$(bashio::config 'summary_cache_file')"
if [ -z "$SUMMARY_CACHE_FILE" ]; then
  SUMMARY_CACHE_FILE="/data/summary_cache.json"
fi

RUNTIME_CONFIG_FILE="$(bashio::config 'runtime_config_file')"
if [ -z "$RUNTIME_CONFIG_FILE" ]; then
  RUNTIME_CONFIG_FILE="/data/runtime_config.json"
fi

CAPABILITIES_FILE="$(bashio::config 'capabilities_file')"
if [ -z "$CAPABILITIES_FILE" ]; then
  CAPABILITIES_FILE="/data/capabilities.yaml"
fi

AGENT_SECRET="$(bashio::config 'agent_secret')"

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

bashio::log.info "Starting Automation Studio on port 8124"
exec uvicorn agent_server:app --host 0.0.0.0 --port 8124
