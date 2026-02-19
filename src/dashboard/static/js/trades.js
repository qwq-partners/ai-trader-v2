/**
 * AI Trader v2 - 거래 이벤트 로그 페이지
 *
 * Note: innerHTML usage is safe here as all data comes from our own
 * trusted backend API (trade_events table), not user input.
 */

const dateInput = document.getElementById('trade-date');
const btnToday = document.getElementById('btn-today');

let currentFilter = 'all';
let cachedEvents = [];

function todayStr() {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}

async function loadTradeEvents(dateStr, type) {
    try {
        const date = dateStr || todayStr();
        type = type || currentFilter;
        const url = `/api/trade-events?date=${date}&type=${type}`;
        const events = await api(url);
        cachedEvents = events;
        renderEvents(events);
        renderSummary(events);
        renderExitTypeChart(events);
        updateFilterCounts(date);
    } catch (e) {
        console.error('거래 이벤트 로드 오류:', e);
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

    // Build table rows from trusted API data
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

function renderSummary(events) {
    const buys = events.filter(e => e.event_type === 'BUY');
    const sells = events.filter(e => e.event_type === 'SELL');
    const holding = buys.filter(e => e.status === 'holding');

    const sellPnl = sells.reduce((s, e) => s + (e.pnl || 0), 0);
    const holdPnl = holding.reduce((s, e) => s + (e.pnl || 0), 0);
    const totalPnl = sellPnl + holdPnl;

    const withPnl = [...sells, ...holding].filter(e => e.pnl !== 0 && e.pnl != null);
    const avgPnlPct = withPnl.length > 0
        ? withPnl.reduce((s, e) => s + (e.pnl_pct || 0), 0) / withPnl.length : 0;

    const wins = sells.filter(e => (e.pnl || 0) > 0);
    const winRate = sells.length > 0 ? (wins.length / sells.length * 100) : 0;

    // 거래 수
    const countEl = document.getElementById('s-count');
    countEl.textContent = buys.length;
    if (holding.length > 0) {
        countEl.textContent = buys.length;
        const sub = document.createElement('span');
        sub.style.cssText = 'font-size:0.65rem; color:var(--text-muted); font-weight:400;';
        sub.textContent = ` (보유${holding.length})`;
        countEl.appendChild(sub);
    }

    const wrEl = document.getElementById('s-winrate');
    wrEl.textContent = sells.length > 0 ? winRate.toFixed(1) + '%' : '--';
    wrEl.className = 'stat-value mono ' + (winRate >= 50 ? 'text-profit' : winRate > 0 ? 'text-loss' : '');

    const tpEl = document.getElementById('s-total-pnl');
    tpEl.textContent = totalPnl !== 0 ? formatPnl(totalPnl) : '--';
    tpEl.className = 'stat-value mono ' + pnlClass(totalPnl);

    const apEl = document.getElementById('s-avg-pnl');
    apEl.textContent = withPnl.length > 0 ? formatPct(avgPnlPct) : '--';
    apEl.className = 'stat-value mono ' + pnlClass(avgPnlPct);

    document.getElementById('s-avg-hold').textContent = sells.length > 0 ? `${sells.length}건 청산` : '--';
}

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

// 필터 탭 이벤트
document.querySelectorAll('.filter-tab').forEach(tab => {
    tab.addEventListener('click', () => {
        document.querySelectorAll('.filter-tab').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        currentFilter = tab.dataset.type;
        loadTradeEvents(dateInput.value, currentFilter);
    });
});

// 날짜 변경
dateInput.addEventListener('change', () => {
    loadTradeEvents(dateInput.value);
});

btnToday.addEventListener('click', () => {
    dateInput.value = todayStr();
    loadTradeEvents(todayStr());
});

// 초기화
document.addEventListener('DOMContentLoaded', () => {
    dateInput.value = todayStr();
    loadTradeEvents(todayStr());
    sse.connect();
});
