import socket, time
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(0.8)
target_ip = "10.0.2.3" # Inizia puntando a H3
while True:
    try:
        # Legge l'IP aggiornato dal file DNS fittizio
        with open("/mnt/dns_record.txt", "r") as f: target_ip = f.read().strip()
    except: pass
    
    start = time.perf_counter()
    sock.sendto(b"req", (target_ip, 8888))
    try:
        data, _ = sock.recvfrom(1024)
        lat = (time.perf_counter() - start) * 1000
        print(f"CNT:{data.decode('utf-8')}|LAT:{lat:.1f}ms", flush=True)
    except:
        print("TIMEOUT", flush=True)
    time.sleep(1.0)
