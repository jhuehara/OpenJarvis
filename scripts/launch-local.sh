#!/usr/bin/env bash
set -euo pipefail

# ── OpenJarvis: run with the local LLM on this computer ──────────────
# Detects a local inference server (Ollama, LM Studio, llama.cpp), picks a
# model it already has (or pulls a starter model into Ollama), writes a
# local-only config and launches Jarvis. Nothing is sent to the cloud.
#
# Usage:
#   ./scripts/launch-local.sh                      # chat with auto-detected model
#   ./scripts/launch-local.sh qwen3.5:9b           # pick a model
#   ./scripts/launch-local.sh -- serve             # run `jarvis serve` instead
#   ./scripts/launch-local.sh llama3.1:8b -- ask "Olá"
#
# Environment overrides:
#   LOCAL_LLM_ENGINE   force engine: ollama | lmstudio | llamacpp
#   OLLAMA_HOST        default http://localhost:11434
#   LMSTUDIO_HOST      default http://localhost:1234
#   LLAMACPP_HOST      default http://localhost:8080
#   OPENJARVIS_CONFIG  config file to write/use (default ~/.openjarvis/local-llm.toml)
# ──────────────────────────────────────────────────────────────────────

OLLAMA_URL="${OLLAMA_HOST:-http://localhost:11434}"
[[ "$OLLAMA_URL" == http* ]] || OLLAMA_URL="http://$OLLAMA_URL"
LMSTUDIO_URL="${LMSTUDIO_HOST:-http://localhost:1234}"
LLAMACPP_URL="${LLAMACPP_HOST:-http://localhost:8080}"
CONFIG_PATH="${OPENJARVIS_CONFIG:-$HOME/.openjarvis/local-llm.toml}"

MODEL=""
if [[ $# -gt 0 && "$1" != "--" ]]; then
    MODEL="$1"
    shift
fi
if [[ ${1:-} == "--" ]]; then
    shift
fi
if [[ $# -eq 0 ]]; then
    set -- chat
fi

info() { echo "[local-llm] $*" >&2; }
die()  { echo "error: $*" >&2; exit "${2:-1}"; }

if ! command -v jarvis >/dev/null 2>&1; then
    die "'jarvis' CLI not found on PATH (run 'uv sync' and 'source .venv/bin/activate')" 3
fi

up() { curl -fsS --max-time 2 "$1" >/dev/null 2>&1; }

# First model id from an OpenAI-compatible /v1/models response.
first_openai_model() {
    curl -fsS --max-time 3 "$1/v1/models" 2>/dev/null \
        | python3 -c 'import json,sys; d=json.load(sys.stdin).get("data") or []; print(d[0]["id"] if d else "")' \
        2>/dev/null || true
}

ollama_models() {
    curl -fsS --max-time 3 "$OLLAMA_URL/api/tags" 2>/dev/null \
        | python3 -c 'import json,sys; [print(m["name"]) for m in json.load(sys.stdin).get("models", [])]' \
        2>/dev/null || true
}

# Starter model sized to available memory (GB of RAM).
starter_model() {
    local ram_kb ram_gb
    ram_kb=$(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)
    if [[ "$ram_kb" == 0 ]] && command -v sysctl >/dev/null 2>&1; then
        ram_kb=$(( $(sysctl -n hw.memsize 2>/dev/null || echo 0) / 1024 ))
    fi
    ram_gb=$(( ram_kb / 1024 / 1024 ))
    if   (( ram_gb >= 24 )); then echo "qwen3.5:9b"
    elif (( ram_gb >= 12 )); then echo "qwen3.5:4b"
    else                          echo "qwen3.5:2b"
    fi
}

# ── 1. Detect engine ──────────────────────────────────────────────────
ENGINE="${LOCAL_LLM_ENGINE:-}"
if [[ -z "$ENGINE" ]]; then
    if up "$OLLAMA_URL/api/tags"; then
        ENGINE=ollama
    elif up "$LMSTUDIO_URL/v1/models"; then
        ENGINE=lmstudio
    elif up "$LLAMACPP_URL/v1/models"; then
        ENGINE=llamacpp
    elif command -v ollama >/dev/null 2>&1; then
        info "starting 'ollama serve' in the background..."
        nohup ollama serve >"${TMPDIR:-/tmp}/ollama-serve.log" 2>&1 &
        for _ in $(seq 1 20); do
            up "$OLLAMA_URL/api/tags" && break
            sleep 0.5
        done
        up "$OLLAMA_URL/api/tags" || die "Ollama did not start; see ${TMPDIR:-/tmp}/ollama-serve.log" 4
        ENGINE=ollama
    else
        die "no local LLM server found. Install Ollama (https://ollama.com/download),
  or start LM Studio's local server (port 1234) or llama-server (port 8080)." 4
    fi
fi

case "$ENGINE" in
    ollama)   HOST="$OLLAMA_URL" ;;
    lmstudio) HOST="$LMSTUDIO_URL" ;;
    llamacpp) HOST="$LLAMACPP_URL" ;;
    *) die "unsupported LOCAL_LLM_ENGINE '$ENGINE' (use ollama, lmstudio or llamacpp)" 2 ;;
esac
info "engine: $ENGINE ($HOST)"

# ── 2. Pick model ─────────────────────────────────────────────────────
if [[ "$ENGINE" == ollama ]]; then
    INSTALLED="$(ollama_models)"
    if [[ -z "$MODEL" ]]; then
        MODEL="$(printf '%s\n' "$INSTALLED" | grep -v -i embed | head -n1 || true)"
    fi
    if [[ -z "$MODEL" ]]; then
        MODEL="$(starter_model)"
    fi
    if ! printf '%s\n' "$INSTALLED" | grep -qxF "$MODEL" \
        && ! printf '%s\n' "$INSTALLED" | grep -qxF "$MODEL:latest"; then
        command -v ollama >/dev/null 2>&1 \
            || die "model '$MODEL' is not installed and the 'ollama' CLI is not on PATH" 5
        info "pulling '$MODEL' (first run only)..."
        ollama pull "$MODEL"
    fi
elif [[ -z "$MODEL" ]]; then
    MODEL="$(first_openai_model "$HOST")"
    [[ -n "$MODEL" ]] || die "no model loaded in $ENGINE at $HOST — load one first" 5
fi
info "model:  $MODEL"

# ── 3. Write local-only config (kept if it already exists) ───────────
if [[ ! -f "$CONFIG_PATH" ]]; then
    mkdir -p "$(dirname "$CONFIG_PATH")"
    cat >"$CONFIG_PATH" <<EOF
# Generated by scripts/launch-local.sh — local LLM only, no cloud calls.
[engine]
default = "$ENGINE"

[engine.$ENGINE]
host = "$HOST"

[intelligence]
default_model = "$MODEL"
provider = "local"

[agent]
default_agent = "simple"

[server]
host = "127.0.0.1"
port = 8000
agent = "simple"
EOF
    info "config: $CONFIG_PATH (created)"
else
    info "config: $CONFIG_PATH"
fi
export OPENJARVIS_CONFIG="$CONFIG_PATH"

# ── 4. Launch ─────────────────────────────────────────────────────────
SUBCMD="$1"
shift
case "$SUBCMD" in
    chat|ask|serve) exec jarvis "$SUBCMD" --engine "$ENGINE" --model "$MODEL" "$@" ;;
    *)              exec jarvis "$SUBCMD" "$@" ;;
esac
