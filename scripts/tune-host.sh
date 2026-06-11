#!/usr/bin/env bash
# scripts/tune-host.sh — Kernel tuning for LOIC performance
# Run as root: sudo bash scripts/tune-host.sh

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root. Use: sudo bash $0"
    exit 1
fi

echo "=== Tuning kernel parameters for LOIC ==="

cat >> /etc/sysctl.conf <<'EOF'

# LOIC performance tuning
net.ipv4.ip_local_port_range = 1024 65535
net.ipv4.tcp_tw_reuse = 1
net.ipv4.tcp_fin_timeout = 15
net.ipv4.tcp_max_tw_buckets = 2000000

net.core.somaxconn = 65535
net.core.netdev_max_backlog = 65535
net.ipv4.tcp_max_syn_backlog = 65535
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.ipv4.tcp_rmem = 4096 87380 16777216
net.ipv4.tcp_wmem = 4096 65536 16777216

net.ipv4.tcp_slow_start_after_idle = 0

fs.file-max = 2097152
fs.nr_open = 2097152

net.ipv4.neigh.default.gc_thresh2 = 4096
net.ipv4.neigh.default.gc_thresh3 = 8192
EOF

sysctl -p > /dev/null

cat >> /etc/security/limits.conf <<'EOF'
*    soft nofile 1048576
*    hard nofile 1048576
root soft nofile 1048576
root hard nofile 1048576
EOF

ulimit -n 1048576 2>/dev/null || true

echo "Kernel tuning applied."
echo "WARNING: Reboot recommended for fs.file-max and some socket changes."
echo ""
echo "Verify:"
echo "  ulimit -n            # should be 1048576"
echo "  sysctl net.ipv4.tcp_tw_reuse  # should be 1"
