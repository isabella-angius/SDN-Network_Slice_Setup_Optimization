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
from northbound_qos import QosNorthbound, migrate_on_qos_violation
from slice_profiles import PRIORITY, qos_failover_flows, qos_initial_flows

HOST_IMAGE = "dev_test_py3"
DEBUG = False
SERVICE_REMOTE = "/tmp/service_server_runtime.py"
CLIENT_REMOTE = "/tmp/custom_client.py"
CLIENT_LOG = "/tmp/client.log"

MAIN_HOST = "h3"
BACKUP_HOST = "h4"

# These are the knobs that control the *actual* ramp-up timing.
# To speed up the congestion build-up, reduce RAMP_ADVANCE_SEC.
INITIAL_NOISE_DELAY_SEC = 4.0 #initial 12
RAMP_ADVANCE_SEC = 8.0 #initial 8
NOISE_STAGES = [
    (0.4, 55),
    (0.5, 45),
    (0.6, 35),
    (0.7, 25),
]
TRIGGER_FILE = "/tmp/scenario1_trigger.txt"


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
    "cnt": "---",
    "lat": "WAIT",
    "bw_down": 0.0,
    "bw_vid": 0.0,
    "link_load": 0.0,
    "srv": "H3_MAIN",
    "status": "BOOT",
}


CUSTOM_CLIENT = r"""#!/usr/bin/env python3
import os
import socket
import time

LOG_PATH = "/tmp/client.log"
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(1.2)

while True:
    start = time.perf_counter()
    line = "TIMEOUT"
    try:
        sock.sendto(b"req", ("10.0.0.12", 8888))
        data, _ = sock.recvfrom(1024)
        lat = (time.perf_counter() - start) * 1000.0
        line = f"CNT:{data.decode('utf-8')}|LAT:{lat:.1f}ms"
    except Exception:
        line = "TIMEOUT"

    tmp_path = LOG_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        handle.write(line + "\n")
    os.replace(tmp_path, LOG_PATH)

    print(line, flush=True)
    time.sleep(1.0)
"""


def dbg(message: str) -> None:
    print(f"[DBG] {message}", flush=True)
    #return


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


def start_service(node, hostname: str, sync_ip: str, peer_sync_ip: str, get_state: bool = False):
    py = detect_python(node)
    flag = " --get-state" if get_state else ""
    node.cmd("pkill -f '{} --hostname {}' >/dev/null 2>&1 || true".format(SERVICE_REMOTE, hostname))
    node.cmd(
        "{} {} --hostname {} --service-ip 0.0.0.0 --sync-ip {} --peer-sync-ip {}{} > /tmp/{}_service.log 2>&1 &".format(
            py,
            SERVICE_REMOTE,
            hostname,
            sync_ip,
            peer_sync_ip,
            flag,
            hostname,
        )
    )
    time.sleep(1.0)
    if DEBUG:
        dbg("service exe on {} = {}".format(hostname, py))
        dbg("service proc on {} = {}".format(
            hostname,
            node.cmd("ps -ef | grep '{} --hostname {}' | grep -v grep || true".format(SERVICE_REMOTE, hostname)).strip(),
        ))


def stop_service(node, hostname: str):
    node.cmd("pkill -SIGTERM -f '{} --hostname {}' >/dev/null 2>&1 || true".format(SERVICE_REMOTE, hostname))


def start_measurement_client(g1):
    push_text_to_node(g1, CLIENT_REMOTE, CUSTOM_CLIENT)
    Path(CLIENT_LOG).write_text("WAIT\n", encoding="utf-8")
    py = detect_python(g1)
    g1.cmd("pkill -f '{}' >/dev/null 2>&1 || true".format(CLIENT_REMOTE))
    g1.cmd("{} {} > /tmp/custom_client.stdout 2> /tmp/custom_client.stderr &".format(py, CLIENT_REMOTE))
    time.sleep(1.0)
    if DEBUG:
        dbg("client exe on g1 = {}".format(py))
        dbg("client proc on g1 = {}".format(
            g1.cmd("ps -ef | grep '{}' | grep -v grep || true".format(CLIENT_REMOTE)).strip()
        ))


def wait_for_first_sample(timeout_sec: float = 15.0) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            text = Path(CLIENT_LOG).read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        if text and text not in {"WAIT", "TIMEOUT"}:
            dbg(f"first sample: {text}")
            return "CNT:" in text
        time.sleep(0.5)
    return False


def wait_for_service_listener(node, hostname: str, timeout_sec: float = 8.0) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        udp = node.cmd("ss -lun | grep ':8888' || true").strip()
        proc = node.cmd("ps -ef | grep '{} --hostname {}' | grep -v grep || true".format(SERVICE_REMOTE, hostname)).strip()
        if udp or proc:
            return True
        time.sleep(0.5)
    return False


def dump_service_debug(h3, h4, g1):
    return


def safe_ping(node, target: str, label: str) -> None:
    out = node.cmd("ping -c 2 -W 1 {} 2>/dev/null || true".format(target))
    ok = " 0% packet loss" in out or " 0.0% packet loss" in out
    status = "OK" if ok else "FAIL"
    print(f"{label:<24} {status}")


def run_preflight_checks(net) -> None:
    print("\n*** Running safe preflight connectivity checks before dashboard... ***\n", flush=True)
    print("The scenario uses duplicate service VIP nodes, so raw pingall is replaced by safe checks.\n", flush=True)

    unique_hosts = [net["g1"], net["d1"], net["h5"], net["v1"], net["h1"]]
    setLogLevel("info")
    net.ping(unique_hosts)

    print("\nTargeted service checks:")
    safe_ping(net["g1"], "10.0.0.12", "g1 -> service VIP")
    safe_ping(net["d1"], "10.0.0.55", "d1 -> file server")
    safe_ping(net["v1"], "10.0.2.100", "v1 -> video server")
    safe_ping(net["h1"], "10.0.2.1", "video server -> v1")
    print("")



def dashboard_thread():
    last_video_bytes = 0
    last_noise_bytes = 0

    print("\n\n")  
    print(f"{Col.BOLD}{'--- SDN NETWORK SLICING & QoS DASHBOARD ---':^118}{Col.RESET}")
    print("-" * 128)
    print(
        f"{Col.WHITE}| {Col.CYAN}{'GAME & DOWNLOAD SLICE (10.0.0.x)':<90}"
        f"{Col.WHITE}|{Col.MAGENTA} {'VIDEO SLICE (10.0.2.x)':<12}           {Col.WHITE}|{Col.RESET}"
        #f"{Col.WHITE}|{Col.RESET}"
    )    
    print(        
        f"{Col.WHITE}|{Col.CYAN} {'G_CLIENT':<11} {'VIP':<11} {'SRV NAME':<10} {'STATUS':<15} {'CNT':<5} {'LATENCY':<10} {'S1-S2 LOAD':<12} {'DL BW':<8} "
        f"{Col.WHITE}|{Col.MAGENTA} {'V_CLIENT':<11} {'V_SERVER':<11} {'VIDEO BW':<2} {Col.WHITE}|{Col.RESET}"
    )
    print("-" * 128)

    while not GLOBAL["stop"]:
        try:
            noise = int(Path("/sys/class/net/s1-eth3/statistics/rx_bytes").read_text(encoding="utf-8"))
            video = int(Path("/sys/class/net/s1-eth2/statistics/rx_bytes").read_text(encoding="utf-8"))
            if last_noise_bytes > 0:
                GLOBAL["bw_down"] = (noise - last_noise_bytes) * 8 / 1_000_000.0
            if last_video_bytes > 0:
                GLOBAL["bw_vid"] = (video - last_video_bytes) * 8 / 1_000_000.0
            last_noise_bytes = noise
            last_video_bytes = video
        except OSError:
            pass

        try:
            text = Path(CLIENT_LOG).read_text(encoding="utf-8").strip()
            if "CNT:" in text and "LAT:" in text:
                parts = text.split("|", 1)
                GLOBAL["cnt"] = parts[0].replace("CNT:", "").strip()
                GLOBAL["lat"] = parts[1].replace("LAT:", "").strip()
            elif text == "TIMEOUT":
                GLOBAL["lat"] = "TIMEOUT"
        except OSError:
            pass

        c_stat = Col.GREEN if GLOBAL["status"] == "OK" else Col.RED if GLOBAL["status"] == "CONGESTION!" else Col.YELLOW
        c_srv = Col.YELLOW if "BACKUP" in str(GLOBAL["srv"]) else Col.WHITE
        if GLOBAL["lat"] == "TIMEOUT":
            c_lat = Col.RED
        elif "ms" in str(GLOBAL["lat"]) and float(str(GLOBAL["lat"]).replace("ms", "")) > 60.0:
            c_lat = Col.YELLOW
        else:
            c_lat = Col.GREEN

        print(
            f"{Col.WHITE}| {Col.CYAN}{'10.0.0.1':<11} {'10.0.0.12':<11} {c_srv}{GLOBAL['srv']:<10} {c_stat}{GLOBAL['status']:<15} "
            f"{Col.WHITE}{GLOBAL['cnt']:<5} {c_lat}{GLOBAL['lat']:<10} {GLOBAL['link_load']:.2f} Mbps    {GLOBAL['bw_down']:.1f} Mbps "
            f"{Col.WHITE}|{Col.MAGENTA} {'10.0.2.1':<11} {'10.0.2.100':<11} {GLOBAL['bw_vid']:.1f} Mbps{Col.WHITE} |{Col.RESET}"
        )
        time.sleep(1.0)


def start_progressive_background_load(d1):
    def worker():
        time.sleep(INITIAL_NOISE_DELAY_SEC)
        for rate_mbps, duration in NOISE_STAGES:
            #dbg(f"noise traffic {rate_mbps:.1f} Mbps for {duration}s")
            d1.cmd(f"iperf -c 10.0.0.55 -u -b {rate_mbps:.1f}M -t {duration} > /dev/null 2>&1 &")
            time.sleep(RAMP_ADVANCE_SEC)

    threading.Thread(target=worker, daemon=True).start()




def start_external_trigger_watcher(net):
    Path(TRIGGER_FILE).write_text("", encoding="utf-8")

    def handle_command(command: str):
        parts = command.split()
        if not parts:
            return
        if parts[0] == "noise":
            if len(parts) < 2:
                dbg("external trigger ignored: usage noise <rate_mbps> [duration_sec]")
                return
            try:
                rate = float(parts[1])
                duration = int(parts[2]) if len(parts) > 2 else 30
            except ValueError:
                dbg("external trigger ignored: invalid noise arguments")
                return
            net["d1"].cmd(f"iperf -c 10.0.0.55 -u -b {rate:.1f}M -t {duration} > /dev/null 2>&1 &")
            dbg(f"manual trigger: started noise {rate:.1f} Mbps for {duration}s")
            return
        if parts[0] == "noiseprofile":
            def worker():
                for rate_mbps, duration in NOISE_STAGES:
                    dbg(f"manual trigger: stage {rate_mbps:.1f} Mbps for {duration}s")
                    net["d1"].cmd(f"iperf -c 10.0.0.55 -u -b {rate_mbps:.1f}M -t {duration} > /dev/null 2>&1 &")
                    time.sleep(RAMP_ADVANCE_SEC)
            threading.Thread(target=worker, daemon=True).start()
            return
        dbg(f"external trigger ignored: {command}")

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
    parser = argparse.ArgumentParser(description="Scenario 1: QoS-aware service migration")
    parser.add_argument("-man", action="store_true", dest="manual_mode",
                        help="manual mode: no automatic disturbance, trigger it manually")
    parser.add_argument("-pingall", action="store_true", dest="run_pingall",
                        help="run pingall before rendering the dashboard")
    return parser.parse_args()


def start_scenario(manual_mode: bool = False, run_pingall: bool = False):
    setLogLevel("info")
    net = Containernet(controller=RemoteController, link=TCLink, xterms=False)

    info("*** Starting QoS slicing topology... ***\n")
    net.addController("c0", controller=RemoteController, ip="127.0.0.1", port=6653)

    g1 = net.addHost("g1", ip="10.0.0.1/24", mac="00:00:00:00:01:01")
    d1 = net.addHost("d1", ip="10.0.0.3/24", mac="00:00:00:00:03:01")
    h5 = net.addHost("h5", ip="10.0.0.55/24", mac="00:00:00:00:03:00")
    v1 = net.addHost("v1", ip="10.0.2.1/24", mac="00:00:00:00:02:01")
    h1 = net.addHost("h1", ip="10.0.2.100/24", mac="00:00:00:00:02:00")

    h3 = net.addDockerHost("h3", dimage=HOST_IMAGE, ip="10.0.0.12/24", mac="00:00:00:00:01:00", docker_args={"hostname": "h3"})
    h4 = net.addDockerHost("h4", dimage=HOST_IMAGE, ip="10.0.0.12/24", mac="00:00:00:00:01:00", docker_args={"hostname": "h4"})

    s1 = net.addSwitch("s1")
    s2 = net.addSwitch("s2")
    s3 = net.addSwitch("s3")
    s4 = net.addSwitch("s4")

    net.addLink(g1, s1, port2=1)
    net.addLink(v1, s1, port2=2)
    net.addLink(d1, s1, port2=3)
    net.addLink(s1, s2, port1=4, port2=1, bw=1.5, delay="10ms", max_queue_size=80)
    net.addLink(s1, s3, port1=5, port2=1, bw=100, delay="10ms")
    net.addLink(s1, s4, port1=6, port2=1, bw=100)
    net.addLink(h3, s2, port2=2)
    net.addLink(h5, s2, port2=3)
    net.addLink(h4, s3, port2=2)
    net.addLink(h1, s4, port2=2)
    net.addLink(h3, h4, intfName1="h3-sync", intfName2="h4-sync")

    print(f"")
    dbg(f"before net.start()")
    net.start()
    dbg("after net.start()")
    time.sleep(2.0)

    h3.cmd("ip addr add 192.168.0.12/24 dev h3-sync")
    h4.cmd("ip addr add 192.168.0.13/24 dev h4-sync")

    g1.cmd("arp -s 10.0.0.12 00:00:00:00:01:00")
    d1.cmd("arp -s 10.0.0.55 00:00:00:00:03:00")
    v1.cmd("arp -s 10.0.2.100 00:00:00:00:02:00")
    h3.cmd("arp -s 10.0.0.1 00:00:00:00:01:01")
    h4.cmd("arp -s 10.0.0.1 00:00:00:00:01:01")
    h5.cmd("arp -s 10.0.0.3 00:00:00:00:03:01")
    h1.cmd("arp -s 10.0.2.1 00:00:00:00:02:01")

    api = RyuNorthboundAPI()
    dbg(f"switches seen by RYU: {api.wait_for_switches(expected=4, timeout=20)}")
    install_flows(api, qos_initial_flows())

    h5.cmd("iperf -s -u &")
    h1.cmd("iperf -s -u &")
    v1.cmd("iperf -c 10.0.2.100 -u -b 1.0M -t 300 > /dev/null 2>&1 &")

    server_source = Path(__file__).with_name("server.py").read_text(encoding="utf-8")
    push_text_to_node(h3, SERVICE_REMOTE, server_source)
    push_text_to_node(h4, SERVICE_REMOTE, server_source)

    start_service(h3, MAIN_HOST, "192.168.0.12", "192.168.0.13", get_state=False)
    dbg("main service start requested")

    start_measurement_client(g1)
    dbg("measurement client start requested")

    if not wait_for_first_sample():
        dump_service_debug(h3, h4, g1)
        raise RuntimeError("The measurement client never received a valid reply from the service.")

    if run_pingall:
        print("\n*** Running pingall before dashboard... ***\n", flush=True)
        setLogLevel("info")
        net.pingAll()

    if manual_mode:
        print("\n*** MANUAL MODE ENABLED ***", flush=True)
        print("The disturbance will NOT start automatically.", flush=True)
        print("To activate it from another terminal, run one of these commands:", flush=True)
        print("  echo \"noiseprofile\" | sudo tee /tmp/scenario1_trigger.txt >/dev/null", flush=True)
        print("  echo \"noise 0.6 35\" | sudo tee /tmp/scenario1_trigger.txt >/dev/null", flush=True)
        print("", flush=True)

        
    setLogLevel("error")
    threading.Thread(target=dashboard_thread, daemon=True).start()

    if manual_mode:
        start_external_trigger_watcher(net)
    else:
        start_progressive_background_load(d1)

    def migrate_service():
        GLOBAL["status"] = "MIGRATING..."
        start_service(h4, BACKUP_HOST, "192.168.0.13", "192.168.0.12", get_state=True)

        if not wait_for_service_listener(h4, BACKUP_HOST, timeout_sec=12.0):
            GLOBAL["status"] = "MIGRATION FAIL"
            return

        time.sleep(1.5)
        GLOBAL["status"] = "REROUTING..."
        modify_flows(api, qos_failover_flows())
        time.sleep(1.2)
        GLOBAL["srv"] = "H4_BACKUP"
        GLOBAL["status"] = "OK"
        time.sleep(0.5)
        stop_service(h3, MAIN_HOST)

    monitor = QosNorthbound(api=api, global_state=GLOBAL, switch_dpid=1, bottleneck_port=4)

    if manual_mode:
        done = threading.Event()

        def worker():
            migrated = migrate_on_qos_violation(monitor, migrate_service, deadline_sec=3600.0)
            if not migrated:
                GLOBAL["status"] = "NO MIGRATION"
            done.set()

        threading.Thread(target=worker, daemon=True).start()
        done.wait()
    else:
        migrated = migrate_on_qos_violation(monitor, migrate_service, deadline_sec=90.0)
        if not migrated:
            GLOBAL["status"] = "NO MIGRATION"

    time.sleep(10)
    GLOBAL["stop"] = True
    print(f"-" * 128) 
    print(f"\n{Col.BOLD}*** Scenario complete. Entering CLI for test... ***{Col.RESET}\n")
    setLogLevel("info")
    CLI(net)

    stop_service(h3, MAIN_HOST)
    stop_service(h4, BACKUP_HOST)
    net.stop()


if __name__ == "__main__":
    args = parse_args()
    start_scenario(manual_mode=args.manual_mode, run_pingall=args.run_pingall)