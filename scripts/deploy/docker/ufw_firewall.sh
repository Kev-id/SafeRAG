#!/usr/bin/env bash
# ufw_firewall.sh —— 在(目标)机上部署 ufw 防火墙策略, 复现源盒的离线白名单规则
# 用法: sudo ./ufw_firewall.sh [内网网段]
#       默认内网网段 192.168.0.0/16; 可用环境变量 ETH_CIDR 覆盖
#       跳过: FIREWALL_SKIP=1
#
# 规则(与源盒一致):
#   ufw default deny incoming   —— 默认拒绝所有入站(最高防护)
#   ufw default allow outgoing  —— 允许出站(离线环境出站本就应受限, 故开放)
#   放行内网网段 → 端口 22(SSH), 80(前端/反代)
set -euo pipefail

ETH_CIDR="${ETH_CIDR:-${1:-192.168.0.0/16}}"
FIREWALL_SKIP="${FIREWALL_SKIP:-0}"

log()  { printf '[防火墙] %s\n' "$*"; }
warn() { printf '[警告] %s\n' "$*"; }
die()  { printf '[错误] %s\n' "$*" >&2; exit 1; }

main() {
  [ "$(id -u)" = "0" ] || die "请用 sudo 运行"
  if [ "$FIREWALL_SKIP" = "1" ]; then log "已跳过(FIREWALL_SKIP=1)"; exit 0; fi
  if ! command -v ufw >/dev/null 2>&1; then warn "未安装 ufw, 跳过(可改配 iptables)"; exit 0; fi

  log "==== 配置 ufw 防火墙(网段=$ETH_CIDR) ===="
  # 注意: 默认拒绝入站 —— 若你的管理端 IP 不在该网段内, 请先用 ETH_CIDR=<你的网段> 覆盖本次运行,
  #       否则 SSH 会被自己锁死; 可另开一个终端执行 ufw allow from <你的IP> 应急放行。
  ufw default deny incoming
  ufw default allow outgoing
  ufw allow from "$ETH_CIDR" to any port 22 proto tcp
  ufw allow from "$ETH_CIDR" to any port 80 proto tcp
  ufw --force enable
  log "==== 当前规则 ===="
  ufw status verbose
  log "完成。"
}
main "$@"