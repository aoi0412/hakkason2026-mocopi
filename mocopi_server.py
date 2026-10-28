"""Local web server and mocopi UDP/WebSocket bridge (Python standard library only)."""

from __future__ import annotations

import hashlib
import json
import math
import os
import socket
import socketserver
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PAGE = ROOT / "mocopi-posture-monitor.html"
DATA_DIR = Path(os.environ.get("DATA_DIR", str(ROOT))).resolve()
PROFILE_FILE = DATA_DIR / "mocopi-profile.json"
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8000"))
WS_PORT = int(os.environ.get("WS_PORT", "8765"))
UDP_PORT = int(os.environ.get("MOCOPI_PORT", "12351"))
WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
CONTAINERS = {"head", "sndf", "skdf", "bons", "bndt", "fram", "btrs", "btdt", "sndt"}

clients: set[tuple[socket.socket, threading.Lock]] = set()
client_roles: dict[tuple[socket.socket, threading.Lock], str] = {}
clients_lock = threading.Lock()
profile_lock = threading.Lock()
skeleton: dict[int, dict] = {}
last_packet_at = 0.0
packet_count = 0
last_parse_error_at = 0.0


def parse_boxes(data: bytes, start: int = 0, end: int | None = None, depth: int = 0) -> list[dict]:
    if end is None:
        end = len(data)
    if depth > 12:
        raise ValueError("SMF box nesting limit exceeded")
    result = []
    offset = start
    while offset + 8 <= end:
        size = struct.unpack_from("<i", data, offset)[0]
        kind = data[offset + 4:offset + 8].decode("ascii", errors="replace")
        body_start = offset + 8
        body_end = body_start + size
        if size < 0 or body_end > end:
            raise ValueError("invalid SMF box size")
        box = {"type": kind, "size": size, "data": data[body_start:body_end], "children": []}
        if kind in CONTAINERS:
            box["children"] = parse_boxes(data, body_start, body_end, depth + 1)
        result.append(box)
        offset = body_end
    if offset != end:
        raise ValueError("trailing bytes in SMF box list")
    return result


def find_box(boxes: list[dict], kind: str) -> dict | None:
    for box in boxes:
        if box["type"] == kind:
            return box
        found = find_box(box["children"], kind)
        if found is not None:
            return found
    return None


def number(box: dict | None, fmt: str):
    if box is None:
        return None
    size = struct.calcsize(fmt)
    if len(box["data"]) < size:
        return None
    return struct.unpack_from("<" + fmt, box["data"])[0]


def transform(box: dict | None) -> dict:
    values = struct.unpack_from("<7f", box["data"]) if box and len(box["data"]) >= 28 else (0, 0, 0, 1, 0, 0, 0)
    return {"q": list(values[:4]), "p": list(values[4:7])}


def parse_skeleton(boxes: list[dict]) -> bool:
    global skeleton
    skdf = find_box(boxes, "skdf")
    bons = find_box(skdf["children"] if skdf else [], "bons")
    if bons is None:
        return False
    new_skeleton = {}
    for record in bons["children"]:
        if record["type"] != "bndt":
            continue
        bone_id = number(find_box(record["children"], "bnid"), "h")
        parent_id = number(find_box(record["children"], "pbid"), "h")
        if bone_id is not None:
            new_skeleton[bone_id] = {"id": bone_id, "parent": parent_id, **transform(find_box(record["children"], "tran"))}
    if not new_skeleton:
        return False
    skeleton = new_skeleton
    print(f"骨格定義を受信しました（{len(skeleton)} joints）", flush=True)
    return True


def parse_frame(boxes: list[dict]) -> dict | None:
    frame = find_box(boxes, "fram")
    btrs = find_box(frame["children"] if frame else [], "btrs")
    if btrs is None:
        return None
    transforms = {}
    for item in btrs["children"]:
        if item["type"] != "btdt":
            continue
        bone_id = number(find_box(item["children"], "bnid"), "h")
        if bone_id is not None:
            transforms[bone_id] = transform(find_box(item["children"], "tran"))
    frame_number = number(find_box(frame["children"], "fnum"), "i")
    return {"transforms": transforms, "frame": frame_number} if transforms else None


def q_multiply(a: list[float], b: list[float]) -> list[float]:
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return [aw*bx + ax*bw + ay*bz - az*by, aw*by - ax*bz + ay*bw + az*bx,
            aw*bz + ax*by - ay*bx + az*bw, aw*bw - ax*bx - ay*by - az*bz]


def q_rotate(q: list[float], v: list[float]) -> list[float]:
    x, y, z, w = q
    vx, vy, vz = v
    tx, ty, tz = 2*(y*vz-z*vy), 2*(z*vx-x*vz), 2*(x*vy-y*vx)
    return [vx+w*tx+(y*tz-z*ty), vy+w*ty+(z*tx-x*tz), vz+w*tz+(x*ty-y*tx)]


def add(a: list[float], b: list[float]) -> list[float]:
    return [a[0]+b[0], a[1]+b[1], a[2]+b[2]]


def subtract(a: list[float], b: list[float]) -> list[float]:
    return [a[0]-b[0], a[1]-b[1], a[2]-b[2]]


def distance(a: list[float], b: list[float]) -> float:
    return math.dist(a, b)


def angle_at(a: list[float], b: list[float], c: list[float]) -> float:
    u, v = subtract(a, b), subtract(c, b)
    nu, nv = math.sqrt(sum(x*x for x in u)), math.sqrt(sum(x*x for x in v))
    if not nu or not nv:
        return 0.0
    cosine = sum(u[i]*v[i] for i in range(3)) / (nu*nv)
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def world_positions(frame: dict) -> dict[int, list[float]]:
    positions: dict[int, list[float]] = {}
    rotations: dict[int, list[float]] = {}
    visiting: set[int] = set()

    def resolve(bone_id: int):
        if bone_id in positions or bone_id in visiting or bone_id not in skeleton:
            return
        visiting.add(bone_id)
        bone = skeleton[bone_id]
        current = frame["transforms"].get(bone_id, bone)
        parent = skeleton.get(bone["parent"])
        if parent:
            resolve(parent["id"])
            parent_pos, parent_rot = positions.get(parent["id"]), rotations.get(parent["id"])
            if parent_pos is not None and parent_rot is not None:
                positions[bone_id] = add(parent_pos, q_rotate(parent_rot, bone["p"]))
                rotations[bone_id] = q_multiply(parent_rot, current["q"])
        else:
            positions[bone_id] = current["p"]
            rotations[bone_id] = current["q"]
        visiting.remove(bone_id)

    for bone_id in skeleton:
        resolve(bone_id)
    return positions


def extract_features(frame: dict) -> dict:
    positions = world_positions(frame)
    features = {}
    def point(bone_id: int):
        if bone_id not in positions:
            raise KeyError(bone_id)
        return positions[bone_id]
    measures = {
        "leftElbow": lambda: angle_at(point(12), point(13), point(14)),
        "rightElbow": lambda: angle_at(point(16), point(17), point(18)),
        "leftShoulder": lambda: angle_at(point(6), point(11), point(12)),
        "rightShoulder": lambda: angle_at(point(6), point(15), point(16)),
        "leftWristHead": lambda: distance(point(14), point(10)),
        "rightWristHead": lambda: distance(point(18), point(10)),
        "leftWristHip": lambda: distance(point(14), point(0)),
        "rightWristHip": lambda: distance(point(18), point(0)),
    }
    for key, measure in measures.items():
        try:
            value = measure()
            if math.isfinite(value):
                features[key] = value
        except (KeyError, ValueError):
            pass
    return features


def websocket_frame(message: str | bytes, opcode: int | None = None) -> bytes:
    if isinstance(message, str):
        data = message.encode("utf-8")
        opcode = opcode or 1
    else:
        data = message
        opcode = opcode or 2
    length = len(data)
    if length < 126:
        header = bytes((0x80 | opcode, length))
    elif length < 65536:
        header = bytes((0x80 | opcode, 126)) + struct.pack(">H", length)
    else:
        header = bytes((0x80 | opcode, 127)) + struct.pack(">Q", length)
    return header + data


def send_to_client(client: tuple[socket.socket, threading.Lock], message: str | bytes, opcode: int | None = None):
    sock, lock = client
    try:
        with lock:
            sock.sendall(websocket_frame(message, opcode))
    except OSError:
        with clients_lock:
            clients.discard(client)


def broadcast(payload: dict):
    message = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with clients_lock:
        current = list(clients)
    for client in current:
        send_to_client(client, message)


def broadcast_raw(payload: bytes):
    with clients_lock:
        current = [client for client in clients if client_roles.get(client) == "dashboard"]
    for client in current:
        send_to_client(client, payload)


def send_alert(name: str, sender: tuple[socket.socket, threading.Lock]):
    message = json.dumps({"type": "alert", "name": name}, ensure_ascii=False, separators=(",", ":"))
    with clients_lock:
        current = [client for client in clients if client is not sender and client_roles.get(client) == "dashboard"]
    for client in current:
        send_to_client(client, message)


def recv_exact(sock: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        data = sock.recv(length - len(chunks))
        if not data:
            raise ConnectionError("websocket closed")
        chunks.extend(data)
    return bytes(chunks)


class WebSocketHandler(socketserver.BaseRequestHandler):
    def handle(self):
        sock = self.request
        sock.settimeout(15)
        header = bytearray()
        while b"\r\n\r\n" not in header and len(header) < 16384:
            chunk = sock.recv(2048)
            if not chunk:
                return
            header.extend(chunk)
        lines = bytes(header).decode("latin-1").split("\r\n")
        request_line = lines[0].split(" ") if lines else []
        request_target = request_line[1] if len(request_line) > 1 else "/mocopi"
        role = "receiver" if "role=receiver" in request_target else "dashboard"
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        key = headers.get("sec-websocket-key")
        if not key:
            sock.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            return
        accept = hashlib.sha1((key + WS_MAGIC).encode("ascii")).digest()
        import base64
        response = base64.b64encode(accept).decode("ascii")
        sock.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + response + "\r\n\r\n").encode("ascii"))
        sock.settimeout(None)
        client = (sock, threading.Lock())
        with clients_lock:
            clients.add(client)
            client_roles[client] = role
        send_to_client(client, json.dumps({"type": "status", "message": f"{role} connected"}))
        try:
            while True:
                first, second = recv_exact(sock, 2)
                opcode = first & 0x0F
                length = second & 0x7F
                if length == 126:
                    length = struct.unpack(">H", recv_exact(sock, 2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", recv_exact(sock, 8))[0]
                mask = recv_exact(sock, 4) if second & 0x80 else b""
                payload = bytearray(recv_exact(sock, length)) if length else bytearray()
                if mask:
                    for index in range(length):
                        payload[index] ^= mask[index % 4]
                if opcode == 8:
                    send_to_client(client, "", opcode=8)
                    break
                if opcode == 9:
                    send_to_client(client, bytes(payload).decode("latin-1"), opcode=10)
                elif opcode == 1 and role == "dashboard":
                    try:
                        command = json.loads(payload.decode("utf-8"))
                        if command.get("type") == "alert":
                            label = command.get("name", "姿勢")
                            if isinstance(label, str):
                                send_alert(label[:24], client)
                    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                        pass
        except (OSError, ConnectionError, TimeoutError):
            pass
        finally:
            with clients_lock:
                clients.discard(client)
                client_roles.pop(client, None)


class ReusableThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class PageHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/profile":
            with profile_lock:
                try:
                    stored = json.loads(PROFILE_FILE.read_text(encoding="utf-8")) if PROFILE_FILE.exists() else {"revision": 0, "profile": None}
                except (OSError, json.JSONDecodeError):
                    stored = {"revision": 0, "profile": None}
            body = json.dumps(stored, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/health":
            with clients_lock:
                dashboard_clients = sum(role == "dashboard" for role in client_roles.values())
            public_url = os.environ.get("PUBLIC_URL", "").rstrip("/")
            request_host = self.headers.get("Host", "").split(":", 1)[0].strip("[]")
            if public_url:
                phone_urls = [f"{public_url}/"]
            elif request_host and request_host not in {"localhost", "127.0.0.1"}:
                # In Docker Desktop, container IPs are not reachable from phones.
                # Reuse the host name or LAN IP that the browser used to reach us.
                phone_urls = [f"http://{request_host}:{HTTP_PORT}/"]
            else:
                phone_urls = []
            body = json.dumps({"bridge": "ready", "udpPort": UDP_PORT, "dashboardClients": dashboard_clients,
                               "udpPackets": packet_count,
                               "lastPacketAt": last_packet_at or None,
                               "phoneUrls": phone_urls}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path in ("/", "/receiver", "/mocopi-posture-monitor.html"):
            page_path = PAGE
        else:
            self.send_error(404, "Not found")
            return
        try:
            page = page_path.read_bytes()
        except OSError:
            self.send_error(500, "画面ファイルを読み込めません")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def do_POST(self):
        if self.path != "/api/profile":
            self.send_error(404, "Not found")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 131072:
                raise ValueError("プロフィールのサイズが正しくありません")
            request = json.loads(self.rfile.read(length).decode("utf-8"))
            profile = request.get("profile")
            if not isinstance(profile, dict) or not isinstance(profile.get("poses", []), list):
                raise ValueError("プロフィール形式が正しくありません")
            if len(profile.get("poses", [])) > 100:
                raise ValueError("登録できる姿勢は100件までです")
            profile = json.loads(json.dumps(profile, ensure_ascii=False, allow_nan=False))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError, AttributeError, TypeError) as error:
            message = json.dumps({"error": str(error) or "プロフィールを読み込めません"}, ensure_ascii=False).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(message)))
            self.end_headers()
            self.wfile.write(message)
            return
        with profile_lock:
            revision = time.time_ns()
            temporary = PROFILE_FILE.with_suffix(".tmp")
            temporary.write_text(json.dumps({"revision": revision, "profile": profile}, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, PROFILE_FILE)
        body = json.dumps({"revision": revision}, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print("HTTP " + (fmt % args), flush=True)


def udp_loop():
    global last_packet_at, packet_count
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        udp.bind(("0.0.0.0", UDP_PORT))
    except OSError as error:
        print(f"UDP受信を開始できませんでした: {error}", flush=True)
        return
    print(f"mocopi UDP受信を開始しました: 0.0.0.0:{UDP_PORT}", flush=True)
    while True:
        try:
            data, remote = udp.recvfrom(65535)
            last_packet_at = time.monotonic()
            packet_count += 1
            if packet_count == 1 or packet_count % 60 == 0:
                print(f"UDP受信 {packet_count}件目: {len(data)} bytes, {remote[0]}:{remote[1]}", flush=True)
            broadcast_raw(data)
        except OSError as error:
            print(f"UDP受信エラー: {error}", flush=True)


def local_ipv4_addresses():
    addresses = set()
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_DGRAM):
            address = item[4][0]
            if not address.startswith(("127.", "169.254.")):
                addresses.add(address)
    except OSError:
        pass
    return sorted(addresses)


def status_loop():
    while True:
        time.sleep(5)
        if last_packet_at and time.monotonic() - last_packet_at > 3:
            broadcast({"type": "status", "message": "waiting for mocopi"})


def main():
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print(f"データ保存先を作成できませんでした: {error}", flush=True)
        return
    try:
        http_server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), PageHandler)
    except OSError as error:
        print(f"HTTPサーバーを起動できませんでした（ポート {HTTP_PORT}）: {error}", flush=True)
        return
    try:
        ws_server = ReusableThreadingTCPServer(("0.0.0.0", WS_PORT), WebSocketHandler)
    except OSError as error:
        http_server.server_close()
        print(f"WebSocket中継を起動できませんでした（ポート {WS_PORT}）: {error}", flush=True)
        return
    threading.Thread(target=udp_loop, daemon=True).start()
    threading.Thread(target=status_loop, daemon=True).start()
    print(f"PC操作画面: http://localhost:{HTTP_PORT}/", flush=True)
    public_url = os.environ.get("PUBLIC_URL", "").rstrip("/")
    if public_url:
        print(f"PC・スマホWebアプリ: {public_url}/", flush=True)
    else:
        print("スマートフォンでも同じWebアプリを開いてください。", flush=True)
        for address in local_ipv4_addresses():
            print(f"  PC・スマホWebアプリ: http://{address}:{HTTP_PORT}/", flush=True)
    print(f"WebSocket: ws://<PCのIPv4アドレス>:{WS_PORT}/（姿勢画面から自動接続）", flush=True)
    print("終了するには Ctrl+C を押してください。", flush=True)
    try:
        threading.Thread(target=ws_server.serve_forever, daemon=True).start()
        http_server.serve_forever()
    except KeyboardInterrupt:
        print("サーバーを終了します。", flush=True)
    finally:
        http_server.shutdown()
        http_server.server_close()
        ws_server.shutdown()
        ws_server.server_close()


if __name__ == "__main__":
    main()
