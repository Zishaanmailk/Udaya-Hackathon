import tkinter as tk
from tkinter import scrolledtext
import socket
import threading


PORT = 5050
SERVER = 'localhost'
ADD = (SERVER, PORT)
format = 'utf-8'
header = 64
clint = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

def connect_to_server():
    try:
        clint.connect(ADD)
        label_status.config(text=f"Connected to {SERVER}")
        start_receiving()
    except Exception as e:
        label_status.config(text=f"Connection failed: {e}")

def send_msg():
    msg = entry_message.get()
    if msg:
        message = msg.encode(format)
        send_length = str(len(message)).encode(format)
        send_length += b' ' * (header - len(send_length))
        clint.send(send_length)
        clint.send(message)
        chat_box.insert(tk.END, f"You: {msg}\n")
        entry_message.delete(0, tk.END)
        if msg.lower() == "exit":
            clint.close()
            root.destroy()

def receive_loop():
    while True:
        try:
            msg_length_bytes = clint.recv(header)
            if not msg_length_bytes:
                break
            msg_length = int(msg_length_bytes.decode(format).strip())
            if msg_length > 0:
                msg = clint.recv(msg_length).decode(format)
                chat_box.insert(tk.END, f"Friend: {msg}\n")
        except:
            break

def start_receiving():
    thread = threading.Thread(target=receive_loop, daemon=True)
    thread.start()


root = tk.Tk()
root.title("LAN Chat App")
root.iconbitmap("whatsapp.ico")

label_status = tk.Label(root, text="Not connected")
label_status.pack(pady=5)


chat_box = scrolledtext.ScrolledText(root, width=50, height=15)
chat_box.pack(padx=10, pady=5)

frame_msg = tk.Frame(root)
frame_msg.pack(pady=5)
entry_message = tk.Entry(frame_msg, width=40)
entry_message.pack(side=tk.LEFT, padx=5)
tk.Button(frame_msg, text="Send", command=send_msg).pack(side=tk.LEFT)


tk.Button(root, text="Connect", command=connect_to_server).pack(pady=5)

root.mainloop()
