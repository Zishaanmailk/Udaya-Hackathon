import socket
import threading
import time

PORT=5050
#SERVER=socket.gethostbyname(socket.gethostname())
SERVER='localhost'
ADD=(SERVER,PORT)
print(SERVER)
#print(socket.gethostname())
server=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
server.bind(ADD)
format='utf-8'

def handel_clint(c, add):
    print('connected with ', add)
    connected = True
    while connected:
        msg_length_bytes = c.recv(64)
        if not msg_length_bytes:
            print("Client disconnected", add)
            break  # exit the loop

        msg_length = int(msg_length_bytes.decode(format).strip())
        if msg_length > 0:
            msg = c.recv(msg_length).decode(format)
            print(add, ':', msg)
            if msg.lower() == 'exit':
                connected = False

    c.close()


def start():
    server.listen()
    print('server is lisining in',socket.gethostname())
    while True:
        c,add=server.accept()
        thread=threading.Thread(target=handel_clint,args=(c,add))
        thread.start()
        time.sleep(0.00001)
        print("active connections: ",threading.active_count()-1)

print("sesrver is started")
start()
