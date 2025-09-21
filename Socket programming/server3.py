import socket
import threading

PORT = 5050
SERVER = 'localhost'
ADDR = (SERVER, PORT)
FORMAT = 'utf-8'
HEADER = 64

server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.bind(ADDR)

# Function to handle receiving/sending with a client
def handle_client(conn, addr):
    print(f"[NEW CONNECTION] {addr} connected.")

    def receive_messages():
        """Thread function: keeps receiving messages from this client"""
        while True:
            try:
                msg_length_bytes = conn.recv(HEADER)
                if not msg_length_bytes:
                    print(f"[DISCONNECT] {addr}")
                    break
                msg_length = int(msg_length_bytes.decode(FORMAT).strip())
                msg = conn.recv(msg_length).decode(FORMAT)

                print(f"\n{addr} : {msg}\nServer: ", end="")  # keep prompt clean
                if msg.lower() == "exit":
                    break
            except:
                break
        conn.close()

    def send_messages():
        """Thread function: allows server to send messages back to client"""
        while True:
            msg = input("Server: ")
            if msg:
                message = msg.encode(FORMAT)
                send_length = str(len(message)).encode(FORMAT)
                send_length += b' ' * (HEADER - len(send_length))
                try:
                    conn.send(send_length)
                    conn.send(message)
                except:
                    break
                if msg.lower() == "exit":
                    break

    # start both threads
    recv_thread = threading.Thread(target=receive_messages)
    send_thread = threading.Thread(target=send_messages)

    recv_thread.start()
    send_thread.start()

    # wait until both threads finish
    recv_thread.join()
    send_thread.join()


def start():
    server.listen()
    print(f"[LISTENING] Server is listening on {SERVER}:{PORT}")
    while True:
        conn, addr = server.accept()
        thread = threading.Thread(target=handle_client, args=(conn, addr))
        thread.start()
        print(f"[ACTIVE CONNECTIONS] {threading.active_count() - 1}")


print("[STARTING] server is starting...")
start()
