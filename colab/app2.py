# mychat_av_ui_fixed.py
# Extended mychat_av with UI fixes: removes preview "grey box" glitch and improves layout.
# Keep prior functionality (P2P chat + files + voice + video).
#
# NOTE dependencies:
# pip install sounddevice opencv-python Pillow numpy

import os
import uuid
import json
import socket
import sqlite3
import time
import threading
import queue
import datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

# AV libs
import numpy as np
import sounddevice as sd
import cv2
from PIL import Image, ImageTk

# ------------------------------ CONFIG & FILESYSTEM ------------------------------
BASE_DIR = Path("MyChat")
DB_DIR = BASE_DIR / "Database"
MEDIA_DIR = BASE_DIR / "Media"
IMG_DIR = MEDIA_DIR / "images"
VID_DIR = MEDIA_DIR / "videos"
DOC_DIR = MEDIA_DIR / "documents"
CONF_PATH = BASE_DIR / "config.json"

for p in (DB_DIR, IMG_DIR, VID_DIR, DOC_DIR):
    p.mkdir(parents=True, exist_ok=True)

DEFAULT_TCP_PORT = 323
DISCOVERY_PORT = 32300
DISCOVERY_INTERVAL = 4  # seconds
HEADER = 64  # bytes framing header
FORMAT = "utf-8"
LOCAL_HOST = "0.0.0.0"  # bind all interfaces for listening

# AV config
AUDIO_RATE = 16000  # Hz
AUDIO_CHANNELS = 1
AUDIO_BLOCK_MS = 50  # chunk size in ms
AUDIO_BLOCK_FRAMES = int(AUDIO_RATE * AUDIO_BLOCK_MS / 1000)  # frames per callback
VIDEO_FPS = 15
VIDEO_QUALITY = 50  # JPEG quality

# ------------------------------ UTIL: framing ------------------------------
def send_frame(sock: socket.socket, data: bytes):
    """Send a framed message: 64-byte header with length, then payload."""
    hdr = str(len(data)).encode(FORMAT)
    hdr += b' ' * (HEADER - len(hdr))
    sock.sendall(hdr)
    sock.sendall(data)

def recvall(sock: socket.socket, n: int) -> bytes | None:
    """Receive exactly n bytes or return None on EOF."""
    data = bytearray()
    while len(data) < n:
        try:
            chunk = sock.recv(n - len(data))
        except Exception:
            return None
        if not chunk:
            return None
        data.extend(chunk)
    return bytes(data)

def read_frame(sock: socket.socket) -> bytes | None:
    """Read a frame: read HEADER then payload length bytes."""
    hdr = recvall(sock, HEADER)
    if not hdr:
        return None
    try:
        length = int(hdr.decode(FORMAT).strip())
    except Exception:
        return None
    if length == 0:
        return b""
    return recvall(sock, length)

# ------------------------------ CONFIG & DB ------------------------------
def load_or_create_config():
    if not CONF_PATH.exists():
        cfg = {
            "peer_id": str(uuid.uuid4()),
            "number": "",           # filled on login
            "display_name": "",     # filled on login
            "listen_port": DEFAULT_TCP_PORT
        }
        with open(CONF_PATH, "w") as f:
            json.dump(cfg, f)
        return cfg
    with open(CONF_PATH, "r") as f:
        return json.load(f)

def save_config(cfg):
    with open(CONF_PATH, "w") as f:
        json.dump(cfg, f)

config = load_or_create_config()

DB_PATH = DB_DIR / "chat.db"
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_cursor = _conn.cursor()

def init_db():
    _cursor.execute("""
    CREATE TABLE IF NOT EXISTS contacts (
        id INTEGER PRIMARY KEY,
        peer_id TEXT UNIQUE,
        number TEXT,
        display_name TEXT,
        ip TEXT,
        port INTEGER,
        last_seen TEXT
    )""")
    _cursor.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY,
        peer_id TEXT,
        direction TEXT,
        type TEXT,
        content TEXT,
        file_name TEXT,
        file_size INTEGER,
        timestamp TEXT,
        status TEXT
    )""")
    _conn.commit()

init_db()

# ------------------------------ IN-MEMORY PEER MANAGEMENT ------------------------------
peers = {}  # peer_id -> {conn, ip, port, last_seen, number, display_name}
peers_lock = threading.Lock()

def add_peer_socket(peer_id, conn, ip, port, number=None, display_name=None):
    """Add the socket connection for a peer. Returns True if added."""
    with peers_lock:
        if peer_id in peers:
            return False
        peers[peer_id] = {"conn": conn, "ip": ip, "port": port, "number": number,
                          "display_name": display_name, "last_seen": datetime.datetime.utcnow().isoformat()}
        return True

def remove_peer_socket(peer_id):
    with peers_lock:
        if peer_id in peers:
            try:
                peers[peer_id]["conn"].close()
            except Exception:
                pass
            peers.pop(peer_id, None)

def get_peer_conn_by_peerid(peer_id):
    with peers_lock:
        p = peers.get(peer_id)
        return p["conn"] if p else None

def list_connected_peers():
    with peers_lock:
        return list(peers.items())

# ------------------------------ DB helpers ------------------------------
def upsert_contact(peer_id, number, display_name, ip, port):
    now = datetime.datetime.utcnow().isoformat()
    _cursor.execute("""
    INSERT INTO contacts (peer_id, number, display_name, ip, port, last_seen)
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(peer_id) DO UPDATE SET
      number=excluded.number,
      display_name=excluded.display_name,
      ip=excluded.ip,
      port=excluded.port,
      last_seen=excluded.last_seen
    """, (peer_id, number, display_name, ip, port, now))
    _conn.commit()

def add_message(peer_id, direction, msg_type, content, file_name=None, file_size=None, status="received"):
    ts = datetime.datetime.utcnow().isoformat()
    _cursor.execute("""
    INSERT INTO messages (peer_id, direction, type, content, file_name, file_size, timestamp, status)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (peer_id, direction, msg_type, content, file_name, file_size, ts, status))
    _conn.commit()

def get_contacts():
    _cursor.execute("SELECT peer_id, number, display_name, ip, port, last_seen FROM contacts ORDER BY last_seen DESC")
    return _cursor.fetchall()

def get_messages_for(peer_id):
    _cursor.execute("SELECT direction, type, content, file_name, timestamp FROM messages WHERE peer_id=? ORDER BY id", (peer_id,))
    return _cursor.fetchall()

# ------------------------------ NETWORK: DISCOVERY ------------------------------
stop_event = threading.Event()
incoming_ui_queue = queue.Queue()  # UI thread polls this to display incoming messages and events

def discovery_broadcast_thread():
    """Broadcast presence on the LAN."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    my_msg = {
        "type": "discover",
        "peer_id": config["peer_id"],
        "number": config["number"],
        "display_name": config["display_name"],
        "port": config["listen_port"]
    }
    payload = json.dumps(my_msg).encode(FORMAT)
    while not stop_event.is_set():
        try:
            s.sendto(payload, ("<broadcast>", DISCOVERY_PORT))
        except Exception:
            pass
        time.sleep(DISCOVERY_INTERVAL)
    s.close()

def discovery_listener_thread():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", DISCOVERY_PORT))
    s.settimeout(1.0)
    while not stop_event.is_set():
        try:
            data, addr = s.recvfrom(4096)
            j = json.loads(data.decode(FORMAT))
            if j.get("peer_id") == config["peer_id"]:
                continue
            # update DB contact (with IP from addr)
            peer_ip = addr[0]
            peer_port = j.get("port", DEFAULT_TCP_PORT)
            peer_id = j.get("peer_id")
            number = j.get("number")
            display = j.get("display_name")
            upsert_contact(peer_id, number, display, peer_ip, peer_port)
            incoming_ui_queue.put(("discovered", {"peer_id": peer_id, "number": number, "display_name": display, "ip": peer_ip, "port": peer_port}))
        except socket.timeout:
            continue
        except Exception:
            continue
    s.close()

# ------------------------------ NETWORK: TCP server & connector ------------------------------
def handle_connection_incoming(conn, addr):
    """
    Accept a connection, read handshake, respond with our handshake, then switch to recv loop for messages.
    """
    peer_id = None
    try:
        raw = read_frame(conn)
        if not raw:
            conn.close()
            return
        j = json.loads(raw.decode(FORMAT))
        if j.get("type") != "handshake":
            conn.close()
            return
        peer_id = j["peer_id"]
        peer_number = j.get("number")
        peer_display = j.get("display_name")
        peer_port = j.get("port", addr[1])
        add_peer_socket(peer_id, conn, addr[0], peer_port, peer_number, peer_display)
        upsert_contact(peer_id, peer_number, peer_display, addr[0], peer_port)
        # send our handshake response
        our = {"type": "handshake", "peer_id": config["peer_id"], "number": config["number"], "display_name": config["display_name"], "port": config["listen_port"]}
        send_frame(conn, json.dumps(our).encode(FORMAT))
        incoming_ui_queue.put(("connected", {"peer_id": peer_id, "number": peer_number, "display_name": peer_display}))
        # now receive frames
        recv_loop(conn, peer_id)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
    finally:
        if peer_id:
            remove_peer_socket(peer_id)
            incoming_ui_queue.put(("disconnected", {"peer_id": peer_id}))

def server_listen_thread():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((LOCAL_HOST, config["listen_port"]))
    except Exception as e:
        incoming_ui_queue.put(("error", f"Failed to bind to port {config['listen_port']}: {e}"))
        return
    s.listen(8)
    s.settimeout(1.0)
    incoming_ui_queue.put(("server_listening", config["listen_port"]))
    while not stop_event.is_set():
        try:
            conn, addr = s.accept()
            threading.Thread(target=handle_connection_incoming, args=(conn, addr), daemon=True).start()
        except socket.timeout:
            continue
        except Exception:
            continue
    try:
        s.close()
    except Exception:
        pass

def connector_thread_try_connect(peer_ip, peer_port, peer_id=None):
    try:
        c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        c.settimeout(4.0)
        c.connect((peer_ip, peer_port))
        c.settimeout(None)
        hs = {"type":"handshake", "peer_id": config["peer_id"], "number": config["number"], "display_name": config["display_name"], "port": config["listen_port"]}
        send_frame(c, json.dumps(hs).encode(FORMAT))
        raw = read_frame(c)
        if not raw:
            c.close()
            return False
        j = json.loads(raw.decode(FORMAT))
        if j.get("type") != "handshake":
            c.close()
            return False
        their_id = j.get("peer_id")
        their_num = j.get("number")
        their_display = j.get("display_name")
        added = add_peer_socket(their_id, c, peer_ip, peer_port, their_num, their_display)
        if not added:
            c.close()
            return False
        upsert_contact(their_id, their_num, their_display, peer_ip, peer_port)
        incoming_ui_queue.put(("connected", {"peer_id": their_id, "number": their_num, "display_name": their_display}))
        threading.Thread(target=recv_loop, args=(c, their_id), daemon=True).start()
        return True
    except Exception:
        try:
            c.close()
        except Exception:
            pass
        return False

def connector_auto_thread():
    while not stop_event.is_set():
        contacts = get_contacts()
        for (peer_id, number, display_name, ip, port, last_seen) in contacts:
            if peer_id == config["peer_id"]:
                continue
            with peers_lock:
                if peer_id in peers:
                    continue
            try:
                connector_thread_try_connect(ip, port, peer_id)
            except Exception:
                pass
        time.sleep(3)

# ------------------------------ RECV LOOP: message handling (extended with AV) ------------------------------
def recv_loop(conn, peer_id):
    """
    Handles incoming framed JSON messages:
      - text
      - file (metadata + next frame = raw file bytes)
      - audio (metadata frame then raw PCM frame)
      - video (metadata frame then raw JPEG frame)
    """
    try:
        while not stop_event.is_set():
            raw = read_frame(conn)
            if raw is None:
                break
            try:
                j = json.loads(raw.decode(FORMAT))
            except Exception:
                # not JSON — ignore
                continue
            mtype = j.get("type")
            if mtype == "text":
                body = j.get("body", "")
                add_message(peer_id, "in", "text", body, None, None, status="received")
                incoming_ui_queue.put(("message", {"peer_id": peer_id, "direction": "in", "type": "text", "content": body}))
            elif mtype == "file":
                fname = j.get("file_name")
                fsize = int(j.get("file_size", 0))
                ftype = j.get("file_type", "file")
                raw_file = read_frame(conn)
                if raw_file is None:
                    break
                if ftype == "image":
                    target = IMG_DIR / f"{uuid.uuid4().hex}_{fname}"
                elif ftype == "video":
                    target = VID_DIR / f"{uuid.uuid4().hex}_{fname}"
                else:
                    target = DOC_DIR / f"{uuid.uuid4().hex}_{fname}"
                with open(target, "wb") as wf:
                    wf.write(raw_file)
                add_message(peer_id, "in", "file", str(target), fname, len(raw_file), status="received")
                incoming_ui_queue.put(("message", {"peer_id": peer_id, "direction": "in", "type": "file", "content": str(target), "file_name": fname}))
            elif mtype == "audio":
                raw_audio = read_frame(conn)
                if raw_audio:
                    try:
                        arr = np.frombuffer(raw_audio, dtype=np.int16)
                        threading.Thread(target=lambda a: sd.play(a, AUDIO_RATE), args=(arr,), daemon=True).start()
                    except Exception:
                        pass
            elif mtype == "video":
                raw_vid = read_frame(conn)
                if raw_vid:
                    try:
                        incoming_ui_queue.put(("video_frame", {"peer_id": peer_id, "jpeg": raw_vid}))
                    except Exception:
                        pass
            elif mtype == "presence":
                upsert_contact(j.get("peer_id"), j.get("number"), j.get("display_name"), j.get("ip"), j.get("port"))
            else:
                pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
        remove_peer_socket(peer_id)
        incoming_ui_queue.put(("disconnected", {"peer_id": peer_id}))

# ------------------------------ SENDER helpers (extended with AV) ------------------------------
def send_text_to_peer(peer_id, text):
    conn = get_peer_conn_by_peerid(peer_id)
    if not conn:
        return False
    j = {"type":"text", "body": text}
    try:
        send_frame(conn, json.dumps(j).encode(FORMAT))
        add_message(peer_id, "out", "text", text, None, None, status="sent")
        return True
    except Exception:
        remove_peer_socket(peer_id)
        return False

def send_file_to_peer(peer_id, path, file_type="file"):
    conn = get_peer_conn_by_peerid(peer_id)
    if not conn:
        return False
    p = Path(path)
    if not p.exists():
        return False
    data = p.read_bytes()
    meta = {"type":"file", "file_name": p.name, "file_size": len(data), "file_type": file_type}
    try:
        send_frame(conn, json.dumps(meta).encode(FORMAT))
        send_frame(conn, data)
        add_message(peer_id, "out", "file", str(p), p.name, len(data), status="sent")
        return True
    except Exception:
        remove_peer_socket(peer_id)
        return False

# --- Audio send ---
audio_send_threads = {}  # peer_id -> thread info
audio_stream_controls = {}  # peer_id -> stop_event for that stream

def start_audio_call(peer_id):
    """Start sending microphone audio to peer_id."""
    if peer_id in audio_send_threads:
        return False
    conn = get_peer_conn_by_peerid(peer_id)
    if not conn:
        return False
    stop_evt = threading.Event()
    audio_stream_controls[peer_id] = stop_evt

    def callback(indata, frames, timeinfo, status):
        try:
            arr = (indata * 32767).astype(np.int16)
            data = arr.tobytes()
            meta = {"type":"audio", "size": len(data)}
            try:
                send_frame(conn, json.dumps(meta).encode(FORMAT))
                send_frame(conn, data)
            except Exception:
                stop_evt.set()
        except Exception:
            pass

    def run_input():
        try:
            with sd.InputStream(samplerate=AUDIO_RATE, channels=AUDIO_CHANNELS, dtype='float32', blocksize=AUDIO_BLOCK_FRAMES, callback=callback):
                while not stop_evt.is_set() and not stop_event.is_set():
                    sd.sleep(100)
        except Exception:
            pass
        finally:
            audio_send_threads.pop(peer_id, None)
            audio_stream_controls.pop(peer_id, None)

    t = threading.Thread(target=run_input, daemon=True)
    audio_send_threads[peer_id] = t
    t.start()
    return True

def stop_audio_call(peer_id):
    ev = audio_stream_controls.get(peer_id)
    if ev:
        ev.set()
    return True

# --- Video send ---
video_send_threads = {}
video_stream_controls = {}

def start_video_call(peer_id):
    if peer_id in video_send_threads:
        return False
    conn = get_peer_conn_by_peerid(peer_id)
    if not conn:
        return False
    stop_evt = threading.Event()
    video_stream_controls[peer_id] = stop_evt

    def run_capture():
        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            stop_evt.set()
            return
        try:
            while not stop_evt.is_set() and not stop_event.is_set():
                ret, frame = cap.read()
                if not ret:
                    break
                h, w = frame.shape[:2]
                max_w = 640
                if w > max_w:
                    scale = max_w / w
                    frame = cv2.resize(frame, (int(w*scale), int(h*scale)))
                ret2, enc = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), VIDEO_QUALITY])
                if not ret2:
                    continue
                data = enc.tobytes()
                meta = {"type":"video", "size": len(data)}
                try:
                    send_frame(conn, json.dumps(meta).encode(FORMAT))
                    send_frame(conn, data)
                except Exception:
                    break
                time.sleep(1.0 / VIDEO_FPS)
        finally:
            try:
                cap.release()
            except Exception:
                pass
            video_send_threads.pop(peer_id, None)
            video_stream_controls.pop(peer_id, None)

    t = threading.Thread(target=run_capture, daemon=True)
    video_send_threads[peer_id] = t
    t.start()
    return True

def stop_video_call(peer_id):
    ev = video_stream_controls.get(peer_id)
    if ev:
        ev.set()
    return True

# ------------------------------ UI ------------------------------
# Helper to make a solid color placeholder image for preview to avoid grey box
def make_placeholder_image(width, height, color=(240,240,240)):
    img = Image.new("RGB", (width, height), color)
    return ImageTk.PhotoImage(img)

class MyChatApp:
    def __init__(self, root):
        self.root = root
        root.title("MyChat — P2P Prototype (AV Enabled)")
        root.geometry("1000x650")
        # Configure a pleasant theme for ttk widgets
        self.setup_theme()
        self.setup_ui()
        self.root.after(200, self.poll_incoming_queue)
        self.video_last_frames = {}
        self.root.after(50, self.video_render_loop)

    def setup_theme(self):
        style = ttk.Style(self.root)
        # try to use default theme and tweak fonts/colors
        try:
            style.theme_use('clam')
        except Exception:
            pass
        style.configure("TButton", padding=6, font=("Segoe UI", 10))
        style.configure("TLabel", font=("Segoe UI", 10))
        style.configure("Header.TLabel", font=("Segoe UI", 12, "bold"))
        style.configure("Search.TEntry", padding=4)
        self.bg_color = "#FFFFFF"
        self.panel_bg = "#F6F6F6"

    def setup_ui(self):
        # Top frame
        top_frame = ttk.Frame(self.root)
        top_frame.pack(side="top", fill="x")
        title = ttk.Label(top_frame, text="MyChat (AV)", style="Header.TLabel")
        title.pack(side="left", padx=8, pady=6)

        btn_container = ttk.Frame(top_frame)
        btn_container.pack(side="right", padx=8)
        self.add_contact_btn = ttk.Button(btn_container, text="Add Contact", command=self.add_contact_dialog)
        self.add_contact_btn.pack(side="left", padx=4)
        self.btn_send_file = ttk.Button(btn_container, text="Send File", command=self.send_file_current)
        self.btn_send_file.pack(side="left", padx=4)

        self.lbl_status = ttk.Label(top_frame, text="Starting...", foreground="gray")
        self.lbl_status.pack(side="right", padx=12)

        # Main paned window
        main = ttk.Panedwindow(self.root, orient="horizontal")
        main.pack(fill="both", expand=True)

        # Left: contacts
        left_frame = ttk.Frame(main, width=300)
        main.add(left_frame, weight=0)

        search_entry = ttk.Entry(left_frame, textvariable=tk.StringVar(), style="Search.TEntry")
        # We'll attach a StringVar to it later in refresh
        search_entry.pack(fill="x", padx=8, pady=6)
        self.search_var = tk.StringVar()
        search_entry.config(textvariable=self.search_var)
        search_entry.bind("<KeyRelease>", lambda e: self.refresh_contacts())

        self.contacts_listbox = tk.Listbox(left_frame, highlightthickness=0, bd=0)
        self.contacts_listbox.pack(fill="both", expand=True, padx=8, pady=6)
        self.contacts_listbox.bind("<<ListboxSelect>>", self.on_contact_select)

        # Right: chat and controls
        right_frame = ttk.Frame(main)
        main.add(right_frame, weight=1)

        header_frame = ttk.Frame(right_frame)
        header_frame.pack(fill="x", pady=(6,0))
        self.chat_title = ttk.Label(header_frame, text="No chat selected", style="Header.TLabel")
        self.chat_title.pack(side="left", anchor="w", padx=8)

        # call buttons
        call_btns = ttk.Frame(header_frame)
        call_btns.pack(side="right", padx=6)
        self.voice_btn = ttk.Button(call_btns, text="📞 Voice Call", command=self.toggle_voice_call, state="disabled")
        self.voice_btn.pack(side="left", padx=4)
        self.video_btn = ttk.Button(call_btns, text="📹 Video Call", command=self.toggle_video_call, state="disabled")
        self.video_btn.pack(side="left", padx=4)

        # Chat text area (use normal Text inside a frame)
        chat_container = tk.Frame(right_frame, bg=self.bg_color, bd=0)
        chat_container.pack(fill="both", expand=True, padx=8, pady=6)

        self.chat_box = tk.Text(chat_container, state="disabled", wrap="word", bg=self.bg_color, bd=0, relief="flat")
        self.chat_box.pack(fill="both", expand=True, side="left")

        # Right-side column for preview and small info
        side_col = ttk.Frame(chat_container, width=340)
        side_col.pack(side="right", fill="y", padx=(8,0))
        side_col.pack_propagate(False)

        # Create a labeled preview frame with a fixed size, background same as chat
        preview_label = ttk.Label(side_col, text="Preview", anchor="center")
        preview_label.pack(fill="x")

        preview_frame = tk.Frame(side_col, width=320, height=180, bg=self.panel_bg, bd=1, relief="solid")
        preview_frame.pack(pady=6)
        preview_frame.pack_propagate(False)

        # Preview image holder (Label)
        self.video_preview_label = tk.Label(preview_frame, bg=self.panel_bg, bd=0)
        self.video_preview_label.pack(fill="both", expand=True)
        # placeholder image so it doesn't show a grey uninitialized box
        self.preview_placeholder = make_placeholder_image(320, 180, color=(246,246,246))
        self.video_preview_label.configure(image=self.preview_placeholder)
        self.video_preview_label.image = self.preview_placeholder  # keep reference

        # bottom: message entry
        bottom = ttk.Frame(right_frame)
        bottom.pack(fill="x", side="bottom", padx=8, pady=8)
        self.msg_entry = ttk.Entry(bottom, textvariable=tk.StringVar())
        self.msg_entry.pack(side="left", fill="x", expand=True, padx=4)
        self.msg_entry.bind("<Return>", lambda e: self.send_message())

        self.send_btn = ttk.Button(bottom, text="Send", command=self.send_message)
        self.send_btn.pack(side="right", padx=4)

        # internal
        self.selected_peer_id = None
        self.refresh_contacts()

    # ---------------- UI actions ----------------
    def add_contact_dialog(self):
        def on_ok():
            number = ent_num.get().strip()
            display = ent_name.get().strip() or number
            port = int(ent_port.get().strip() or DEFAULT_TCP_PORT)
            temp_id = f"local:{number}"
            upsert_contact(temp_id, number, display, "", port)
            self.refresh_contacts()
            dlg.destroy()
        dlg = tk.Toplevel(self.root)
        dlg.title("Add contact")
        dlg.geometry("320x200")
        tk.Label(dlg, text="Number:").pack(padx=8, pady=4, anchor="w")
        ent_num = tk.Entry(dlg)
        ent_num.pack(padx=8, pady=4, fill="x")
        tk.Label(dlg, text="Display name (optional):").pack(padx=8, pady=4, anchor="w")
        ent_name = tk.Entry(dlg)
        ent_name.pack(padx=8, pady=4, fill="x")
        tk.Label(dlg, text="Listen port for contact (LAN):").pack(padx=8, pady=4, anchor="w")
        ent_port = tk.Entry(dlg)
        ent_port.insert(0, str(DEFAULT_TCP_PORT))
        ent_port.pack(padx=8, pady=4, fill="x")
        ttk.Button(dlg, text="Add", command=on_ok).pack(pady=8)

    def refresh_contacts(self):
        self.contacts_listbox.delete(0, "end")
        rows = get_contacts()
        q = self.search_var.get().lower().strip() if self.search_var.get() else ""
        for (peer_id, number, display_name, ip, port, last_seen) in rows:
            label = f"{display_name or number} ({number})"
            if q and q not in label.lower():
                continue
            self.contacts_listbox.insert("end", f"{peer_id}||{label}")

    def on_contact_select(self, event):
        sel = self.contacts_listbox.curselection()
        if not sel:
            return
        raw = self.contacts_listbox.get(sel[0])
        peer_id = raw.split("||",1)[0]
        self.open_chat(peer_id)

    def open_chat(self, peer_id):
        self.selected_peer_id = peer_id
        _cursor.execute("SELECT display_name, number FROM contacts WHERE peer_id=?", (peer_id,))
        r = _cursor.fetchone()
        if r:
            self.chat_title.config(text=f"{r[0]} ({r[1]})")
        else:
            self.chat_title.config(text="Unknown")
        # enable call buttons if connected
        with peers_lock:
            connected = peer_id in peers
        state = "normal" if connected else "disabled"
        if state == "normal":
            self.voice_btn.state(["!disabled"])
            self.video_btn.state(["!disabled"])
        else:
            self.voice_btn.state(["disabled"])
            self.video_btn.state(["disabled"])

        self.chat_box.config(state="normal")
        self.chat_box.delete("1.0", "end")
        rows = get_messages_for(peer_id)
        for (direction, mtype, content, file_name, ts) in rows:
            t = ts.split("T")[0] if ts else ""
            if mtype == "text":
                line = f"[{t}] {'You' if direction=='out' else 'Them'}: {content}\n"
            else:
                line = f"[{t}] {'You' if direction=='out' else 'Them'}: <{mtype}:{file_name}> - {content}\n"
            self.chat_box.insert("end", line)
        self.chat_box.config(state="disabled")
        self.chat_box.see("end")

    def send_message(self):
        txt = self.msg_entry.get().strip()
        if not txt or not self.selected_peer_id:
            return
        success = send_text_to_peer(self.selected_peer_id, txt)
        if not success:
            messagebox.showwarning("Not connected", "Peer not connected. Try again later or click contact to connect.")
        else:
            self.chat_box.config(state="normal")
            self.chat_box.insert("end", f"You: {txt}\n")
            self.chat_box.config(state="disabled")
            self.msg_entry.delete(0, "end")
            self.chat_box.see("end")

    def send_file_current(self):
        if not self.selected_peer_id:
            messagebox.showinfo("Select contact", "Select a contact first")
            return
        path = filedialog.askopenfilename()
        if not path:
            return
        ext = Path(path).suffix.lower()
        if ext in (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"):
            ftype = "image"
        elif ext in (".mp4", ".mov", ".mkv", ".avi"):
            ftype = "video"
        else:
            ftype = "file"
        ok = send_file_to_peer(self.selected_peer_id, path, file_type=ftype)
        if ok:
            self.chat_box.config(state="normal")
            self.chat_box.insert("end", f"You: <file:{Path(path).name}>\n")
            self.chat_box.config(state="disabled")
            self.chat_box.see("end")
        else:
            messagebox.showwarning("Send failed", "Failed to send file. Peer may be offline.")

    # ---------------- Voice / Video call controls ----------------
    def toggle_voice_call(self):
        pid = self.selected_peer_id
        if not pid:
            return
        if pid in audio_send_threads:
            stop_audio_call(pid)
            self.voice_btn.config(text="📞 Voice Call")
            self.lbl_status.config(text=f"Voice call stopped")
        else:
            ok = start_audio_call(pid)
            if ok:
                self.voice_btn.config(text="⛔ Stop Voice")
                self.lbl_status.config(text=f"In voice call with {pid}")
            else:
                messagebox.showwarning("Call failed", "Unable to start voice call. Peer may be offline.")

    def toggle_video_call(self):
        pid = self.selected_peer_id
        if not pid:
            return
        if pid in video_send_threads:
            stop_video_call(pid)
            self.video_btn.config(text="📹 Video Call")
            self.lbl_status.config(text=f"Video call stopped")
            # reset preview to placeholder
            self.video_preview_label.configure(image=self.preview_placeholder)
            self.video_preview_label.image = self.preview_placeholder
        else:
            ok = start_video_call(pid)
            if ok:
                self.video_btn.config(text="⛔ Stop Video")
                self.lbl_status.config(text=f"In video call with {pid}")
            else:
                messagebox.showwarning("Call failed", "Unable to start video call. Peer may be offline.")

    # ---------------- UI: polling incoming queue ----------------
    def poll_incoming_queue(self):
        while True:
            try:
                ev, data = incoming_ui_queue.get_nowait()
            except queue.Empty:
                break
            if ev == "discovered":
                upsert_contact(data["peer_id"], data["number"], data["display_name"], data["ip"], data["port"])
                self.lbl_status.config(text=f"Discovered {data['display_name'] or data['number']}")
                self.refresh_contacts()
            elif ev == "connected":
                self.lbl_status.config(text=f"Connected to {data.get('display_name') or data.get('number')}")
                self.refresh_contacts()
            elif ev == "disconnected":
                self.lbl_status.config(text=f"Peer disconnected")
                self.refresh_contacts()
            elif ev == "message":
                pid = data["peer_id"]
                if pid == self.selected_peer_id:
                    if data["type"] == "text":
                        self.chat_box.config(state="normal")
                        self.chat_box.insert("end", f"Them: {data['content']}\n")
                        self.chat_box.config(state="disabled")
                        self.chat_box.see("end")
                    elif data["type"] == "file":
                        self.chat_box.config(state="normal")
                        self.chat_box.insert("end", f"Them: <file:{data.get('file_name')}> saved: {data['content']}\n")
                        self.chat_box.config(state="disabled")
                        self.chat_box.see("end")
                else:
                    self.lbl_status.config(text=f"Message from {pid}... click contact")
                self.refresh_contacts()
            elif ev == "video_frame":
                pid = data["peer_id"]
                try:
                    np_arr = np.frombuffer(data["jpeg"], dtype=np.uint8)
                    frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    img = Image.fromarray(frame)
                    img = img.resize((320, 180))
                    imgtk = ImageTk.PhotoImage(img)
                    self.video_last_frames[pid] = imgtk
                except Exception:
                    pass
            elif ev == "server_listening":
                self.lbl_status.config(text=f"Listening on port {data}")
            elif ev == "error":
                messagebox.showerror("Error", str(data))
            else:
                pass
        self.root.after(200, self.poll_incoming_queue)

    def video_render_loop(self):
        # display last frame if available, otherwise keep placeholder
        pid = self.selected_peer_id
        if pid and pid in self.video_last_frames:
            try:
                imgtk = self.video_last_frames.get(pid)
                if imgtk:
                    self.video_preview_label.configure(image=imgtk)
                    self.video_preview_label.image = imgtk
            except Exception:
                pass
        self.root.after(50, self.video_render_loop)

# ------------------------------ LOGIN UI (first-run) ------------------------------
def show_login_then_start():
    root = tk.Tk()
    root.title("MyChat Login")
    root.geometry("360x260")
    root.resizable(False, False)

    ttk.Label(root, text="Welcome to MyChat (P2P + AV)", font=("Segoe UI", 11, "bold")).pack(pady=8)
    ttk.Label(root, text="Phone Number:").pack(anchor="w", padx=12)
    ent_num = ttk.Entry(root)
    ent_num.pack(fill="x", padx=12)
    ent_num.insert(0, config.get("number",""))

    ttk.Label(root, text="Display Name:").pack(anchor="w", padx=12, pady=(8,0))
    ent_name = ttk.Entry(root)
    ent_name.pack(fill="x", padx=12)
    ent_name.insert(0, config.get("display_name",""))

    ttk.Label(root, text="Listen Port (for local testing / different instances):").pack(anchor="w", padx=12, pady=(8,0))
    ent_port = ttk.Entry(root)
    ent_port.pack(fill="x", padx=12)
    ent_port.insert(0, str(config.get("listen_port", DEFAULT_TCP_PORT)))

    def on_ok():
        number = ent_num.get().strip()
        name = ent_name.get().strip() or number
        try:
            port = int(ent_port.get().strip())
        except Exception:
            messagebox.showerror("Invalid port", "Enter a valid number for port")
            return
        config["number"] = number
        config["display_name"] = name
        config["listen_port"] = port
        save_config(config)
        root.destroy()
        start_network_and_ui()

    ttk.Button(root, text="Start MyChat", command=on_ok).pack(pady=12)
    root.mainloop()

# ------------------------------ STARTUP: threads & UI ------------------------------
def start_network_and_ui():
    threading.Thread(target=discovery_broadcast_thread, daemon=True).start()
    threading.Thread(target=discovery_listener_thread, daemon=True).start()
    threading.Thread(target=server_listen_thread, daemon=True).start()
    threading.Thread(target=connector_auto_thread, daemon=True).start()
    root = tk.Tk()
    app = MyChatApp(root)
    root.protocol("WM_DELETE_WINDOW", lambda: on_close(root))
    root.mainloop()

def on_close(root):
    if messagebox.askokcancel("Quit", "Do you want to quit MyChat?"):
        stop_event.set()
        with peers_lock:
            for pid, info in list(peers.items()):
                try:
                    info["conn"].close()
                except Exception:
                    pass
        try:
            _conn.close()
        except Exception:
            pass
        root.destroy()

# ------------------------------ RUN ------------------------------
if __name__ == "__main__":
    if config.get("number") and config.get("display_name"):
        start_network_and_ui()
    else:
        show_login_then_start()
