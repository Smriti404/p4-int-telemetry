#ifndef __HEADERS_P4__
#define __HEADERS_P4__

// ═══════════════════════════════════════════════════════════════
// Standard headers
// ═══════════════════════════════════════════════════════════════

header ethernet_t {
    bit<48> dstAddr;
    bit<48> srcAddr;
    bit<16> etherType;
}

header ipv4_t {
    bit<4>  version;
    bit<4>  ihl;
    bit<8>  diffserv;
    bit<16> totalLen;
    bit<16> identification;
    bit<3>  flags;
    bit<13> fragOffset;
    bit<8>  ttl;
    bit<8>  protocol;
    bit<16> hdrChecksum;
    bit<32> srcAddr;
    bit<32> dstAddr;
}

header tcp_t {
    bit<16> srcPort;
    bit<16> dstPort;
    bit<32> seqNo;
    bit<32> ackNo;
    bit<4>  dataOffset;
    bit<4>  res;
    bit<8>  flags;
    bit<16> window;
    bit<16> checksum;
    bit<16> urgentPtr;
}

header udp_t {
    bit<16> srcPort;
    bit<16> dstPort;
    bit<16> len;
    bit<16> checksum;
}

// ═══════════════════════════════════════════════════════════════
// INT shim + data headers (for in-band INT stamping, optional)
// ═══════════════════════════════════════════════════════════════

header int_shim_t {
    bit<4>  int_type;
    bit<2>  rsvd1;
    bit<2>  npt;
    bit<8>  len;
    bit<6>  rsvd2;
    bit<1>  e;
    bit<1>  c;
    bit<16> rsvd3;
}

// All sub-word fields padded to byte boundaries for BMv2 compatibility
header int_data_t {
    bit<8>  ver_rep;
    bit<8>  flags;
    bit<8>  hop_ml;
    bit<8>  remaining_hop_cnt;
    bit<16> instruction_mask;
    bit<16> rsvd2;
    bit<32> switch_id;
    bit<32> ingress_port;
    bit<32> egress_port;
    bit<48> ingress_timestamp;
    bit<48> egress_timestamp;
    bit<32> enq_qdepth;
    bit<32> deq_qdepth;
    bit<32> enq_congest_stat;
    bit<32> deq_timedelta;
    bit<32> l3_mtu;
}

// ═══════════════════════════════════════════════════════════════
// Telemetry report header — must match REPORT_FMT in collector.py
//
// Python struct format: ">IIQQIIIIHHIIBHHx"
// Field layout (54 bytes total):
//   switch_id          4   I
//   seq_num            4   I
//   ingress_timestamp  8   Q  (48-bit value, upper 16 bits = 0)
//   egress_timestamp   8   Q  (48-bit value, upper 16 bits = 0)
//   enq_qdepth         4   I
//   deq_qdepth         4   I
//   deq_timedelta      4   I
//   flow_hash          4   I
//   ingress_port       2   H
//   egress_port        2   H
//   src_ip             4   I
//   dst_ip             4   I
//   protocol           1   B
//   src_port           2   H
//   dst_port           2   H
//   pad                1   x
//                     --
//                     54 bytes
//
// P4 total bits: 32+32+48+48+32+32+32+32+16+16+32+32+8+16+16+8 = 432 bits = 54 bytes ✓
// ═══════════════════════════════════════════════════════════════

header telemetry_report_t {
    bit<32> switch_id;
    bit<32> seq_num;
    bit<48> ingress_timestamp;
    bit<48> egress_timestamp;
    bit<32> enq_qdepth;
    bit<32> deq_qdepth;
    bit<32> deq_timedelta;
    bit<32> flow_hash;
    bit<16> ingress_port;
    bit<16> egress_port;
    bit<32> src_ip;
    bit<32> dst_ip;
    bit<8>  protocol;
    bit<16> src_port;
    bit<16> dst_port;
    bit<8>  pad;              // align to byte boundary
}

// ═══════════════════════════════════════════════════════════════
// Metadata
// ═══════════════════════════════════════════════════════════════

struct metadata_t {
    @field_list(0) bit<32> flow_hash;
    @field_list(0) bit<9>  ingress_port;
    @field_list(0) bit<9>  egress_port;
    @field_list(0) bit<1>  send_telemetry;
    @field_list(0) bit<1>  is_rerouted;
                   bit<32> nhop_ipv4;
                   bit<9>  nhop_port;
    @field_list(0) bit<16> src_port;
    @field_list(0) bit<16> dst_port;
    @field_list(0) bit<8>  ip_proto;
}

// ═══════════════════════════════════════════════════════════════
// Header bundle
// ═══════════════════════════════════════════════════════════════

struct headers_t {
    // Original packet headers
    ethernet_t ethernet;
    ipv4_t     ipv4;
    tcp_t      tcp;
    udp_t      udp;

    // Optional in-band INT headers (not emitted in this version)
    int_shim_t int_shim;
    int_data_t int_data;

    // Telemetry clone packet headers
    ethernet_t          report_ethernet;
    ipv4_t              report_ipv4;
    udp_t               report_udp;
    telemetry_report_t  report;
}

// ═══════════════════════════════════════════════════════════════
// Constants
// ═══════════════════════════════════════════════════════════════

const bit<16> ETHERTYPE_IPV4     = 0x0800;
const bit<8>  PROTO_TCP          = 0x06;
const bit<8>  PROTO_UDP          = 0x11;

// Collector: h_ctrl at 10.0.1.254, listening on UDP 9001
const bit<32> COLLECTOR_IP       = 0x0A0001FE;   // 10.0.1.254
const bit<16> COLLECTOR_UDP_PORT = 9001;

// BMv2 clone session ID (set via: mirroring_add 100 <port>)
const bit<32> TELEMETRY_CLONE_SID = 100;

// Telemetry report outer IP/UDP lengths
// IP total len  = IP header (20) + UDP header (8) + report payload (54) = 82
// UDP len       = UDP header (8) + report payload (54) = 62
const bit<16> REPORT_IP_TOTAL_LEN = 82;
const bit<16> REPORT_UDP_LEN      = 62;

#endif  // __HEADERS_P4__
