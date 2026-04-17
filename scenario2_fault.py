#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import threading
import time
import warnings
from pathlib import Path

warnings.filterwarnings(action="ignore", module=".*paramiko.*")
warnings.filterwarnings("ignore", category=DeprecationWarning)

from comnetsemu.cli import CLI
from comnetsemu.net import Containernet
from mininet.link import TCLink
from mininet.log import info, setLogLevel
from mininet.node import RemoteController

from northbound_api import RyuNorthboundAPI
from northbound_fault import FaultNorthbound, failover_on_service_loss
from slice_profiles import PRIORITY, fault_failover_flows, fault_initial_flows

HOST_IMAGE = "dev_test_py3"
SERVICE_REMOTE = "/tmp/service_server_runtime.py"
VIDEO_CLIENT_REMOTE = "/tmp/video_client.py"
GAME_CLIENT_REMOTE = "/tmp/game_client.py"
GAME_SERVER_REMOTE = "/tmp/udp_backend.py"

MAIN_HOST = "h1"
BACKUP_HOST = "h2"
VIDEO_BW_PORT = 9997
TRIGGER_FILE = "/tmp/scenario2_trigger.txt"


class Col:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    MAGENTA = "\033[95m"
    WHITE = "\033[97m"


GLOBAL = {
    "stop": False,
    "v_cnt": "WAIT",
    "g_lat": "WAIT",
    "main_path_load": 0.0,
    "srv": "H1_MAIN",
    "status": "OK",
}


VIDEO_CLIENT = r"""#!/usr/bin/env python3
import os
import socket
import time

LOG_PATH = "/tmp/video_cnt.log"
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(1.0)

while True:
    line = "TIMEOUT"
    try:
        sock.sendto(b"req", ("10.0.0.12", 8888))
        data, _ = sock.recvfrom(1024)
        line = data.decode("utf-8")
    except Exception:
        line = "TIMEOUT"

    tmp_path = LOG_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        handle.write(line + "\n")
    os.replace(tmp_path, LOG_PATH)
    print(line, flush=True)
    time.sleep(1.0)
"""

GAME_CLIENT = r"""#!/usr/bin/env python3
import os
import socket
import time

LOG_PATH = "/tmp/game_lat.log"
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(1.0)

while True:
    start = time.perf_counter()
    line = "TIMEOUT"
    try:
        sock.sendto(b"ping", ("10.0.1.100", 8889))
        sock.recvfrom(1024)
        lat = (time.perf_counter() - start) * 1000.0
        line = f"{lat:.1f}ms"
    except Exception:
        line = "TIMEOUT"

    tmp_path = LOG_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        handle.write(line + "\n")
    os.replace(tmp_path, LOG_PATH)
    print(line, flush=True)
    time.sleep(1.0)
"""

GAME_SERVER = r"""#!/usr/bin/env python3
import socket

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("0.0.0.0", 8889))

while True:
    data, addr = sock.recvfrom(1024)
    sock.sendto(b"pong", addr)
"""


def dbg(message: str) -> None:
    print(f"[DBG] {message}", flush=True)


def install_flows(api: RyuNorthboundAPI, flows):
    for flow in flows:
        api.add_flow(flow["dpid"], flow["match"], flow["actions"], priority=PRIORITY)


def modify_flows(api: RyuNorthboundAPI, flows):
    for flow in flows:
        api.modify_flow(flow["dpid"], flow["match"], flow["actions"], priority=PRIORITY)


def push_text_to_node(node, remote_path: str, text: str) -> None:
    marker = "__PY_EOF__"
    cmd = "cat > {} <<'{}'\n{}\n{}\nchmod +x {}".format(remote_path, marker, text, marker, remote_path)
    node.cmd(cmd)


def detect_python(node) -> str:
    py = node.cmd("command -v python3 || command -v python || true").strip()
    return py or "python3"


def start_counter_service(node, hostname: str, sync_ip: str, peer_sync_ip: str, get_state: bool = False):
    py = detect_python(node)
    flag = " --get-state" if get_state else ""
    node.cmd("pkill -f '{} --hostname {}' >/dev/null 2>&1 || true".format(SERVICE_REMOTE, hostname))
    node.cmd(
        "{} {} --hostname {} --service-ip 0.0.0.0 --sync-ip {} --peer-sync-ip {}{} > /tmp/{}_service.log 2>&1 &".format(
            py, SERVICE_REMOTE, hostname, sync_ip, peer_sync_ip, flag, hostname
        )
    )
    time.sleep(1.0)
#     dbg("service exe on {} = {}".format(hostname, py))
#     dbg("service proc on {} = {}".format(
#         hostname,
#         node.cmd("ps -ef | grep '{} --hostname {}' | grep -v grep || true".format(SERVICE_REMOTE, hostname)).strip(),
#     ))


def stop_counter_service(node, hostname: str):
    node.cmd("pkill -SIGTERM -f '{} --hostname {}' >/dev/null 2>&1 || true".format(SERVICE_REMOTE, hostname))


def start_simple_script(node, remote_path: str, script_text: str, stdout_path: str, stderr_path: str):
    push_text_to_node(node, remote_path, script_text)
    py = detect_python(node)
    node.cmd("pkill -f '{}' >/dev/null 2>&1 || true".format(remote_path))
    node.cmd("{} {} > {} 2> {} &".format(py, remote_path, stdout_path, stderr_path))
    time.sleep(1.0)
#     dbg("script exe on {} = {}".format(getattr(node, "name", "node"), py))
#     dbg("script proc on {} = {}".format(
#         getattr(node, "name", "node"),
#         node.cmd("ps -ef | grep '{}' | grep -v grep || true".format(remote_path)).strip(),
#     ))


def start_video_bw_stream(v1, h1, h2):
    h1.cmd("pkill -f 'iperf -s -u -p {}' >/dev/null 2>&1 || true".format(VIDEO_BW_PORT))
    h2.cmd("pkill -f 'iperf -s -u -p {}' >/dev/null 2>&1 || true".format(VIDEO_BW_PORT))
    v1.cmd("pkill -f 'iperf -c 10.0.0.12 -u -p {}' >/dev/null 2>&1 || true".format(VIDEO_BW_PORT))
    h1.cmd("iperf -s -u -p {} > /tmp/iperf_h1.log 2>&1 &".format(VIDEO_BW_PORT))
    h2.cmd("iperf -s -u -p {} > /tmp/iperf_h2.log 2>&1 &".format(VIDEO_BW_PORT))
    v1.cmd("iperf -c 10.0.0.12 -u -p {} -b 1M -t 300 > /tmp/iperf_v1.log 2>&1 &".format(VIDEO_BW_PORT))
    dbg("video stream traffic started")


def wait_for_service_listener(node, hostname: str, timeout_sec: float = 8.0) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        udp = node.cmd("ss -lun | grep ':8888' || true").strip()
        proc = node.cmd("ps -ef | grep '{} --hostname {}' | grep -v grep || true".format(SERVICE_REMOTE, hostname)).strip()
        if udp or proc:
            return True
        time.sleep(0.5)
    return False


def wait_for_log_value(path: str, timeout_sec: float = 12.0) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            text = Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        if text and text not in {"WAIT", "---", "TIMEOUT"}:
            #dbg(f"first sample from {path}: {text}")
            return True
        time.sleep(0.5)
    return False


def dashboard_thread():
    last_main_bytes = 0
    last_backup_bytes = 0
    print("\n\n")
    print(f"{Col.BOLD}{'--- SDN HIGH AVAILABILITY DASHBOARD (LINK FAILURE) ---':^106}{Col.RESET}")
    print("-" * 107)
    print(
        f"{Col.WHITE}| {Col.MAGENTA}{'VIDEO SLICE (10.0.0.x)':<70}"
        f"{Col.WHITE}|{Col.CYAN} {'GAME SLICE (10.0.1.x)':<2}           {Col.WHITE}|{Col.RESET}"
    )
    print(
        f"{Col.WHITE}| {Col.MAGENTA}{'V_CLIENT':<11} {'VIP':<11} {'SRV_NAME':<10} {'STATUS':<15} {'CNT':<7} {'VIDEO BW':<10} "
        f"{Col.WHITE}|{Col.CYAN} {'G_CLIENT':<11} {'G_SERVER':<11} {'LATENCY':<1} {Col.WHITE}|{Col.RESET}"
    )
    print("-" * 107)

    while not GLOBAL["stop"]:
        try:
            GLOBAL["v_cnt"] = Path("/tmp/video_cnt.log").read_text(encoding="utf-8").strip() or "---"
            GLOBAL["g_lat"] = Path("/tmp/game_lat.log").read_text(encoding="utf-8").strip() or "WAIT"
        except OSError:
            pass

        try:
            main_bytes = int(Path("/sys/class/net/s1-eth6/statistics/tx_bytes").read_text(encoding="utf-8"))
            backup_bytes = int(Path("/sys/class/net/s1-eth5/statistics/tx_bytes").read_text(encoding="utf-8"))
            main_mbps = 0.0 if last_main_bytes == 0 else (main_bytes - last_main_bytes) * 8 / 1_000_000.0
            backup_mbps = 0.0 if last_backup_bytes == 0 else (backup_bytes - last_backup_bytes) * 8 / 1_000_000.0
            last_main_bytes = main_bytes
            last_backup_bytes = backup_bytes
            GLOBAL["main_path_load"] = max(main_mbps, backup_mbps)
        except OSError:
            pass

        c_stat = Col.GREEN if GLOBAL["status"] == "OK" else Col.RED if GLOBAL["status"] in {"LINK DOWN", "SERVICE LOSS"} else Col.YELLOW
        c_v_cnt = Col.RED if GLOBAL["v_cnt"] == "TIMEOUT" else Col.WHITE
        c_g_lat = Col.RED if GLOBAL["g_lat"] == "TIMEOUT" else Col.GREEN
        c_srv = Col.YELLOW if "BACKUP" in str(GLOBAL["srv"]) else Col.WHITE

        print(
            f"{Col.WHITE}| {Col.MAGENTA}{'10.0.0.1':<11} {'10.0.0.12':<11} {c_srv}{GLOBAL['srv']:<10} {c_stat}{GLOBAL['status']:<15} {c_v_cnt}{GLOBAL['v_cnt']:<7} "
            f"{GLOBAL['main_path_load']:.2f} Mbps  {Col.WHITE}|{Col.CYAN} {'10.0.1.1':<11} {'10.0.1.100':<11} {c_g_lat}{GLOBAL['g_lat']:<2}  {Col.WHITE}|{Col.RESET}"
        )
        time.sleep(1.0)


def inject_link_failure(net):
    def worker():
        time.sleep(15)
        GLOBAL["status"] = "LINK DOWN"
        net.configLinkStatus("s1", "s4", "down")
    threading.Thread(target=worker, daemon=True).start()


def dump_fault_debug(h1, h2, g1, v1, h3):
    print("\n[DBG] ===== fault service diagnostics =====", flush=True)
    print("[DBG] h1 proc =", h1.cmd("ps -ef | grep '{} --hostname {}' | grep -v grep || true".format(SERVICE_REMOTE, MAIN_HOST)).strip(), flush=True)
    print("[DBG] h1 udp8888 =", h1.cmd("ss -lun | grep 8888 || true").strip(), flush=True)
    print("[DBG] h1 service log:\n" + h1.cmd("cat /tmp/h1_service.log 2>&1 || true"), flush=True)
    print("[DBG] h2 proc =", h2.cmd("ps -ef | grep '{} --hostname {}' | grep -v grep || true".format(SERVICE_REMOTE, BACKUP_HOST)).strip(), flush=True)
    print("[DBG] h2 udp8888 =", h2.cmd("ss -lun | grep 8888 || true").strip(), flush=True)
    print("[DBG] h2 service log:\n" + h2.cmd("cat /tmp/h2_service.log 2>&1 || true"), flush=True)
    print("[DBG] h3 game server:\n" + h3.cmd("ps -ef | grep '{}' | grep -v grep || true".format(GAME_SERVER_REMOTE)), flush=True)
    print("[DBG] g1 game client:\n" + g1.cmd("ps -ef | grep '{}' | grep -v grep || true".format(GAME_CLIENT_REMOTE)), flush=True)
    print("[DBG] v1 video client:\n" + v1.cmd("ps -ef | grep '{}' | grep -v grep || true".format(VIDEO_CLIENT_REMOTE)), flush=True)
    print("[DBG] g1 stderr:\n" + g1.cmd("cat /tmp/game_client.stderr 2>/dev/null || true"), flush=True)
    print("[DBG] v1 stderr:\n" + v1.cmd("cat /tmp/video_client.stderr 2>/dev/null || true"), flush=True)
    try:
        print("[DBG] host /tmp/video_cnt.log:\n" + Path("/tmp/video_cnt.log").read_text(encoding="utf-8"), flush=True)
        print("[DBG] host /tmp/game_lat.log:\n" + Path("/tmp/game_lat.log").read_text(encoding="utf-8"), flush=True)
    except OSError:
        pass




def start_external_trigger_watcher(net):
    Path(TRIGGER_FILE).write_text("", encoding="utf-8")

    def handle_command(command: str):
        cmd = command.strip().lower()
        if cmd == "failmain":
            GLOBAL["status"] = "LINK DOWN"
            net.configLinkStatus("s1", "s4", "down")
            #dbg("manual trigger: main link s1-s4 set to DOWN")
            return
        if cmd == "restoremain":
            net.configLinkStatus("s1", "s4", "up")
            #dbg("manual trigger: main link s1-s4 set to UP")
            return
        #dbg(f"external trigger ignored: {command}")

    def watcher():
        last_seen = ""
        while not GLOBAL["stop"]:
            try:
                command = Path(TRIGGER_FILE).read_text(encoding="utf-8").strip()
            except OSError:
                command = ""
            if command and command != last_seen:
                last_seen = command
                handle_command(command)
                try:
                    Path(TRIGGER_FILE).write_text("", encoding="utf-8")
                except OSError:
                    pass
                last_seen = ""
            time.sleep(0.5)

    threading.Thread(target=watcher, daemon=True).start()


def parse_args():
    parser = argparse.ArgumentParser(description="Scenario 2: link-failure failover")
    parser.add_argument("-man", action="store_true", dest="manual_mode",
                        help="manual mode: no automatic link down, trigger it manually")
    parser.add_argument("-pingall", action="store_true", dest="run_pingall",
                        help="run pingall before rendering the dashboard")
    return parser.parse_args()


def start_scenario(manual_mode: bool = False, run_pingall: bool = False):
    setLogLevel("info")
    net = Containernet(controller=RemoteController, link=TCLink, xterms=False)
    info("*** Starting High Availability slicing topology... ***\n")
    net.addController("c0", controller=RemoteController, ip="127.0.0.1", port=6653)

    v1 = net.addHost("v1", ip="10.0.0.1/24", mac="00:00:00:00:00:01")
    g1 = net.addHost("g1", ip="10.0.1.1/24", mac="00:00:00:00:01:01")
    h3 = net.addHost("h3", ip="10.0.1.100/24", mac="00:00:00:00:01:00")

    h1 = net.addDockerHost("h1", dimage=HOST_IMAGE, ip="10.0.0.12/24", mac="00:00:00:00:00:12", docker_args={"hostname": "h1"})
    h2 = net.addDockerHost("h2", dimage=HOST_IMAGE, ip="10.0.0.12/24", mac="00:00:00:00:00:12", docker_args={"hostname": "h2"})

    s1 = net.addSwitch("s1")
    s2 = net.addSwitch("s2")
    s3 = net.addSwitch("s3")
    s4 = net.addSwitch("s4")

    net.addLink(v1, s1, port2=1)
    net.addLink(g1, s1, port2=2)
    net.addLink(s1, s2, port1=4, port2=1, bw=100, delay="10ms")
    net.addLink(s1, s3, port1=5, port2=1, bw=100, delay="10ms")
    net.addLink(s1, s4, port1=6, port2=1, bw=100, delay="10ms")
    net.addLink(h3, s2, port2=2)
    net.addLink(h2, s3, port2=2)
    net.addLink(h1, s4, port2=2)
    net.addLink(h1, h2, intfName1="h1-sync", intfName2="h2-sync")

    print(f"")
    dbg("before net.start()")
    net.start()
    dbg("after net.start()")
    time.sleep(2.0)

    h1.cmd("ip addr add 192.168.0.12/24 dev h1-sync")
    h2.cmd("ip addr add 192.168.0.13/24 dev h2-sync")

    v1.cmd("arp -s 10.0.0.12 00:00:00:00:00:12")
    h1.cmd("arp -s 10.0.0.1 00:00:00:00:00:01")
    h2.cmd("arp -s 10.0.0.1 00:00:00:00:00:01")
    g1.cmd("arp -s 10.0.1.100 00:00:00:00:01:00")
    h3.cmd("arp -s 10.0.1.1 00:00:00:00:01:01")

    api = RyuNorthboundAPI()
    dbg(f"switches seen by RYU: {api.wait_for_switches(expected=4, timeout=20)}")
    install_flows(api, fault_initial_flows())

    Path("/tmp/video_cnt.log").write_text("WAIT\n", encoding="utf-8")
    Path("/tmp/game_lat.log").write_text("WAIT\n", encoding="utf-8")

    server_source = Path(__file__).with_name("server.py").read_text(encoding="utf-8")
    push_text_to_node(h1, SERVICE_REMOTE, server_source)
    push_text_to_node(h2, SERVICE_REMOTE, server_source)
    start_counter_service(h1, MAIN_HOST, "192.168.0.12", "192.168.0.13", get_state=False)
    dbg("main service start requested")

    start_simple_script(h3, GAME_SERVER_REMOTE, GAME_SERVER, "/tmp/udp_backend.stdout", "/tmp/udp_backend.stderr")
    start_simple_script(g1, GAME_CLIENT_REMOTE, GAME_CLIENT, "/tmp/game_client.stdout", "/tmp/game_client.stderr")
    start_simple_script(v1, VIDEO_CLIENT_REMOTE, VIDEO_CLIENT, "/tmp/video_client.stdout", "/tmp/video_client.stderr")
    start_video_bw_stream(v1, h1, h2)

    ok_video = wait_for_log_value("/tmp/video_cnt.log", timeout_sec=12.0)
    ok_game = wait_for_log_value("/tmp/game_lat.log", timeout_sec=12.0)

    if not ok_video or not ok_game or not wait_for_service_listener(h1, MAIN_HOST, timeout_sec=8.0):
        dump_fault_debug(h1, h2, g1, v1, h3)
        raise RuntimeError("The fault scenario clients did not receive valid replies from the running services.")

    if run_pingall:
        print("\n*** Running pingall before dashboard... ***\n", flush=True)
        setLogLevel("info")
        net.pingAll()
       
    if manual_mode:        
        print("\n*** MANUAL MODE ENABLED ***", flush=True)
        print("The link-down fault will NOT start automatically.", flush=True)
        print("To activate it from another terminal, run:", flush=True)
        print("  echo \"failmain\" | sudo tee /tmp/scenario2_trigger.txt >/dev/null", flush=True)
        print("To restore the main link, run:", flush=True)
        print("  echo \"restoremain\" | sudo tee /tmp/scenario2_trigger.txt >/dev/null", flush=True)
        print("", flush=True)        

    setLogLevel("error")
    threading.Thread(target=dashboard_thread, daemon=True).start()

    if manual_mode:
        start_external_trigger_watcher(net)
    else:
        inject_link_failure(net)

    def failover_service():
        GLOBAL["status"] = "MIGRATING..."
        start_counter_service(h2, BACKUP_HOST, "192.168.0.13", "192.168.0.12", get_state=True)
        time.sleep(0.8)
        stop_counter_service(h1, MAIN_HOST)
        time.sleep(1.5)
        if not wait_for_service_listener(h2, BACKUP_HOST, timeout_sec=8.0):
            dbg("backup service on h2 did not open UDP/8888 in time")
            dump_fault_debug(h1, h2, g1, v1, h3)
            return
        GLOBAL["status"] = "REROUTING..."
        modify_flows(api, fault_failover_flows())
        time.sleep(1.0)
        GLOBAL["srv"] = "H2_BACKUP"
        GLOBAL["status"] = "OK"
        #dbg("failover callback completed")

    monitor = FaultNorthbound(api=api, global_state=GLOBAL, switch_dpid=1, main_port=6)
    failed_over = failover_on_service_loss(monitor, failover_service, deadline_sec=60.0)

    if not failed_over:
        GLOBAL["status"] = "NO FAILOVER"

    time.sleep(10)
    GLOBAL["stop"] = True        
    print(f"-" * 107)
    print(f"\n{Col.BOLD}*** Scenario complete. Entering CLI for test... ***{Col.RESET}\n")
    setLogLevel("info")
    CLI(net)

    stop_counter_service(h1, MAIN_HOST)
    stop_counter_service(h2, BACKUP_HOST)
    net.stop()


if __name__ == "__main__":
    start_scenario()