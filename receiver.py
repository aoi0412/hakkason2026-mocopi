import os
import socket
import time

HOST = "0.0.0.0"
PORT = int(os.environ.get("MOCOPI_UDP_PORT", "12351"))
BUFFER_SIZE = 65535
REPORT_INTERVAL = 1.0

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((HOST, PORT))
sock.settimeout(0.25)

print(f"Listening for UDP packets on {HOST}:{PORT}", flush=True)
count = 0
last_report = time.monotonic()
last_source = None
last_size = 0
last_preview = b""

try:
    while True:
        try:
            packet, source = sock.recvfrom(BUFFER_SIZE)
            print(f"Received {len(packet)} bytes from {source[0]}:{source[1]}", flush=True)
            count += 1
            last_source = source
            last_size = len(packet)
            last_preview = packet[:32]
        except socket.timeout:
            pass

        now = time.monotonic()
        if now - last_report >= REPORT_INTERVAL:
            if count:
                print(
                    f"{count} packet(s)/s | last from {last_source[0]}:{last_source[1]} "
                    f"| {last_size} bytes | first 32 bytes: {last_preview.hex(' ')}",
                    flush=True,
                )
            count = 0
            last_report = now
except KeyboardInterrupt:
    print("Stopping receiver", flush=True)
finally:
    sock.close()
