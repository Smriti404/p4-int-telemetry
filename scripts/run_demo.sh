#!/bin/bash
# =============================================================================
# scripts/run_demo.sh
# One-shot demo: compile → start network → collector → controller → quick test
# Run from project root: sudo bash scripts/run_demo.sh
# =============================================================================

set -e
cd "$(dirname "$0")/.."

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; NC='\033[0m'

log()  { echo -e "${GREEN}[+]${NC} $1"; }
info() { echo -e "${BLUE}[i]${NC} $1"; }

VENV=/opt/p4/venv
PYTHON=$VENV/bin/python3

# ── 1. Compile P4 ────────────────────────────────────────────
log "Compiling P4 program..."
mkdir -p build
p4c --target bmv2 --arch v1model --std p4-16 \
    -o build \
    --p4runtime-files build/int_telemetry.p4info.txt \
    p4/int_telemetry.p4

log "Compilation succeeded: build/int_telemetry.json"

# ── 2. Start collector (background) ──────────────────────────
log "Starting telemetry collector..."
$PYTHON controller/collector.py \
    --udp-port 9001 --api-port 5000 --threshold 800 \
    > logs/collector.log 2>&1 &
COLLECTOR_PID=$!
echo $COLLECTOR_PID > /tmp/p4-collector.pid
sleep 1

# ── 3. Start Mininet topology (background, interactive off) ───
log "Starting Mininet topology..."
sudo $PYTHON topology/topo.py \
    --json build/int_telemetry.json \
    --p4info build/int_telemetry.p4info.txt \
    > logs/mininet.log 2>&1 &
MININET_PID=$!
echo $MININET_PID > /tmp/p4-mininet.pid

log "Waiting for switches to bind gRPC ports..."
sleep 5

# ── 4. Install tables ─────────────────────────────────────────
log "Installing P4Runtime tables..."
$PYTHON controller/runtime.py \
    --p4info build/int_telemetry.p4info.txt \
    --json   build/int_telemetry.json \
    --no-policy \
    > logs/controller.log 2>&1
log "Tables installed."

# ── 5. Quick connectivity test ────────────────────────────────
log "Running ping test h1 → h2..."
sudo mn --test pingall 2>&1 | grep -E "(Loss|loss|PASS|FAIL)" || true

# ── 6. Show collector stats ───────────────────────────────────
sleep 2
info "Collector health:"
curl -s http://localhost:5000/health | python3 -m json.tool 2>/dev/null || true

echo ""
echo -e "${GREEN}══════════════════════════════════════════${NC}"
echo -e "${GREEN}  Demo running!${NC}"
echo -e "${GREEN}══════════════════════════════════════════${NC}"
echo -e "  Collector REST API: http://localhost:5000"
echo -e "  Collector log:      logs/collector.log"
echo -e "  Mininet log:        logs/mininet.log"
echo ""
echo -e "  ${YELLOW}Next steps:${NC}"
echo -e "  1. Start policy:    $PYTHON controller/runtime.py --p4info build/int_telemetry.p4info.txt --json build/int_telemetry.json"
echo -e "  2. Run benchmark:   $PYTHON eval/benchmark.py --mode all"
echo -e "  3. Generate plots:  $PYTHON eval/plot_results.py"
echo ""
echo -e "  ${YELLOW}To stop everything:${NC}  sudo bash scripts/stop_demo.sh"
