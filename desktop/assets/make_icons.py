"""데스크톱 앱 아이콘 생성 — 표준 라이브러리만 (Pillow 없이 PNG/ICO 직접 인코딩).

둥근 모서리 사각형 + 세로 그라디언트 배경, 흰 편지봉투(열린 뚜껑) + 우상단 미읽음 점.
2배 슈퍼샘플링 후 박스 다운스케일로 가장자리를 부드럽게 한다.

  python desktop/assets/make_icons.py
→ assets/icon.png (256, 앱/창) · assets/tray.png (32, 트레이) · assets/icon.ico (16~256 멀티)
"""
import struct
import zlib
from pathlib import Path

# 팔레트 (RGB)
BG_TOP = (74, 128, 240)     # 밝은 파랑
BG_BOTTOM = (37, 88, 210)   # 짙은 파랑
ENVELOPE = (255, 255, 255)
FLAP = (223, 234, 252)      # 옅은 파랑빛 흰색 (뚜껑 음영)
LINE = (200, 214, 240)      # 봉투 안쪽 라인
DOT = (255, 179, 71)        # 미읽음 점 (앰버)

SS = 3  # 슈퍼샘플 배율


def _lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _rounded_rect(x, y, w, h, r, px, py):
    """(px,py)가 모서리 반경 r인 둥근 사각형 [x,x+w]×[y,y+h] 안인가."""
    if px < x or px > x + w or py < y or py > y + h:
        return False
    cx = min(max(px, x + r), x + w - r)
    cy = min(max(py, y + r), y + h - r)
    return (px - cx) ** 2 + (py - cy) ** 2 <= r * r


def _render(size):
    """size×size RGB 픽셀 행렬. 내부적으로 size*SS 로 그리고 박스 평균 다운스케일."""
    S = size * SS
    m = S * 0.085          # 배경 여백
    rad = S * 0.235        # 배경 모서리 반경
    # 봉투 박스
    ew, eh = S * 0.64, S * 0.46
    ex = (S - ew) / 2
    ey = (S - eh) / 2 + S * 0.02
    flap_h = eh * 0.62     # 뚜껑 높이
    # 미읽음 점
    dot_cx, dot_cy, dot_r = S * 0.80, S * 0.20, S * 0.115

    hi = [[(0, 0, 0)] * S for _ in range(S)]
    for j in range(S):
        for i in range(S):
            px = None
            # 배경 (둥근 사각 + 세로 그라디언트)
            if _rounded_rect(m, m, S - 2 * m, S - 2 * m, rad, i, j):
                px = _lerp(BG_TOP, BG_BOTTOM, j / S)
            # 봉투 본체
            if ex <= i <= ex + ew and ey <= j <= ey + eh:
                px = ENVELOPE
                cx = ex + ew / 2
                # 뚜껑: 양 끝에서 중앙 꼭짓점으로 내려오는 삼각형
                edge = abs(i - cx) / (ew / 2)          # 0(중앙)~1(끝)
                flap_y = ey + flap_h * (1 - edge)
                if j <= flap_y:
                    px = FLAP
                elif abs(j - flap_y) < S * 0.006:
                    px = LINE
            # 미읽음 점 (봉투 위, 배경 위 모두 덮음)
            if (i - dot_cx) ** 2 + (j - dot_cy) ** 2 <= dot_r ** 2:
                px = DOT
            hi[j][i] = px if px else (0, 0, 0)

    # 박스 평균 다운스케일 (알파 없음 — 투명 대신 배경색이 이미 채워짐)
    out = [[(0, 0, 0)] * size for _ in range(size)]
    for j in range(size):
        for i in range(size):
            r = g = b = 0
            for dj in range(SS):
                for di in range(SS):
                    c = hi[j * SS + dj][i * SS + di]
                    r += c[0]; g += c[1]; b += c[2]
            n = SS * SS
            out[j][i] = (r // n, g // n, b // n)
    return out


def _png_bytes(pixels):
    h = len(pixels); w = len(pixels[0])
    raw = bytearray()
    for row in pixels:
        raw.append(0)
        for r, g, b in row:
            raw += bytes((r, g, b))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8-bit RGB
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b"")


def _ico_bytes(sizes):
    """PNG-embedded ICO (Vista+). 각 size별 _render → PNG → 디렉터리."""
    entries = []
    blobs = []
    offset = 6 + 16 * len(sizes)
    for s in sizes:
        png = _png_bytes(_render(s))
        entries.append(struct.pack(
            "<BBBBHHII",
            0 if s >= 256 else s, 0 if s >= 256 else s, 0, 0, 1, 32, len(png), offset,
        ))
        blobs.append(png)
        offset += len(png)
    return struct.pack("<HHH", 0, 1, len(sizes)) + b"".join(entries) + b"".join(blobs)


def main():
    here = Path(__file__).resolve().parent
    (here / "icon.png").write_bytes(_png_bytes(_render(256)))
    (here / "tray.png").write_bytes(_png_bytes(_render(32)))
    (here / "icon.ico").write_bytes(_ico_bytes([16, 24, 32, 48, 64, 128, 256]))
    print("wrote icon.png (256), tray.png (32), icon.ico (16-256) →", here)


if __name__ == "__main__":
    main()
