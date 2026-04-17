# Test Commands Sheet

This quick reference collects the most useful commands to run while testing the two final scenarios.

---

## Scenario 1 — QoS / migration `h3 -> h4`

| Command | Where to run it | What it checks / does |
|---|---|---|
| `sudo python3 scenario1_qos.py` | Main terminal | Starts the scenario in automatic and autoconsistent mode |
| `sudo python3 scenario1_qos.py -man -pingall` | Main terminal | Starts the scenario in manual mode and shows the initial `pingall` |
| `echo "noiseprofile" \| sudo tee /tmp/scenario1_trigger.txt >/dev/null` | Another host terminal | Starts the full background-noise profile |
| `echo "noise 0.6 35" \| sudo tee /tmp/scenario1_trigger.txt >/dev/null` | Another host terminal | Starts one targeted disturbance |
| `ping -c 3 10.0.0.12` | `g1` node terminal | Checks reachability of the service VIP |
| `iperf -c 10.0.0.55 -u -b 0.6M -t 35` | `d1` node terminal | Generates download traffic toward `h5` |
| `ping -c 3 10.0.2.100` | `v1` node terminal | Checks the video slice |
| `ss -lun \| grep 8888` | `h3` or `h4` Docker terminal | Checks whether the UDP service is listening |
| `cat /tmp/h3_service.log` | `h3` Docker terminal | Reads the primary service log |
| `cat /tmp/h4_service.log` | `h4` Docker terminal | Reads the backup service log |
| `pingall` | Final Mininet CLI | Checks global connectivity after migration |
| `nodes` | Final Mininet CLI | Lists all topology nodes |
| `links` | Final Mininet CLI | Shows the links and their status |

---

## Scenario 2 — fault / failover `h1 -> h2`

| Command | Where to run it | What it checks / does |
|---|---|---|
| `sudo python3 scenario2_fault_modes_traditional_pingall.py -man -pingall` | Main terminal | Starts the scenario in manual mode and shows the initial `pingall` |
| `echo "failmain" \| sudo tee /tmp/scenario2_trigger.txt >/dev/null` | Another host terminal | Brings the main link `s1-s4` down |
| `echo "restoremain" \| sudo tee /tmp/scenario2_trigger.txt >/dev/null` | Another host terminal | Restores the main link |
| `ping -c 3 10.0.0.12` | `v1` node terminal | Checks the video service VIP |
| `ping -c 3 10.0.1.100` | `g1` node terminal | Checks the game slice |
| `ss -lun \| grep 8888` | `h1` or `h2` Docker terminal | Checks the service listener |
| `cat /tmp/h1_service.log` | `h1` Docker terminal | Reads the primary service log |
| `cat /tmp/h2_service.log` | `h2` Docker terminal | Reads the backup service log |
| `pingall` | Final Mininet CLI | Checks global connectivity after failover |
| `link s1 s4 down` | Final Mininet CLI | Manual fault on the main link |
| `link s1 s4 up` | Final Mininet CLI | Restores the main link |
| `nodes` | Final Mininet CLI | Lists all nodes |
| `links` | Final Mininet CLI | Shows link status |

---

## Quick node-terminal launch commands

| Node | Command |
|---|---|
| `g1` | `gnome-terminal --title="g1" -- bash -lc 'sudo mnexec -a $(pgrep -of "mininet:g1") env PS1="(g1) \\u@\\h:\\w\\$ " bash --noprofile --norc'` |
| `d1` | `gnome-terminal --title="d1" -- bash -lc 'sudo mnexec -a $(pgrep -of "mininet:d1") env PS1="(d1) \\u@\\h:\\w\\$ " bash --noprofile --norc'` |
| `v1` | `gnome-terminal --title="v1" -- bash -lc 'sudo mnexec -a $(pgrep -of "mininet:v1") env PS1="(v1) \\u@\\h:\\w\\$ " bash --noprofile --norc'` |
| `h5` | `gnome-terminal --title="h5" -- bash -lc 'sudo mnexec -a $(pgrep -of "mininet:h5") env PS1="(h5) \\u@\\h:\\w\\$ " bash --noprofile --norc'` |
| `h3` as normal Mininet host | `gnome-terminal --title="h3" -- bash -lc 'sudo mnexec -a $(pgrep -of "mininet:h3") env PS1="(h3) \\u@\\h:\\w\\$ " bash --noprofile --norc'` |

---

## Test flow 

| Phase | Scenario 1 | Scenario 2 |
|---|---|---|
| Before the event | `-man -pingall` | `-man -pingall` |
| Event trigger | `noiseprofile` | `failmain` |
| During the event | monitor the dashboard | monitor the dashboard |
| After migration/failover | Mininet CLI + Docker logs | Mininet CLI + Docker logs |
