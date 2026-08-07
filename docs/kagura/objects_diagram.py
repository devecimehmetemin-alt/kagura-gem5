"""Draw the Kagura object graph as a PNG.

Same topology as the ASCII diagram in the README: grid columns/rows are
mapped to pixels, every edge is orthogonal, labels sit where the ASCII
labels sit.
"""
from PIL import Image, ImageDraw, ImageFont

S = 2  # supersample factor, downscaled at the end


def C(c):  # grid col -> x
    return (40 + c * 22) * S


def R(r):  # grid row -> y
    return (40 + r * 34) * S


W, H = 1560 * S, 1180 * S
img = Image.new("RGB", (W, H), "white")
d = ImageDraw.Draw(img)

INK = (34, 34, 34)
CODE = (60, 60, 60)
FILL = (250, 250, 250)
LW = 2 * S

f_title = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 21 * S)
f_body = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 17 * S)
f_code = ImageFont.truetype("C:/Windows/Fonts/consola.ttf", 16 * S)
f_lbl = ImageFont.truetype("C:/Windows/Fonts/consola.ttf", 15 * S)


def box(x1, y1, x2, y2, lines):
    d.rectangle([x1, y1, x2, y2], fill=FILL, outline=INK, width=LW)
    y = y1 + 12 * S
    for kind, text in lines:
        if kind == "hr":
            d.line([x1 + 14 * S, y + 4 * S, x2 - 14 * S, y + 4 * S],
                   fill=(180, 180, 180), width=S)
            y += 12 * S
            continue
        font = {"t": f_title, "b": f_body, "c": f_code}[kind]
        d.text((x1 + 16 * S, y), text, font=font, fill=INK if kind == "t" else CODE)
        y += (30 if kind == "t" else 25) * S


def arrow_head(x, y, direction):
    a = 7 * S
    if direction == "down":
        pts = [(x - a, y - 2 * a), (x + a, y - 2 * a), (x, y)]
    elif direction == "up":
        pts = [(x - a, y + 2 * a), (x + a, y + 2 * a), (x, y)]
    elif direction == "left":
        pts = [(x + 2 * a, y - a), (x + 2 * a, y + a), (x, y)]
    else:  # right
        pts = [(x - 2 * a, y - a), (x - 2 * a, y + a), (x, y)]
    d.polygon(pts, fill=INK)


def edge(points, head):
    for i in range(len(points) - 1):
        d.line([points[i], points[i + 1]], fill=INK, width=LW)
    x, y = points[-1]
    arrow_head(x, y, head)


def label(x, y, text, anchor="la"):
    d.text((x, y), text, font=f_lbl, fill=CODE, anchor=anchor)


# ---- boxes -----------------------------------------------------------------
box(C(20), R(0), C(47), R(3), [
    ("t", "IntermittentMinorCPU"),
    ("b", "MinorCPU subclass"),
])

box(C(3), R(7), C(54), R(13), [
    ("t", "IntermittentController: the capacitor"),
    ("c", "updateCapacitor() every 1 us"),
    ("c", "suspendContext() / activateContext()"),
    ("hr", ""),
    ("t", "KaguraController (its subclass)"),
    ("c", "adds R_mem, R_prev, R_thres, R_evict"),
])

box(C(37), R(17), C(62), R(21), [
    ("t", "ACC (one per cache)"),
    ("b", "EnergyCompressor subclass"),
    ("b", "holds the GCP"),
])

box(C(3), R(22), C(20), R(26), [
    ("t", "ACCCache"),
    ("b", "icache, dcache"),
])

box(C(3), R(29), C(20), R(32), [
    ("t", "NvmMemCtrl"),
    ("b", "main memory"),
])

# ---- edges -----------------------------------------------------------------
# CPU -> controller
edge([(C(25), R(3)), (C(25), R(7))], "down")
label(C(25) - 8 * S, (R(3) + R(7)) // 2, "totalInsts()", anchor="rm")

edge([(C(40), R(3)), (C(40), R(7))], "down")
label(C(40) + 8 * S, (R(3) + R(7)) // 2 - 10 * S,
      "RetiredLoads / RetiredStores", anchor="lm")
label(C(40) + 8 * S, (R(3) + R(7)) // 2 + 10 * S,
      "(gem5 probe points)", anchor="lm")

# ACCCache -> controller
edge([(C(6), R(22)), (C(6), R(13))], "up")
label(C(6) + 8 * S, R(15), "tags (dirty walk)", anchor="lm")

edge([(C(16), R(22)), (C(16), R(13))], "up")
label(C(16) + 8 * S, R(18), "blockEvicted()", anchor="lm")

# ACC -> controller: getEnergy()
edge([(C(37), R(19)), (C(33), R(19)), (C(33), R(13))], "up")
label(C(33) - 8 * S, R(15) + 8 * S, "getEnergy()", anchor="rm")

# controller -> ACC: broadcastMode
edge([(C(44), R(13)), (C(44), R(17))], "down")
label(C(44) + 8 * S, R(14) + 8 * S, "broadcastMode()", anchor="lm")
label(C(44) + 8 * S, R(15) + 8 * S, "-> setRegularMode()", anchor="lm")

# ACCCache -> ACC: reward/penalize and compress/passThrough
edge([(C(20), R(23) + 17 * S), (C(41), R(23) + 17 * S), (C(41), R(21))], "up")
label(C(41) + 8 * S, R(22), "reward() / penalize()", anchor="lm")

edge([(C(20), R(25)), (C(51), R(25)), (C(51), R(21))], "up")
label(C(51) + 8 * S, R(22) + 17 * S, "compress() / passThrough()", anchor="lm")

# ACCCache -> NvmMemCtrl
edge([(C(12), R(26)), (C(12), R(29))], "down")
label(C(12) + 8 * S, (R(26) + R(29)) // 2, "misses, writebacks", anchor="lm")

# NvmMemCtrl -> controller: bytesRead/bytesWritten
edge([(C(20), R(30) + 17 * S), (C(66), R(30) + 17 * S), (C(66), R(10)),
      (C(54), R(10))], "left")
label(C(66) - 8 * S, R(24), "bytesRead() /", anchor="rm")
label(C(66) - 8 * S, R(24) + 20 * S, "bytesWritten()", anchor="rm")

img = img.resize((1560, 1180), Image.LANCZOS)
img.save("kagura_objects.png")
print("saved kagura_objects.png")
