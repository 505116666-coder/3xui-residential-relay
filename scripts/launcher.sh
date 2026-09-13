#!/usr/bin/env bash
# Upload this single file to your Ubuntu/Debian server and run as root.
set -euo pipefail
umask 077
if [[ "${1:-}" == "--version" ]]; then
  echo 'relay 1.1.2 / 3X-UI v3.7.0'
  exit 0
fi
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  echo '用法：bash 3xui-residential-relay.sh [--resume | --check | --results | --copy [序号] | --add-residential | --rollback-add | --migrate | --rollback-migration | --menu | --edit-residential | --rename-residential | --delete-residential | --rollback-change | --diagnostics | --version]'
  echo '仅适用于全新 Ubuntu 22.04+ / Debian 12+ 的 systemd 服务器。'
  exit 0
fi
if [[ "$(uname -s)" != Linux || "$EUID" -ne 0 ]]; then
  echo '请在服务器 SSH 中使用 root 运行；不要在 Mac 上安装。' >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  if [[ ! -f /etc/os-release ]]; then exit 1; fi
  . /etc/os-release
  if [[ "$ID" != ubuntu && "$ID" != debian ]]; then
    echo '仅支持 Ubuntu / Debian。' >&2
    exit 1
  fi
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y python3
fi
if [[ ! -r /dev/tty ]]; then
  echo '请在交互式 SSH 终端运行。' >&2
  exit 1
fi
script_tmp=$(mktemp /tmp/3xui-dual.XXXXXXXX.py)
trap 'rm -f -- "$script_tmp"' EXIT
cat > "$script_tmp" <<'PYTHON_3XUI_DUAL_EOF'
