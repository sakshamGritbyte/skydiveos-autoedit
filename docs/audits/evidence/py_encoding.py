from email.message import EmailMessage
m = EmailMessage()
m["Subject"] = "Votre vidéo est prête / Your skydive video is ready, Émilie!"
m["From"] = "a@b.c"; m["To"] = "x@y.z"
m.set_content("Hi Émilie,\n\nYour skydive video is ready! 🪂\n")
raw = m.as_string()
print("\n".join(l for l in raw.splitlines() if l.lower().startswith(("subject:", "content-type:", "content-transfer-encoding:"))))
