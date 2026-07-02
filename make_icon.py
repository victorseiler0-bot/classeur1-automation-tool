from PIL import Image, ImageDraw

SIZE = 256
img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

margin = 14
d.rounded_rectangle([margin, margin, SIZE - margin, SIZE - margin], radius=24, fill="#217346")

# bande "titre" plus foncée en haut, dans l'esprit tableur
d.rounded_rectangle([margin, margin, SIZE - margin, margin + 56], radius=24, fill="#1a5c38")
d.rectangle([margin, margin + 38, SIZE - margin, margin + 56], fill="#1a5c38")

# grand X blanc stylisé, dans l'esprit d'un logo tableur
cx, cy = SIZE // 2, SIZE // 2 + 14
s = 46
w = 22
d.line([(cx - s, cy - s), (cx + s, cy + s)], fill="white", width=w)
d.line([(cx - s, cy + s), (cx + s, cy - s)], fill="white", width=w)

sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
img.save("icon.ico", sizes=sizes)
print("icon.ico créée")
