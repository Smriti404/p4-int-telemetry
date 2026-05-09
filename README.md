# P4 Programmable Data Plane Telemetry — Complete Guide

## Project Structure

```
p4-telemetry/
├── p4/
│   ├── headers.p4          ← All header & metadata definitions
│   └── int_telemetry.p4    ← Main P4 pipeline (parser→ingress→egress→deparser)
├── controller/
│   ├── collector.py        ← UDP telemetry receiver + REST API
│   └── runtime.py          ← P4Runtime table manager + congestion policy
├── topology/
│   └── topo.py             ← Mininet 2-path topology with BMv2 switches
├── eval/
│   ├── benchmark.py        ← Automated iperf3 benchmarking (3 experiments)
│   └── plot_results.py     ← Matplotlib result plots
├── scripts/
│   ├── install.sh          ← Full dependency installer
│   ├── run_demo.sh         ← One-shot demo launcher
│   └── stop_demo.sh        ← Clean shutdown
├── Makefile                ← Build + run shortcuts
└── README.md               ← This file
```

---

## Step 1 — Prepare Your System

**Requirements:**
- Ubuntu 22.04 LTS (VM or bare metal) — 4 GB RAM minimum, 8 GB recommended
- 20 GB free disk space (for BMv2 + p4c source builds)
- Internet access during install

**If using a VM**, enable nested virtualization (needed for Mininet's network namespaces):
```bash
# On the host (VirtualBox example):
VBoxManage modifyvm "YourVMName" --nested-hw-virt on
```

---

## Step 2 — Run the Installer

```bash
# Clone or copy the project
git clone <your-repo-url> p4-telemetry
cd p4-telemetry

# Run the full installer (takes 20–40 minutes on first run)
sudo bash scripts/install.sh

# Load the environment
source /etc/profile.d/p4-telemetry.sh
```

The installer installs (in order):
1. System packages (gcc, cmake, libpcap, libboost, etc.)
2. Python 3 virtualenv with Flask, Scapy, grpcio, p4runtime-shell
3. Protocol Buffers 3.20.3
4. gRPC 1.51.1
5. BMv2 (behavioral-model — the software P4 switch)
6. p4c (P4 compiler — via apt PPA or source build)
7. PI (P4Runtime reference implementation)
8. Mininet

---

## Step 3 — Compile the P4 Program

```bash
cd p4-telemetry
make compile
```

This produces:
- `build/int_telemetry.json` — BMv2 dataplane binary
- `build/int_telemetry.p4info.txt` — P4Runtime table/action descriptors

---

## Step 4 — Run the System (4 terminals)

Open 4 separate terminal windows in the project directory.

### Terminal 1 — Mininet Topology
```bash
sudo make topology
# or: sudo python3 topology/topo.py
```

You will see the Mininet `mininet>` prompt when ready.
Switches start on gRPC ports 50051–50054.

### Terminal 2 — Telemetry Collector
```bash
make collector
# or: python3 controller/collector.py
```

The collector listens on:
- UDP port 9001 (telemetry reports from switches)
- HTTP port 5000 (REST API)

Check it's working:
```bash
curl http://localhost:5000/health
```

### Terminal 3 — P4Runtime Controller
```bash
make controller
# or: python3 controller/runtime.py
```

This will:
1. Connect to all 4 switches via gRPC
2. Install IPv4 LPM forwarding rules
3. Configure clone sessions for telemetry
4. Start the congestion-aware policy loop (polling every 200 ms)

### Terminal 4 — Test / Evaluation
```bash
# Quick connectivity check (from inside Mininet Terminal 1):
mininet> h1 ping h2 -c 3

# Generate traffic:
mininet> h2 iperf3 -s -D
mininet> h1 iperf3 -c 10.0.2.1 -b 9M -t 20

# Watch telemetry in real-time:
watch -n 0.5 'curl -s http://localhost:5000/stats | python3 -m json.tool'

# Watch congestion alerts:
watch -n 0.5 'curl -s http://localhost:5000/congestion | python3 -m json.tool'
```

---

## Step 5 — Run the Evaluation

```bash
# Full benchmark (runs all 3 experiments):
make eval

# Or individual experiments:
make eval-load      # Experiment 1: increasing offered load
make eval-burst     # Experiment 2: bursty traffic
make eval-degrade   # Experiment 3: link degradation (netem)

# Generate plots from results:
make plots
# → eval/results/plots/*.png
```

### Baseline vs Telemetry Comparison

For a clean comparison:
1. Run `make tables-only` (installs forwarding but NO policy) and run benchmark → baseline results
2. Run `make controller` (full policy) and run benchmark → telemetry-driven results

Results are saved as CSV files in `eval/results/`.

---

## REST API Reference

| Endpoint | Description |
|---|---|
| `GET /health` | Liveness check + packet count |
| `GET /stats` | Per-flow stats: avg queue depth, delay, loss |
| `GET /stats/raw?n=100` | Last N raw telemetry records |
| `GET /congestion` | Flows currently above threshold |
| `POST /threshold {"threshold": 600}` | Update congestion threshold live |

---

## Tuning Parameters

| Parameter | Location | Default | Effect |
|---|---|---|---|
| `CONGESTION_THRESHOLD` | `controller/collector.py` | 800 cells | Queue depth that triggers rerouting |
| `WINDOW_SECONDS` | `collector.py` | 2.0 s | Rolling average window |
| `POLL_INTERVAL_SEC` | `runtime.py` | 0.2 s | Control loop frequency |
| `TELEMETRY_CLONE_SID` | `headers.p4` | 100 | BMv2 clone session ID |
| Link bandwidth | `topology/topo.py` | 10 Mbps | Backbone link capacity |

---

## Troubleshooting

**`simple_switch_grpc` not found:**
```bash
which simple_switch_grpc
# If missing: check /usr/local/bin or re-run make install in behavioral-model/
export PATH=/usr/local/bin:$PATH
```

**gRPC connection refused (controller can't connect):**
```bash
# Check switches are running:
ps aux | grep simple_switch
# Check ports:
ss -tlnp | grep 5005
```

**P4 compilation error:**
```bash
# Check p4c version:
p4c --version
# Must be >= 1.2.3.0 for v1model support
```

**Mininet cleanup after crash:**
```bash
sudo mn -c
sudo pkill simple_switch_grpc
```

**No telemetry arriving at collector:**
```bash
# Check clone session is installed on switch:
# In Mininet terminal:
mininet> s1 simple_switch_CLI --thrift-port 9090
RuntimeCmd: mc_dump
# Should show mirror session 100
```

---

## Architecture Deep-Dive

### Data Plane (P4)
- Parser extracts Ethernet/IP/TCP/UDP headers
- Flow hash computed from 5-tuple (CRC32)
- `flow_reroute` table checked first (controller-managed, exact-match)
- `ipv4_lpm` table as default forwarding
- Every packet is cloned (session 100) → egress builds telemetry report
- Telemetry report contains: switch_id, timestamps, queue depths, flow hash

### Telemetry Path
- BMv2 clones packet → sends clone to collector port
- Egress strips original headers, adds Ethernet+IP+UDP+telemetry_report headers
- Collector receives UDP datagram, parses struct, stores in ring buffer
- Rolling 2-second window used for all statistics

### Control Plane
- Controller polls `/congestion` every 200 ms
- If avg_enq_qdepth > 800: installs reroute entry on s1 for that flow
- If avg_enq_qdepth < 400 (hysteresis): removes reroute entry
- P4Runtime gRPC used for all table modifications (no restart required)
