"""生成 MemGuard 的程序图标 mem_guard.ico（蓝底内存颗粒）。"""
from PIL import Image, ImageDraw

size = 64
img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([6, 6, 57, 57], radius=12, fill=(37, 99, 235, 255))
for r in range(3):
    for c in range(4):
        x0, y0 = 14 + c * 11, 16 + r * 11
        d.rectangle([x0, y0, x0 + 7, y0 + 7], fill=(226, 232, 240, 255))
img.save("mem_guard.ico")
print("icon generated -> mem_guard.ico")
