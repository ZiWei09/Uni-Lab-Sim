#!/bin/bash

# Shared bootstrap helpers for macOS/Linux launchers.
# Keep this file compatible with the Bash 3.2 shipped by older macOS releases.

plcsim_log() {
    printf '%s\n' "$*" >&2
}

plcsim_python_is_supported() {
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)' \
        >/dev/null 2>&1
}

plcsim_find_python() {
    local candidate
    local resolved

    if [ -n "${PYTHON:-}" ]; then
        if [ -x "$PYTHON" ]; then
            candidate="$PYTHON"
        else
            resolved="$(command -v "$PYTHON" 2>/dev/null || true)"
            candidate="$resolved"
        fi
        if [ -n "$candidate" ] && plcsim_python_is_supported "$candidate"; then
            printf '%s\n' "$candidate"
            return 0
        fi
    fi

    # PLC-Sim intentionally supports only Python 3.11. The generic python3
    # fallback is accepted only when it resolves to a 3.11 interpreter.
    for candidate in python3.11 python3; do
        resolved="$(command -v "$candidate" 2>/dev/null || true)"
        if [ -n "$resolved" ] && plcsim_python_is_supported "$resolved"; then
            printf '%s\n' "$resolved"
            return 0
        fi
    done

    return 1
}

plcsim_file_fingerprint() {
    local file="$1"

    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$file" | awk '{print $1}'
    elif command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$file" | awk '{print $1}'
    else
        cksum "$file" | awk '{print $1 ":" $2}'
    fi
}

plcsim_ensure_venv() {
    local project_root="$1"
    local venv_dir="${PLCSIM_VENV_DIR:-$project_root/.venv}"
    local venv_python="$venv_dir/bin/python"
    local requirements="$project_root/requirements.txt"
    local marker="$venv_dir/.plcsim-requirements"
    local bootstrap_python
    local current_fingerprint
    local installed_fingerprint=""

    if [ ! -x "$venv_python" ]; then
        bootstrap_python="$(plcsim_find_python || true)"
        if [ -z "$bootstrap_python" ]; then
            plcsim_log "[X] 未找到 Python 3.11。PLC-Sim 仅支持 Python 3.11.x。"
            plcsim_log "    请先通过 python.org 或 Homebrew 安装 Python 3.11。"
            return 1
        fi

        plcsim_log "[1/2] 创建 Python 虚拟环境: $venv_dir"
        "$bootstrap_python" -m venv "$venv_dir"
    elif ! plcsim_python_is_supported "$venv_python"; then
        plcsim_log "[X] 现有虚拟环境不是 Python 3.11: $venv_dir"
        plcsim_log "    请移走该 .venv 后，用 Python 3.11 重新双击启动器。"
        return 1
    fi

    current_fingerprint="$(plcsim_file_fingerprint "$requirements")"
    if [ -f "$marker" ]; then
        installed_fingerprint="$(sed -n '1p' "$marker")"
    fi

    if [ "$current_fingerprint" != "$installed_fingerprint" ]; then
        plcsim_log "[2/2] 安装/更新项目依赖..."
        PIP_DISABLE_PIP_VERSION_CHECK=1 "$venv_python" -m pip install -r "$requirements"
        printf '%s\n' "$current_fingerprint" > "$marker"
    else
        plcsim_log "[OK] Python 环境已就绪。"
    fi

    PLCSIM_ROOT="$project_root"
    PLCSIM_VENV="$venv_dir"
    PLCSIM_PY="$venv_python"
    export PLCSIM_ROOT PLCSIM_VENV PLCSIM_PY
}

plcsim_report_exit() {
    local status="$1"

    case "$status" in
        0|130|143)
            return 0
            ;;
    esac

    plcsim_log ""
    plcsim_log "[X] 启动失败，退出码: $status"
    if [ "${PLCSIM_NO_PAUSE:-0}" != "1" ] && [ -t 0 ]; then
        printf '按回车键关闭窗口...' >&2
        read -r _plcsim_answer
    fi
}

plcsim_wait_for_tcp() {
    local host="$1"
    local port="$2"
    local attempts="${3:-150}"
    local count=0

    while [ "$count" -lt "$attempts" ]; do
        if "$PLCSIM_PY" - "$host" "$port" <<'PY' >/dev/null 2>&1
import socket
import sys

with socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=0.2):
    pass
PY
        then
            return 0
        fi
        count=$((count + 1))
        sleep 0.1
    done

    return 1
}
