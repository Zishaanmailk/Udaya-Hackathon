import socket
import threading

PORT=5050
#SERVER=socket.gethostbyname(socket.gethostname())
SERVER='localhost'
ADD=(SERVER,PORT)
print(SERVER)
#print(socket.gethostname())
clint=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
clint.connect(ADD)
format='utf-8'
header=64

def send(msg):
    message=msg.encode(format)
    send_length=str(len(message)).encode(format)
    send_length+=b' '*(header-len(send_length))
    clint.send(send_length)
    clint.send(message)
while True:
    msg=input('you: ')
    send(msg)
    if msg=='exit':
        break



