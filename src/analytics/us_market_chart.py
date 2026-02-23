"""
미국증시 마감 리포트 — 차트 이미지 생성

S&P 500 섹터 히트맵 + 주요 지수 바 차트를 하나의 이미지로 생성하여
BytesIO로 반환합니다.
"""

from __future__ import annotations

import io
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

# ── 섹터 ETF → 섹터명 / 약칭 / S&P500 비중(추정) ──────────────────────────
SECTOR_META: Dict[str, Dict] = {
    "XLK":  {"name": "Technology",          "short": "IT",      "weight": 29.0},
    "XLF":  {"name": "Financials",          "short": "금융",     "weight": 13.0},
    "XLV":  {"name": "Health Care",         "short": "헬스케어",  "weight": 12.0},
    "XLY":  {"name": "Cons. Discret.",      "short": "임의소비",  "weight": 11.0},
    "XLC":  {"name": "Comm. Services",      "short": "통신",     "weight":  9.0},
    "XLI":  {"name": "Industrials",         "short": "산업재",   "weight":  8.0},
    "XLP":  {"name": "Cons. Staples",       "short": "필수소비",  "weight":  6.0},
    "XLE":  {"name": "Energy",              "short": "에너지",   "weight":  4.0},
    "XLB":  {"name": "Materials",           "short": "소재",     "weight":  3.0},
    "XLRE": {"name": "Real Estate",         "short": "부동산",   "weight":  2.5},
    "XLU":  {"name": "Utilities",           "short": "유틸리티",  "weight":  2.5},
}

# 지수 표시 순서 및 표시 이름
INDEX_DISPLAY: List[tuple] = [
    ("^GSPC",  "S&P 500"),
    ("^IXIC",  "NASDAQ"),
    ("^DJI",   "DOW"),
    ("^RUT",   "Russell 2K"),
    ("^VIX",   "VIX"),
    ("^SOX",   "SOX"),
]

# ── 색상 팔레트 ──────────────────────────────────────────────────────────────
BG_COLOR      = "#0d1117"
CARD_COLOR    = "#161b22"
GRID_COLOR    = "#21262d"
TEXT_PRIMARY  = "#e6edf3"
TEXT_MUTED    = "#7d8590"
GREEN_STRONG  = "#2ea043"
GREEN_MID     = "#56d364"
GREEN_LIGHT   = "#a3f0a3"
RED_STRONG    = "#da3633"
RED_MID       = "#f85149"
RED_LIGHT     = "#ffa198"
NEUTRAL_COLOR = "#444c56"


def _pct_to_color(pct: float) -> str:
    """등락률 → 히트맵 색상 (진한 빨강 ↔ 진한 초록)"""
    clamp = max(-5.0, min(5.0, pct))
    if clamp >= 0:
        t = clamp / 5.0
        # 연초록 → 진초록
        r = int(0x16 + (0x2e - 0x16) * t)
        g = int(0x1b + (0xa0 - 0x1b) * t)
        b = int(0x22 + (0x43 - 0x22) * t)
    else:
        t = (-clamp) / 5.0
        # 연빨강 → 진빨강
        r = int(0x16 + (0xda - 0x16) * t)
        g = int(0x1b + (0x36 - 0x1b) * t)
        b = int(0x22 + (0x33 - 0x22) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def _text_color_for_bg(bg_hex: str) -> str:
    """배경색 밝기에 따라 텍스트 색상 결정"""
    bg_hex = bg_hex.lstrip("#")
    r, g, b = int(bg_hex[0:2], 16), int(bg_hex[2:4], 16), int(bg_hex[4:6], 16)
    lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return "#ffffff" if lum < 0.45 else "#0d1117"


def generate_us_market_chart(
    quotes: Dict[str, Any],
    date_str: str = "",
    avg_pct: float = 0.0,
) -> Optional[io.BytesIO]:
    """
    미국증시 차트 이미지 생성

    Args:
        quotes: us_market_data.fetch_us_market_summary() 반환값
                {symbol: {price, change_pct, name, ...}, ...}
        date_str: 날짜 문자열 (헤더 표시용)
        avg_pct: 지수 평균 등락률 (mood 계산용)

    Returns:
        PNG 이미지가 담긴 BytesIO (실패 시 None)
    """
    try:
        import matplotlib
        matplotlib.use("Agg")  # 화면 없이 렌더링
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        import matplotlib.font_manager as fm
        import squarify
        import numpy as np

        # ── 한글 폰트 설정 ────────────────────────────────────────────────
        _FONT_CANDIDATES = [
            "/home/user/.local/share/fonts/NotoSansKR.ttf",
            "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        ]
        _font_prop = None
        for _fp in _FONT_CANDIDATES:
            import os as _os
            if _os.path.exists(_fp):
                _font_prop = fm.FontProperties(fname=_fp)
                # matplotlib 기본 폰트 패밀리에도 등록
                fm.fontManager.addfont(_fp)
                _font_name = fm.FontProperties(fname=_fp).get_name()
                matplotlib.rcParams["font.family"] = _font_name
                matplotlib.rcParams["axes.unicode_minus"] = False
                break

        fig = plt.figure(figsize=(14, 9), facecolor=BG_COLOR)

        # ── 레이아웃: 상단(지수) 40% + 하단(섹터맵) 60% ──────────────────
        gs = fig.add_gridspec(
            2, 1,
            height_ratios=[2, 3],
            hspace=0.35,
            left=0.03, right=0.97,
            top=0.92, bottom=0.04,
        )

        # ════════════════════════════════════════════════════════════════════
        # 상단: 주요 지수 수평 바 차트
        # ════════════════════════════════════════════════════════════════════
        ax_idx = fig.add_subplot(gs[0])
        ax_idx.set_facecolor(CARD_COLOR)

        idx_data = []
        for sym, label in INDEX_DISPLAY:
            q = quotes.get(sym)
            if q:
                idx_data.append((label, q["change_pct"], q["price"]))

        if idx_data:
            labels   = [d[0]   for d in idx_data]
            pcts     = [d[1]   for d in idx_data]
            prices   = [d[2]   for d in idx_data]
            colors   = [_pct_to_color(p) for p in pcts]

            y_pos = np.arange(len(labels))
            bars = ax_idx.barh(y_pos, pcts, color=colors, height=0.55,
                               edgecolor="none")

            # 기준선(0%)
            ax_idx.axvline(0, color=GRID_COLOR, linewidth=1.2, zorder=0)

            # 레이블
            ax_idx.set_yticks(y_pos)
            ax_idx.set_yticklabels(labels, color=TEXT_PRIMARY, fontsize=11,
                                   fontweight="bold")
            ax_idx.tick_params(axis="x", colors=TEXT_MUTED, labelsize=9)
            ax_idx.tick_params(axis="y", length=0)

            # 막대 끝에 수치 표시
            for i, (bar, pct, price) in enumerate(zip(bars, pcts, prices)):
                xval = bar.get_width()
                offset = 0.03
                ha = "left" if xval >= 0 else "right"
                x_text = xval + offset if xval >= 0 else xval - offset
                sign = "+" if pct > 0 else ""
                ax_idx.text(x_text, i, f"{sign}{pct:.2f}%",
                            va="center", ha=ha, color=TEXT_PRIMARY,
                            fontsize=10, fontweight="bold")
                # 지수값은 오른쪽 끝에 표시
                ax_idx.text(0.99, i / len(idx_data) + 0.5 / len(idx_data),
                            f"{price:,.1f}",
                            transform=ax_idx.transAxes,
                            va="center", ha="right",
                            color=TEXT_MUTED, fontsize=9)

            # 그리드
            ax_idx.xaxis.grid(True, color=GRID_COLOR, linewidth=0.5, alpha=0.6)
            ax_idx.set_axisbelow(True)

            # 테두리
            for spine in ax_idx.spines.values():
                spine.set_edgecolor(GRID_COLOR)

        # 소제목
        ax_idx.set_title("주요 지수", color=TEXT_MUTED, fontsize=10,
                         loc="left", pad=8, fontweight="normal")

        # ════════════════════════════════════════════════════════════════════
        # 하단: S&P 500 섹터 히트맵 (treemap)
        # ════════════════════════════════════════════════════════════════════
        ax_sec = fig.add_subplot(gs[1])
        ax_sec.set_facecolor(BG_COLOR)
        ax_sec.set_xlim(0, 100)
        ax_sec.set_ylim(0, 100)
        ax_sec.axis("off")

        # 섹터 데이터 수집 (ETF 심볼 순서)
        sec_items = []
        for sym, meta in SECTOR_META.items():
            q = quotes.get(sym)
            pct = q["change_pct"] if q else 0.0
            sec_items.append({
                "sym":    sym,
                "short":  meta["short"],
                "name":   meta["name"],
                "weight": meta["weight"],
                "pct":    pct,
                "color":  _pct_to_color(pct),
            })

        sizes   = [s["weight"] for s in sec_items]
        colors  = [s["color"]  for s in sec_items]

        rects = squarify.squarify(
            squarify.normalize_sizes(sizes, 100, 100),
            x=0, y=0, dx=100, dy=100,
        )

        for rect, item in zip(rects, sec_items):
            x, y, w, h = rect["x"], rect["y"], rect["dx"], rect["dy"]

            # 배경 사각형
            patch = mpatches.FancyBboxPatch(
                (x + 0.3, y + 0.3), w - 0.6, h - 0.6,
                boxstyle="round,pad=0.0",
                facecolor=item["color"],
                edgecolor=BG_COLOR,
                linewidth=1.5,
                zorder=2,
            )
            ax_sec.add_patch(patch)

            # 텍스트 (섹터명 + 등락률)
            tc = _text_color_for_bg(item["color"])
            cx, cy = x + w / 2, y + h / 2
            sign = "+" if item["pct"] > 0 else ""
            pct_str = f"{sign}{item['pct']:.2f}%"

            # 블록이 충분히 크면 두 줄로
            if w > 10 and h > 10:
                ax_sec.text(cx, cy + h * 0.08, item["short"],
                            ha="center", va="center", color=tc,
                            fontsize=max(7, min(12, w * 0.8)),
                            fontweight="bold", zorder=3)
                ax_sec.text(cx, cy - h * 0.12, pct_str,
                            ha="center", va="center", color=tc,
                            fontsize=max(6, min(10, w * 0.6)),
                            zorder=3)
            elif w > 5 and h > 5:
                ax_sec.text(cx, cy, f"{item['short']}\n{pct_str}",
                            ha="center", va="center", color=tc,
                            fontsize=max(5, min(8, w * 0.7)),
                            fontweight="bold", zorder=3)

        ax_sec.set_title("S&P 500 섹터 히트맵", color=TEXT_MUTED, fontsize=10,
                         loc="left", pad=6, fontweight="normal")

        # ── 전체 타이틀 ──────────────────────────────────────────────────────
        if avg_pct >= 1.0:
            mood_str = "📈 강세 마감"
            mood_color = GREEN_MID
        elif avg_pct <= -1.0:
            mood_str = "📉 약세 마감"
            mood_color = RED_MID
        else:
            mood_str = "➡ 보합 마감"
            mood_color = TEXT_MUTED

        fig.text(0.03, 0.96, f"🇺🇸  미국증시 마감  {date_str}",
                 color=TEXT_PRIMARY, fontsize=14, fontweight="bold",
                 va="top")
        fig.text(0.97, 0.96, mood_str,
                 color=mood_color, fontsize=12, fontweight="bold",
                 va="top", ha="right")

        # ── 범례 (색상 스케일) ────────────────────────────────────────────
        legend_x = [0.03 + i * 0.017 for i in range(20)]
        legend_pcts = [-5 + i * 0.5 for i in range(20)]
        for lx, lp in zip(legend_x, legend_pcts):
            fig.add_axes([lx, 0.005, 0.015, 0.018]).set_facecolor(
                _pct_to_color(lp)
            )
            plt.gca().set_xticks([])
            plt.gca().set_yticks([])
            for sp in plt.gca().spines.values():
                sp.set_visible(False)
        fig.text(0.03, 0.026, "-5%", color=TEXT_MUTED, fontsize=7, va="bottom")
        fig.text(0.36, 0.026, "0%",  color=TEXT_MUTED, fontsize=7, va="bottom",
                 ha="center")
        fig.text(0.368, 0.026, "+5%", color=TEXT_MUTED, fontsize=7, va="bottom")

        # ── 저장 ────────────────────────────────────────────────────────────
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=110, bbox_inches="tight",
                    facecolor=BG_COLOR, edgecolor="none")
        plt.close(fig)
        buf.seek(0)

        logger.info("[차트] 미국증시 차트 생성 완료")
        return buf

    except Exception as e:
        logger.error(f"[차트] 미국증시 차트 생성 실패: {e}", exc_info=True)
        return None
