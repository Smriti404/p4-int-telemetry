#!/usr/bin/env python3
"""
topology/topo.py
================
Mininet 2-path topology with BMv2 (simple_switch_grpc) P4 switches.

Topology:
    h1 ─── s1 ─── s2 ─── s4 ─── h2
              └─── s3 ───┘
    h_ctrl ── s1  (collector host, receives cloned telemetry packets)

Paths:
  Primary   : h1 → s1 → s2 → s4 → h2  (1 ms delay, 10 Mbps)
  Alternate : h1 → s1 → s3 → s4 → h2  (2 ms delay, 10 Mbps)
"""

import os
import sys
import subprocess
import time

from mininet.net import Mininet
from mininet.node import Host, OVSSwitch
from mininet.log import setLogLevel, info, error
from mininet.cli import CLI
from mininet.link import TCLink


class P4Switch(OVSSwitch):
    """
    Mininet node that runs simple_switch_grpc (BMv2 P4 software switch).
    Each instance gets a unique gRPC port and Thrift port.
    """

    _sw_index = 0  # class-level counter — reset to 0 before creating switches

    def __init__(self, name: str, json_path: str, p4info_path: str, **kwargs):
        super().__init__(name, **kwargs)
        self.json_path   = json_path
        self.p4info_path = p4info_path
        self.grpc_port   = 50051 + P4Switch._sw_index
        self.thrift_port = 9090  + P4Switch._sw_index
        self.sw_id       = P4Switch._sw_index + 1
        P4Switch._sw_index += 1

        info(
            f"[DEBUG] P4Switch created: name={name} "
            f"sw_id={self.sw_id} "
            f"grpc_port={self.grpc_port} "
            f"thrift_port={self.thrift_port}\n"
        )

    def start(self, controllers):
        os.makedirs("logs", exist_ok=True)
        log_file = f"logs/{self.name}.log"

        # Remove stale log so we always see fresh output
        if os.path.exists(log_file):
            os.remove(log_file)

        # Build interface list: BMv2 port numbers start at 1
        # intfList() includes "lo" — skip it
        intf_list = [
            (port, intf.name)
            for port, intf in enumerate(self.intfList(), start=1)
            if intf.name != "lo"
        ]
        info(f"[DEBUG] {self.name} interface list: {intf_list}\n")

        if not intf_list:
            error(f"[ERROR] {self.name} has NO interfaces — something is wrong!\n")

        intfs = " ".join(f"-i {port}@{name}" for port, name in intf_list)

        # Full BMv2 command
        cmd = (
            f"simple_switch_grpc "
            f"--device-id {self.sw_id} "
            f"--thrift-port {self.thrift_port} "
            f"--log-file logs/{self.name} "
            f"--log-flush "
            f"--no-p4 "           # load pipeline via P4Runtime after start
            f"{intfs} "
            f"-- "
            f"--grpc-server-addr 0.0.0.0:{self.grpc_port} "
            f"> {log_file} 2>&1 &"
        )

        info(f"[DEBUG] {self.name} CMD:\n  {cmd}\n")
        self.cmd(cmd)

        # Give BMv2 time to bind ports
        time.sleep(3)

        # Check if the process actually started
        pid_output = self.cmd("pgrep -a simple_switch_grpc").strip()
        info(f"[DEBUG] {self.name} running processes:\n  {pid_output}\n")

        # Verify gRPC port is open
        port_check = subprocess.run(
            ["ss", "-tlnp"],
            capture_output=True, text=True
        )
        grpc_listening = f":{self.grpc_port}" in port_check.stdout
        thrift_listening = f":{self.thrift_port}" in port_check.stdout

        if grpc_listening:
            info(f"[OK] {self.name} gRPC port {self.grpc_port} is OPEN\n")
        else:
            error(
                f"[ERROR] {self.name} gRPC port {self.grpc_port} NOT listening! "
                f"Check logs/{self.name}.log\n"
            )
            self._dump_log(log_file)

        if thrift_listening:
            info(f"[OK] {self.name} Thrift port {self.thrift_port} is OPEN\n")
        else:
            error(
                f"[ERROR] {self.name} Thrift port {self.thrift_port} NOT listening! "
                f"Check logs/{self.name}.log\n"
            )

    def _dump_log(self, log_file: str):
        """Print last 20 lines of the switch log for debugging."""
        try:
            with open(log_file) as f:
                lines = f.readlines()
            error(f"[LOG] Last 20 lines of {log_file}:\n")
            for line in lines[-20:]:
                error(f"  {line}")
        except FileNotFoundError:
            error(f"[LOG] {log_file} not found\n")

    def stop(self, deleteIntfs=True):
        info(f"[DEBUG] Stopping {self.name} (grpc={self.grpc_port} thrift={self.thrift_port})\n")
        self.cmd(f"fuser -k {self.grpc_port}/tcp  2>/dev/null; true")
        self.cmd(f"fuser -k {self.thrift_port}/tcp 2>/dev/null; true")
        self.cmd("kill %simple_switch_grpc 2>/dev/null; true")
        super().stop(deleteIntfs)


def check_port_free(port: int) -> bool:
    result = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True)
    if f":{port}" in result.stdout:
        info(f"[WARN] Port {port} already in use! Kill old processes first.\n")
        return False
    return True


def check_binary_available(binary: str) -> bool:
    result = subprocess.run(["which", binary], capture_output=True, text=True)
    if result.returncode != 0:
        error(
            f"[FATAL] '{binary}' not found in PATH. "
            f"Run scripts/install.sh and source /etc/profile.d/p4-telemetry.sh\n"
        )
        return False
    info(f"[OK] Found {binary} at {result.stdout.strip()}\n")
    return True


def run_network(
    json_path: str   = "build/int_telemetry.json",
    p4info_path: str = "build/int_telemetry.p4info.txt",
    interactive: bool = True,
):
    # ── Pre-flight checks ────────────────────────────────────────
    info("*** Pre-flight checks...\n")

    if not os.path.exists(json_path):
        error(f"[FATAL] {json_path} not found — run 'make compile' first\n")
        sys.exit(1)

    if not os.path.exists(p4info_path):
        error(f"[FATAL] {p4info_path} not found — run 'make compile' first\n")
        sys.exit(1)

    if not check_binary_available("simple_switch_grpc"):
        sys.exit(1)

    if not check_binary_available("simple_switch_CLI"):
        sys.exit(1)

    # Port availability
    for port in [50051, 50052, 50053, 50054, 9090, 9091, 9092, 9093]:
        check_port_free(port)

    # Kill stale BMv2 processes
    info("*** Killing any leftover simple_switch_grpc processes...\n")
    os.system("pkill -9 -f simple_switch_grpc 2>/dev/null; sleep 2")

    # ── Build network ─────────────────────────────────────────────
    setLogLevel("info")
    P4Switch._sw_index = 0

    net = Mininet(topo=None, link=TCLink, controller=None, autoSetMacs=True)

    info("*** Adding hosts\n")
    h1     = net.addHost("h1",     ip="10.0.1.1/24",   mac="00:00:00:00:01:01")
    h2     = net.addHost("h2",     ip="10.0.2.1/24",   mac="00:00:00:00:02:01")
    h_ctrl = net.addHost("h_ctrl", ip="10.0.1.254/24", mac="00:00:00:00:01:FE")

    info("*** Adding P4 switches\n")
    sw_kw = dict(
        cls=P4Switch,
        json_path=json_path,
        p4info_path=p4info_path,
        failMode="standalone",
    )
    s1 = net.addSwitch("s1", **sw_kw)
    s2 = net.addSwitch("s2", **sw_kw)
    s3 = net.addSwitch("s3", **sw_kw)
    s4 = net.addSwitch("s4", **sw_kw)

    info("*** Adding links\n")
    # Host ↔ switch links (no TC constraints — max speed)
    net.addLink(h1,     s1, intfName1="h1-eth0",    intfName2="s1-eth1")
    net.addLink(h_ctrl, s1, intfName1="hctrl-eth0", intfName2="s1-eth4")
    net.addLink(h2,     s4, intfName1="h2-eth0",    intfName2="s4-eth1")

    # Inter-switch links (TC bandwidth + delay)
    net.addLink(s1, s2,
                intfName1="s1-eth2", intfName2="s2-eth1",
                cls=TCLink, bw=5, delay="1ms")
    net.addLink(s2, s4,
                intfName1="s2-eth2", intfName2="s4-eth2",
                cls=TCLink, bw=5, delay="1ms")
    net.addLink(s1, s3,
                intfName1="s1-eth3", intfName2="s3-eth1",
                cls=TCLink, bw=5, delay="2ms")
    net.addLink(s3, s4,
                intfName1="s3-eth2", intfName2="s4-eth3",
                cls=TCLink, bw=5, delay="2ms")

    info("*** Starting network\n")
    net.start()
    

    # ADD THIS BLOCK HERE
    h1.cmd("sysctl -w net.ipv6.conf.all.disable_ipv6=1")
    h2.cmd("sysctl -w net.ipv6.conf.all.disable_ipv6=1")
    h_ctrl.cmd("sysctl -w net.ipv6.conf.all.disable_ipv6=1")

    info("*** Waiting 10s for all BMv2 switches to fully start...\n")
    time.sleep(10)

    # ── Switch status verification ────────────────────────────────
    info("\n*** === Switch Status Report ===\n")
    all_pids = {}
    for sw in [s1, s2, s3, s4]:
        pids = sw.cmd("pgrep -f simple_switch_grpc").strip().split()
        all_pids[sw.name] = pids
        info(
            f"    {sw.name}: pids={pids} "
            f"grpc={sw.grpc_port} "
            f"thrift={sw.thrift_port} "
            f"sw_id={sw.sw_id}\n"
        )

    # Warn if switches share PIDs (means they failed to start independently)
    flat = [p for plist in all_pids.values() for p in plist]
    unique_pids = set(flat)
    if len(unique_pids) < 4:
        error(
            f"\n[WARNING] Only {len(unique_pids)} unique PIDs for 4 switches! "
            f"Some switches failed to start.\n"
            f"  Likely causes: port conflict, missing JSON, or PATH issue.\n"
        )
        info("  Dumping all log files:\n")
        for name in ["s1", "s2", "s3", "s4"]:
            lf = f"logs/{name}.log"
            info(f"\n--- {lf} ---\n")
            try:
                with open(lf) as f:
                    info(f.read())
            except FileNotFoundError:
                info("  (file not found)\n")
    else:
        info(f"\n[OK] All 4 switches have unique PIDs: {unique_pids}\n")

    # ── Port verification ─────────────────────────────────────────
    info("\n*** Open gRPC ports (expect 50051-50054):\n")
    grpc_result = os.popen("ss -tlnp | grep -E ':5005[1-4]'").read()
    info(grpc_result if grpc_result else "  NONE — no gRPC ports open!\n")

    info("\n*** Open Thrift ports (expect 9090-9093):\n")
    thrift_result = os.popen("ss -tlnp | grep -E ':909[0-3]'").read()
    info(thrift_result if thrift_result else "  NONE — no Thrift ports open!\n")

    # ── BMv2 port map from logs ───────────────────────────────────
    info("\n*** BMv2 interface assignments (from logs):\n")
    for name in ["s1", "s2", "s3", "s4"]:
        lf = f"logs/{name}.log"
        try:
            with open(lf) as f:
                lines = [l for l in f if "Adding interface" in l]
            if lines:
                info(f"    {name}:\n")
                for l in lines:
                    info(f"      {l.strip()}\n")
            else:
                info(f"    {name}: (no 'Adding interface' lines found — check {lf})\n")
        except FileNotFoundError:
            info(f"    {name}: log not found\n")

    # ── Host routing + static ARP ────────────────────────────────
    info("\n*** Configuring host routes and static ARP\n")
    h1.cmd("ip route add 10.0.2.0/24 dev h1-eth0 2>/dev/null || true")
    h2.cmd("ip route add 10.0.1.0/24 dev h2-eth0 2>/dev/null || true")
    h_ctrl.cmd("ip route add 10.0.2.0/24 dev hctrl-eth0 2>/dev/null || true")
    h1.cmd("arp -s 10.0.2.1 00:00:00:00:02:01")
    h2.cmd("arp -s 10.0.1.1 00:00:00:00:01:01")
    info("[OK] Static ARP entries set — no ARP broadcasts needed\n")
    info(f"    h1 routes:     {h1.cmd('ip route').strip()}\n")
    info(f"    h2 routes:     {h2.cmd('ip route').strip()}\n")
    info(f"    h_ctrl routes: {h_ctrl.cmd('ip route').strip()}\n")

    # ── Start collector inside h_ctrl network namespace ─────────
    # IMPORTANT: collector.py MUST run inside h_ctrl's namespace.
    # The P4 switches send clone packets to 10.0.1.254:9001 (h_ctrl's IP).
    # That IP only exists inside Mininet — the host never sees those packets.
    info("*** Starting collector inside h_ctrl namespace...\n")
    collector_cmd = (
        "/opt/p4/venv/bin/python3 controller/collector.py "
        "--udp-host 10.0.1.254 --udp-port 9001 "
        "--api-port 5000 --threshold 10 --log-level INFO "
        "> logs/collector.log 2>&1 &"
    )
    h_ctrl.cmd(collector_cmd)
    time.sleep(2)
    col_pid = h_ctrl.cmd("pgrep -f 'collector.py'").strip()
    if col_pid:
        info(f"[OK] Collector running in h_ctrl (pid={col_pid})\n")
    else:
        error("[ERROR] Collector failed to start — check logs/collector.log\n")

    # ── socat bridge: host 127.0.0.1:5000 → h_ctrl 10.0.1.254:5000 ──────────
    # runtime.py runs on the HOST and needs to reach the collector REST API.
    # 10.0.1.254 is unreachable from the host (different net namespace).
    # We use nsenter to run socat in h_ctrl's namespace, forwarding
    # host 127.0.0.1:5000 to 10.0.1.254:5000 inside the namespace.
    #hctrl_pid = h_ctrl.cmd("cat /proc/self/stat").strip().split()[0]
    hctrl_pid = h_ctrl.pid
    info(f"*** h_ctrl PID={hctrl_pid} — setting up socat bridge host:5000→h_ctrl:5000\n")
    
    #socat_cmd = (
    #    "socat TCP-LISTEN:5000,fork,reuseaddr,bind=0.0.0.0 "
    #    "TCP:10.0.1.254:5000 > logs/socat.log 2>&1 &"
    #)
    #os.system(socat_cmd)
    # NEW (working):
    os.system(f"ip addr add 10.0.1.253/24 dev s1-eth4 2>/dev/null; true")
    check = os.popen("ip addr show s1-eth4").read()
    if "10.0.1.253" in check:
        info("[OK] Host can reach collector directly at http://10.0.1.254:5000\n")
    else:
        info("[WARN] Could not add IP to s1-eth4 — controller may not reach collector\n")
    
    #time.sleep(1)
    #socat_check = os.popen("ss -tlnp | grep ':5000'").read().strip()
    #if socat_check:
    #    info(f"[OK] socat bridge on host port 5000: {socat_check}\n")
    #else:
    #    info("[WARN] socat not detected on host:5000. Check:\n")
    #    info("       1. Is socat installed? Run: sudo apt-get install -y socat\n")
    #    info("       2. Check logs/socat.log for errors\n")

    # ── Ready banner ──────────────────────────────────────────────
    info("\n" + "=" * 60 + "\n")
    info("*** READY — open 1 more terminal and run:\n")
    info("    Terminal 2: make controller\n")
    info("    (Collector auto-started in h_ctrl, socat bridges host:5000→h_ctrl)\n")
    info("    DO NOT run 'make collector' — already running in h_ctrl!\n")
    info("\n*** Inside this Mininet CLI:\n")
    info("    mininet> h1 ping h2 -c 3\n")
    info("    mininet> h2 iperf3 -s -D\n")
    info("    mininet> h1 iperf3 -c 10.0.2.1 -b 9M -t 30\n")
    info("\n*** Watch telemetry (from another host terminal):\n")
    info("    curl -s http://localhost:5000/health | python3 -m json.tool\n")
    info("    watch -n 1 'curl -s http://localhost:5000/stats | python3 -m json.tool'\n")
    info("=" * 60 + "\n")

    if interactive:
        CLI(net)
    else:
        return net

    os.system("pkill -f 'socat.*5000' 2>/dev/null; true")
    net.stop()
    info("*** Network stopped.\n")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="P4 Telemetry Mininet Topology")
    p.add_argument("--json",   default="build/int_telemetry.json",
                   help="Path to BMv2 JSON pipeline")
    p.add_argument("--p4info", default="build/int_telemetry.p4info.txt",
                   help="Path to P4Info file")
    a = p.parse_args()

    run_network(json_path=a.json, p4info_path=a.p4info)
