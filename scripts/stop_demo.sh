#!/bin/bash
# scripts/stop_demo.sh — cleanly stop all project processes

echo "[*] Stopping P4 Telemetry demo..."

# Kill collector
[ -f /tmp/p4-collector.pid ] && kill $(cat /tmp/p4-collector.pid) 2>/dev/null; rm -f /tmp/p4-collector.pid

# Kill mininet
sudo mn -c 2>/dev/null
[ -f /tmp/p4-mininet.pid ] && kill $(cat /tmp/p4-mininet.pid) 2>/dev/null; rm -f /tmp/p4-mininet.pid

# Kill any lingering BMv2 processes
sudo pkill -f simple_switch_grpc 2>/dev/null || true
sudo pkill -f simple_switch 2>/dev/null || true

echo "[✓] All processes stopped."
