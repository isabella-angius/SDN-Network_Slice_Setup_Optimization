#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Northbound logic for service-loss driven failover."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

from northbound_api import RyuNorthboundAPI


@dataclass
class FaultPolicy:
    timeout_limit: int = 2
    hold_down_sec: float = 2.0
    poll_interval_sec: float = 1.0


class FaultNorthbound:
    def __init__(
        self,
        api: RyuNorthboundAPI,
        global_state: Dict[str, object],
        video_log_path: str = "/tmp/video_cnt.log",
        switch_dpid: int = 1,
        main_port: int = 6,
        policy: Optional[FaultPolicy] = None,
    ):
        self.api = api
        self.global_state = global_state
        self.video_log_path = video_log_path
        self.switch_dpid = switch_dpid
        self.main_port = main_port
        self.policy = policy or FaultPolicy()

    def _service_timed_out(self) -> bool:
        try:
            with open(self.video_log_path, "r", encoding="utf-8") as handle:
                text = handle.read().strip()
        except OSError:
            return False
        return text == "TIMEOUT"

    def _read_main_port_tx(self, previous: Optional[Tuple[float, int]]) -> Tuple[float, Tuple[float, int] | None]:
        now = time.time()
        stat = self.api.get_port_stat(self.switch_dpid, self.main_port)
        if not stat:
            return 0.0, previous
        try:
            tx_bytes = int(stat.get("tx_bytes", 0))
        except (TypeError, ValueError):
            return 0.0, previous
        if previous is None:
            return 0.0, (now, tx_bytes)
        prev_ts, prev_bytes = previous
        dt = max(now - prev_ts, 0.001)
        db = max(tx_bytes - prev_bytes, 0)
        return (db * 8.0) / dt / 1_000_000.0, (now, tx_bytes)

    def wait_for_service_loss(self, deadline_sec: float = 60.0) -> bool:
        deadline = time.time() + deadline_sec
        consecutive_timeouts = 0
        loss_since: Optional[float] = None
        previous_stat: Optional[Tuple[float, int]] = None

        while time.time() < deadline and not self.global_state.get("stop"):
            if self._service_timed_out():
                consecutive_timeouts += 1
            else:
                consecutive_timeouts = 0
                loss_since = None

            port_mbps, previous_stat = self._read_main_port_tx(previous_stat)
            self.global_state["main_path_load"] = port_mbps

            if consecutive_timeouts > 0:
                self.global_state["status"] = "SERVICE LOSS"
                if loss_since is None:
                    loss_since = time.time()
                elif (
                    consecutive_timeouts >= self.policy.timeout_limit
                    and time.time() - loss_since >= self.policy.hold_down_sec
                ):
                    return True
            else:
                if self.global_state.get("status") != "LINK DOWN":
                    self.global_state["status"] = "OK"

            time.sleep(self.policy.poll_interval_sec)

        return False


def failover_on_service_loss(
    monitor: FaultNorthbound,
    failover_callback: Callable[[], None],
    deadline_sec: float = 60.0,
) -> bool:
    should_failover = monitor.wait_for_service_loss(deadline_sec=deadline_sec)
    if should_failover:
        monitor.global_state["status"] = "MIGRATING..."
        failover_callback()
        monitor.global_state["status"] = "OK"
    return should_failover
