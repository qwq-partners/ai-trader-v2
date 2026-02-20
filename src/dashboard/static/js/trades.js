/**
 * AI Trader v2 - 거래 내역 (통합 거래+정산)
 *
 * Note: innerHTML usage is safe here as all data comes from our own
 * trusted backend API (trade_events table), not user input.
 */

const dateInput = document.getElementById('trade-date');
const btnToday = document.getElementById('btn-today');
const loadingEl = document.getElementById('loading-indicator');

let currentFilter = 'all';
let cachedEvents = [];

function todayStr() {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}

// HTML 이스케이프 (XSS 방지)
function esc(s) {
    if (s === null || s === undefined) return '';
    const d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
}

// ============================================================
// 데이터 로드 (양쪽 API 병렬 호출)
// ============================================================

async function loadTradeData(dateStr, type) {
    dateStr = dateStr || todayStr();
    type = type || currentFilter;
    loadingEl.style.display = 'inline';

    try {
        // 두 API 병렬 호출
        const [events, settlement] = await Promise.all([
            api(`/api/trade-events?date=${dateStr}&type=${type}`).catch(() => []),
            api(`/api/daily-settlement?date=${encodeURIComponent(dateStr)}`).catch(() => null),
        ]);

        cachedEvents = events;

        // 렌더링
        renderEvents(events);
        renderExitTypeChart(events);

        // 정산 데이터 있으면 정산 기반 요약, 없으면 이벤트 기반
        if (settlement && !settlement.error) {
            renderSettlementSummary(settlement);
            renderHoldings(settlement.holdings);
        } else {
            renderEventSummary(events);
            hideHoldings();
        }

        // 청산유형별 금액 요약 테이블
        renderExitTypeSummary(events);

        updateFilterCounts(dateStr);
    } catch (e) {
        console.warn('[거래] 데이터 로드 오류:', e);
    } finally {
        loadingEl.style.display = 'none';
    }
}

async function updateFilterCounts(dateStr) {
    try {
        const all = await api(`/api/trade-events?date=${dateStr}&type=all`);
        const buys = all.filter(e => e.event_type === 'BUY');
        const sells = all.filter(e => e.event_type === 'SELL');

        document.querySelectorAll('.filter-tab').forEach(tab => {
            const type = tab.dataset.type;
            const count = tab.querySelector('.filter-count');
            if (type === 'all') count.textContent = all.length;
            else if (type === 'buy') count.textContent = buys.length;
            else if (type === 'sell') count.textContent = sells.length;
        });
    } catch (e) {
        // 카운트 실패는 무시
    }
}

// ============================================================
// 정산 기반 요약 (KIS 데이터 사용)
// ============================================================

function renderSettlementSummary(settlement) {
    if (!settlement) return;
    const s = settlement.summary;
    if (!s) return;
    const el = (id) => document.getElementById(id);

    const realized = el('s-realized');
    realized.textContent = formatPnl(s.realized_pnl);
    realized.className = 'stat-value mono ' + pnlClass(s.realized_pnl);

    const unrealized = el('s-unrealized');
    unrealized.textContent = formatPnl(s.unrealized_pnl);
    unrealized.className = 'stat-value mono ' + pnlClass(s.unrealized_pnl);

    // 익절/손절 합계 — sells 배열에서 계산
    const sells = settlement.sells || [];
    const tpTotal = sells.reduce((acc, t) => acc + ((t.pnl || 0) > 0 ? (t.pnl || 0) : 0), 0);
    const slTotal = sells.reduce((acc, t) => acc + ((t.pnl || 0) < 0 ? (t.pnl || 0) : 0), 0);

    const tpEl = el('s-tp-total');
    tpEl.textContent = tpTotal > 0 ? formatPnl(tpTotal) : '--';
    tpEl.className = 'stat-value mono ' + (tpTotal > 0 ? 'text-profit' : '');

    const slEl = el('s-sl-total');
    slEl.textContent = slTotal < 0 ? formatPnl(slTotal) : '--';
    slEl.className = 'stat-value mono ' + (slTotal < 0 ? 'text-loss' : '');

    // 승/패
    const wl = el('s-winloss');
    wl.textContent = '';
    const winSpan = document.createElement('span');
    winSpan.className = 'text-profit';
    winSpan.textContent = s.win_count;
    const sep = document.createTextNode(' / ');
    const lossSpan = document.createElement('span');
    lossSpan.className = 'text-loss';
    lossSpan.textContent = s.loss_count;
    wl.appendChild(winSpan);
    wl.appendChild(sep);
    wl.appendChild(lossSpan);
}

// ============================================================
// 이벤트 기반 요약 (DB 데이터 — 과거 날짜용)
// ============================================================

function renderEventSummary(events) {
    const el = (id) => document.getElementById(id);

    const sells = events.filter(e => e.event_type === 'SELL');
    const buys = events.filter(e => e.event_type === 'BUY');
    const holding = buys.filter(e => e.status === 'holding');

    const sellPnl = sells.reduce((s, e) => s + (e.pnl || 0), 0);
    const holdPnl = holding.reduce((s, e) => s + (e.pnl || 0), 0);

    const realized = el('s-realized');
    realized.textContent = sells.length > 0 ? formatPnl(sellPnl) : '--';
    realized.className = 'stat-value mono ' + pnlClass(sellPnl);

    const unrealized = el('s-unrealized');
    unrealized.textContent = holding.length > 0 ? formatPnl(holdPnl) : '--';
    unrealized.className = 'stat-value mono ' + pnlClass(holdPnl);

    // 익절/손절 합계
    const tpTotal = sells.reduce((acc, e) => acc + ((e.pnl || 0) > 0 ? (e.pnl || 0) : 0), 0);
    const slTotal = sells.reduce((acc, e) => acc + ((e.pnl || 0) < 0 ? (e.pnl || 0) : 0), 0);

    const tpEl = el('s-tp-total');
    tpEl.textContent = tpTotal > 0 ? formatPnl(tpTotal) : '--';
    tpEl.className = 'stat-value mono ' + (tpTotal > 0 ? 'text-profit' : '');

    const slEl = el('s-sl-total');
    slEl.textContent = slTotal < 0 ? formatPnl(slTotal) : '--';
    slEl.className = 'stat-value mono ' + (slTotal < 0 ? 'text-loss' : '');

    // 승/패
    const wins = sells.filter(e => (e.pnl || 0) > 0).length;
    const losses = sells.filter(e => (e.pnl || 0) < 0).length;
    const wl = el('s-winloss');
    wl.textContent = '';
    if (sells.length > 0) {
        const winSpan = document.createElement('span');
        winSpan.className = 'text-profit';
        winSpan.textContent = wins;
        const sep = document.createTextNode(' / ');
        const lossSpan = document.createElement('span');
        lossSpan.className = 'text-loss';
        lossSpan.textContent = losses;
        wl.appendChild(winSpan);
        wl.appendChild(sep);
        wl.appendChild(lossSpan);
    } else {
        wl.textContent = '--';
    }
}

// ============================================================
// 이벤트 로그 테이블
// ============================================================

function renderEvents(events) {
    const tbody = document.getElementById('trades-body');

    if (!events || events.length === 0) {
        tbody.textContent = '';
        const tr = document.createElement('tr');
        const td = document.createElement('td');
        td.colSpan = 10;
        td.style.cssText = 'padding: 40px 0; text-align: center; color: var(--text-muted); font-size: 0.85rem;';
        td.textContent = '거래 내역 없음';
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
    }

    const fragment = document.createDocumentFragment();
    events.forEach(ev => {
        const tr = document.createElement('tr');
        tr.className = 'border-b';
        tr.style.borderColor = 'rgba(99,102,241,0.08)';

        const isBuy = ev.event_type === 'BUY';
        const pnl = ev.pnl || 0;
        const pnlPct = ev.pnl_pct || 0;
        const pnlCls = pnl > 0 ? 'text-profit' : pnl < 0 ? 'text-loss' : '';

        // 시간
        const tdTime = createTd('py-2 pr-3 text-xs', ev.event_time ? formatTime(ev.event_time) : '--');
        tdTime.style.color = 'var(--text-muted)';

        // 종목
        const tdName = document.createElement('td');
        tdName.className = 'py-2 pr-3 font-medium';
        tdName.style.color = '#fff';
        if (ev.name && ev.name !== ev.symbol) {
            tdName.textContent = ev.name + ' ';
            const code = document.createElement('span');
            code.style.cssText = 'color:var(--text-muted); font-size:0.72rem;';
            code.textContent = ev.symbol || '';
            tdName.appendChild(code);
        } else {
            tdName.textContent = ev.symbol || '--';
        }

        // 유형 배지
        const tdType = document.createElement('td');
        tdType.className = 'py-2 pr-3';
        const typeBadge = document.createElement('span');
        typeBadge.className = 'badge';
        if (isBuy) {
            typeBadge.style.cssText = 'background:rgba(99,102,241,0.12); color:var(--accent-blue); border:1px solid rgba(99,102,241,0.15);';
            typeBadge.textContent = '매수';
        } else if (pnl >= 0) {
            typeBadge.className = 'badge badge-green';
            typeBadge.textContent = '매도';
        } else {
            typeBadge.className = 'badge badge-red';
            typeBadge.textContent = '매도';
        }
        tdType.appendChild(typeBadge);

        // 가격
        const tdPrice = document.createElement('td');
        tdPrice.className = 'py-2 pr-3 text-right mono';
        tdPrice.textContent = formatNumber(ev.price);
        if (isBuy && ev.current_price && ev.status === 'holding') {
            const arrow = document.createElement('span');
            arrow.style.cssText = 'color:var(--accent-cyan); font-size:0.75rem;';
            arrow.textContent = ' → ' + formatNumber(ev.current_price);
            tdPrice.appendChild(arrow);
        }

        // 수량
        const tdQty = createTd('py-2 pr-3 text-right mono', ev.quantity || '--');

        // 손익
        const tdPnl = createTd('py-2 pr-3 text-right mono ' + pnlCls, pnl !== 0 ? formatPnl(pnl) : '--');

        // 수익률
        const tdPct = createTd('py-2 pr-3 text-right mono ' + pnlCls, pnlPct !== 0 ? formatPct(pnlPct) : '--');

        // 전략
        const strategy = (ev.strategy && ev.strategy !== 'unknown') ? ev.strategy : '--';
        const tdStrategy = createTd('py-2 pr-3 text-xs', strategy);
        tdStrategy.style.color = 'var(--text-secondary)';

        // 상태
        const tdStatus = document.createElement('td');
        tdStatus.className = 'py-2 pr-3';
        tdStatus.appendChild(createStatusBadge(ev.status || '', isBuy));

        // 사유 (매도 이벤트만)
        const exitReason = (!isBuy && ev.exit_reason) ? ev.exit_reason : '';
        const tdReason = createTd('py-2 text-xs', exitReason);
        tdReason.style.color = 'var(--text-muted)';
        tdReason.style.maxWidth = '200px';
        tdReason.style.overflow = 'hidden';
        tdReason.style.textOverflow = 'ellipsis';
        tdReason.style.whiteSpace = 'nowrap';
        if (exitReason) tdReason.title = exitReason;

        tr.append(tdTime, tdName, tdType, tdPrice, tdQty, tdPnl, tdPct, tdStrategy, tdStatus, tdReason);
        fragment.appendChild(tr);
    });

    tbody.textContent = '';
    tbody.appendChild(fragment);
}

function createTd(className, text) {
    const td = document.createElement('td');
    td.className = className;
    td.textContent = text;
    return td;
}

function createStatusBadge(status, isBuy) {
    const span = document.createElement('span');
    const map = {
        'holding':           ['badge badge-blue', '보유중'],
        'partial':           ['badge badge-yellow', '부분매도'],
        'take_profit':       ['badge badge-green', '익절'],
        'first_take_profit': ['badge badge-green', '1차익절'],
        'second_take_profit':['badge badge-green', '2차익절'],
        'trailing':          ['badge badge-yellow', '트레일링'],
        'breakeven':         ['badge badge-yellow', '본전'],
        'stop_loss':         ['badge badge-red', '손절'],
        'manual':            ['badge badge-blue', '수동'],
        'kis_sync':          ['badge badge-blue', '동기화'],
        'closed':            ['badge badge-blue', '청산'],
    };
    const entry = map[status];
    if (entry) {
        span.className = entry[0];
        span.textContent = entry[1];
    } else if (status) {
        span.className = 'badge badge-blue';
        span.textContent = status;
    }
    return span;
}

// ============================================================
// 보유 현황
// ============================================================

function renderHoldings(holdings) {
    const section = document.getElementById('holdings-section');
    const tbody = document.getElementById('holdings-tbody');
    tbody.textContent = '';

    if (!holdings || holdings.length === 0) {
        section.style.display = 'none';
        return;
    }

    section.style.display = 'block';
    let totalUnrealized = 0;

    for (const h of holdings) {
        totalUnrealized += h.unrealized_pnl;
        const badgeCls = h.unrealized_pnl >= 0 ? 'badge-sell-win' : 'badge-sell-loss';
        const tr = document.createElement('tr');
        tr.style.borderBottom = '1px solid var(--border-subtle)';

        // 종목
        const tdName = document.createElement('td');
        tdName.style.padding = '10px 12px 10px 0';
        const nameDiv = document.createElement('div');
        nameDiv.style.cssText = 'font-weight:600; font-size:0.85rem;';
        nameDiv.textContent = h.name || h.symbol;
        const codeDiv = document.createElement('div');
        codeDiv.style.cssText = 'font-size:0.7rem; color:var(--text-muted);';
        codeDiv.textContent = h.symbol;
        tdName.appendChild(nameDiv);
        tdName.appendChild(codeDiv);

        // 수량
        const tdQty = createTd('mono', formatNumber(h.quantity));
        tdQty.style.cssText = 'padding:10px 12px 10px 0; text-align:right;';

        // 평균단가
        const tdAvg = createTd('mono', formatNumber(h.avg_price));
        tdAvg.style.cssText = 'padding:10px 12px 10px 0; text-align:right;';

        // 현재가
        const tdCur = createTd('mono', formatNumber(h.current_price));
        tdCur.style.cssText = 'padding:10px 12px 10px 0; text-align:right;';

        // 평가손익
        const tdPnl = document.createElement('td');
        tdPnl.style.cssText = 'padding:10px 0; text-align:right;';
        const pctBadge = document.createElement('span');
        pctBadge.className = 'badge ' + esc(badgeCls);
        pctBadge.textContent = esc(formatPct(h.unrealized_pct));
        const pnlDiv = document.createElement('div');
        pnlDiv.className = 'mono ' + esc(pnlClass(h.unrealized_pnl));
        pnlDiv.style.cssText = 'font-size:0.85rem; font-weight:600; margin-top:2px;';
        pnlDiv.textContent = formatPnl(h.unrealized_pnl);
        tdPnl.appendChild(pctBadge);
        tdPnl.appendChild(pnlDiv);

        tr.append(tdName, tdQty, tdAvg, tdCur, tdPnl);
        tbody.appendChild(tr);
    }

    // 합계
    const totalTr = document.createElement('tr');
    totalTr.style.borderTop = '2px solid var(--border-accent)';
    const tdLabel = document.createElement('td');
    tdLabel.colSpan = 4;
    tdLabel.style.cssText = 'padding:12px 0;text-align:right;font-weight:600;color:var(--text-secondary);font-size:0.85rem;';
    tdLabel.textContent = '합계';
    const tdTotal = document.createElement('td');
    tdTotal.style.cssText = 'padding:12px 0;text-align:right;';
    const totalDiv = document.createElement('div');
    totalDiv.className = 'mono ' + esc(pnlClass(totalUnrealized));
    totalDiv.style.cssText = 'font-size:1rem;font-weight:700;';
    totalDiv.textContent = formatPnl(totalUnrealized);
    tdTotal.appendChild(totalDiv);
    totalTr.append(tdLabel, tdTotal);
    tbody.appendChild(totalTr);
}

function hideHoldings() {
    document.getElementById('holdings-section').style.display = 'none';
}

// ============================================================
// 청산 유형 차트
// ============================================================

function renderExitTypeChart(events) {
    const sells = events.filter(e => e.event_type === 'SELL' && e.exit_type);
    if (sells.length === 0) {
        const el = document.getElementById('exit-type-chart');
        el.textContent = '';
        const msg = document.createElement('div');
        msg.style.cssText = 'display:flex; align-items:center; justify-content:center; height:100%; color:var(--text-muted); font-size:0.85rem;';
        msg.textContent = '데이터 없음';
        el.appendChild(msg);
        return;
    }

    const counts = {};
    sells.forEach(e => {
        const type = e.exit_type || 'unknown';
        counts[type] = (counts[type] || 0) + 1;
    });

    const labelMap = { take_profit: '익절', first_take_profit: '1차익절', second_take_profit: '2차익절', stop_loss: '손절', trailing: '트레일링', breakeven: '본전', manual: '수동', kis_sync: '동기화' };
    const colorMap = { take_profit: '#34d399', first_take_profit: '#34d399', second_take_profit: '#22d3ee', stop_loss: '#f87171', trailing: '#fbbf24', breakeven: '#fbbf24', manual: '#6366f1', kis_sync: '#a78bfa' };

    const labels = Object.keys(counts).map(k => labelMap[k] || k);
    const data = [{
        x: labels,
        y: Object.values(counts),
        type: 'bar',
        marker: {
            color: Object.keys(counts).map(k => colorMap[k] || '#a78bfa'),
            borderRadius: 4,
        },
    }];

    const layout = {
        paper_bgcolor: 'transparent', plot_bgcolor: 'transparent',
        margin: { t: 10, b: 40, l: 40, r: 10 },
        xaxis: { color: '#8892b0', gridcolor: 'rgba(99,102,241,0.08)' },
        yaxis: { color: '#8892b0', gridcolor: 'rgba(99,102,241,0.08)', dtick: 1 },
        height: 220,
        font: { color: '#e2e8f0', family: 'DM Sans, sans-serif' },
    };

    Plotly.react('exit-type-chart', data, layout, { displayModeBar: false, responsive: true });
}

// ============================================================
// 청산 유형별 금액 요약 테이블
// ============================================================

function renderExitTypeSummary(events) {
    const container = document.getElementById('exit-type-summary');
    const tbody = document.getElementById('exit-type-tbody');
    const sells = events.filter(e => e.event_type === 'SELL' && e.exit_type);

    if (sells.length === 0) {
        container.style.display = 'none';
        return;
    }

    container.style.display = 'block';
    tbody.textContent = '';

    const labelMap = {
        take_profit: '익절', first_take_profit: '1차익절', second_take_profit: '2차익절',
        third_take_profit: '3차익절', stop_loss: '손절', trailing: '트레일링',
        breakeven: '본전', manual: '수동', kis_sync: '동기화',
    };

    // exit_type별 그룹핑
    const groups = {};
    sells.forEach(e => {
        const type = e.exit_type;
        if (!groups[type]) groups[type] = { count: 0, pnlSum: 0, pctSum: 0 };
        groups[type].count++;
        groups[type].pnlSum += (e.pnl || 0);
        groups[type].pctSum += (e.pnl_pct || 0);
    });

    const fragment = document.createDocumentFragment();
    for (const [type, g] of Object.entries(groups)) {
        const avgPct = g.count > 0 ? g.pctSum / g.count : 0;
        const pnlCls = g.pnlSum > 0 ? 'text-profit' : g.pnlSum < 0 ? 'text-loss' : '';

        const tr = document.createElement('tr');
        tr.style.borderBottom = '1px solid var(--border-subtle)';

        const tdType = document.createElement('td');
        tdType.style.cssText = 'padding:8px 12px 8px 0; font-size:0.85rem; font-weight:500;';
        tdType.textContent = labelMap[type] || type;

        const tdCount = document.createElement('td');
        tdCount.style.cssText = 'padding:8px 12px 8px 0; text-align:right; font-size:0.85rem;';
        tdCount.className = 'mono';
        tdCount.textContent = g.count + '건';

        const tdPnl = document.createElement('td');
        tdPnl.style.cssText = 'padding:8px 12px 8px 0; text-align:right; font-size:0.85rem; font-weight:600;';
        tdPnl.className = 'mono ' + pnlCls;
        tdPnl.textContent = formatPnl(g.pnlSum);

        const tdAvgPct = document.createElement('td');
        tdAvgPct.style.cssText = 'padding:8px 0; text-align:right; font-size:0.85rem;';
        tdAvgPct.className = 'mono ' + (avgPct > 0 ? 'text-profit' : avgPct < 0 ? 'text-loss' : '');
        tdAvgPct.textContent = formatPct(avgPct);

        tr.append(tdType, tdCount, tdPnl, tdAvgPct);
        fragment.appendChild(tr);
    }

    tbody.appendChild(fragment);
}

// ============================================================
// 이벤트 핸들러
// ============================================================

// 필터 탭
document.querySelectorAll('.filter-tab').forEach(tab => {
    tab.addEventListener('click', () => {
        document.querySelectorAll('.filter-tab').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        currentFilter = tab.dataset.type;
        loadTradeData(dateInput.value, currentFilter);
    });
});

// 날짜 변경
dateInput.addEventListener('change', () => {
    loadTradeData(dateInput.value);
});

btnToday.addEventListener('click', () => {
    dateInput.value = todayStr();
    loadTradeData(todayStr());
});

// 초기화
document.addEventListener('DOMContentLoaded', () => {
    dateInput.value = todayStr();
    loadTradeData(todayStr());
    sse.connect();
});
