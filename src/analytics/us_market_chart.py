"""
미국증시 마감 리포트 — 차트 이미지 생성 (v2)

레이아웃:
  상단: 주요 지수 카드 (2행 × 3열, 색상 배경)
  하단: S&P 500 섹터 히트맵 (treemap)
"""

from __future__ import annotations

import io
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# ── 섹터 ETF 메타 (표시명 / S&P500 비중) ─────────────────────────────────────
SECTOR_META: Dict[str, Dict] = {
    "XLK":  {"name": "Tech",        "full": "Technology",        "weight": 29.0},
    "XLF":  {"name": "Finance",     "full": "Financials",        "weight": 13.0},
    "XLV":  {"name": "Health",      "full": "Health Care",       "weight": 12.0},
    "XLY":  {"name": "Disc.",       "full": "Cons. Discret.",    "weight": 11.0},
    "XLC":  {"name": "Comm.",       "full": "Comm. Services",    "weight":  9.0},
    "XLI":  {"name": "Indust.",     "full": "Industrials",       "weight":  8.0},
    "XLP":  {"name": "Staples",     "full": "Cons. Staples",     "weight":  6.0},
    "XLE":  {"name": "Energy",      "full": "Energy",            "weight":  4.0},
    "XLB":  {"name": "Materials",   "full": "Materials",         "weight":  3.0},
    "XLRE": {"name": "Real Est.",   "full": "Real Estate",       "weight":  2.5},
    "XLU":  {"name": "Utilities",   "full": "Utilities",         "weight":  2.5},
}

# 표시할 지수 순서
INDEX_ORDER = [
    ("^GSPC",  "S&P 500"),
    ("^IXIC",  "NASDAQ"),
    ("^DJI",   "DOW"),
    ("^RUT",   "Russell 2K"),
    ("^SOX",   "SOX"),
    ("^VIX",   "VIX"),
]

# ── 색상 팔레트 ───────────────────────────────────────────────────────────────
BG           = "#0d1117"
CARD_BG      = "#161b22"
DIVIDER      = "#30363d"
TEXT_PRI     = "#e6edf3"
TEXT_SEC     = "#8b949e"

# 수익률 기반 카드 색상
def _card_colors(pct: float):
    """등락률 → (카드 배경, 강조 테두리, % 텍스트 색상)"""
    if pct >= 1.5:
        return "#0d2818", "#2ea043", "#3fb950"
    if pct >= 0.3:
        return "#0d2011", "#1c6b32", "#26a641"
    if pct > -0.3:
        return "#161b22", "#30363d", "#8b949e"
    if pct > -1.5:
        return "#2d1117", "#6e1c1c", "#f85149"
    return "#3d0b0b", "#da3633", "#ff7b72"

def _heatmap_color(pct: float) -> str:
    """섹터 히트맵 색상 — 선명한 레드↔그린"""
    clamp = max(-4.0, min(4.0, pct))
    if clamp >= 0:
        t = clamp / 4.0
        r = int(13  + (0   - 13 ) * t)
        g = int(27  + (180 - 27 ) * t)
        b = int(18  + (50  - 18 ) * t)
    else:
        t = (-clamp) / 4.0
        r = int(13  + (218 - 13 ) * t)
        g = int(27  + (54  - 27 ) * t)
        b = int(18  + (51  - 18 ) * t)
    return f"#{r:02x}{g:02x}{b:02x}"

def _lum(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2],16), int(h[2:4],16), int(h[4:6],16)
    return (0.299*r + 0.587*g + 0.114*b) / 255


def _setup_font():
    """한글 폰트 설정 → font properties 반환"""
    import os, matplotlib.font_manager as fm, matplotlib
    candidates = [
        "/home/user/.local/share/fonts/NotoSansKR.ttf",
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ]
    for fp in candidates:
        if os.path.exists(fp):
            fm.fontManager.addfont(fp)
            prop = fm.FontProperties(fname=fp)
            matplotlib.rcParams["font.family"] = prop.get_name()
            matplotlib.rcParams["axes.unicode_minus"] = False
            return prop
    return None


def generate_us_market_chart(
    quotes: Dict[str, Any],
    date_str: str = "",
    avg_pct: float = 0.0,
) -> Optional[io.BytesIO]:
    """
    미국증시 차트 이미지 생성

    Args:
        quotes:   {symbol: {price, change_pct, ...}} — fetch_us_market_summary() 반환값
        date_str: 날짜 문자열 (헤더 표시)
        avg_pct:  지수 평균 등락률

    Returns:
        PNG BytesIO (실패 시 None)
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        import matplotlib.patheffects as pe
        import squarify
        import numpy as np

        _setup_font()

        # ── 캔버스 ────────────────────────────────────────────────────────
        FIG_W, FIG_H = 14, 9.5
        fig = plt.figure(figsize=(FIG_W, FIG_H), facecolor=BG, dpi=110)

        # ── 타이틀 ────────────────────────────────────────────────────────
        if avg_pct >= 1.0:
            mood, mood_c = "▲  강세 마감", "#3fb950"
        elif avg_pct <= -1.0:
            mood, mood_c = "▼  약세 마감", "#ff7b72"
        else:
            mood, mood_c = "●  보합 마감", "#8b949e"

        fig.text(0.04, 0.965, f"미국증시 마감  —  {date_str}",
                 color=TEXT_PRI, fontsize=15, fontweight="bold", va="top")
        fig.text(0.96, 0.965, mood,
                 color=mood_c, fontsize=13, fontweight="bold", va="top", ha="right")

        # 가로 구분선
        fig.add_artist(plt.Line2D([0.04, 0.96], [0.935, 0.935],
                                  transform=fig.transFigure,
                                  color=DIVIDER, linewidth=0.8))

        # ═══════════════════════════════════════════════════════════════════
        # 상단: 지수 카드 (2행 × 3열)
        # ═══════════════════════════════════════════════════════════════════
        CARD_ROWS, CARD_COLS = 2, 3
        CARD_Y0   = 0.60   # 카드 영역 하단 (figure 좌표)
        CARD_Y1   = 0.925  # 카드 영역 상단
        CARD_X0   = 0.04
        CARD_X1   = 0.96
        PAD_X     = 0.012
        PAD_Y     = 0.012

        card_w = (CARD_X1 - CARD_X0 - PAD_X * (CARD_COLS - 1)) / CARD_COLS
        card_h = (CARD_Y1 - CARD_Y0 - PAD_Y * (CARD_ROWS - 1)) / CARD_ROWS

        for idx, (sym, label) in enumerate(INDEX_ORDER):
            row, col = divmod(idx, CARD_COLS)
            cx = CARD_X0 + col * (card_w + PAD_X)
            cy = CARD_Y1 - (row + 1) * card_h - row * PAD_Y   # 위에서 아래로

            q = quotes.get(sym)
            pct   = q["change_pct"] if q else 0.0
            price = q["price"]      if q else 0.0

            bg_c, border_c, pct_c = _card_colors(pct)

            # 카드 배경
            card_rect = mpatches.FancyBboxPatch(
                (cx, cy), card_w, card_h,
                boxstyle="round,pad=0.005",
                transform=fig.transFigure,
                facecolor=bg_c,
                edgecolor=border_c,
                linewidth=1.5,
                clip_on=False,
                zorder=2,
            )
            fig.add_artist(card_rect)

            # 지수명
            fig.text(cx + 0.014, cy + card_h - 0.012,
                     label, color=TEXT_SEC, fontsize=10,
                     fontweight="normal", va="top", transform=fig.transFigure)

            # % 변동 (크게)
            sign = "+" if pct > 0 else ""
            arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "●")
            fig.text(cx + card_w / 2, cy + card_h / 2 + 0.016,
                     f"{arrow}  {sign}{pct:.2f}%",
                     color=pct_c, fontsize=17, fontweight="bold",
                     ha="center", va="center", transform=fig.transFigure)

            # 현재가
            if sym == "^VIX":
                price_str = f"{price:.2f}"
            elif price >= 10000:
                price_str = f"{price:,.0f}"
            else:
                price_str = f"{price:,.2f}"
            fig.text(cx + card_w / 2, cy + 0.014,
                     price_str, color=TEXT_SEC, fontsize=9,
                     ha="center", va="bottom", transform=fig.transFigure,
                     fontfamily="monospace")

        # ── 구분선 ────────────────────────────────────────────────────────
        fig.add_artist(plt.Line2D([0.04, 0.96], [0.585, 0.585],
                                  transform=fig.transFigure,
                                  color=DIVIDER, linewidth=0.8))

        # 소제목
        fig.text(0.04, 0.575, "S&P 500  Sector Heatmap",
                 color=TEXT_SEC, fontsize=10, va="top")

        # ═══════════════════════════════════════════════════════════════════
        # 하단: 섹터 히트맵
        # ═══════════════════════════════════════════════════════════════════
        # 절대 좌표 axes (figure-level)
        MAP_L  = 0.04
        MAP_R  = 0.96
        MAP_B  = 0.04
        MAP_T  = 0.555
        map_w  = MAP_R - MAP_L
        map_h  = MAP_T - MAP_B

        ax = fig.add_axes([MAP_L, MAP_B, map_w, map_h], facecolor=BG)
        ax.set_xlim(0, 100)
        ax.set_ylim(0, 100)
        ax.axis("off")

        # 섹터 데이터
        sec_items = []
        for sym, meta in SECTOR_META.items():
            q    = quotes.get(sym)
            pct  = q["change_pct"] if q else 0.0
            sec_items.append({
                "sym":    sym,
                "name":   meta["name"],
                "full":   meta["full"],
                "weight": meta["weight"],
                "pct":    pct,
                "color":  _heatmap_color(pct),
            })

        sizes  = [s["weight"] for s in sec_items]
        rects  = squarify.squarify(
            squarify.normalize_sizes(sizes, 100, 100),
            x=0, y=0, dx=100, dy=100,
        )

        for rect, item in zip(rects, sec_items):
            x, y, w, h = rect["x"], rect["y"], rect["dx"], rect["dy"]
            GAP = 0.6

            patch = mpatches.FancyBboxPatch(
                (x + GAP, y + GAP), w - GAP*2, h - GAP*2,
                boxstyle="round,pad=0.0",
                facecolor=item["color"],
                edgecolor=BG,
                linewidth=2,
                zorder=2,
            )
            ax.add_patch(patch)

            tc = "#ffffff" if _lum(item["color"]) < 0.50 else "#0d1117"
            cx_, cy_ = x + w/2, y + h/2
            sign = "+" if item["pct"] > 0 else ""
            pct_str = f"{sign}{item['pct']:.2f}%"

            # 블록 크기에 따라 폰트 조절
            base_fs = min(w, h) * 1.05
            name_fs = max(7.5, min(14, base_fs * 0.65))
            pct_fs  = max(8.5, min(16, base_fs * 0.75))

            if w > 8 and h > 8:
                # 섹터명 + 등락률 두 줄
                ax.text(cx_, cy_ + h * 0.10, item["name"],
                        ha="center", va="center", color=tc,
                        fontsize=name_fs, fontweight="bold", zorder=3)
                ax.text(cx_, cy_ - h * 0.14, pct_str,
                        ha="center", va="center", color=tc,
                        fontsize=pct_fs, zorder=3,
                        alpha=0.92)
            elif w > 5 and h > 5:
                ax.text(cx_, cy_, f"{item['name']}\n{pct_str}",
                        ha="center", va="center", color=tc,
                        fontsize=max(6, name_fs*0.8),
                        fontweight="bold", zorder=3,
                        linespacing=1.3)

        # ── 색상 범례 ─────────────────────────────────────────────────────
        LEG_Y    = -7
        LEG_H    =  4
        n_steps  = 40
        step_w   = 100 / n_steps
        for i in range(n_steps):
            p = -4.0 + i * (8.0 / n_steps)
            ax.add_patch(mpatches.Rectangle(
                (i * step_w, LEG_Y), step_w, LEG_H,
                facecolor=_heatmap_color(p), edgecolor="none", zorder=2,
            ))
        ax.text(0,   LEG_Y - 1.5, "-4%", ha="left",   va="top", color=TEXT_SEC, fontsize=8)
        ax.text(50,  LEG_Y - 1.5, "0",   ha="center", va="top", color=TEXT_SEC, fontsize=8)
        ax.text(100, LEG_Y - 1.5, "+4%", ha="right",  va="top", color=TEXT_SEC, fontsize=8)
        ax.set_ylim(LEG_Y - 5, 100)

        # ── 저장 ─────────────────────────────────────────────────────────
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=110, bbox_inches="tight",
                    facecolor=BG, edgecolor="none")
        plt.close(fig)
        buf.seek(0)
        logger.info("[차트] 미국증시 차트 생성 완료")
        return buf

    except Exception as e:
        logger.error(f"[차트] 생성 실패: {e}", exc_info=True)
        return None


def generate_sp500_map(
    stock_quotes: Dict[str, Any],
    date_str: str = "",
) -> Optional[io.BytesIO]:
    """
    S&P 500 개별 종목 히트맵 (finviz 스타일 중첩 treemap)

    섹터 → 종목 2단계 계층 구조:
    - 외부 사각형: 섹터 (S&P500 비중에 비례)
    - 내부 사각형: 개별 종목 (섹터 내 시총 비중)
    - 색상: 개별 종목 등락률

    Args:
        stock_quotes: {symbol: {price, change_pct, name}} — fetch_sp500_stocks() 반환값
        date_str:     헤더 날짜 문자열

    Returns:
        PNG BytesIO (실패 시 None)
    """
    try:
        from ..data.providers.us_market_data import SP500_STOCKS

        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        import squarify

        _setup_font()

        FIG_W, FIG_H = 15, 9
        fig = plt.figure(figsize=(FIG_W, FIG_H), facecolor=BG, dpi=110)

        # ── 타이틀 ────────────────────────────────────────────────────────
        fig.text(0.03, 0.97, f"S&P 500 Map  —  {date_str}",
                 color=TEXT_PRI, fontsize=14, fontweight="bold", va="top")
        fig.text(0.97, 0.97, "size ∝ market cap  |  color = % change",
                 color=TEXT_SEC, fontsize=9, va="top", ha="right")

        # ── 메인 axes (섹터 treemap 영역) ────────────────────────────────
        ax = fig.add_axes([0.01, 0.02, 0.98, 0.91], facecolor=BG)
        ax.set_xlim(0, 100)
        ax.set_ylim(0, 100)
        ax.axis("off")

        # ── 섹터 외곽 사각형 계산 ─────────────────────────────────────────
        sec_keys   = list(SP500_STOCKS.keys())          # ETF 심볼 순서
        sec_meta   = {k: SECTOR_META[k] for k in sec_keys}
        sec_weights = [sec_meta[k]["weight"] for k in sec_keys]

        sec_rects = squarify.squarify(
            squarify.normalize_sizes(sec_weights, 100, 100),
            x=0, y=0, dx=100, dy=100,
        )

        OUTER_GAP = 0.8   # 섹터 간격
        INNER_GAP = 0.35  # 종목 간격

        for sec_rect, sec_key in zip(sec_rects, sec_keys):
            SX  = sec_rect["x"]  + OUTER_GAP
            SY  = sec_rect["y"]  + OUTER_GAP
            SDX = sec_rect["dx"] - OUTER_GAP * 2
            SDY = sec_rect["dy"] - OUTER_GAP * 2

            if SDX <= 0 or SDY <= 0:
                continue

            # 섹터 배경 (어두운 테두리 효과)
            ax.add_patch(mpatches.FancyBboxPatch(
                (SX, SY), SDX, SDY,
                boxstyle="square,pad=0",
                facecolor="#1c2128",
                edgecolor="#30363d",
                linewidth=1.0,
                zorder=1,
            ))

            # 섹터명 레이블 영역 높이
            LABEL_H = min(SDY * 0.12, 4.0)
            LABEL_H = max(LABEL_H, 2.2)

            # 섹터명
            sec_name = sec_meta[sec_key]["name"]
            ax.text(SX + SDX * 0.5, SY + SDY - LABEL_H * 0.45,
                    sec_name,
                    ha="center", va="center", color=TEXT_PRI,
                    fontsize=max(6, min(10, SDX * 0.55)),
                    fontweight="bold", zorder=4, clip_on=True)

            # ── 종목 내부 treemap ─────────────────────────────────────────
            stocks = SP500_STOCKS[sec_key]  # [(sym, name, weight), ...]
            stock_weights = [w for _, _, w in stocks]
            INNER_Y = SY
            INNER_H = SDY - LABEL_H

            if INNER_H <= 0:
                continue

            stock_rects = squarify.squarify(
                squarify.normalize_sizes(stock_weights, SDX, INNER_H),
                x=SX, y=INNER_Y, dx=SDX, dy=INNER_H,
            )

            for sr, (sym, disp_name, _) in zip(stock_rects, stocks):
                IX  = sr["x"]  + INNER_GAP
                IY  = sr["y"]  + INNER_GAP
                IDX = sr["dx"] - INNER_GAP * 2
                IDY = sr["dy"] - INNER_GAP * 2

                if IDX <= 0.5 or IDY <= 0.5:
                    continue

                q    = stock_quotes.get(sym, {})
                pct  = q.get("change_pct", 0.0)
                clr  = _heatmap_color(pct)
                tc   = "#ffffff" if _lum(clr) < 0.50 else "#0d1117"

                ax.add_patch(mpatches.FancyBboxPatch(
                    (IX, IY), IDX, IDY,
                    boxstyle="round,pad=0.0",
                    facecolor=clr,
                    edgecolor=BG,
                    linewidth=1.2,
                    zorder=2,
                ))

                sign   = "+" if pct > 0 else ""
                pct_str = f"{sign}{pct:.1f}%"

                # 블록 크기에 따라 텍스트 배치 조정
                min_dim = min(IDX, IDY)
                tick_fs = max(5.5, min(13, min_dim * 0.75))
                pct_fs  = max(5.0, min(11, min_dim * 0.62))

                if IDX > 4.5 and IDY > 4.5:
                    # 티커 + % 두 줄
                    ax.text(IX + IDX/2, IY + IDY/2 + IDY*0.10,
                            sym,
                            ha="center", va="center", color=tc,
                            fontsize=tick_fs, fontweight="bold",
                            zorder=3, clip_on=True)
                    ax.text(IX + IDX/2, IY + IDY/2 - IDY*0.12,
                            pct_str,
                            ha="center", va="center", color=tc,
                            fontsize=pct_fs, alpha=0.92,
                            zorder=3, clip_on=True)
                elif IDX > 2.5 and IDY > 2.5:
                    # 공간 부족 → 한 줄 (티커만)
                    ax.text(IX + IDX/2, IY + IDY/2,
                            sym,
                            ha="center", va="center", color=tc,
                            fontsize=max(5, tick_fs * 0.8), fontweight="bold",
                            zorder=3, clip_on=True)

        # ── 색상 범례 ─────────────────────────────────────────────────────
        # figure 하단 얇은 그라데이션 바
        N = 50
        for i in range(N):
            p = -4.0 + i * (8.0 / N)
            fig.add_axes([0.03 + i * (0.44 / N), 0.005, 0.44/N, 0.015],
                         facecolor=_heatmap_color(p)).set_axis_off()
        fig.text(0.03,  0.024, "−4%", color=TEXT_SEC, fontsize=7.5, va="bottom")
        fig.text(0.25,  0.024, "0",   color=TEXT_SEC, fontsize=7.5, va="bottom",
                 ha="center")
        fig.text(0.47,  0.024, "+4%", color=TEXT_SEC, fontsize=7.5, va="bottom",
                 ha="right")

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=110, bbox_inches="tight",
                    facecolor=BG, edgecolor="none")
        plt.close(fig)
        buf.seek(0)
        logger.info("[차트] S&P500 맵 생성 완료")
        return buf

    except Exception as e:
        logger.error(f"[차트] S&P500 맵 생성 실패: {e}", exc_info=True)
        return None
