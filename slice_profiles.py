#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Static flow profiles used by the northbound scripts."""

from __future__ import annotations

from typing import Dict, List

ETH_TYPE_IPV4 = 0x0800
PRIORITY = 30000
SWITCH_DPID = {"s1": 1, "s2": 2, "s3": 3, "s4": 4}


def output(port: int) -> List[Dict[str, int | str]]:
    return [{"type": "OUTPUT", "port": int(port)}]


def ipv4_match(in_port: int, ipv4_dst: str) -> Dict[str, int | str]:
    return {
        "in_port": int(in_port),
        "eth_type": ETH_TYPE_IPV4,
        "ipv4_dst": ipv4_dst,
    }


def qos_initial_flows() -> List[Dict[str, object]]:
    return [
        # Game/service slice toward main service on h3 via s2.
        {"dpid": SWITCH_DPID["s1"], "match": ipv4_match(1, "10.0.0.12"), "actions": output(4)},
        {"dpid": SWITCH_DPID["s2"], "match": ipv4_match(1, "10.0.0.12"), "actions": output(2)},
        {"dpid": SWITCH_DPID["s2"], "match": ipv4_match(2, "10.0.0.1"), "actions": output(1)},
        {"dpid": SWITCH_DPID["s1"], "match": ipv4_match(4, "10.0.0.1"), "actions": output(1)},
        # Backup return path is installed from the beginning.
        {"dpid": SWITCH_DPID["s3"], "match": ipv4_match(1, "10.0.0.12"), "actions": output(2)},
        {"dpid": SWITCH_DPID["s3"], "match": ipv4_match(2, "10.0.0.1"), "actions": output(1)},
        {"dpid": SWITCH_DPID["s1"], "match": ipv4_match(5, "10.0.0.1"), "actions": output(1)},
        # Download/noise slice on the bottleneck path.
        {"dpid": SWITCH_DPID["s1"], "match": ipv4_match(3, "10.0.0.55"), "actions": output(4)},
        {"dpid": SWITCH_DPID["s2"], "match": ipv4_match(1, "10.0.0.55"), "actions": output(3)},
        {"dpid": SWITCH_DPID["s2"], "match": ipv4_match(3, "10.0.0.3"), "actions": output(1)},
        {"dpid": SWITCH_DPID["s1"], "match": ipv4_match(4, "10.0.0.3"), "actions": output(3)},
        # Video slice.
        {"dpid": SWITCH_DPID["s1"], "match": ipv4_match(2, "10.0.2.100"), "actions": output(6)},
        {"dpid": SWITCH_DPID["s4"], "match": ipv4_match(1, "10.0.2.100"), "actions": output(2)},
        {"dpid": SWITCH_DPID["s4"], "match": ipv4_match(2, "10.0.2.1"), "actions": output(1)},
        {"dpid": SWITCH_DPID["s1"], "match": ipv4_match(6, "10.0.2.1"), "actions": output(2)},
    ]


def qos_failover_flows() -> List[Dict[str, object]]:
    return [
        {"dpid": SWITCH_DPID["s1"], "match": ipv4_match(1, "10.0.0.12"), "actions": output(5)},
    ]


def fault_initial_flows() -> List[Dict[str, object]]:
    return [
        # Video slice toward main service on h1 through s4.
        {"dpid": SWITCH_DPID["s1"], "match": ipv4_match(1, "10.0.0.12"), "actions": output(6)},
        {"dpid": SWITCH_DPID["s4"], "match": ipv4_match(1, "10.0.0.12"), "actions": output(2)},
        {"dpid": SWITCH_DPID["s4"], "match": ipv4_match(2, "10.0.0.1"), "actions": output(1)},
        {"dpid": SWITCH_DPID["s1"], "match": ipv4_match(6, "10.0.0.1"), "actions": output(1)},
        # Backup path pre-installed on s3 for fast cutover.
        {"dpid": SWITCH_DPID["s3"], "match": ipv4_match(1, "10.0.0.12"), "actions": output(2)},
        {"dpid": SWITCH_DPID["s3"], "match": ipv4_match(2, "10.0.0.1"), "actions": output(1)},
        {"dpid": SWITCH_DPID["s1"], "match": ipv4_match(5, "10.0.0.1"), "actions": output(1)},
        # Game slice isolated from the video fault.
        {"dpid": SWITCH_DPID["s1"], "match": ipv4_match(2, "10.0.1.100"), "actions": output(4)},
        {"dpid": SWITCH_DPID["s2"], "match": ipv4_match(1, "10.0.1.100"), "actions": output(2)},
        {"dpid": SWITCH_DPID["s2"], "match": ipv4_match(2, "10.0.1.1"), "actions": output(1)},
        {"dpid": SWITCH_DPID["s1"], "match": ipv4_match(4, "10.0.1.1"), "actions": output(2)},
    ]


def fault_failover_flows() -> List[Dict[str, object]]:
    return [
        {"dpid": SWITCH_DPID["s1"], "match": ipv4_match(1, "10.0.0.12"), "actions": output(5)},
    ]
