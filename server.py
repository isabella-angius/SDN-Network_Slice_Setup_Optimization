#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import print_function

import argparse
import signal
import socket
import time

INTERNAL_PORT = 9999
SERVICE_PORT = 8888


class StatefulCounterServer(object):
    def __init__(self, hostname, sync_ip, peer_sync_ip, service_ip="0.0.0.0", get_state=False):
        self.hostname = hostname
        self.sync_ip = sync_ip
        self.peer_sync_ip = peer_sync_ip
        self.service_ip = service_ip
        self.get_state = get_state
        self.counter = 0
        self.running = True

    def _recv_state(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.sync_ip, INTERNAL_PORT))
        sock.settimeout(10.0)
        try:
            payload, _ = sock.recvfrom(1024)
            return int(payload.decode("utf-8"))
        finally:
            sock.close()

    def _push_state(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            payload = str(self.counter).encode("utf-8")
            for _ in range(8):
                sock.sendto(payload, (self.peer_sync_ip, INTERNAL_PORT))
                time.sleep(0.05)
        finally:
            sock.close()

    def _term_signal_handler(self, signum, frame):
        self._push_state()
        self.running = False

    def run(self):
        if self.get_state:
            self.counter = self._recv_state()
            print("[{}] received initial state {}".format(self.hostname, self.counter))

        signal.signal(signal.SIGTERM, self._term_signal_handler)
        signal.signal(signal.SIGINT, self._term_signal_handler)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.service_ip, SERVICE_PORT))
        sock.settimeout(1.0)

        print("[{}] serving on {}:{}, counter={}".format(
            self.hostname, self.service_ip, SERVICE_PORT, self.counter
        ))

        while self.running:
            try:
                _, addr = sock.recvfrom(1024)
            except socket.timeout:
                continue

            self.counter += 1
            sock.sendto(str(self.counter).encode("utf-8"), addr)
            time.sleep(0.5)

        sock.close()
        print("[{}] stopped with counter={}".format(self.hostname, self.counter))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simple counting server.")
    parser.add_argument("--hostname", required=True)
    parser.add_argument("--sync-ip", required=True)
    parser.add_argument("--peer-sync-ip", required=True)
    parser.add_argument("--service-ip", default="0.0.0.0")
    parser.add_argument("--get-state", action="store_true")
    args = parser.parse_args()

    StatefulCounterServer(
        hostname=args.hostname,
        sync_ip=args.sync_ip,
        peer_sync_ip=args.peer_sync_ip,
        service_ip=args.service_ip,
        get_state=args.get_state,
    ).run()
