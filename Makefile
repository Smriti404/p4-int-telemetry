# =============================================================================
# P4 Telemetry Project — Makefile
# =============================================================================

P4SRC     := p4/int_telemetry.p4
BUILD_DIR := build
JSON      := $(BUILD_DIR)/int_telemetry.json
P4INFO    := $(BUILD_DIR)/int_telemetry.p4info.txt

# Use the venv Python if it exists, otherwise system Python3
VENV      := /opt/p4/venv
ifneq ("$(wildcard $(VENV)/bin/python3)","")
    PYTHON := $(VENV)/bin/python3
else
    PYTHON := python3
endif

.PHONY: all compile topology collector controller tables-only \
        policy eval eval-load eval-burst eval-degrade plots \
        clean clean-results test-compile test-collector \
        debug-collector debug-topology install help

# ─── Default ──────────────────────────────────────────────────────────────────
all: compile
	@echo ""
	@echo "Build complete. Run in 4 separate terminals:"
	@echo "  Terminal 1:  sudo make topology"
	@echo "  Terminal 2:  make collector"
	@echo "  Terminal 3:  make controller"
	@echo "  Terminal 4:  make eval"
	@echo ""

# ─── Compile P4 ───────────────────────────────────────────────────────────────
compile: $(JSON)

$(JSON): $(P4SRC) p4/headers.p4
	@mkdir -p $(BUILD_DIR)
	@echo "[p4c] Compiling $(P4SRC) ..."
	p4c --target bmv2 \
	    --arch v1model \
	    --std p4-16 \
	    -o $(BUILD_DIR) \
	    --p4runtime-files $(P4INFO) \
	    $(P4SRC)
	@echo "[p4c] JSON   → $(JSON)"
	@echo "[p4c] P4Info → $(P4INFO)"
	@echo "[p4c] Compilation successful ✓"

# ─── Topology ─────────────────────────────────────────────────────────────────
topology: compile
	@echo "[mn] Starting Mininet topology (requires sudo)..."
	sudo $(PYTHON) topology/topo.py \
	    --json   $(JSON) \
	    --p4info $(P4INFO)

# ─── Telemetry Collector ──────────────────────────────────────────────────────
collector:
	@echo "[collector] Starting UDP:9001  REST:5000 ..."
	$(PYTHON) controller/collector.py \
	    --udp-port  9001 \
	    --api-port  5000 \
	    --threshold 800 \
	    --log-level INFO

# Debug mode: verbose logging
debug-collector:
	@echo "[collector] Starting in DEBUG mode..."
	$(PYTHON) controller/collector.py \
	    --udp-port  9001 \
	    --api-port  5000 \
	    --threshold 800 \
	    --log-level DEBUG

# ─── Controller (tables + policy) ─────────────────────────────────────────────
controller: compile
	@echo "[controller] Installing tables and starting policy loop..."
	$(PYTHON) controller/runtime.py \
	    --p4info    $(P4INFO) \
	    --json      $(JSON) \
	    --collector http://10.0.1.254:5000 \
	    --log-level INFO
	    
#controller: compile
#	@echo "[controller] Installing tables and starting policy loop..."
#	$(PYTHON) controller/runtime.py \
#	    --p4info    $(P4INFO) \
#	    --json      $(JSON) \
#	    --log-level INFO

# Debug mode: verbose logging
debug-controller: compile
	$(PYTHON) controller/runtime.py \
	    --p4info    $(P4INFO) \
	    --json      $(JSON) \
	    --log-level DEBUG

# ─── Tables only (no policy — for baseline experiment) ────────────────────────
# REPLACE WITH:
tables-only: compile
	@echo "[controller] Installing tables only (baseline mode — no rerouting)..."
	$(PYTHON) controller/runtime.py \
	    --p4info    $(P4INFO) \
	    --json      $(JSON) \
	    --collector http://10.0.1.254:5000 \
	    --no-policy \
	    --log-level INFO
	    
#tables-only: compile
#	@echo "[controller] Installing tables only (baseline mode — no rerouting)..."
#	$(PYTHON) controller/runtime.py \
#	    --p4info    $(P4INFO) \
#	    --json      $(JSON) \
#	    --no-policy \
#	    --log-level INFO

# ─── Evaluation ───────────────────────────────────────────────────────────────
eval:
	@echo "[eval] Running full benchmark suite..."
	$(PYTHON) eval/benchmark.py --mode all

eval-load:
	$(PYTHON) eval/benchmark.py --mode load

eval-burst:
	$(PYTHON) eval/benchmark.py --mode burst

eval-degrade:
	$(PYTHON) eval/benchmark.py --mode degrade

plots:
	@echo "[plots] Generating result plots..."
	$(PYTHON) eval/plot_results.py --results eval/results

# ─── Tests ────────────────────────────────────────────────────────────────────
test-compile: compile
	@echo "[test] P4 compilation OK ✓"
	@echo "[test] JSON size: $$(du -h $(JSON) | cut -f1)"
	@echo "[test] P4Info size: $$(du -h $(P4INFO) | cut -f1)"

test-collector:
	@echo "[test] Checking collector health..."
	#curl -s http://localhost:5000/health | $(PYTHON) -m json.tool
	curl -s http://10.0.1.254:5000/health | $(PYTHON) -m json.tool
	@echo ""
	@echo "[test] Current stats:"
	curl -s http://localhost:5000/stats | $(PYTHON) -m json.tool
	@echo ""
	@echo "[test] Congestion status:"
	curl -s http://localhost:5000/congestion | $(PYTHON) -m json.tool

test-switches:
	@echo "[test] Checking BMv2 gRPC ports..."
	@ss -tlnp | grep -E ':5005[1-4]' || echo "  WARNING: No gRPC ports open!"
	@echo "[test] Checking BMv2 Thrift ports..."
	@ss -tlnp | grep -E ':909[0-3]' || echo "  WARNING: No Thrift ports open!"
	@echo "[test] BMv2 processes:"
	@pgrep -a simple_switch_grpc || echo "  WARNING: No simple_switch_grpc running!"

test-report-size:
	@echo "[test] Verifying Python struct parses 54 bytes as expected..."
	@$(PYTHON) -c "\
import struct; \
fmt=''>IIQQIIIIHHIIBHHx''; \
sz=struct.calcsize(fmt); \
print(f''REPORT_FMT={fmt}''); \
print(f''REPORT_SIZE={sz} bytes''); \
assert sz==54, f''Expected 54, got {sz}!''; \
print(''✓ Size check passed'')"

# ─── Cleanup ──────────────────────────────────────────────────────────────────
clean:
	@rm -rf $(BUILD_DIR)
	@rm -rf logs/
	@echo "Cleaned build artifacts and logs."
	

clean-results:
	@rm -rf eval/results/
	@echo "Cleaned evaluation results."

clean-mininet:
	@echo "[clean] Removing Mininet state..."
	sudo mn -c 2>/dev/null || true
	sudo pkill -9 -f simple_switch_grpc 2>/dev/null || true
	@echo "[clean] Done."

# ─── Help ─────────────────────────────────────────────────────────────────────
help:
	@echo ""
	@echo "P4 Programmable Telemetry — Makefile Targets"
	@echo "=============================================="
	@echo ""
	@echo "Build:"
	@echo "  make compile          Compile P4 → BMv2 JSON + P4Info"
	@echo "  make test-compile     Compile and verify output files"
	@echo "  make test-report-size Verify Python struct size matches P4 header"
	@echo ""
	@echo "Run (use 4 terminals):"
	@echo "  make topology         Start Mininet with BMv2 switches [sudo]"
	@echo "  make collector        Start telemetry UDP collector + REST API"
	@echo "  make controller       Install tables + run congestion policy"
	@echo "  make tables-only      Install tables only (baseline experiment)"
	@echo ""
	@echo "Debug:"
	@echo "  make debug-collector  Collector with DEBUG logging"
	@echo "  make debug-controller Controller with DEBUG logging"
	@echo "  make test-collector   Query REST API endpoints"
	@echo "  make test-switches    Check gRPC/Thrift ports + processes"
	@echo ""
	@echo "Evaluation:"
	@echo "  make eval             Run full benchmark (all 3 experiments)"
	@echo "  make eval-load        Experiment 1: increasing load"
	@echo "  make eval-burst       Experiment 2: bursty traffic"
	@echo "  make eval-degrade     Experiment 3: link degradation"
	@echo "  make plots            Generate matplotlib plots"
	@echo ""
	@echo "Cleanup:"
	@echo "  make clean            Remove build/ and logs/"
	@echo "  make clean-results    Remove eval/results/"
	@echo "  make clean-mininet    Kill Mininet + BMv2 processes"
	@echo ""
	@echo "Typical session:"
	@echo "  Terminal 1: sudo make topology"
	@echo "  Terminal 2: make collector         (or make debug-collector)"
	@echo "  Terminal 3: make controller        (or make debug-controller)"
	@echo "  Terminal 4: make test-collector    (verify data flowing)"
	@echo "              make eval"
	@echo ""
