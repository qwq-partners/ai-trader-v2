"""
미국증시 마감 리포트 — 차트 이미지 생성 v3
가독성 우선 설계: 큰 폰트, 충분한 여백, 높은 DPI
"""

from __future__ import annotations

import io
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# ── 섹터 ETF 메타 ─────────────────────────────────────────────────────────────
SECTOR_META: Dict[str, Dict] = {
    "XLK":  {"name": "Technology",     "weight": 29.0},
    "XLF":  {"name": "Financials",     "weight": 13.0},
    "XLV":  {"name": "Health Care",    "weight": 12.0},
    "XLY":  {"name": "Cons. Discret.", "weight": 11.0},
    "XLC":  {"name": "Comm. Svcs",     "weight":  9.0},
    "XLI":  {"name": "Industrials",    "weight":  8.0},
    "XLP":  {"name": "Cons. Staples",  "weight":  6.0},
    "XLE":  {"name": "Energy",         "weight":  4.0},
    "XLB":  {"name": "Materials",      "weight":  3.0},
    "XLRE": {"name": "Real Estate",    "weight":  2.5},
    "XLU":  {"name": "Utilities",      "weight":  2.5},
}

INDEX_ORDER = [
    ("^GSPC", "S&P 500"),
    ("^IXIC", "NASDAQ"),
    ("^DJI",  "DOW"),
    ("^RUT",  "Russell 2K"),
    ("^SOX",  "SOX"),
    ("^VIX",  "VIX"),
]

# ── 색상 ─────────────────────────────────────────────────────────────────────
BG       = "#0d1117"
DIVIDER  = "#30363d"
TEXT_PRI = "#e6edf3"
TEXT_SEC = "#8b949e"


def _card_colors(pct: float):
    if pct >= 1.5:  return "#0d2818", "#2ea043", "#3fb950"
    if pct >= 0.3:  return "#0d2011", "#1a5c2a", "#26a641"
    if pct > -0.3:  return "#1c2128", "#30363d", "#8b949e"
    if pct > -1.5:  return "#2d1117", "#6e1c1c", "#f85149"
    return              "#3d0b0b",  "#da3633", "#ff7b72"


def _heat(pct: float) -> str:
    """등락률 → 선명한 레드↔그린"""
    c = max(-4.0, min(4.0, pct))
    if c >= 0:
        t = c / 4.0
        r = int(13  + (0   - 13 ) * t)
        g = int(27  + (190 - 27 ) * t)
        b = int(18  + (50  - 18 ) * t)
    else:
        t = (-c) / 4.0
        r = int(13  + (218 - 13 ) * t)
        g = int(27  + (54  - 27 ) * t)
        b = int(18  + (51  - 18 ) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def _lum(h: str) -> float:
    h = h.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (0.299*r + 0.587*g + 0.114*b) / 255


def _setup_font():
    import os, matplotlib.font_manager as fm, matplotlib
    for fp in [
        "/home/user/.local/share/fonts/NotoSansKR.ttf",
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ]:
        if os.path.exists(fp):
            fm.fontManager.addfont(fp)
            p = fm.FontProperties(fname=fp)
            matplotlib.rcParams["font.family"] = p.get_name()
            matplotlib.rcParams["axes.unicode_minus"] = False
            return p
    return None


# ═════════════════════════════════════════════════════════════════════════════
# Chart 1: 지수 카드 + 섹터 ETF 히트맵
# ═════════════════════════════════════════════════════════════════════════════

def generate_us_market_chart(
    quotes: Dict[str, Any],
    date_str: str = "",
    avg_pct: float = 0.0,
) -> Optional[io.BytesIO]:
    try:
        import matplotlib; matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mp
        import squarify, numpy as np

        _setup_font()

        FW, FH = 16, 10
        fig = plt.figure(figsize=(FW, FH), facecolor=BG, dpi=130)

        # 타이틀
        if avg_pct >= 1.0:   mood, mc = "▲  강세 마감", "#3fb950"
        elif avg_pct <= -1.0: mood, mc = "▼  약세 마감", "#ff7b72"
        else:                 mood, mc = "●  보합 마감", "#8b949e"

        fig.text(0.04, 0.967, f"미국증시 마감  —  {date_str}",
                 color=TEXT_PRI, fontsize=17, fontweight="bold", va="top")
        fig.text(0.96, 0.967, mood,
                 color=mc, fontsize=15, fontweight="bold", va="top", ha="right")

        fig.add_artist(plt.Line2D([0.04, 0.96], [0.932, 0.932],
                                  transform=fig.transFigure,
                                  color=DIVIDER, linewidth=1.0))

        # ── 지수 카드 (2행 × 3열) ─────────────────────────────────────────
        CX0, CX1 = 0.04, 0.96
        CY0, CY1 = 0.595, 0.925
        COLS, ROWS = 3, 2
        PX, PY = 0.014, 0.014
        cw = (CX1 - CX0 - PX * (COLS-1)) / COLS
        ch = (CY1 - CY0 - PY * (ROWS-1)) / ROWS

        for i, (sym, label) in enumerate(INDEX_ORDER):
            row, col = divmod(i, COLS)
            cx = CX0 + col * (cw + PX)
            cy = CY1 - (row+1)*ch - row*PY

            q = quotes.get(sym, {})
            pct   = q.get("change_pct", 0.0)
            price = q.get("price", 0.0)
            bg_c, border_c, pct_c = _card_colors(pct)

            fig.add_artist(mp.FancyBboxPatch(
                (cx, cy), cw, ch,
                boxstyle="round,pad=0.004",
                transform=fig.transFigure,
                facecolor=bg_c, edgecolor=border_c,
                linewidth=1.8, clip_on=False, zorder=2,
            ))

            # 지수명 (소)
            fig.text(cx + 0.013, cy + ch - 0.011,
                     label, color=TEXT_SEC, fontsize=11,
                     va="top", transform=fig.transFigure)

            # 등락률 (대 — 핵심)
            sign  = "+" if pct > 0 else ""
            arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "●")
            fig.text(cx + cw/2, cy + ch/2 + ch*0.08,
                     f"{arrow}  {sign}{pct:.2f}%",
                     color=pct_c, fontsize=21, fontweight="bold",
                     ha="center", va="center", transform=fig.transFigure)

            # 현재가
            pstr = f"{price:.2f}" if sym == "^VIX" else (
                   f"{price:,.0f}" if price >= 10000 else f"{price:,.2f}")
            fig.text(cx + cw/2, cy + 0.012,
                     pstr, color=TEXT_SEC, fontsize=10,
                     ha="center", va="bottom", transform=fig.transFigure,
                     fontfamily="monospace")

        # ── 섹터 ETF 히트맵 ───────────────────────────────────────────────
        fig.add_artist(plt.Line2D([0.04, 0.96], [0.582, 0.582],
                                  transform=fig.transFigure,
                                  color=DIVIDER, linewidth=1.0))
        fig.text(0.04, 0.572, "S&P 500  Sector ETF",
                 color=TEXT_SEC, fontsize=10, va="top")

        ax = fig.add_axes([0.04, 0.045, 0.92, 0.515], facecolor=BG)
        ax.set_xlim(0, 100); ax.set_ylim(0, 100); ax.axis("off")

        sec_items = []
        for sym, meta in SECTOR_META.items():
            q   = quotes.get(sym, {})
            pct = q.get("change_pct", 0.0)
            sec_items.append({"sym": sym, "name": meta["name"],
                               "weight": meta["weight"], "pct": pct,
                               "color": _heat(pct)})

        rects = squarify.squarify(
            squarify.normalize_sizes([s["weight"] for s in sec_items], 100, 100),
            x=0, y=0, dx=100, dy=100)

        for rect, item in zip(rects, sec_items):
            x, y, w, h = rect["x"], rect["y"], rect["dx"], rect["dy"]
            G = 0.5
            ax.add_patch(mp.FancyBboxPatch(
                (x+G, y+G), w-G*2, h-G*2,
                boxstyle="round,pad=0.0",
                facecolor=item["color"], edgecolor=BG, linewidth=2.0, zorder=2))

            tc   = "#fff" if _lum(item["color"]) < 0.48 else "#0d1117"
            sign = "+" if item["pct"] > 0 else ""
            pstr = f"{sign}{item['pct']:.2f}%"

            # 이름 + % (블록이 충분히 클 때만)
            if w > 9 and h > 9:
                nfs = max(9,  min(16, min(w,h) * 0.80))
                pfs = max(10, min(18, min(w,h) * 0.90))
                ax.text(x+w/2, y+h/2+h*0.09, item["name"],
                        ha="center", va="center", color=tc,
                        fontsize=nfs, fontweight="bold", zorder=3)
                ax.text(x+w/2, y+h/2-h*0.13, pstr,
                        ha="center", va="center", color=tc,
                        fontsize=pfs, zorder=3)
            elif w > 5 and h > 5:
                fs = max(8, min(13, min(w,h) * 0.80))
                ax.text(x+w/2, y+h/2, f"{item['name']}\n{pstr}",
                        ha="center", va="center", color=tc,
                        fontsize=fs, fontweight="bold",
                        zorder=3, linespacing=1.25)

        # 범례
        N = 40
        for i in range(N):
            ax.add_patch(mp.Rectangle(
                (i*(100/N), -7), 100/N, 4,
                facecolor=_heat(-4.0 + i*(8.0/N)), edgecolor="none", zorder=2))
        ax.text(0,   -9, "−4%", ha="left",   va="top", color=TEXT_SEC, fontsize=9)
        ax.text(50,  -9, "0",   ha="center", va="top", color=TEXT_SEC, fontsize=9)
        ax.text(100, -9, "+4%", ha="right",  va="top", color=TEXT_SEC, fontsize=9)
        ax.set_ylim(-14, 100)

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                    facecolor=BG, edgecolor="none")
        plt.close(fig)
        buf.seek(0)
        logger.info("[차트] 지수/섹터 ETF 차트 생성 완료")
        return buf

    except Exception as e:
        logger.error(f"[차트] 생성 실패: {e}", exc_info=True)
        return None


# ═════════════════════════════════════════════════════════════════════════════
# Chart 2: S&P 500 개별 종목 히트맵 (finviz 스타일 중첩 treemap)
# ═════════════════════════════════════════════════════════════════════════════

# 가독성을 위해 섹터당 상위 4~5개만 표시
SP500_DISPLAY: Dict[str, list] = {
    "XLK":  [("AAPL","Apple",15.0),("MSFT","Microsoft",13.5),
              ("NVDA","NVIDIA",11.0),("AVGO","Broadcom",3.5),("ORCL","Oracle",2.0)],
    "XLF":  [("BRK-B","Berkshire",4.5),("JPM","JPMorgan",4.2),
              ("V","Visa",4.0),("MA","Mastercard",3.5),("BAC","BofA",2.0)],
    "XLV":  [("LLY","Lilly",5.0),("UNH","UnitedHlth",4.2),
              ("JNJ","J&J",2.5),("ABBV","AbbVie",2.2),("MRK","Merck",2.0)],
    "XLY":  [("AMZN","Amazon",8.5),("TSLA","Tesla",4.2),
              ("HD","Home Depot",2.2),("MCD","McDonald's",1.5)],
    "XLC":  [("GOOG","Alphabet",9.0),("META","Meta",7.5),
              ("NFLX","Netflix",2.5),("DIS","Disney",1.5)],
    "XLI":  [("GE","GE Aero",2.1),("CAT","Caterpillar",1.9),
              ("RTX","RTX",1.8),("UNP","Union Pac.",1.6),("HON","Honeywell",1.3)],
    "XLP":  [("WMT","Walmart",3.2),("COST","Costco",2.8),
              ("PG","P&G",2.6),("KO","Coca-Cola",2.0)],
    "XLE":  [("XOM","ExxonMobil",2.8),("CVX","Chevron",2.2),
              ("COP","ConocoPhil",1.3)],
    "XLB":  [("LIN","Linde",1.6),("SHW","Sherwin-W.",0.9),("APD","Air Prod.",0.7)],
    "XLRE": [("PLD","Prologis",0.9),("AMT","Amer. Tower",0.8),("EQIX","Equinix",0.6)],
    "XLU":  [("NEE","NextEra",1.0),("DUK","Duke En.",0.6),("SO","Southern",0.5)],
}


def generate_sp500_map(
    stock_quotes: Dict[str, Any],
    date_str: str = "",
) -> Optional[io.BytesIO]:
    try:
        import matplotlib; matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mp
        import squarify

        _setup_font()

        FW, FH = 16, 10
        fig = plt.figure(figsize=(FW, FH), facecolor=BG, dpi=130)

        # 타이틀
        fig.text(0.03, 0.97, f"S&P 500  Map  —  {date_str}",
                 color=TEXT_PRI, fontsize=17, fontweight="bold", va="top")
        fig.text(0.97, 0.97, "size ∝ market cap   color = daily % change",
                 color=TEXT_SEC, fontsize=10, va="top", ha="right")

        ax = fig.add_axes([0.01, 0.03, 0.98, 0.90], facecolor=BG)
        ax.set_xlim(0, 100); ax.set_ylim(0, 100); ax.axis("off")

        sec_keys    = list(SP500_DISPLAY.keys())
        sec_weights = [SECTOR_META[k]["weight"] for k in sec_keys]

        sec_rects = squarify.squarify(
            squarify.normalize_sizes(sec_weights, 100, 100),
            x=0, y=0, dx=100, dy=100)

        OG = 0.7    # 섹터 외부 갭
        IG = 0.4    # 종목 내부 갭
        LH_RATIO = 0.14  # 섹터 레이블 높이 비율

        for sec_rect, sec_key in zip(sec_rects, sec_keys):
            SX  = sec_rect["x"]  + OG
            SY  = sec_rect["y"]  + OG
            SDX = sec_rect["dx"] - OG * 2
            SDY = sec_rect["dy"] - OG * 2
            if SDX < 1 or SDY < 1:
                continue

            # 섹터 배경
            ax.add_patch(mp.FancyBboxPatch(
                (SX, SY), SDX, SDY,
                boxstyle="square,pad=0",
                facecolor="#1c2128", edgecolor="#21262d",
                linewidth=1.2, zorder=1))

            # 섹터 레이블 영역
            LH = max(min(SDY * LH_RATIO, 5.5), 2.8)
            ax.text(SX + SDX*0.5, SY + SDY - LH*0.45,
                    SECTOR_META[sec_key]["name"],
                    ha="center", va="center", color=TEXT_PRI,
                    fontsize=max(6.5, min(11, SDX * 0.50)),
                    fontweight="bold", zorder=4, clip_on=True)

            # 종목 treemap
            stocks  = SP500_DISPLAY[sec_key]
            weights = [w for _, _, w in stocks]
            IH      = SDY - LH          # 종목 영역 높이

            if IH < 1:
                continue

            stock_rects = squarify.squarify(
                squarify.normalize_sizes(weights, SDX, IH),
                x=SX, y=SY, dx=SDX, dy=IH)

            for sr, (sym, dname, _) in zip(stock_rects, stocks):
                IX  = sr["x"]  + IG
                IY  = sr["y"]  + IG
                IDX = sr["dx"] - IG * 2
                IDY = sr["dy"] - IG * 2
                if IDX < 0.8 or IDY < 0.8:
                    continue

                q    = stock_quotes.get(sym, {})
                pct  = q.get("change_pct", 0.0)
                clr  = _heat(pct)
                tc   = "#ffffff"   # 항상 흰색 (다크 배경 전제)

                ax.add_patch(mp.FancyBboxPatch(
                    (IX, IY), IDX, IDY,
                    boxstyle="round,pad=0.0",
                    facecolor=clr, edgecolor=BG,
                    linewidth=1.5, zorder=2))

                sign = "+" if pct > 0 else ""
                pstr = f"{sign}{pct:.1f}%"
                md   = min(IDX, IDY)

                # 가독성 기준: 블록이 충분히 클 때만 텍스트
                if IDX > 6 and IDY > 6:
                    # 티커 + % 두 줄
                    tfs = max(9, min(16, md * 0.72))
                    pfs = max(9, min(15, md * 0.65))
                    ax.text(IX+IDX/2, IY+IDY/2+IDY*0.12, sym,
                            ha="center", va="center", color=tc,
                            fontsize=tfs, fontweight="bold", zorder=3, clip_on=True)
                    ax.text(IX+IDX/2, IY+IDY/2-IDY*0.14, pstr,
                            ha="center", va="center", color=tc,
                            fontsize=pfs, zorder=3, clip_on=True)
                elif IDX > 3.5 and IDY > 3.5:
                    # 티커만
                    tfs = max(7.5, min(12, md * 0.68))
                    ax.text(IX+IDX/2, IY+IDY/2, sym,
                            ha="center", va="center", color=tc,
                            fontsize=tfs, fontweight="bold", zorder=3, clip_on=True)

        # 색상 범례
        N = 50
        for i in range(N):
            p = -4.0 + i * (8.0/N)
            fig.add_axes([0.03 + i*(0.44/N), 0.005, 0.44/N, 0.018],
                         facecolor=_heat(p)).set_axis_off()
        fig.text(0.03,  0.027, "−4%", color=TEXT_SEC, fontsize=9, va="bottom")
        fig.text(0.25,  0.027, "0",   color=TEXT_SEC, fontsize=9, va="bottom", ha="center")
        fig.text(0.475, 0.027, "+4%", color=TEXT_SEC, fontsize=9, va="bottom")

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                    facecolor=BG, edgecolor="none")
        plt.close(fig)
        buf.seek(0)
        logger.info("[차트] S&P500 맵 생성 완료")
        return buf

    except Exception as e:
        logger.error(f"[차트] S&P500 맵 생성 실패: {e}", exc_info=True)
        return None
