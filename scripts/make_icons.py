"""erion 아이콘 굽기 — 대시보드 머리의 워드마크(`erion` + 노란 점)를 그대로 옮긴다.

홈화면·탭 크기에서는 `erion.` 다섯 글자가 안 읽혀서 첫 글자 `e.` 만 쓴다.
글꼴은 대시보드 --serif 스택의 Charter (서버엔 Bitstream Charter Type1 이 있다).
점은 글꼴의 마침표가 아니라 원으로 그린다 — 대시보드에서도 점만 색이 다르다.

여백 아이콘과 섞이지 않게 하는 게 목적이다(2026-09-23). erion 에 아이콘이 없으니
Safari 가 같은 도메인의 여백 favicon 을 끌어다 썼다.

실행: .venv/bin/python scripts/make_icons.py  → app/static/*.png
Pillow 는 이 스크립트만 쓴다. 런타임 의존성이 아니다.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "app" / "static"
FONT = "/usr/share/fonts/X11/Type1/c0648bt_.pfb"      # Bitstream Charter Regular
DESK = (0x20, 0x1F, 0x1D)
INK = (0xF1, 0xEF, 0xE9)
MARK = (0xFF, 0xD8, 0x4D)
SS = 4                                                  # 슈퍼샘플


def icon(size: int, scale: float) -> Image.Image:
    """정사각 불투명. scale = 글자 높이 / 한 변 (maskable 은 안전영역 때문에 작게)."""
    S = size * SS
    im = Image.new("RGB", (S, S), DESK)
    d = ImageDraw.Draw(im)
    font = ImageFont.truetype(FONT, int(S * scale))
    l, t, r, b = d.textbbox((0, 0), "e", font=font)
    ew, eh = r - l, b - t
    dot = eh * 0.30                                     # 점 지름
    gap = eh * 0.08
    total = ew + gap + dot
    x0 = (S - total) / 2 - l
    y0 = (S - eh) / 2 - t
    d.text((x0, y0), "e", font=font, fill=INK)
    bx = x0 + l + ew + gap
    by = y0 + t + eh - dot                              # 기준선에 맞춘다
    d.ellipse([bx, by, bx + dot, by + dot], fill=MARK)
    return im.resize((size, size), Image.LANCZOS)


def badge(size: int) -> Image.Image:
    """안드로이드 상태바·알림 머리의 작은 아이콘. 알파만 쓰이므로 흰 글자에 투명 바탕.
    점도 같은 흰색 — 색은 어차피 버려진다."""
    S = size * SS
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    font = ImageFont.truetype(FONT, int(S * .80))
    l, t, r, b = d.textbbox((0, 0), "e", font=font)
    ew, eh = r - l, b - t
    dot, gap = eh * 0.30, eh * 0.08
    x0 = (S - (ew + gap + dot)) / 2 - l
    y0 = (S - eh) / 2 - t
    d.text((x0, y0), "e", font=font, fill=(255, 255, 255, 255))
    bx, by = x0 + l + ew + gap, y0 + t + eh - dot
    d.ellipse([bx, by, bx + dot, by + dot], fill=(255, 255, 255, 255))
    return im.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for size, scale, name in [(180, .62, "icon-180.png"), (192, .62, "icon-192.png"),
                              (512, .62, "icon-512.png"), (512, .46, "icon-512-maskable.png"),
                              (32, .78, "icon-32.png"), (16, .82, "icon-16.png")]:
        icon(size, scale).save(OUT / name, optimize=True)
        print(name)
    badge(96).save(OUT / "badge-96.png", optimize=True)
    print("badge-96.png")


if __name__ == "__main__":
    main()
