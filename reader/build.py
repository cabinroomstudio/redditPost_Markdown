"""Build dist\\RedditReader\\RedditReader.exe.

    venv\\Scripts\\python reader\\build.py
"""
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
ICON = ROOT / "reader" / "icon.ico"


def make_icon() -> None:
    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((8, 8, size - 8, size - 8), radius=56, fill=(194, 65, 12, 255))
    # an open book: two pages
    d.polygon([(48, 80), (124, 96), (124, 200), (48, 184)], fill=(255, 247, 237, 255))
    d.polygon([(132, 96), (208, 80), (208, 184), (132, 200)], fill=(255, 237, 213, 255))
    for i in range(4):
        y = 116 + i * 18
        d.line([(62, y - 4), (110, y + 6)], fill=(194, 65, 12, 160), width=5)
        d.line([(146, y + 6), (194, y - 4)], fill=(194, 65, 12, 160), width=5)
    img.save(ICON, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def main() -> int:
    make_icon()
    sep = ";"
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
        "--name", "RedditReader", "--icon", str(ICON),
        "--add-data", f"{ROOT / 'reader' / 'static'}{sep}static",
        "--hidden-import", "clr", "--paths", str(ROOT),
        "--exclude-module", "tkinter",
        "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"), "--specpath", str(ROOT / "build"),
        str(ROOT / "reader" / "app.py"),
    ]
    return subprocess.call(cmd, cwd=ROOT)


if __name__ == "__main__":
    sys.exit(main())
