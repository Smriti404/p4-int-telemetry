/*
 * p4/int_telemetry.p4
 * ====================
 * Main P4 pipeline: congestion-aware routing with INT-style telemetry.
 *
 * Pipeline:
 *   Ingress:
 *     1. Parse Ethernet / IP / TCP|UDP
 *     2. Compute 5-tuple CRC32 flow hash
 *     3. flow_reroute table (controller-managed, exact-match 5-tuple)
 *        → overrides LPM when a reroute entry exists
 *     4. ipv4_lpm table (normal destination-based forwarding)
 *     5. telemetry_trigger table (decides whether to clone)
 *
 *   Egress:
 *     - Original packet: normal forwarding
 *     - Cloned packet (instance_type == 1):
 *         build_telemetry_report() fills report_* headers,
 *         invalidates original headers → pure telemetry packet
 *
 * Telemetry packet format (after deparser):
 *   Ethernet(14) + IP(20) + UDP(8) + telemetry_report_t(54) = 96 bytes
 */

#include <core.p4>
#include <v1model.p4>

#include "headers.p4"

// ═══════════════════════════════════════════════════════════════
// PARSER
// ═══════════════════════════════════════════════════════════════

parser MyParser(
    packet_in             pkt,
    out headers_t         hdr,
    inout metadata_t      meta,
    inout standard_metadata_t std_meta)
{
    state start {
        transition parse_ethernet;
    }

    state parse_ethernet {
        pkt.extract(hdr.ethernet);
        transition select(hdr.ethernet.etherType) {
            ETHERTYPE_IPV4: parse_ipv4;
            default:        accept;
        }
    }

    state parse_ipv4 {
        pkt.extract(hdr.ipv4);
        transition select(hdr.ipv4.protocol) {
            PROTO_TCP: parse_tcp;
            PROTO_UDP: parse_udp;
            default:   accept;
        }
    }

    state parse_tcp {
        pkt.extract(hdr.tcp);
        meta.src_port = hdr.tcp.srcPort;
        meta.dst_port = hdr.tcp.dstPort;
        meta.ip_proto = PROTO_TCP;
        transition accept;
    }

    state parse_udp {
        pkt.extract(hdr.udp);
        meta.src_port = hdr.udp.srcPort;
        meta.dst_port = hdr.udp.dstPort;
        meta.ip_proto = PROTO_UDP;
        transition accept;
    }
}

// ═══════════════════════════════════════════════════════════════
// CHECKSUM VERIFICATION
// ═══════════════════════════════════════════════════════════════

control MyVerifyChecksum(inout headers_t hdr, inout metadata_t meta) {
    apply {
        verify_checksum(
            hdr.ipv4.isValid(),
            {
                hdr.ipv4.version,
                hdr.ipv4.ihl,
                hdr.ipv4.diffserv,
                hdr.ipv4.totalLen,
                hdr.ipv4.identification,
                hdr.ipv4.flags,
                hdr.ipv4.fragOffset,
                hdr.ipv4.ttl,
                hdr.ipv4.protocol,
                hdr.ipv4.srcAddr,
                hdr.ipv4.dstAddr
            },
            hdr.ipv4.hdrChecksum,
            HashAlgorithm.csum16
        );
    }
}

// ═══════════════════════════════════════════════════════════════
// INGRESS
// ═══════════════════════════════════════════════════════════════

control MyIngress(
    inout headers_t          hdr,
    inout metadata_t         meta,
    inout standard_metadata_t std_meta)
{
    // Per-port and per-flow packet/byte counters
    counter(512, CounterType.packets_and_bytes) ingress_port_counter;
    counter(512, CounterType.packets_and_bytes) flow_counter;

    // ── Actions ────────────────────────────────────────────────

    action drop() {
        mark_to_drop(std_meta);
    }

    // Normal LPM forwarding: set output port and rewrite dst MAC
    action forward(bit<9> port, bit<48> dst_mac) {
        std_meta.egress_spec = port;
        hdr.ethernet.dstAddr = dst_mac;
        hdr.ipv4.ttl         = hdr.ipv4.ttl - 1;
        meta.egress_port     = port;
    }

    // Controller-driven reroute: same as forward, sets is_rerouted flag
    action set_nhop(bit<9> port, bit<48> dst_mac) {
        std_meta.egress_spec = port;
        hdr.ethernet.dstAddr = dst_mac;
        hdr.ipv4.ttl         = hdr.ipv4.ttl - 1;
        meta.egress_port     = port;
        meta.is_rerouted     = 1;
    }

    // Mark packet for telemetry cloning (I2E clone)
    action trigger_telemetry() {
        meta.send_telemetry = 1;
        // Field list 0 — copies metadata_t fields into the clone
        // so egress can build the telemetry report
        clone_preserving_field_list(CloneType.I2E, (bit<32>)TELEMETRY_CLONE_SID, 0);
    }

    // ── Tables ────────────────────────────────────────────────

    // Exact-match 5-tuple reroute table.
    // Written by runtime.py when congestion is detected.
    // Checked BEFORE ipv4_lpm so it can override the default path.
    table flow_reroute {
        key = {
            hdr.ipv4.srcAddr  : exact;
            hdr.ipv4.dstAddr  : exact;
            hdr.ipv4.protocol : exact;
            meta.src_port     : exact;
            meta.dst_port     : exact;
        }
        actions     = { set_nhop; NoAction; }
        size        = 1024;
        default_action = NoAction();
    }

    // Destination LPM forwarding (normal path)
    table ipv4_lpm {
        key     = { hdr.ipv4.dstAddr : lpm; }
        actions  = { forward; drop; NoAction; }
        size    = 1024;
        default_action = drop();
    }

    // Per-ingress-port telemetry control.
    // Controller sets this to NoAction on most ports to avoid
    // re-cloning already-cloned telemetry packets.
    // Default: trigger_telemetry() (demo mode — clone everything)
    table telemetry_trigger {
        key     = { std_meta.ingress_port : exact; }
        actions  = { trigger_telemetry; NoAction; }
        size    = 64;
        default_action = trigger_telemetry();  // sample all in demo mode
    }

    // ── Apply ─────────────────────────────────────────────────

    apply {
        ingress_port_counter.count((bit<32>) std_meta.ingress_port);

        if (hdr.ipv4.isValid()) {
            // 1. Compute 5-tuple flow hash for telemetry correlation
            hash(
                meta.flow_hash,
                HashAlgorithm.crc32,
                32w0,
                {
                    hdr.ipv4.srcAddr,
                    hdr.ipv4.dstAddr,
                    hdr.ipv4.protocol,
                    meta.src_port,
                    meta.dst_port
                },
                32w0xFFFFFFFF
            );

            meta.ingress_port = std_meta.ingress_port;

            // 2. Try controller-managed reroute first
            if (!flow_reroute.apply().hit) {
                // 3. Fall through to standard LPM
                ipv4_lpm.apply();
            }

            // 4. Per-flow packet count
            flow_counter.count(meta.flow_hash & 0x1FF);

            // 5. Telemetry sampling decision
            telemetry_trigger.apply();
        }
    }
}

// ═══════════════════════════════════════════════════════════════
// EGRESS
// ═══════════════════════════════════════════════════════════════

control MyEgress(
    inout headers_t          hdr,
    inout metadata_t         meta,
    inout standard_metadata_t std_meta)
{
    // Register holding this switch's ID (index 0).
    // Written by the controller at startup via Thrift:
    //   register_write switch_id_reg 0 <id>
    // v1model does not expose device_id in standard_metadata, so we
    // store it in a register instead.
    register<bit<32>>(1) switch_id_reg;

    // Build a full telemetry UDP report packet on the cloned instance.
    // The clone arrives here with:
    //   std_meta.instance_type == PKT_INSTANCE_TYPE_INGRESS_CLONE (1)
    //   std_meta.enq_qdepth, deq_qdepth, deq_timedelta populated
    //   meta.* copied from ingress via field_list(0)
    action build_telemetry_report() {
        // Read switch ID from register (written by controller at startup)
        bit<32> sw_id;
        switch_id_reg.read(sw_id, 0);

        // ── Report Ethernet ─────────────────────────────────────
        hdr.report_ethernet.setValid();
        hdr.report_ethernet.dstAddr   = 0xFFFFFFFFFFFF;  // broadcast — collector sees it
        hdr.report_ethernet.srcAddr   = hdr.ethernet.srcAddr;
        hdr.report_ethernet.etherType = ETHERTYPE_IPV4;

        // ── Report IP ───────────────────────────────────────────
        hdr.report_ipv4.setValid();
        hdr.report_ipv4.version        = 4;
        hdr.report_ipv4.ihl            = 5;
        hdr.report_ipv4.diffserv       = 0;
        hdr.report_ipv4.totalLen       = REPORT_IP_TOTAL_LEN;  // 82
        hdr.report_ipv4.identification = 0;
        hdr.report_ipv4.flags          = 0;
        hdr.report_ipv4.fragOffset     = 0;
        hdr.report_ipv4.ttl            = 64;
        hdr.report_ipv4.protocol       = PROTO_UDP;
        hdr.report_ipv4.hdrChecksum    = 0;  // recomputed in MyComputeChecksum
        hdr.report_ipv4.srcAddr        = hdr.ipv4.srcAddr;  // preserve original src
        hdr.report_ipv4.dstAddr        = COLLECTOR_IP;

        // ── Report UDP ──────────────────────────────────────────
        hdr.report_udp.setValid();
        hdr.report_udp.srcPort  = 0xDEAD;
        hdr.report_udp.dstPort  = COLLECTOR_UDP_PORT;  // 9001
        hdr.report_udp.len      = REPORT_UDP_LEN;      // 62
        hdr.report_udp.checksum = 0;

        // ── Telemetry payload ───────────────────────────────────
        hdr.report.setValid();
        hdr.report.switch_id         = sw_id;  // from register, set by controller
        hdr.report.seq_num           = 0;      // could increment via a second register
        hdr.report.ingress_timestamp = (bit<48>) std_meta.ingress_global_timestamp;
        hdr.report.egress_timestamp  = (bit<48>) std_meta.egress_global_timestamp;
        hdr.report.enq_qdepth        = (bit<32>) std_meta.enq_qdepth;
        hdr.report.deq_qdepth        = (bit<32>) std_meta.deq_qdepth;
        hdr.report.deq_timedelta     = std_meta.deq_timedelta;
        hdr.report.flow_hash         = meta.flow_hash;
        hdr.report.ingress_port      = (bit<16>) meta.ingress_port;
        hdr.report.egress_port       = (bit<16>) meta.egress_port;
        hdr.report.pad               = 0;
        hdr.report.src_ip            = hdr.ipv4.srcAddr;
        hdr.report.dst_ip            = hdr.ipv4.dstAddr;
        hdr.report.protocol          = hdr.ipv4.protocol;
        hdr.report.src_port          = meta.src_port;
        hdr.report.dst_port          = meta.dst_port;

        // Invalidate original headers so only report_* headers are emitted
        hdr.ethernet.setInvalid();
        hdr.ipv4.setInvalid();
        hdr.tcp.setInvalid();
        hdr.udp.setInvalid();
    }

    // Egress telemetry dispatch table.
    // Only fires on cloned packets (instance_type == 1).
    // const entries avoids a P4Runtime write for this table.
    table egress_telemetry {
        key     = { std_meta.instance_type : exact; }
        actions  = { build_telemetry_report; NoAction; }
        const entries = {
            // PKT_INSTANCE_TYPE_INGRESS_CLONE = 1
            1 : build_telemetry_report();
        }
        default_action = NoAction();
    }

    apply {
        // Only attempt to build telemetry on packets marked in ingress
        egress_telemetry.apply();
        //if (meta.send_telemetry == 1) {
          //  egress_telemetry.apply();
        //}
    }
}

// ═══════════════════════════════════════════════════════════════
// CHECKSUM UPDATE
// ═══════════════════════════════════════════════════════════════

control MyComputeChecksum(inout headers_t hdr, inout metadata_t meta) {
    apply {
        // Recompute original IP checksum (TTL was decremented in ingress)
        update_checksum(
            hdr.ipv4.isValid(),
            {
                hdr.ipv4.version,
                hdr.ipv4.ihl,
                hdr.ipv4.diffserv,
                hdr.ipv4.totalLen,
                hdr.ipv4.identification,
                hdr.ipv4.flags,
                hdr.ipv4.fragOffset,
                hdr.ipv4.ttl,
                hdr.ipv4.protocol,
                hdr.ipv4.srcAddr,
                hdr.ipv4.dstAddr
            },
            hdr.ipv4.hdrChecksum,
            HashAlgorithm.csum16
        );

        // Compute telemetry report IP checksum
        update_checksum(
            hdr.report_ipv4.isValid(),
            {
                hdr.report_ipv4.version,
                hdr.report_ipv4.ihl,
                hdr.report_ipv4.diffserv,
                hdr.report_ipv4.totalLen,
                hdr.report_ipv4.identification,
                hdr.report_ipv4.flags,
                hdr.report_ipv4.fragOffset,
                hdr.report_ipv4.ttl,
                hdr.report_ipv4.protocol,
                hdr.report_ipv4.srcAddr,
                hdr.report_ipv4.dstAddr
            },
            hdr.report_ipv4.hdrChecksum,
            HashAlgorithm.csum16
        );
    }
}

// ═══════════════════════════════════════════════════════════════
// DEPARSER
// ═══════════════════════════════════════════════════════════════

control MyDeparser(packet_out pkt, in headers_t hdr) {
    apply {
        // Telemetry clone path: only report_* headers are valid
        pkt.emit(hdr.report_ethernet);
        pkt.emit(hdr.report_ipv4);
        pkt.emit(hdr.report_udp);
        pkt.emit(hdr.report);

        // Original packet path: normal headers
        pkt.emit(hdr.ethernet);
        pkt.emit(hdr.ipv4);
        pkt.emit(hdr.tcp);
        pkt.emit(hdr.udp);
    }
}

// ═══════════════════════════════════════════════════════════════
// SWITCH INSTANTIATION
// ═══════════════════════════════════════════════════════════════

V1Switch(
    MyParser(),
    MyVerifyChecksum(),
    MyIngress(),
    MyEgress(),
    MyComputeChecksum(),
    MyDeparser()
) main;
