#!/usr/bin/env python3
"""
controller/collector.py
=======================
Telemetry collector — listens for INT report packets from BMv2 switches,
parses them, maintains a rolling-window stats store, and exposes a REST API.

Endpoints:
  GET /stats              — per (switch, flow) avg queue depth + delay
  GET /stats/raw          — last 500 raw records
  GET /congestion         — flows currently above threshold
  GET /health             — liveness probe
  POST /threshold         — update congestion threshold live

Run:
    python3 controller/collector.py [--udp-port 9001] [--api-port 5000]
"""

import argparse
import json
import logging
import socket
import struct
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

from flask import Flask, jsonify, request
from flask_cors import CORS

# ─────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────

CONGESTION_THRESHOLD = 1000    
WINDOW_SECONDS       = 2.0    # rolling window for averages
MAX_RECORDS          = 2000   # ring buffer size per flow

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("collector")

# timestamps are bit<48> in P4 = 6 bytes on wire.
# Python struct has no 6-byte integer code, so we use '6s' (6-byte raw string)
# and convert manually.  Do NOT use 'Q' (8 bytes) — that would make the
# struct 58 bytes and mis-align every field after the timestamps.
#
# Layout (big-endian), total = 54 bytes:
#   switch_id         : 4 bytes  (I)
#   seq_num           : 4 bytes  (I)
#   ingress_timestamp : 6 bytes  (6s → int.from_bytes)
#   egress_timestamp  : 6 bytes  (6s → int.from_bytes)
#   enq_qdepth        : 4 bytes  (I)
#   deq_qdepth        : 4 bytes  (I)
#   deq_timedelta     : 4 bytes  (I)
#   flow_hash         : 4 bytes  (I)
#   ingress_port      : 2 bytes  (H)
#   egress_port       : 2 bytes  (H)
#   src_ip            : 4 bytes  (I)
#   dst_ip            : 4 bytes  (I)
#   protocol          : 1 byte   (B)
#   src_port          : 2 bytes  (H)
#   dst_port          : 2 bytes  (H)
#   pad               : 1 byte   (x)
#                      ──────────
#                      54 bytes total  ← must match telemetry_report_t in headers.p4

# timestamps are bit<48> in P4 = 6 bytes on wire.
# Python struct has no 6-byte integer code, so we use '6s' (6-byte raw string)
# and convert manually.  Do NOT use 'Q' (8 bytes) — that would make the
# struct 58 bytes and mis-align every field after the timestamps.
REPORT_FMT  = ">II6s6sIIIIHHIIBHHx"
REPORT_SIZE = struct.calcsize(REPORT_FMT)

log.info(f"[INIT] REPORT_FMT='{REPORT_FMT}'  REPORT_SIZE={REPORT_SIZE} bytes")
assert REPORT_SIZE == 54, (
    f"[BUG] REPORT_SIZE={REPORT_SIZE} but expected 54! "
    f"Check headers.p4 telemetry_report_t layout and REPORT_FMT."
)


@dataclass
class TelemetryRecord:
    switch_id:     int
    seq_num:       int
    ingress_ts:    int    # nanoseconds (48-bit, stored in 64-bit Q)
    egress_ts:     int
    enq_qdepth:    int    # cells
    deq_qdepth:    int
    deq_timedelta: int    # microseconds
    flow_hash:     int
    ingress_port:  int
    egress_port:   int
    src_ip:        str
    dst_ip:        str
    protocol:      int
    src_port:      int
    dst_port:      int
    recv_time:     float  # local clock (time.time())

    @property
    def hop_delay_us(self) -> int:
        return self.deq_timedelta

    @property
    def key(self) -> str:
        return f"{self.switch_id}:{self.flow_hash:08x}"

    def to_dict(self):
        d = asdict(self)
        d["hop_delay_us"] = self.hop_delay_us
        d["key"] = self.key
        return d


def ip_int_to_str(ip: int) -> str:
    return socket.inet_ntoa(struct.pack(">I", ip))


def parse_report(data: bytes) -> Optional[TelemetryRecord]:
    """
    Parse a raw UDP payload into a TelemetryRecord.
    The OS UDP socket strips Ethernet+IP+UDP headers — only the
    telemetry_report_t payload arrives here.
    """
    if len(data) < REPORT_SIZE:
        log.debug(
            f"[PARSE] Packet too short: got {len(data)} bytes, "
            f"need {REPORT_SIZE}. Raw hex: {data.hex()}"
        )
        return None

    # Warn if there are extra bytes (possible header mis-alignment)
    if len(data) > REPORT_SIZE:
        log.debug(
            f"[PARSE] Extra bytes: got {len(data)}, expected {REPORT_SIZE}. "
            f"Will parse first {REPORT_SIZE} bytes; trailing={data[REPORT_SIZE:].hex()}"
        )

    try:
        (sw_id, seq,
         ing_ts_bytes, eg_ts_bytes,
         enq_q, deq_q, deq_delta,
         flow_hash,
         ingress_port, egress_port,
         src_ip_int, dst_ip_int,
         proto, src_port, dst_port) = struct.unpack(REPORT_FMT, data[:REPORT_SIZE])

        # Convert 6-byte big-endian strings to integers (48-bit timestamps)
        ingress_ts = int.from_bytes(ing_ts_bytes, byteorder='big')
        egress_ts  = int.from_bytes(eg_ts_bytes,  byteorder='big')

        # Sanity checks — log warnings on obviously wrong values
        if sw_id == 0 or sw_id > 64:
            log.warning(f"[PARSE] Suspicious switch_id={sw_id} — check device-id in topo.py")
        if enq_q > 1_000_000:
            log.warning(f"[PARSE] enq_qdepth={enq_q} looks too large — struct alignment issue?")
        if src_ip_int == 0 or dst_ip_int == 0:
            log.warning(f"[PARSE] src_ip or dst_ip is 0 — clone metadata may be missing")

        rec = TelemetryRecord(
            switch_id     = sw_id,
            seq_num       = seq,
            ingress_ts    = ingress_ts,
            egress_ts     = egress_ts,
            enq_qdepth    = enq_q,
            deq_qdepth    = deq_q,
            deq_timedelta = deq_delta,
            flow_hash     = flow_hash,
            ingress_port  = ingress_port,
            egress_port   = egress_port,
            src_ip        = ip_int_to_str(src_ip_int),
            dst_ip        = ip_int_to_str(dst_ip_int),
            protocol      = proto,
            src_port      = src_port,
            dst_port      = dst_port,
            recv_time     = time.time(),
        )

        log.debug(
            f"[PARSE OK] sw={rec.switch_id} seq={rec.seq_num} "
            f"flow={rec.flow_hash:08x} "
            f"{rec.src_ip}:{rec.src_port} → {rec.dst_ip}:{rec.dst_port} "
            f"proto={rec.protocol} "
            f"enq_q={rec.enq_qdepth} deq_q={rec.deq_qdepth} "
            f"delay_us={rec.deq_timedelta} "
            f"ing_port={rec.ingress_port} eg_port={rec.egress_port}"
        )
        return rec

    except struct.error as e:
        log.error(
            f"[PARSE ERROR] struct.unpack failed: {e}. "
            f"data_len={len(data)} fmt='{REPORT_FMT}'. "
            f"Raw hex: {data[:REPORT_SIZE].hex()}"
        )
        return None
    except Exception as e:
        log.error(f"[PARSE ERROR] Unexpected error: {e}", exc_info=True)
        return None


# ─────────────────────────────────────────────────────────────
# Stats store (thread-safe)
# ─────────────────────────────────────────────────────────────

class StatsStore:
    """
    Thread-safe ring-buffer store.
    Keyed by "<switch_id>:<flow_hash_hex>".
    """

    def __init__(self, window_sec: float = WINDOW_SECONDS):
        self._lock       = threading.Lock()
        self._window     = window_sec
        self._records: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=MAX_RECORDS)
        )
        self._total_pkts = 0
        self.threshold   = CONGESTION_THRESHOLD
        log.info(
            f"[StatsStore] window={window_sec}s  "
            f"threshold={self.threshold}  max_records={MAX_RECORDS}"
        )

    def add(self, rec: TelemetryRecord):
        with self._lock:
            self._records[rec.key].append(rec)
            self._total_pkts += 1
            if self._total_pkts % 100 == 0:
                log.info(
                    f"[StatsStore] Milestone: {self._total_pkts} total packets "
                    f"across {len(self._records)} flows"
                )

    def _recent(self, records: deque) -> List[TelemetryRecord]:
        cutoff = time.time() - self._window
        return [r for r in records if r.recv_time >= cutoff]

    def get_stats(self) -> dict:
        now    = time.time()
        result = {}
        with self._lock:
            for key, buf in self._records.items():
                recent = self._recent(buf)
                if not recent:
                    continue
                avg_enq   = sum(r.enq_qdepth    for r in recent) / len(recent)
                avg_deq   = sum(r.deq_qdepth    for r in recent) / len(recent)
                avg_delay = sum(r.deq_timedelta  for r in recent) / len(recent)
                max_enq   = max(r.enq_qdepth    for r in recent)
                last      = recent[-1]
                # congested = avg_enq > self.threshold
                
  
                congested = avg_delay > 1000   # microseconds threshold
                result[key] = {
                    "switch_id":        last.switch_id,
                    "flow_hash":        f"{last.flow_hash:08x}",
                    "src_ip":           last.src_ip,
                    "dst_ip":           last.dst_ip,
                    "protocol":         last.protocol,
                    "src_port":         last.src_port,
                    "dst_port":         last.dst_port,
                    "ingress_port":     last.ingress_port,
                    "egress_port":      last.egress_port,
                    "avg_enq_qdepth":   round(avg_enq,   1),
                    "avg_deq_qdepth":   round(avg_deq,   1),
                    "max_enq_qdepth":   max_enq,
                    "avg_delay_us":     round(avg_delay,  1),
                    "pkt_count_window": len(recent),
                    "congested":        congested,
                    "last_seen":        round(now - last.recv_time, 3),
                }
                if congested:
                    log.debug(
                        f"[StatsStore] CONGESTED key={key} "
                        f"avg_enq={avg_enq:.1f} > threshold={self.threshold}"
                    )
        return result

    def get_congested(self) -> list:
        stats     = self.get_stats()
        congested = [v for v in stats.values() if v["congested"]]
        if congested:
            log.info(
                f"[StatsStore] {len(congested)} congested flow(s): "
                + ", ".join(
                    f"{c['src_ip']}:{c['src_port']}→"
                    f"{c['dst_ip']}:{c['dst_port']} "
                    f"q={c['avg_enq_qdepth']}"
                    for c in congested
                )
            )
        return congested

    def get_raw(self, n: int = 100) -> list:
        all_recs = []
        with self._lock:
            for buf in self._records.values():
                all_recs.extend(list(buf)[-20:])  # last 20 per flow
        all_recs.sort(key=lambda r: r.recv_time, reverse=True)
        return [r.to_dict() for r in all_recs[:n]]

    @property
    def total_packets(self) -> int:
        return self._total_pkts

    def debug_summary(self):
        with self._lock:
            log.info(
                f"[StatsStore DEBUG] total_pkts={self._total_pkts} "
                f"flows={len(self._records)} "
                f"threshold={self.threshold} "
                f"window={self._window}s"
            )
            for key, buf in self._records.items():
                recent = self._recent(buf)
                log.info(
                    f"  flow={key} buf_size={len(buf)} "
                    f"recent={len(recent)}"
                )


# ─────────────────────────────────────────────────────────────
# UDP listener thread
# ─────────────────────────────────────────────────────────────

_udp_recv_count    = 0
_udp_parse_ok      = 0
_udp_parse_fail    = 0
_udp_last_log_time = 0.0

def udp_listener(store: StatsStore, host: str, port: int):
    """Receive telemetry UDP packets and push to the stats store."""
    global _udp_recv_count, _udp_parse_ok, _udp_parse_fail, _udp_last_log_time

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)  # 4 MB buffer

    try:
        sock.bind((host, port))
    except OSError as e:
        log.error(f"[UDP] Cannot bind {host}:{port} — {e}")
        log.error("[UDP] Is another collector already running?  ss -ulnp | grep 9001")
        raise

    log.info(f"[UDP] Listening on {host}:{port}  (REPORT_SIZE={REPORT_SIZE})")

    while True:
        try:
            data, addr = sock.recvfrom(4096)
            _udp_recv_count += 1

            now = time.time()
            if now - _udp_last_log_time >= 10.0:
                log.info(
                    f"[UDP] Stats: recv={_udp_recv_count} "
                    f"parsed_ok={_udp_parse_ok} "
                    f"parse_fail={_udp_parse_fail}"
                )
                _udp_last_log_time = now

            log.debug(
                f"[UDP] Packet from {addr}  len={len(data)}  "
                f"hex[:16]={data[:16].hex()}"
            )

            rec = parse_report(data)
            if rec:
                _udp_parse_ok += 1
                store.add(rec)
            else:
                _udp_parse_fail += 1
                if _udp_parse_fail <= 10 or _udp_parse_fail % 50 == 0:
                    log.warning(
                        f"[UDP] Parse failure #{_udp_parse_fail} "
                        f"from {addr}  len={len(data)}  "
                        f"hex={data.hex()}"
                    )

        except Exception as e:
            log.warning(f"[UDP] Receive error: {e}", exc_info=True)


# ─────────────────────────────────────────────────────────────
# REST API (Flask)
# ─────────────────────────────────────────────────────────────

def make_app(store: StatsStore) -> Flask:
    app = Flask("collector")
    CORS(app)

    @app.route("/health")
    def health():
        return jsonify({
            "status":        "ok",
            "total_packets": store.total_packets,
            "udp_recv":      _udp_recv_count,
            "udp_parse_ok":  _udp_parse_ok,
            "udp_parse_fail": _udp_parse_fail,
            "report_size_bytes": REPORT_SIZE,
            "threshold":     store.threshold,
            "timestamp":     time.time(),
        })

    @app.route("/stats")
    def stats():
        return jsonify(store.get_stats())

    @app.route("/stats/raw")
    def stats_raw():
        n = int(request.args.get("n", 100))
        return jsonify(store.get_raw(n))

    @app.route("/congestion")
    def congestion():
        return jsonify({
            "threshold": store.threshold,
            "congested": store.get_congested(),
            "timestamp": time.time(),
        })

    @app.route("/threshold", methods=["POST"])
    def set_threshold():
        body = request.get_json(force=True)
        if body is None:
            return jsonify({"error": "Invalid JSON body"}), 400
        old = store.threshold
        store.threshold = int(body.get("threshold", store.threshold))
        log.info(f"[API] Threshold updated: {old} → {store.threshold}")
        return jsonify({"threshold": store.threshold})

    @app.route("/debug")
    def debug():
        store.debug_summary()
        return jsonify({
            "udp_recv":       _udp_recv_count,
            "udp_parse_ok":   _udp_parse_ok,
            "udp_parse_fail": _udp_parse_fail,
            "report_fmt":     REPORT_FMT,
            "report_size":    REPORT_SIZE,
            "flows":          list(store._records.keys()),
        })

    return app


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="P4 Telemetry Collector")
    parser.add_argument("--udp-host",  default="0.0.0.0", help="UDP bind address")
    parser.add_argument("--udp-port",  type=int, default=9001, help="UDP listen port")
    parser.add_argument("--api-host",  default="0.0.0.0", help="REST API bind address")
    parser.add_argument("--api-port",  type=int, default=5000, help="REST API port")
    parser.add_argument("--window",    type=float, default=2.0, help="Stats window (seconds)")
    parser.add_argument("--threshold", type=int,   default=1000, help="Congestion threshold (cells)")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Logging verbosity")
    args = parser.parse_args()

    logging.getLogger().setLevel(getattr(logging, args.log_level))

    global CONGESTION_THRESHOLD
    CONGESTION_THRESHOLD = args.threshold

    log.info("=" * 60)
    log.info("[STARTUP] P4 Telemetry Collector")
    log.info(f"  UDP listen : {args.udp_host}:{args.udp_port}")
    log.info(f"  REST API   : http://{args.api_host}:{args.api_port}")
    log.info(f"  Window     : {args.window}s")
    log.info(f"  Threshold  : {args.threshold} cells")
    log.info(f"  REPORT_SIZE: {REPORT_SIZE} bytes")
    log.info(f"  REPORT_FMT : {REPORT_FMT}")
    log.info("=" * 60)

    store = StatsStore(window_sec=args.window)
    store.threshold = args.threshold

    # Background UDP listener thread
    t = threading.Thread(
        target=udp_listener,
        args=(store, args.udp_host, args.udp_port),
        daemon=True,
        name="udp-listener",
    )
    t.start()
    log.info(f"[STARTUP] UDP listener thread started (tid={t.ident})")

    # Background debug-summary thread (every 30 s)
    def _periodic_debug():
        while True:
            time.sleep(30)
            store.debug_summary()

    td = threading.Thread(target=_periodic_debug, daemon=True, name="debug-summary")
    td.start()

    # REST API (blocking)
    log.info(f"[STARTUP] Starting Flask REST API...")
    app = make_app(store)
    app.run(host=args.api_host, port=args.api_port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
