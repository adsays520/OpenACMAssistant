#!/usr/bin/env bash
set -euo pipefail
assistant_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONDONTWRITEBYTECODE=1
if [[ $# -eq 0 ]]; then
    set -- chat
fi
assistant_python="${OPENACM_PYTHON:-$assistant_root/.venv/bin/python}"
if [[ ! -x "$assistant_python" ]]; then
    printf '%s\n' '请先创建 .venv 并安装 requirements.lock.txt，或设置 OPENACM_PYTHON。' >&2
    exit 1
fi
exec "$assistant_python" "$assistant_root/app/assistant.py" "$@"
