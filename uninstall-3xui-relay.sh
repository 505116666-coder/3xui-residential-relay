#!/usr/bin/env bash
# Remove only this project's deployment. Shared system packages and SSH are preserved.
set -euo pipefail
umask 077
[[ "$(uname -s)" == Linux && "$EUID" -eq 0 ]] || { echo '请在服务器 SSH 中用 root 运行。'; exit 1; }
root=/root/3xui-dual
unit=/etc/systemd/system/x-ui.service
owned=0
if [[ -f "$root/owner" ]] && [[ "$(cat "$root/owner")" == 3xui-dual-v1 ]]; then owned=1; fi
if [[ -f "$root/state.json" ]] && grep -q '"managed_by": "3xui-dual-v1"' "$root/state.json"; then owned=1; fi
if [[ -f "$unit" ]] && grep -q 'Description=3x-ui panel (dual-node deployment)' "$unit"; then owned=1; fi
if [[ "$owned" == 1 && -f "$unit" ]] && ! grep -q 'Description=3x-ui panel (dual-node deployment)' "$unit"; then
  echo '检测到其他程序管理的 x-ui 服务，为避免误删，已停止。'; exit 1
fi
exec 9>/run/lock/3xui-dual.lock
flock -n 9 || { echo '安装或检查仍在运行，请结束后再卸载。'; exit 1; }
if [[ "$owned" == 1 ]]; then
  echo '正在卸载面板、节点、证书和保存的账号信息……'
  for service in 3xui-dual-renew.timer 3xui-dual-renew.service x-ui.service 3xui-dual-socks-bridge.service; do
    if systemctl cat "$service" >/dev/null 2>&1; then
      systemctl disable "$service" >/dev/null 2>&1 || true
      systemctl stop "$service" || { echo "无法停止 $service，已停止清理，请检查服务状态。"; exit 1; }
    fi
  done
  # Only rules added and tagged by this installer are recorded here.
  if command -v ufw >/dev/null 2>&1 && [[ -f "$root/ufw-added.txt" ]]; then
    while IFS= read -r port; do
      [[ "$port" =~ ^[0-9]+$ ]] || continue
      (( port >= 1 && port <= 65535 )) || continue
      if ufw status | grep -E "^${port}/tcp[[:space:]].*# Didushan-3xui-relay" >/dev/null; then
        ufw --force delete allow "${port}/tcp" || { echo '防火墙规则清理失败，保留部署记录供重试。'; exit 1; }
      fi
    done < "$root/ufw-added.txt"
  fi
  rm -f -- "$unit" /etc/systemd/system/3xui-dual-socks-bridge.service \
    /etc/systemd/system/3xui-dual-renew.service /etc/systemd/system/3xui-dual-renew.timer
  rm -rf -- /usr/local/x-ui /etc/x-ui "$root"
  systemctl daemon-reload
  systemctl reset-failed x-ui.service 3xui-dual-renew.service 3xui-dual-socks-bridge.service 2>/dev/null || true
else
  # A failure before ownership was established must not erase an unrelated panel.
  if [[ -e /usr/local/x-ui || -e /etc/x-ui || -e "$unit" ]]; then
    echo '未确认这些面板文件属于本脚本，已保留，避免误删。'
    exit 1
  fi
  if [[ -d "$root" ]] && ! rmdir "$root" 2>/dev/null; then
    echo "部署目录有无法确认归属的文件，已保留：$root"; exit 1
  fi
fi
# Remove only the command owned by this installer; preserve unrelated relay programs.
if [[ -f /usr/local/bin/relay && ! -L /usr/local/bin/relay ]] && grep -Fxq '# Managed by 3xui-residential-relay' /usr/local/bin/relay; then
  rm -f -- /usr/local/bin/relay
fi
# Exact installer names and installer-owned temporary Python files only.
rm -f -- /root/deploy-3xui-dual.sh /root/3xui-residential-relay.sh
find /tmp -maxdepth 1 -type f -user root -name '3xui-dual.????????.py' -delete
rm -f -- /root/uninstall-3xui-relay.sh
printf '\n卸载完成。原面板账号和节点链接已失效，可以重新安装。\n'
echo '系统公共依赖及原有防火墙规则保留。'
