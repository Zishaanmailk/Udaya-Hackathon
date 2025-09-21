import socket
s = socket.socket()
print('Socket Created')
s.bind(('localhost', 5050))
s.listen(3)
print('waiting for connections')
while True:
    c, addr = s.accept()
    name=c.recv(1024).decode()
    print("Connected with", addr,name)
    c.send(bytes('Welcome to my server','utf-8'))
    c.close()
