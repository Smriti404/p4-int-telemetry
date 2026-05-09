#!/usr/bin/env python3
"""
controller/runtime.py
=====================
P4Runtime controller with Thrift-based clone sessions.

Verified BMv2 port map (from logs/sX.log "Adding interface" lines):

  s1: s1-eth1 (h1)     → port 2
      s1-eth4 (h_ctrl)  → port 3
      s1-eth2 (s2)      → port 4
      s1-eth3 (s3)      → port 5

  s2: s2-eth1 (s1)     → port 2
      s2-eth2 (s4)      → port 3

  s3: s3-eth1 (s1)     → port 2
      s3-eth2 (s4)      → port 3

  s4: s4-eth1 (h2)     → port 2
      s4-eth2 (s2)      → port 3
      s4-eth3 (s3)      → port 4
"""

import argparse
import logging
import subprocess
import sys
import time

import requests

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("controller")

try:
    import p4runtime_sh.shell as sh
except ImportError:
    log.error("[STARTUP] p4runtime-shell not installed — run: pip install p4runtime-shell")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────
# Verified port map (matches BMv2 logs exactly)
# ─────────────────────────────────────────────────────────────

S1 = {"h1": 2, "ctrl": 3, "s2": 4, "s3": 5}
S2 = {"s1": 2, "s4": 3}
S3 = {"s1": 2, "s4": 3}
S4 = {"h2": 2, "s2": 3, "s3": 4}

SUBNET_H1 = "10.0.1.0/24"
SUBNET_H2 = "10.0.2.0/24"
CLONE_SID  = 100

CONGESTION_THRESHOLD = 1000
HYSTERESIS           = 2      # restore once avg_q drops below this (cells)
POLL_SEC             = 0.5

THRIFT = {"s1": 9090, "s2": 9091, "s3": 9092, "s4": 9093}
GRPC   = {"s1": 50051, "s2": 50052, "s3": 50053, "s4": 50054}
DEV_ID = {"s1": 1, "s2": 2, "s3": 3, "s4": 4}

# MAC addresses — dst_mac written into forwarded packets
MACS = {
    "s1_h1":  "00:00:00:00:01:01",
    "s1_s2":  "00:aa:00:02:01:00",
    "s1_s3":  "00:aa:00:03:01:00",
    "s2_s1":  "00:aa:00:01:02:00",
    "s2_s4":  "00:aa:00:04:02:00",
    "s3_s1":  "00:aa:00:01:03:00",
    "s3_s4":  "00:aa:00:04:03:00",
    "s4_h2":  "00:00:00:00:02:01",
    "s4_s2":  "00:aa:00:02:04:00",
    "s4_s3":  "00:aa:00:03:04:00",
}

# Track whether traffic is currently on alternate path
_on_alternate_path = False


# ─────────────────────────────────────────────────────────────
# Thrift helpers
# ─────────────────────────────────────────────────────────────

def thrift_cmd(sw_name: str, commands: list) -> str:
    thrift_port = THRIFT[sw_name]
    cmd_str = "\n".join(commands)
    log.debug(f"[{sw_name}] Thrift commands: {commands}")
    try:
        result = subprocess.run(
            ["simple_switch_CLI", "--thrift-port", str(thrift_port)],
            input=cmd_str,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.stdout.strip():
            log.debug(f"[{sw_name}] Thrift stdout: {result.stdout.strip()}")
        if result.stderr.strip():
            log.warning(f"[{sw_name}] Thrift stderr: {result.stderr.strip()}")
        return result.stdout
    except FileNotFoundError:
        log.error("[FATAL] simple_switch_CLI not found in PATH.")
        return ""
    except subprocess.TimeoutExpired:
        log.error(f"[{sw_name}] Thrift timed out on port {thrift_port}")
        return ""


def set_switch_id(sw_name: str, sw_id: int):
    log.info(f"[{sw_name}] Writing switch_id={sw_id} to register")
    thrift_cmd(sw_name, [f"register_write MyEgress.switch_id_reg 0 {sw_id}"])


def setup_mirror_session(sw_name: str, session_id: int, egress_port: int):
    log.info(f"[{sw_name}] Setting mirror session {session_id} → port {egress_port}")
    for attempt in range(1, 6):
        time.sleep(1)
        thrift_cmd(sw_name, [f"mirroring_add {session_id} {egress_port}"])
        verify = thrift_cmd(sw_name, [f"mirroring_get {session_id}"])
        if "MirroringSessionConfig" in verify or str(egress_port) in verify:
            log.info(f"[{sw_name}] Mirror session confirmed ✓ (attempt {attempt})")
            return
        log.warning(f"[{sw_name}] Mirror not confirmed yet (attempt {attempt}/5)")
    log.error(f"[{sw_name}] Mirror session FAILED after 5 attempts!")


def disable_telemetry_default(sw_name: str):
    """CRITICAL: stop cloning ALL packets — only clone explicitly enabled ports."""
    log.info(f"[{sw_name}] Disabling default telemetry trigger (NoAction default)")
    thrift_cmd(sw_name, ["table_set_default MyIngress.telemetry_trigger NoAction"])


def enable_telemetry_on_port(sw_name: str, port: int):
    log.info(f"[{sw_name}] Enabling telemetry clone on ingress port {port}")
    out = thrift_cmd(sw_name, [
        f"table_add MyIngress.telemetry_trigger trigger_telemetry {port} =>"
    ])
    log.debug(f"[{sw_name}] table_add: {out.strip()!r}")


# ─────────────────────────────────────────────────────────────
# P4Runtime helpers
# ─────────────────────────────────────────────────────────────

def connect(sw_name: str, p4info: str, json_path: str, retries: int = 5) -> bool:
    addr = f"localhost:{GRPC[sw_name]}"
    log.info(f"[{sw_name}] Connecting gRPC {addr} device_id={DEV_ID[sw_name]}")
    for attempt in range(1, retries + 1):
        try:
            sh.setup(
                device_id=DEV_ID[sw_name],
                grpc_addr=addr,
                election_id=(0, 1),
                config=sh.FwdPipeConfig(p4info, json_path),
            )
            log.info(f"[{sw_name}] gRPC connected (attempt {attempt})")
            return True
        except Exception as e:
            log.warning(f"[{sw_name}] gRPC attempt {attempt}/{retries} failed: {e}")
            if attempt < retries:
                time.sleep(2)
    log.error(f"[{sw_name}] All {retries} gRPC attempts failed.")
    return False


def add_lpm(prefix: str, port: int, dst_mac: str):
    ip, plen = prefix.split("/")
    log.debug(f"[LPM] {prefix} → port={port} mac={dst_mac}")
    te = sh.TableEntry("MyIngress.ipv4_lpm")(action="MyIngress.forward")
    te.match["hdr.ipv4.dstAddr"] = f"{ip}/{plen}"
    te.action["port"]            = str(port)
    te.action["dst_mac"]         = dst_mac
    try:
        te.insert()
        log.info(f"[LPM] INSERT {prefix} → port={port} ({dst_mac})")
    except Exception:
        try:
            te.modify()
            log.info(f"[LPM] MODIFY {prefix} → port={port} ({dst_mac})")
        except Exception as e:
            log.error(f"[LPM] Failed for {prefix}: {e}", exc_info=True)


# ─────────────────────────────────────────────────────────────
# Per-switch table installation
# ─────────────────────────────────────────────────────────────

def install_s1(p4info: str, json_path: str) -> bool:
    log.info("[s1] === Installing tables ===")
    if not connect("s1", p4info, json_path):
        return False

    # s1-eth1 (h1) = port 2,  s1-eth2 (s2) = port 4
    add_lpm(SUBNET_H1, S1["h1"], MACS["s1_h1"])   # 10.0.1.0/24 → port 2
    add_lpm(SUBNET_H2, S1["s2"], MACS["s1_s2"])   # 10.0.2.0/24 → port 4 (primary via s2)

    sh.teardown()
    log.info("[s1] gRPC teardown — pipeline committed")

    set_switch_id("s1", DEV_ID["s1"])

    # Clones from s1 go to h_ctrl (port 3)
    setup_mirror_session("s1", CLONE_SID, S1["ctrl"])  # port 3

    # CRITICAL: disable default clone-everything, then enable ONLY on h1 port
    # This stops s1 from cloning REST-API traffic, ARP, etc.
    disable_telemetry_default("s1")
    enable_telemetry_on_port("s1", S1["h1"])  # port 2 — only clone h1→h2 traffic

    log.info("[s1] === Install complete ===")
    return True


def install_s2(p4info: str, json_path: str) -> bool:
    log.info("[s2] === Installing tables ===")
    if not connect("s2", p4info, json_path):
        return False

    # s2-eth1 (s1) = port 2,  s2-eth2 (s4) = port 3
    add_lpm(SUBNET_H1, S2["s1"], MACS["s2_s1"])   # back to s1
    add_lpm(SUBNET_H2, S2["s4"], MACS["s2_s4"])   # forward to s4

    sh.teardown()
    log.info("[s2] gRPC teardown — pipeline committed")

    set_switch_id("s2", DEV_ID["s2"])

    # Clones from s2 go back to s1 (port 2), which routes them to h_ctrl
    setup_mirror_session("s2", CLONE_SID, S2["s1"])  # port 2

    # CRITICAL: only clone traffic arriving from s1 (toward h2)
    # Do NOT clone traffic coming from s4 (return path) — avoids loop
    disable_telemetry_default("s2")
    enable_telemetry_on_port("s2", S2["s1"])  # port 2 — traffic from s1

    log.info("[s2] === Install complete ===")
    return True


def install_s3(p4info: str, json_path: str) -> bool:
    log.info("[s3] === Installing tables ===")
    if not connect("s3", p4info, json_path):
        return False

    # s3-eth1 (s1) = port 2,  s3-eth2 (s4) = port 3
    add_lpm(SUBNET_H1, S3["s1"], MACS["s3_s1"])
    add_lpm(SUBNET_H2, S3["s4"], MACS["s3_s4"])

    sh.teardown()
    log.info("[s3] gRPC teardown — pipeline committed")

    set_switch_id("s3", DEV_ID["s3"])

    # Clones go back to s1 (port 2)
    setup_mirror_session("s3", CLONE_SID, S3["s1"])  # port 2

    disable_telemetry_default("s3")
    enable_telemetry_on_port("s3", S3["s1"])  # port 2 — traffic from s1

    log.info("[s3] === Install complete ===")
    return True


def install_s4(p4info: str, json_path: str) -> bool:
    log.info("[s4] === Installing tables ===")
    if not connect("s4", p4info, json_path):
        return False

    # s4-eth1 (h2) = port 2,  s4-eth2 (s2) = port 3,  s4-eth3 (s3) = port 4
    add_lpm(SUBNET_H2, S4["h2"], MACS["s4_h2"])   # 10.0.2.0/24 → h2 port 2
    add_lpm(SUBNET_H1, S4["s2"], MACS["s4_s2"])   # 10.0.1.0/24 → back via s2 port 3

    sh.teardown()
    log.info("[s4] gRPC teardown — pipeline committed")

    set_switch_id("s4", DEV_ID["s4"])

    # Clones from s4 go back toward s2 (port 3), s2 routes to s1 → h_ctrl
    setup_mirror_session("s4", CLONE_SID, S4["s2"])  # port 3

    # Only clone traffic arriving from s2/s3 (h1→h2 direction)
    # Do NOT clone h2's outbound traffic to avoid confusion
    disable_telemetry_default("s4")
    enable_telemetry_on_port("s4", S4["s2"])  # port 3 — traffic arriving from s2
    # Also enable on s3 port in case traffic is rerouted via s3
    enable_telemetry_on_port("s4", S4["s3"])  # port 4 — traffic arriving from s3

    log.info("[s4] === Install complete ===")
    return True


# ─────────────────────────────────────────────────────────────
# Congestion policy — reroute / restore
# ─────────────────────────────────────────────────────────────

def reroute_to_s3(p4info: str, json_path: str):
    """Switch all h1→h2 traffic from primary path (s2) to alternate (s3)."""
    global _on_alternate_path
    if _on_alternate_path:
        return

    log.info("[policy] REROUTING: switching 10.0.2.0/24 → s3 (port 5)")
    if not connect("s1", p4info, json_path):
        log.error("[policy] Cannot connect to s1 for reroute")
        return
    try:
        te = sh.TableEntry("MyIngress.ipv4_lpm")(action="MyIngress.forward")
        te.match["hdr.ipv4.dstAddr"] = "10.0.2.0/24"
        te.action["port"]            = str(S1["s3"])   # port 5
        te.action["dst_mac"]         = MACS["s1_s3"]  # 00:aa:00:03:01:00
        #te.modify()
        try:
            te.insert()
            log.info("[policy] INSERT success → rerouted via s3")
        except Exception:
            try:
                te.modify()
                log.info("[policy] MODIFY success → rerouted via s3")
            except Exception as e:
                log.error(f"[policy] Reroute FAILED: {e}", exc_info=True)
        log.info("[policy] ✓ Traffic rerouted via s3 (port 5)")
        _on_alternate_path = True
    except Exception as e:
        log.error(f"[policy] Reroute FAILED: {e}", exc_info=True)
    finally:
        sh.teardown()


def restore_to_s2(p4info: str, json_path: str):
    """Restore h1→h2 traffic back to primary path (s2)."""
    global _on_alternate_path
    if not _on_alternate_path:
        return

    log.info("[policy] RESTORING: switching 10.0.2.0/24 → s2 (port 4)")
    if not connect("s1", p4info, json_path):
        log.error("[policy] Cannot connect to s1 for restore")
        return
    try:
        te = sh.TableEntry("MyIngress.ipv4_lpm")(action="MyIngress.forward")
        te.match["hdr.ipv4.dstAddr"] = "10.0.2.0/24"
        te.action["port"]            = str(S1["s2"])   # port 4
        te.action["dst_mac"]         = MACS["s1_s2"]  # 00:aa:00:02:01:00
        #te.modify()
        try:
            te.insert()
            log.info("[policy] INSERT success → restored to s2")
        except Exception:
            try:
                te.modify()
                log.info("[policy] MODIFY success → restored to s2")
            except Exception as e:
                log.error(f"[policy] Restore FAILED: {e}", exc_info=True)
        log.info("[policy] ✓ Traffic restored to s2 (port 4)")
        _on_alternate_path = False
    except Exception as e:
        log.error(f"[policy] Restore FAILED: {e}", exc_info=True)
    finally:
        sh.teardown()


def policy_loop(p4info: str, json_path: str, collector: str):
    """
    Poll collector every POLL_SEC seconds.
    Reroute ALL traffic via s3 when any flow's avg_enq_qdepth > CONGESTION_THRESHOLD.
    Restore to s2 when all flows drop below HYSTERESIS.
    """
    poll_count = 0

    log.info(
        f"[policy] Starting — threshold={CONGESTION_THRESHOLD} "
        f"hysteresis={HYSTERESIS} poll={POLL_SEC}s"
    )

    while True:
        poll_count += 1
        if poll_count % 20 == 0:
            log.info(
                f"[policy] Poll #{poll_count} — "
                f"on_alternate_path={_on_alternate_path}"
            )

        try:
            r   = requests.get(f"{collector}/congestion", timeout=2)
            r.raise_for_status()
            data = r.json()

            congested = data.get("congested", [])

            # Filter: only care about real h1→h2 flows, not internal traffic
            real_congestion = [
                f for f in congested
                if f.get("dst_ip") == "10.0.2.1"   # h2's IP
            ]
            
                        
            #log.info("[policy] DEMO: forcing congestion → rerouting")
            #reroute_to_s3(p4info, json_path)
            #time.sleep(10)

            #log.info("[policy] DEMO: restoring path")
            #restore_to_s2(p4info, json_path)
            #time.sleep(10)
            #continue

            if real_congestion:
                # worst = max(real_congestion, key=lambda f: f["avg_enq_qdepth"])
                worst = max(real_congestion, key=lambda f: f["avg_delay_us"])
                log.info(
                    f"[policy] CONGESTION DETECTED: "
                    f"{worst['src_ip']}→{worst['dst_ip']} "
                    # f"avg_q={worst['avg_enq_qdepth']:.1f} > threshold={CONGESTION_THRESHOLD}"
                    f"avg_delay={worst['avg_delay_us']:.1f} > threshold={CONGESTION_THRESHOLD}"
                )
                reroute_to_s3(p4info, json_path)

            elif _on_alternate_path:
                # Check if we can restore — all h1→h2 flows must be below hysteresis
                sr = requests.get(f"{collector}/stats", timeout=2)
                sr.raise_for_status()
                stats = sr.json()

                h2_flows = [
                    v for v in stats.values()
                    if v.get("dst_ip") == "10.0.2.1"
                ]
                if h2_flows:
                    max_q = max(v["avg_enq_qdepth"] for v in h2_flows)
                    if max_q < HYSTERESIS:
                        log.info(
                            f"[policy] RECOVERY: max_q={max_q:.1f} < hysteresis={HYSTERESIS}"
                        )
                        restore_to_s2(p4info, json_path)

        except requests.RequestException as e:
            log.warning(f"[policy] Collector unreachable: {e}")
        except Exception as e:
            log.error(f"[policy] Unexpected error: {e}", exc_info=True)

        time.sleep(POLL_SEC)


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="P4Runtime Controller")
    ap.add_argument("--p4info",    default="build/int_telemetry.p4info.txt")
    ap.add_argument("--json",      default="build/int_telemetry.json")
    ap.add_argument("--collector", default="http://10.0.1.254:5000")
    ap.add_argument("--no-policy", action="store_true")
    ap.add_argument("--log-level", default="INFO",
                    choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = ap.parse_args()

    logging.getLogger().setLevel(getattr(logging, args.log_level))

    log.info("=" * 60)
    log.info("[STARTUP] P4Runtime Controller")
    log.info(f"  p4info    : {args.p4info}")
    log.info(f"  json      : {args.json}")
    log.info(f"  collector : {args.collector}")
    log.info(f"  no-policy : {args.no_policy}")
    log.info("=" * 60)

    import os
    for f in [args.p4info, args.json]:
        if not os.path.exists(f):
            log.error(f"[STARTUP] File not found: {f} — run 'make compile' first")
            sys.exit(1)

    log.info("[STARTUP] Installing tables on all switches...")
    results = {
        "s1": install_s1(args.p4info, args.json),
        "s2": install_s2(args.p4info, args.json),
        "s3": install_s3(args.p4info, args.json),
        "s4": install_s4(args.p4info, args.json),
    }

    ok = sum(results.values())
    log.info(f"[STARTUP] Results: {results} — {ok}/4 switches configured")

    if ok == 0:
        log.error("[STARTUP] No switches reachable. Is Mininet running?")
        sys.exit(1)

    if args.no_policy:
        log.info("[STARTUP] --no-policy: done.")
        return

    try:
        r = requests.get(f"{args.collector}/health", timeout=3)
        log.info(f"[STARTUP] Collector health: {r.json()}")
    except Exception as e:
        log.warning(f"[STARTUP] Collector not reachable: {e}")

    try:
        policy_loop(args.p4info, args.json, args.collector)
    except KeyboardInterrupt:
        log.info("[SHUTDOWN] Stopped by user.")


if __name__ == "__main__":
    main()
