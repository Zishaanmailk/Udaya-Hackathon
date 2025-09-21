from tkinter import *

def open_chat():
    chat = Toplevel(root)
    chat.title("Mini Messaging App")
    chat.geometry("350x400")

    messages_frame = Frame(chat, bg="white")
    messages_frame.pack(fill="both", expand=True, padx=5, pady=5)

    entry = Entry(chat, width=25)
    entry.pack(side="left", padx=5, pady=5, fill="x", expand=True)

    def add_message(text):
        bubble = Frame(messages_frame, bg="white")
        bubble.pack(fill="x", anchor="e", pady=2)

        lbl = Label(
            bubble, text=text, bg="#DCF8C6", padx=8, pady=4, wraplength=200
        )
        lbl.pack(anchor="e", padx=5)

    def send_message(event=None):
        msg = entry.get().strip()
        if msg:
            add_message(msg)
            entry.delete(0, "end")

    send_btn = Button(chat, text="Send", command=send_message)
    send_btn.pack(side="right", padx=5, pady=5)

    entry.bind("<Return>", send_message)

root = Tk()
root.title("Launcher")
root.geometry("200x100")

btn = Button(root, text="Open Chat", command=open_chat)
btn.pack(expand=True)

root.mainloop()
