import socket
import threading
import time

FORMAT = 'utf-8'
HEADER = 64
LOCAL_HOST = 'localhost'

MY_PORT = 5052
PEER_HOST = 'localhost'
PEER_PORT = 5051

peers = []               # list of (conn, addr)
peers_lock = threading.Lock()
stop_event = threading.Event()


def add_peer(conn, addr):
    """Add peer if not already present (avoid exact-addr duplicates)."""
    with peers_lock:
        for c, a in peers:
            # check identical socket object or identical remote addr:port
            if c is conn or (a[0] == addr[0] and a[1] == addr[1]):
                return False
        peers.append((conn, addr))
        return True


def remove_peer(conn):
    with peers_lock:
        for i, (c, a) in enumerate(peers):
            if c is conn:
                peers.pop(i)
                break


def handle_recv(conn, addr):
    """Receive loop for a single connection (runs in its own thread)."""
    try:
        while not stop_event.is_set():
            msg_length_bytes = conn.recv(HEADER)
            if not msg_length_bytes:
                # remote closed
                break
            try:
                msg_length = int(msg_length_bytes.decode(FORMAT).strip())
            except ValueError:
                # malformed header -> drop
                break
            if msg_length == 0:
                continue
            data = conn.recv(msg_length).decode(FORMAT, errors='replace')
            # Print incoming message on its own line and reprint prompt
            print(f"\n[{addr[0]}:{addr[1]}] {data}\nYou: ", end='', flush=True)
            if data.lower() == "exit":
                stop_event.set()
                break
    except Exception:
        pass
    finally:
        remove_peer(conn)
        try:
            conn.close()
        except Exception:
            pass
        print(f"\n[DISCONNECTED] {addr}\nYou: ", end='', flush=True)


def server_thread():
    """Listens and accepts incoming connections."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((LOCAL_HOST, MY_PORT))
    s.listen()
    print(f"[LISTENING] Local peer listening on {LOCAL_HOST}:{MY_PORT}")

    while not stop_event.is_set():
        try:
            s.settimeout(1.0)
            conn, addr = s.accept()
        except socket.timeout:
            continue
        except Exception:
            break

        added = add_peer(conn, addr)
        if not added:
            # duplicate connection: close new socket
            try:
                conn.close()
            except Exception:
                pass
            continue

        # start recv thread for this connection
        threading.Thread(target=handle_recv, args=(conn, addr), daemon=True).start()
        print(f"[INCOMING] connection from {addr}")


def connector_thread():
    """Tries to connect to the configured peer; retries until success. Adds connection and starts its recv thread."""
    while not stop_event.is_set():
        # if we already have a connection to that peer, skip
        with peers_lock:
            exists = any(a[0] == PEER_HOST and a[1] == PEER_PORT for c, a in peers)
        if exists:
            time.sleep(1)
            continue

        try:
            c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            c.settimeout(3)
            c.connect((PEER_HOST, PEER_PORT))
            c.settimeout(None)
            added = add_peer(c, (PEER_HOST, PEER_PORT))
            if not added:
                c.close()
            else:
                threading.Thread(target=handle_recv, args=(c, (PEER_HOST, PEER_PORT)), daemon=True).start()
                print(f"[OUTGOING] connected to {(PEER_HOST, PEER_PORT)}")
                # once connected, no need to loop immediately
        except (ConnectionRefusedError, OSError):
            # peer not ready yet -> retry
            time.sleep(1)
            continue
        except Exception:
            time.sleep(1)
            continue


def sender_thread():
    """Reads input and broadcasts to all connected peers."""
    try:
        while not stop_event.is_set():
            try:
                msg = input("You: ")
            except EOFError:
                break
            if msg is None:
                continue
            if msg == "":
                continue

            payload = msg.encode(FORMAT)
            hdr = str(len(payload)).encode(FORMAT)
            hdr += b' ' * (HEADER - len(hdr))

            with peers_lock:
                # copy list to avoid modification while iterating
                current_peers = list(peers)

            if not current_peers:
                print("[INFO] No peers connected. Message not sent.")
            for conn, addr in current_peers:
                try:
                    conn.sendall(hdr)
                    conn.sendall(payload)
                except Exception:
                    # connection problem -> remove peer
                    remove_peer(conn)
                    try:
                        conn.close()
                    except Exception:
                        pass
                    print(f"[INFO] Lost connection to {addr}")

            if msg.lower() == "exit":
                stop_event.set()
                break
    finally:
        stop_event.set()


def main():
    # start server (accept incoming)
    threading.Thread(target=server_thread, daemon=True).start()
    # start connector (try to establish outgoing connection)
    threading.Thread(target=connector_thread, daemon=True).start()
    # start sender on main thread so we can join easily
    sender_thread()

    # cleanup when sender exits
    print("\n[SHUTTING DOWN] closing peers...")
    with peers_lock:
        for conn, addr in peers:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
