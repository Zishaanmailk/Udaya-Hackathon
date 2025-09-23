"""
mychat.py
A self-contained P2P chat prototype with Tkinter UI, SQLite persistence,
LAN discovery (UDP broadcast), TCP handshake & message framing, and file send/receive.

Features:
- Login screen (phone number, display name, listen port)
- Contacts list (auto-updated via discovery and handshake)
- Chat window per contact (send text + send file)
- Local storage in MyChat/Database/chat.db and media in MyChat/Media/*
- P2P connections with handshake (peer_id, number, display_name)
- Multi-peer support and broadcast sending to each connected peer
- Clean threading and UI updates via queue
"""

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
        direction TEXT,  -- 'in' or 'out'
        type TEXT,       -- 'text','file'
        content TEXT,    -- text or file path
        file_name TEXT,
        file_size INTEGER,
        timestamp TEXT,
        status TEXT
    )""")
    _conn.commit()

init_db()

# ------------------------------ IN-MEMORY PEER MANAGEMENT ------------------------------
peers = {}  # peer_id -> {conn, addr, ip, port, last_seen, number, display_name}
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
    Handshake is a framed JSON message: {"type":"handshake","peer_id":..., "number":..., "display_name":...}
    """
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
    """
    Try to connect to peer ip:port, perform handshake, then start recv_loop.
    This is run when we explicitly want to connect (user click or discovery).
    """
    try:
        c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        c.settimeout(4.0)
        c.connect((peer_ip, peer_port))
        c.settimeout(None)
        # send handshake
        hs = {"type":"handshake", "peer_id": config["peer_id"], "number": config["number"], "display_name": config["display_name"], "port": config["listen_port"]}
        send_frame(c, json.dumps(hs).encode(FORMAT))
        # read handshake response
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
        # add to peers
        added = add_peer_socket(their_id, c, peer_ip, peer_port, their_num, their_display)
        if not added:
            c.close()
            return False
        upsert_contact(their_id, their_num, their_display, peer_ip, peer_port)
        incoming_ui_queue.put(("connected", {"peer_id": their_id, "number": their_num, "display_name": their_display}))
        # spawn recv loop on this socket
        threading.Thread(target=recv_loop, args=(c, their_id), daemon=True).start()
        return True
    except Exception:
        try:
            c.close()
        except Exception:
            pass
        return False

def connector_auto_thread():
    """Periodically look into DB contacts and attempt connection to any known ip/port not connected yet."""
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

# ------------------------------ RECV LOOP: message handling ------------------------------
def recv_loop(conn, peer_id):
    """Main receive loop for connected socket. Expects framed JSON messages.
    Message types:
      - {"type":"text","body":"hello"}
      - {"type":"file","file_name": "img.jpg", "file_size": N, "file_type":"image"}
    For file: after the JSON metadata frame, the peer sends a binary frame with exact file_size bytes (framed).
    """
    try:
        while not stop_event.is_set():
            raw = read_frame(conn)
            if raw is None:
                break
            # try decode as JSON
            try:
                j = json.loads(raw.decode(FORMAT))
            except Exception:
                # not JSON — ignore
                continue
            mtype = j.get("type")
            if mtype == "text":
                body = j.get("body", "")
                # save to DB
                add_message(peer_id, "in", "text", body, None, None, status="received")
                incoming_ui_queue.put(("message", {"peer_id": peer_id, "direction": "in", "type": "text", "content": body}))
            elif mtype == "file":
                fname = j.get("file_name")
                fsize = int(j.get("file_size", 0))
                ftype = j.get("file_type", "file")
                # read next raw bytes (framed payload expected)
                raw_file = read_frame(conn)
                if raw_file is None:
                    break
                # write to file in media folder
                # choose subdir
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
            elif mtype == "presence":
                # optional: update last seen
                upsert_contact(j.get("peer_id"), j.get("number"), j.get("display_name"), j.get("ip"), j.get("port"))
            else:
                # unknown type: ignore
                pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
        remove_peer_socket(peer_id)
        incoming_ui_queue.put(("disconnected", {"peer_id": peer_id}))

# ------------------------------ SENDER helpers ------------------------------
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
        # send raw payload framed as well (so other end uses read_frame)
        send_frame(conn, data)
        add_message(peer_id, "out", "file", str(p), p.name, len(data), status="sent")
        return True
    except Exception:
        remove_peer_socket(peer_id)
        return False

# ------------------------------ UI ------------------------------
class MyChatApp:
    def __init__(self, root):
        self.root = root
        root.title("MyChat — P2P Prototype")
        root.geometry("900x600")
        self.setup_ui()
        # polling queue for incoming events
        self.root.after(200, self.poll_incoming_queue)

    def setup_ui(self):
        # Top menu & toolbar
        top_frame = tk.Frame(self.root)
        top_frame.pack(side="top", fill="x")
        title = tk.Label(top_frame, text="MyChat", font=("Arial",14,"bold"))
        title.pack(side="left", padx=8, pady=6)
        self.add_contact_btn = tk.Button(top_frame, text="Add Contact", command=self.add_contact_dialog)
        self.add_contact_btn.pack(side="right", padx=8)
        self.btn_send_file = tk.Button(top_frame, text="Send File", command=self.send_file_current)
        self.btn_send_file.pack(side="right", padx=8)
        self.lbl_status = tk.Label(top_frame, text="Starting...", fg="gray")
        self.lbl_status.pack(side="right", padx=12)

        # Main split: left contacts, right chat area
        main = tk.PanedWindow(self.root, sashrelief="sunken")
        main.pack(fill="both", expand=True)

        # Left: contacts list
        left_frame = tk.Frame(main, width=260)
        main.add(left_frame)
        self.search_var = tk.StringVar()
        search_entry = tk.Entry(left_frame, textvariable=self.search_var)
        search_entry.pack(fill="x", padx=8, pady=6)
        search_entry.bind("<KeyRelease>", lambda e: self.refresh_contacts())

        self.contacts_listbox = tk.Listbox(left_frame)
        self.contacts_listbox.pack(fill="both", expand=True, padx=8, pady=6)
        self.contacts_listbox.bind("<<ListboxSelect>>", self.on_contact_select)

        # Right: chat area
        right_frame = tk.Frame(main)
        main.add(right_frame, stretch="always")

        self.chat_title = tk.Label(right_frame, text="No chat selected", font=("Arial",12,"bold"))
        self.chat_title.pack(anchor="w", padx=8, pady=6)

        self.chat_box = tk.Text(right_frame, state="disabled", wrap="word")
        self.chat_box.pack(fill="both", expand=True, padx=8, pady=6)

        bottom = tk.Frame(right_frame)
        bottom.pack(fill="x", side="bottom", padx=8, pady=8)
        self.msg_entry = tk.Entry(bottom)
        self.msg_entry.pack(side="left", fill="x", expand=True, padx=4)
        self.msg_entry.bind("<Return>", lambda e: self.send_message())

        self.send_btn = tk.Button(bottom, text="Send", bg="#25D366", command=self.send_message)
        self.send_btn.pack(side="right", padx=4)

        # internal state
        self.selected_peer_id = None
        self.refresh_contacts()

    # ---------------- UI actions ----------------
    def add_contact_dialog(self):
        def on_ok():
            number = ent_num.get().strip()
            display = ent_name.get().strip() or number
            port = int(ent_port.get().strip() or DEFAULT_TCP_PORT)
            # create local contact entry (peer_id unknown yet); we store by temporary peer_id of number
            # use number as id placeholder (but better to store by peer_id after handshake)
            temp_id = f"local:{number}"
            upsert_contact(temp_id, number, display, "", port)
            self.refresh_contacts()
            dlg.destroy()
        dlg = tk.Toplevel(self.root)
        dlg.title("Add contact")
        tk.Label(dlg, text="Number:").pack(padx=8, pady=4)
        ent_num = tk.Entry(dlg)
        ent_num.pack(padx=8, pady=4)
        tk.Label(dlg, text="Display name (optional):").pack(padx=8, pady=4)
        ent_name = tk.Entry(dlg)
        ent_name.pack(padx=8, pady=4)
        tk.Label(dlg, text="Listen port for contact (LAN):").pack(padx=8, pady=4)
        ent_port = tk.Entry(dlg)
        ent_port.insert(0, str(DEFAULT_TCP_PORT))
        ent_port.pack(padx=8, pady=4)
        tk.Button(dlg, text="Add", command=on_ok).pack(pady=8)

    def refresh_contacts(self):
        self.contacts_listbox.delete(0, "end")
        rows = get_contacts()
        q = self.search_var.get().lower().strip()
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
        # load display name
        _cursor.execute("SELECT display_name, number FROM contacts WHERE peer_id=?", (peer_id,))
        r = _cursor.fetchone()
        if r:
            self.chat_title.config(text=f"{r[0]} ({r[1]})")
        else:
            self.chat_title.config(text="Unknown")
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
            # append in UI
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
        # determine basic file type by extension (image if jpg/png/gif; video if mp4/mkv; else doc)
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

    # ---------------- UI: polling incoming queue ----------------
    def poll_incoming_queue(self):
        while True:
            try:
                ev, data = incoming_ui_queue.get_nowait()
            except queue.Empty:
                break
            if ev == "discovered":
                # update contact list
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
                # if chat open with this peer, append; else update contact last seen and show notification
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
                    # not currently open: brief status update
                    self.lbl_status.config(text=f"Message from {pid}... click contact")
                self.refresh_contacts()
            elif ev == "server_listening":
                self.lbl_status.config(text=f"Listening on port {data}")
            elif ev == "error":
                messagebox.showerror("Error", str(data))
            else:
                # unknown event
                pass
        # schedule next poll
        self.root.after(200, self.poll_incoming_queue)

# ------------------------------ LOGIN UI (first-run) ------------------------------
def show_login_then_start():
    root = tk.Tk()
    root.title("MyChat Login")
    root.geometry("350x220")

    tk.Label(root, text="Welcome to MyChat (P2P)").pack(pady=6)
    tk.Label(root, text="Phone Number:").pack(anchor="w", padx=12)
    ent_num = tk.Entry(root)
    ent_num.pack(fill="x", padx=12)
    ent_num.insert(0, config.get("number",""))

    tk.Label(root, text="Display Name:").pack(anchor="w", padx=12, pady=(8,0))
    ent_name = tk.Entry(root)
    ent_name.pack(fill="x", padx=12)
    ent_name.insert(0, config.get("display_name",""))

    tk.Label(root, text="Listen Port (for local testing / different instances):").pack(anchor="w", padx=12, pady=(8,0))
    ent_port = tk.Entry(root)
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

    tk.Button(root, text="Start MyChat", command=on_ok).pack(pady=12)
    root.mainloop()

# ------------------------------ STARTUP: threads & UI ------------------------------
def start_network_and_ui():
    # start discovery and server and connector threads
    threading.Thread(target=discovery_broadcast_thread, daemon=True).start()
    threading.Thread(target=discovery_listener_thread, daemon=True).start()
    threading.Thread(target=server_listen_thread, daemon=True).start()
    threading.Thread(target=connector_auto_thread, daemon=True).start()
    # start Tkinter main app
    root = tk.Tk()
    app = MyChatApp(root)
    root.protocol("WM_DELETE_WINDOW", lambda: on_close(root))
    root.mainloop()

def on_close(root):
    if messagebox.askokcancel("Quit", "Do you want to quit MyChat?"):
        stop_event.set()
        # close all sockets
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
    # If user already has number & name, skip login; else show login
    if config.get("number") and config.get("display_name"):
        # still allow custom listen port if desired (keeps existing)
        start_network_and_ui()
    else:
        show_login_then_start()
