import socket
import threading

PORT = 5050
SERVER = 'localhost'
ADDR = (SERVER, PORT)
FORMAT = 'utf-8'
HEADER = 64

client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
client.connect(ADDR)

def send_messages():
    """Thread function: continuously take input and send to server"""
    while True:
        msg = input("You: ")
        if msg:
            message = msg.encode(FORMAT)
            send_length = str(len(message)).encode(FORMAT)
            send_length += b' ' * (HEADER - len(send_length))
            client.send(send_length)
            client.send(message)
            if msg.lower() == "exit":
                client.close()
                break

def receive_messages():
    """Thread function: continuously receive messages from server"""
    while True:
        try:
            msg_length_bytes = client.recv(HEADER)
            if not msg_length_bytes:
                break
            msg_length = int(msg_length_bytes.decode(FORMAT).strip())
            msg = client.recv(msg_length).decode(FORMAT)
            print(f"\nServer: {msg}\nYou: ", end="")  # keep input clean
            if msg.lower() == "exit":
                break
        except:
            break

# Start threads
send_thread = threading.Thread(target=send_messages)
recv_thread = threading.Thread(target=receive_messages)

send_thread.start()
recv_thread.start()

# keep client alive until both threads finish
send_thread.join()
recv_thread.join()
