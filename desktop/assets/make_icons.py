"""데스크톱 앱 아이콘 생성 - 표준 라이브러리만 (Pillow 없이 RGBA PNG/ICO 직접 인코딩).

투명 배경 위에 둥근 모서리 사각형(세로 그라디언트) + 부드러운 그림자가 있는 흰 편지봉투
+ 우상단 "AI 스파클" 배지(자동 분류·처리를 상징). 4배 슈퍼샘플링 후 박스 다운스케일
→ 알파와 함께 가장자리를 부드럽게.

  python desktop/assets/make_icons.py
→ assets/icon.png (256, 앱/창) · assets/tray.png (32, 트레이) · assets/icon.ico (16~256 멀티)
"""
import struct
import zlib
from pathlib import Path

# 팔레트 (RGB)
BG_TOP = (96, 156, 255)      # 밝은 파랑
BG_BOTTOM = (58, 92, 220)    # 짙은 파랑 (살짝 인디고)
ENVELOPE = (255, 255, 255)
ENVELOPE_SHADE = (208, 223, 248)   # 봉투 뚜껑 아래 몸통 음영
FLAP = (238, 244, 254)      # 뚜껑 (살짝 밝은 회백)
FLAP_EDGE = (150, 176, 222)  # 뚜껑 접힘선 (더 또렷하게)
BADGE = (255, 168, 24)      # 우상단 강조 배지 (앰버)
BADGE_RING = (255, 255, 255)

SS = 4  # 슈퍼샘플 배율


def _lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _rrect_sd(x, y, w, h, r, px, py):
    """둥근 사각형 [x,x+w]×[y,y+h] 기준 signed distance (<0 이면 내부)."""
    cx = min(max(px, x + r), x + w - r)
    cy = min(max(py, y + r), y + h - r)
    # 코너 영역
    dx, dy = px - cx, py - cy
    corner = (dx * dx + dy * dy) ** 0.5 - r
    # 직선 영역
    inside_x = (x <= px <= x + w)
    inside_y = (y <= py <= y + h)
    if inside_x and inside_y:
        # 내부: 가장 가까운 변까지의 음수 거리, 코너면 corner
        if (px < x + r or px > x + w - r) and (py < y + r or py > y + h - r):
            return corner
        return -min(px - x, x + w - px, py - y, y + h - py)
    return corner if (px < x + r or px > x + w - r) and (py < y + r or py > y + h - r) else \
        max(x - px, px - (x + w), y - py, py - (y + h))


def _render(size):
    """size×size RGBA 픽셀 행렬. size*SS 로 그리고 박스 평균 다운스케일(알파 포함)."""
    S = size * SS
    m = S * 0.055           # 배경 여백
    rad = S * 0.255         # 배경 모서리 반경
    bx, by, bw, bh = m, m, S - 2 * m, S - 2 * m

    # 봉투 박스
    ew, eh = S * 0.62, S * 0.44
    ex = (S - ew) / 2
    ey = (S - eh) / 2 + S * 0.015
    flap_h = eh * 0.60
    er = S * 0.028          # 봉투 모서리 반경

    # 봉투 그림자 오프셋
    sh_dx, sh_dy, sh_blur = S * 0.0, S * 0.03, S * 0.045

    # 우상단 강조 배지 (링 포함 전체가 배경 둥근사각 안에 완전히 들어오도록)
    badge_cx, badge_cy, badge_r = S * 0.755, S * 0.245, S * 0.118
    ring_r = badge_r + S * 0.024

    hi = [[(0, 0, 0, 0)] * S for _ in range(S)]
    for j in range(S):
        for i in range(S):
            r_, g_, b_, a_ = 0, 0, 0, 0

            # 배경 둥근 사각 + 세로 그라디언트 (AA: sd 1px 페더)
            sd = _rrect_sd(bx, by, bw, bh, rad, i, j)
            bg_a = max(0.0, min(1.0, 0.5 - sd / (SS * 1.5)))
            if bg_a > 0:
                cr, cg, cb = _lerp(BG_TOP, BG_BOTTOM, j / S)
                r_, g_, b_, a_ = cr, cg, cb, bg_a

            # 봉투 그림자 (배경 안쪽에만)
            if a_ > 0:
                esd = _rrect_sd(ex + sh_dx, ey + sh_dy, ew, eh, er, i, j)
                shadow = max(0.0, min(1.0, 0.45 - esd / sh_blur)) * 0.28
                if shadow > 0:
                    r_ = round(r_ * (1 - shadow) + 20 * shadow)
                    g_ = round(g_ * (1 - shadow) + 44 * shadow)
                    b_ = round(b_ * (1 - shadow) + 96 * shadow)

            # 봉투 본체 (AA)
            esd = _rrect_sd(ex, ey, ew, eh, er, i, j)
            env_a = max(0.0, min(1.0, 0.5 - esd / (SS * 1.5)))
            if env_a > 0:
                cx = ex + ew / 2
                edge = abs(i - cx) / (ew / 2)
                flap_y = ey + flap_h * (1 - edge)
                if j <= flap_y - S * 0.009:
                    col = FLAP
                elif j <= flap_y + S * 0.009:
                    col = FLAP_EDGE
                else:
                    # 몸통: 위쪽은 살짝 음영, 아래로 갈수록 순백
                    tt = min(1.0, (j - flap_y) / (eh * 0.5))
                    col = _lerp(ENVELOPE_SHADE, ENVELOPE, tt)
                r_ = round(r_ * (1 - env_a) + col[0] * env_a)
                g_ = round(g_ * (1 - env_a) + col[1] * env_a)
                b_ = round(b_ * (1 - env_a) + col[2] * env_a)
                a_ = max(a_, env_a)

            # 우상단 배지 (흰 링 + 앰버 원)
            dr = ((i - badge_cx) ** 2 + (j - badge_cy) ** 2) ** 0.5
            ring_a = max(0.0, min(1.0, 0.5 - (dr - ring_r) / (SS * 1.5)))
            if ring_a > 0:
                r_ = round(r_ * (1 - ring_a) + BADGE_RING[0] * ring_a)
                g_ = round(g_ * (1 - ring_a) + BADGE_RING[1] * ring_a)
                b_ = round(b_ * (1 - ring_a) + BADGE_RING[2] * ring_a)
                a_ = max(a_, ring_a)
            badge_a = max(0.0, min(1.0, 0.5 - (dr - badge_r) / (SS * 1.5)))
            if badge_a > 0:
                r_ = round(r_ * (1 - badge_a) + BADGE[0] * badge_a)
                g_ = round(g_ * (1 - badge_a) + BADGE[1] * badge_a)
                b_ = round(b_ * (1 - badge_a) + BADGE[2] * badge_a)
                a_ = max(a_, badge_a)

                # 배지 안 "AI 스파클" - 두 다이아몬드(세로로 긴 것 + 가로로 긴 것)를
                # 합쳐 4방향 별 모양. 밋밋한 원형 배지 대신 "AI가 자동 처리한다"는
                # 느낌을 주는 액센트.
                nx, ny = (i - badge_cx) / badge_r, (j - badge_cy) / badge_r
                star_sd = min(abs(nx) / 0.82 + abs(ny) / 0.26, abs(nx) / 0.26 + abs(ny) / 0.82) - 1
                star_a = max(0.0, min(1.0, 0.5 - star_sd * badge_r / (SS * 1.2))) * badge_a
                if star_a > 0:
                    r_ = round(r_ * (1 - star_a) + BADGE_RING[0] * star_a)
                    g_ = round(g_ * (1 - star_a) + BADGE_RING[1] * star_a)
                    b_ = round(b_ * (1 - star_a) + BADGE_RING[2] * star_a)

            hi[j][i] = (r_, g_, b_, round(a_ * 255) if a_ <= 1 else 255)

    out = [[(0, 0, 0, 0)] * size for _ in range(size)]
    n = SS * SS
    for j in range(size):
        for i in range(size):
            r = g = b = a = 0
            for dj in range(SS):
                row = hi[j * SS + dj]
                for di in range(SS):
                    c = row[i * SS + di]
                    r += c[0]; g += c[1]; b += c[2]; a += c[3]
            out[j][i] = (r // n, g // n, b // n, a // n)
    return out


def _png_bytes(pixels):
    h = len(pixels); w = len(pixels[0])
    raw = bytearray()
    for row in pixels:
        raw.append(0)
        for r, g, b, a in row:
            raw += bytes((r, g, b, a))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)  # 8-bit RGBA
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
    print("wrote icon.png (256), tray.png (32), icon.ico (16-256) ->", here)


if __name__ == "__main__":
    main()
