#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Helpers for interacting with RYU northbound REST APIs."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


class RyuNorthboundAPI:
    """Very small client for ryu.app.ofctl_rest."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8080, timeout: float = 3.0):
        self.host = host
        self.port = port
        self.timeout = timeout

    def _url(self, path: str) -> str:
        path = path if path.startswith("/") else f"/{path}"
        return f"http://{self.host}:{self.port}{path}"

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Any:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(
            self._url(path),
            data=data,
            headers=headers,
            method=method.upper(),
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = resp.read().decode("utf-8").strip()
            if not body:
                return None
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return body

    def get_switches(self) -> List[int]:
        data = self._request("GET", "/stats/switches")
        return [int(x) for x in (data or [])]

    def wait_for_switches(self, expected: int = 4, timeout: float = 20.0, interval: float = 1.0) -> List[int]:
        deadline = time.time() + timeout
        last: List[int] = []
        while time.time() < deadline:
            try:
                last = self.get_switches()
                if len(last) >= expected:
                    return last
            except Exception:
                pass
            time.sleep(interval)
        raise RuntimeError(
            f"RYU REST did not expose {expected} switches in time. Last seen: {last}"
        )

    def get_port_stats(self, dpid: int) -> List[Dict[str, Any]]:
        data = self._request("GET", f"/stats/port/{dpid}") or {}
        stats = data.get(str(dpid), [])
        return stats if isinstance(stats, list) else []

    def get_port_stat(self, dpid: int, port_no: int) -> Optional[Dict[str, Any]]:
        port_no = int(port_no)
        for entry in self.get_port_stats(dpid):
            try:
                if int(entry.get("port_no")) == port_no:
                    return entry
            except Exception:
                continue
        return None

    def add_flow(
        self,
        dpid: int,
        match: Dict[str, Any],
        actions: List[Dict[str, Any]],
        priority: int = 30000,
        table_id: int = 0,
        idle_timeout: int = 0,
        hard_timeout: int = 0,
    ) -> Any:
        payload = {
            "dpid": int(dpid),
            "table_id": int(table_id),
            "priority": int(priority),
            "idle_timeout": int(idle_timeout),
            "hard_timeout": int(hard_timeout),
            "match": match,
            "actions": actions,
        }
        return self._request("POST", "/stats/flowentry/add", payload)

    def modify_flow(
        self,
        dpid: int,
        match: Dict[str, Any],
        actions: List[Dict[str, Any]],
        priority: int = 30000,
        table_id: int = 0,
    ) -> Any:
        payload = {
            "dpid": int(dpid),
            "table_id": int(table_id),
            "priority": int(priority),
            "match": match,
            "actions": actions,
        }
        return self._request("POST", "/stats/flowentry/modify", payload)

    def delete_flow(
        self,
        dpid: int,
        match: Dict[str, Any],
        priority: int = 30000,
        table_id: int = 0,
    ) -> Any:
        payload = {
            "dpid": int(dpid),
            "table_id": int(table_id),
            "priority": int(priority),
            "match": match,
        }
        return self._request("POST", "/stats/flowentry/delete_strict", payload)


class RyuUnavailable(RuntimeError):
    pass


def safe_port_stat(api: RyuNorthboundAPI, dpid: int, port_no: int) -> Optional[Dict[str, Any]]:
    try:
        return api.get_port_stat(dpid, port_no)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RyuUnavailable(str(exc)) from exc
