/**
 * AI Trader v2 - 거래 내역 페이지
 */

const dateInput = document.getElementById('trade-date');
const btnToday = document.getElementById('btn-today');

function todayStr() {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}

async function loadTrades(dateStr) {
    try {
        const date = dateStr || todayStr();
        const url = `/api/trades?date=${date}`;
        const trades = await api(url);
        renderTrades(trades);
        renderSummary(trades);
        renderExitTypeChart(trades);
    } catch (e) {
        console.error('거래 로드 오류:', e);
    }
}

function renderTrades(trades) {
    const tbody = document.getElementById('trades-body');

    if (!trades || trades.length === 0) {
        tbody.innerHTML = '<tr><td colspan="10" class="py-8 text-center text-gray-500">거래 내역 없음</td></tr>';
        return;
    }

    const rows = trades.map(t => {
        const pnlCls = t.pnl > 0 ? 'text-profit' : t.pnl < 0 ? 'text-loss' : '';
        const isOpen = !t.exit_time;

        const exitTypeLabel = isOpen
            ? '<span class="badge badge-blue">보유중</span>'
            : ({
                'take_profit': '<span class="badge badge-green">익절</span>',
                'stop_loss': '<span class="badge badge-red">손절</span>',
                'trailing': '<span class="badge badge-yellow">트레일링</span>',
                'manual': '<span class="badge badge-blue">수동</span>',
            }[t.exit_type] || `<span class="badge badge-blue">${t.exit_type || '기타'}</span>`);

        // 종목명: name이 symbol과 같으면 코드만 표시
        const nameDisplay = (t.name && t.name !== t.symbol)
            ? `${t.name} <span style="color:var(--text-muted); font-size:0.72rem;">${t.symbol}</span>`
            : t.symbol;

        // 전략: unknown/빈값이면 --
        const strategy = (t.entry_strategy && t.entry_strategy !== 'unknown') ? t.entry_strategy : '--';

        // 미청산 거래: 현재가 표시, 청산 거래: 청산가 표시
        const priceCol = isOpen
            ? (t.current_price ? `<span style="color:var(--accent-cyan);">${formatNumber(t.current_price)}</span>` : '--')
            : (t.exit_price ? formatNumber(t.exit_price) : '--');

        // 보유시간
        const holdTime = t.holding_minutes > 0
            ? (t.holding_minutes >= 60
                ? `${Math.floor(t.holding_minutes / 60)}시간 ${t.holding_minutes % 60}분`
                : `${t.holding_minutes}분`)
            : '--';

        // 진입시간
        const entryTime = t.entry_time ? formatTime(t.entry_time) : '--';

        return `<tr class="border-b" style="border-color:rgba(99,102,241,0.08)">
            <td class="py-2 pr-3 font-medium text-white">${nameDisplay}</td>
            <td class="py-2 pr-3 text-xs" style="color:var(--text-secondary);">${strategy}</td>
            <td class="py-2 pr-3 text-right mono">${formatNumber(t.entry_price)}</td>
            <td class="py-2 pr-3 text-right mono">${priceCol}</td>
            <td class="py-2 pr-3 text-right mono">${t.entry_quantity}</td>
            <td class="py-2 pr-3 text-right mono ${pnlCls}">${t.pnl ? formatPnl(t.pnl) : '--'}</td>
            <td class="py-2 pr-3 text-right mono ${pnlCls}">${t.pnl_pct ? formatPct(t.pnl_pct) : '--'}</td>
            <td class="py-2 pr-3">${exitTypeLabel}</td>
            <td class="py-2 pr-3 text-sm" style="color:var(--text-muted);">${holdTime}</td>
            <td class="py-2 text-xs" style="color:var(--text-muted);">${entryTime}</td>
        </tr>`;
    }).join('');

    tbody.innerHTML = rows;
}

function renderSummary(trades) {
    // 미청산 포함 전체 손익 계산
    const allWithPnl = trades.filter(t => t.pnl !== 0 && t.pnl !== null);
    const totalPnl = trades.reduce((sum, t) => sum + (t.pnl || 0), 0);
    const avgPnlPct = allWithPnl.length > 0 ? allWithPnl.reduce((sum, t) => sum + (t.pnl_pct || 0), 0) / allWithPnl.length : 0;

    const closed = trades.filter(t => t.exit_time);
    const open = trades.filter(t => !t.exit_time);
    const wins = allWithPnl.filter(t => t.pnl > 0);
    const winRate = allWithPnl.length > 0 ? (wins.length / allWithPnl.length * 100) : 0;

    const avgHold = trades.filter(t => t.holding_minutes > 0);
    const avgHoldMin = avgHold.length > 0 ? avgHold.reduce((sum, t) => sum + t.holding_minutes, 0) / avgHold.length : 0;

    // 거래 수: 보유중/청산 구분
    const countEl = document.getElementById('s-count');
    countEl.textContent = trades.length;
    if (open.length > 0) {
        countEl.innerHTML = `${trades.length} <span style="font-size:0.65rem; color:var(--text-muted); font-weight:400;">(보유${open.length})</span>`;
    }

    const wrEl = document.getElementById('s-winrate');
    wrEl.textContent = allWithPnl.length > 0 ? winRate.toFixed(1) + '%' : '--';
    wrEl.className = 'stat-value mono ' + (winRate >= 50 ? 'text-profit' : winRate > 0 ? 'text-loss' : '');

    const tpEl = document.getElementById('s-total-pnl');
    tpEl.textContent = totalPnl !== 0 ? formatPnl(totalPnl) : '--';
    tpEl.className = 'stat-value mono ' + pnlClass(totalPnl);

    const apEl = document.getElementById('s-avg-pnl');
    apEl.textContent = allWithPnl.length > 0 ? formatPct(avgPnlPct) : '--';
    apEl.className = 'stat-value mono ' + pnlClass(avgPnlPct);

    const holdStr = avgHoldMin >= 60
        ? `${Math.floor(avgHoldMin / 60)}시간 ${Math.round(avgHoldMin % 60)}분`
        : (avgHoldMin > 0 ? `${Math.round(avgHoldMin)}분` : '--');
    document.getElementById('s-avg-hold').textContent = holdStr;
}

function renderExitTypeChart(trades) {
    const closed = trades.filter(t => t.exit_type);
    if (closed.length === 0) {
        document.getElementById('exit-type-chart').innerHTML = '<div class="flex items-center justify-center h-full text-gray-500 text-sm">데이터 없음</div>';
        return;
    }

    const counts = {};
    closed.forEach(t => {
        const type = t.exit_type || 'unknown';
        counts[type] = (counts[type] || 0) + 1;
    });

    const labels = Object.keys(counts).map(k => {
        const map = { take_profit: '익절', stop_loss: '손절', trailing: '트레일링', manual: '수동' };
        return map[k] || k;
    });

    const data = [{
        x: labels,
        y: Object.values(counts),
        type: 'bar',
        marker: {
            color: Object.keys(counts).map(k => {
                const map = { take_profit: '#34d399', stop_loss: '#f87171', trailing: '#fbbf24', manual: '#6366f1' };
                return map[k] || '#a78bfa';
            }),
            borderRadius: 4,
        },
    }];

    const layout = {
        paper_bgcolor: 'transparent',
        plot_bgcolor: 'transparent',
        margin: { t: 10, b: 40, l: 40, r: 10 },
        xaxis: { color: '#8892b0', gridcolor: 'rgba(99,102,241,0.08)' },
        yaxis: { color: '#8892b0', gridcolor: 'rgba(99,102,241,0.08)', dtick: 1 },
        height: 220,
        font: { color: '#e2e8f0', family: 'DM Sans, sans-serif' },
    };

    Plotly.react('exit-type-chart', data, layout, { displayModeBar: false, responsive: true });
}

// 이벤트
dateInput.addEventListener('change', () => {
    loadTrades(dateInput.value);
});

btnToday.addEventListener('click', () => {
    dateInput.value = todayStr();
    loadTrades(todayStr());
});

// 초기화
document.addEventListener('DOMContentLoaded', () => {
    dateInput.value = todayStr();
    loadTrades(todayStr());

    sse.connect();
});
