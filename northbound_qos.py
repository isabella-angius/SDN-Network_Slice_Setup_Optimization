#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Northbound logic for QoS-aware migration."""

from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, Optional, Tuple

from northbound_api import RyuNorthboundAPI


@dataclass
class QosPolicy:
    latency_threshold_ms: float = 60.0
    warning_latency_ms: float = 40.0
    timeout_limit: int = 2
    window: int = 5
    hold_down_sec: float = 4.0
    poll_interval_sec: float = 1.0
    min_bottleneck_mbps: float = 1.0


class QosNorthbound:
    def __init__(
        self,
        api: RyuNorthboundAPI,
        global_state: Dict[str, object],
        log_path: str = "/tmp/client.log",
        switch_dpid: int = 1,
        bottleneck_port: int = 4,
        policy: Optional[QosPolicy] = None,
    ):
        self.api = api
        self.global_state = global_state
        self.log_path = log_path
        self.switch_dpid = switch_dpid
        self.bottleneck_port = bottleneck_port
        self.policy = policy or QosPolicy()

    def _read_client_sample(self) -> Tuple[Optional[float], bool]:
        try:
            with open(self.log_path, "r", encoding="utf-8") as handle:
                text = handle.read().strip()
        except OSError:
            return None, False

        if not text:
            return None, False
        if "TIMEOUT" in text:
            return None, True
        if "LAT:" not in text:
            return None, False
        try:
            lat_txt = text.split("LAT:", 1)[1].replace("ms", "").strip()
            return float(lat_txt), False
        except (IndexError, ValueError):
            return None, False

    def _read_bottleneck_load_mbps(self, previous: Optional[Tuple[float, int]]) -> Tuple[float, Tuple[float, int] | None]:
        now = time.time()
        stat = self.api.get_port_stat(self.switch_dpid, self.bottleneck_port)
        if not stat:
            return 0.0, previous
        try:
            tx_bytes = int(stat.get("tx_bytes", 0))
        except (TypeError, ValueError):
            return 0.0, previous
        if previous is None:
            return 0.0, (now, tx_bytes)
        prev_ts, prev_bytes = previous
        delta_t = max(now - prev_ts, 0.001)
        delta_b = max(tx_bytes - prev_bytes, 0)
        mbps = (delta_b * 8.0) / delta_t / 1_000_000.0
        return mbps, (now, tx_bytes)

    def wait_for_violation(self, deadline_sec: float = 90.0) -> bool:
        latencies: Deque[float] = deque(maxlen=self.policy.window)
        consecutive_timeouts = 0
        violation_since: Optional[float] = None
        previous_stat: Optional[Tuple[float, int]] = None
        deadline = time.time() + deadline_sec

        while time.time() < deadline and not self.global_state.get("stop"):
            lat_ms, timed_out = self._read_client_sample()
            load_mbps, previous_stat = self._read_bottleneck_load_mbps(previous_stat)
            self.global_state["link_load"] = load_mbps

            if timed_out:
                consecutive_timeouts += 1
                self.global_state["lat"] = "TIMEOUT"
            else:
                if lat_ms is not None:
                    latencies.append(lat_ms)
                    self.global_state["lat"] = f"{lat_ms:.1f}ms"
                consecutive_timeouts = 0

            avg_lat = sum(latencies) / len(latencies) if latencies else 0.0
            warning = avg_lat >= self.policy.warning_latency_ms or load_mbps >= 0.8 * self.policy.min_bottleneck_mbps
            violation = (
                consecutive_timeouts >= self.policy.timeout_limit
                or (len(latencies) == self.policy.window and avg_lat >= self.policy.latency_threshold_ms)
            ) and load_mbps >= self.policy.min_bottleneck_mbps

            if violation:
                self.global_state["status"] = "CONGESTION!"
                if violation_since is None:
                    violation_since = time.time()
                elif time.time() - violation_since >= self.policy.hold_down_sec:
                    return True
            else:
                violation_since = None
                self.global_state["status"] = "WARNING" if warning else "OK"

            time.sleep(self.policy.poll_interval_sec)

        return False


def migrate_on_qos_violation(
    monitor: QosNorthbound,
    migration_callback: Callable[[], None],
    deadline_sec: float = 90.0,
) -> bool:
    should_migrate = monitor.wait_for_violation(deadline_sec=deadline_sec)
    if should_migrate:
        monitor.global_state["status"] = "MIGRATING..."
        migration_callback()
        monitor.global_state["status"] = "OK"
    return should_migrate
