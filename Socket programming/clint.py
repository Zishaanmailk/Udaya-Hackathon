import socket
c=socket.socket()

c.connect(('localhost',9990))
name=input("enter the name: ")
c.send(bytes(name,'utf-8'))
print(c.recv(1024).decode())
