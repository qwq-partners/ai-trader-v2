/**
 * AI Trader v2 - 메인 대시보드 실시간 업데이트
 */

let pieChartInitialized = false;
const logEntries = [];
const MAX_LOG_ENTRIES = 50;

// 포지션 정렬 상태
let positionSortKey = 'unrealized_pnl_pct';
let positionSortDir = 'desc';
let lastPositions = [];

// ============================================================
// SSE 이벤트 핸들러
// ============================================================

sse.on('portfolio', (data) => {
    // 총자산
    document.getElementById('p-equity').textContent = formatCurrency(data.total_equity);

    // 현금
    document.getElementById('p-cash').textContent = formatCurrency(data.cash);
    const cashPct = data.cash_ratio != null ? (data.cash_ratio * 100).toFixed(0) : '--';
    document.getElementById('p-cash-pct').textContent = `(${cashPct}%)`;

    // 주식 평가
    document.getElementById('p-stock').textContent = formatCurrency(data.total_position_value);

    // 일일 손익
    const dailyPnl = document.getElementById('p-daily-pnl');
    dailyPnl.textContent = formatPnl(data.daily_pnl) + ` (${formatPct(data.daily_pnl_pct)})`;
    dailyPnl.className = 'mono font-semibold ' + pnlClass(data.daily_pnl);

    // 파이 차트 업데이트
    updatePieChart(data.cash, data.total_position_value);
});

sse.on('risk', (data) => {
    // 거래 가능
    const canTrade = document.getElementById('r-can-trade');
    if (data.can_trade) {
        canTrade.textContent = 'Yes';
        canTrade.className = 'badge badge-green';
    } else {
        canTrade.textContent = 'No';
        canTrade.className = 'badge badge-red';
    }

    // 일일 손실
    document.getElementById('r-daily-loss').textContent = formatPct(data.daily_loss_pct);
    document.getElementById('r-daily-loss-limit').textContent = `-${data.daily_loss_limit_pct}%`;
    const lossPct = Math.min(Math.abs(data.daily_loss_pct) / data.daily_loss_limit_pct * 100, 100);
    const lossGauge = document.getElementById('r-loss-gauge');
    lossGauge.style.width = lossPct + '%';
    lossGauge.className = 'gauge-fill ' + (lossPct > 70 ? 'bg-red-500' : lossPct > 40 ? 'bg-yellow-500' : 'bg-green-500');

    // 거래 횟수
    document.getElementById('r-trades').textContent = data.daily_trades;
    document.getElementById('r-max-trades').textContent = data.daily_max_trades;
    const tradesPct = Math.min(data.daily_trades / data.daily_max_trades * 100, 100);
    document.getElementById('r-trades-gauge').style.width = tradesPct + '%';

    // 포지션 수
    document.getElementById('r-positions').textContent = data.position_count;
    document.getElementById('r-max-positions').textContent = data.max_positions;
    const posPct = Math.min(data.position_count / data.max_positions * 100, 100);
    document.getElementById('r-positions-gauge').style.width = posPct + '%';

    // 연속 손실
    const consec = document.getElementById('r-consecutive');
    consec.textContent = data.consecutive_losses;
    consec.className = 'mono' + (data.consecutive_losses >= 3 ? ' text-loss' : '');
});

sse.on('status', (data) => {
    // 엔진 통계
    document.getElementById('r-events').textContent = formatNumber(data.engine.events_processed);
    document.getElementById('r-signals').textContent = formatNumber(data.engine.signals_generated);
    document.getElementById('r-ws-sub').textContent = data.websocket.subscribed_count;

    // 상태바
    document.getElementById('sb-session').textContent = sessionLabel(data.session);
    document.getElementById('sb-uptime').textContent = formatDuration(data.uptime_seconds);
});

sse.on('positions', (data) => {
    updatePositionsTable(data);
});

// ============================================================
// 포지션 테이블
// ============================================================

function updatePositionsTable(positions) {
    if (positions) lastPositions = positions;
    renderSortedPositions();
}

function renderSortedPositions() {
    const tbody = document.getElementById('positions-body');
    const positions = lastPositions;

    if (!positions || positions.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" class="py-8 text-center text-gray-500">보유 포지션 없음</td></tr>';
        return;
    }

    const sorted = [...positions].sort((a, b) => {
        let va = a[positionSortKey];
        let vb = b[positionSortKey];
        if (positionSortKey === 'name') {
            va = (a.name || a.symbol).toLowerCase();
            vb = (b.name || b.symbol).toLowerCase();
        }
        if (va < vb) return positionSortDir === 'asc' ? -1 : 1;
        if (va > vb) return positionSortDir === 'asc' ? 1 : -1;
        return 0;
    });

    const rows = sorted.map(pos => {
        const pnlCls = pnlClass(pos.unrealized_pnl);
        const stageLabel = exitStageLabel(pos.exit_state);

        return `<tr class="border-b" style="border-color:rgba(99,102,241,0.08)">
            <td class="py-2 pr-4 font-medium text-white">${pos.name || pos.symbol} <span style="color:var(--text-muted); font-size:0.72rem; font-weight:400;">${pos.symbol}</span></td>
            <td class="py-2 pr-4 text-right mono">${formatNumber(pos.current_price)}</td>
            <td class="py-2 pr-4 text-right mono text-gray-400">${formatNumber(pos.avg_price)}</td>
            <td class="py-2 pr-4 text-right mono">${pos.quantity}</td>
            <td class="py-2 pr-4 text-right mono">${formatCurrency(pos.market_value)}</td>
            <td class="py-2 pr-4 text-right mono ${pnlCls}">${formatPnl(pos.unrealized_pnl)}</td>
            <td class="py-2 pr-4 text-right mono ${pnlCls}">${formatPct(pos.unrealized_pnl_pct)}</td>
            <td class="py-2">${stageLabel}</td>
        </tr>`;
    }).join('');

    tbody.innerHTML = rows;
    updateSortIcons();
}

function updateSortIcons() {
    document.querySelectorAll('.sortable-th').forEach(th => {
        th.classList.remove('asc', 'desc');
        if (th.dataset.sort === positionSortKey) {
            th.classList.add(positionSortDir);
        }
    });
}

function exitStageLabel(exitState) {
    if (!exitState) return '<span class="badge badge-blue">진입</span>';
    const map = {
        'none': '<span class="badge badge-blue">진입</span>',
        'first': '<span class="badge badge-green">1차익절</span>',
        'second': '<span class="badge badge-green">2차익절</span>',
        'trailing': '<span class="badge badge-yellow">트레일링</span>',
    };
    return map[exitState.stage] || exitState.stage;
}

// ============================================================
// 파이 차트
// ============================================================

function updatePieChart(cash, stock) {
    if (cash === 0 && stock === 0) return;

    const data = [{
        values: [cash, stock],
        labels: ['현금', '주식'],
        type: 'pie',
        hole: 0.6,
        marker: {
            colors: ['#6366f1', '#34d399'],
        },
        textinfo: 'percent',
        textfont: { color: '#e2e8f0', size: 12, family: 'JetBrains Mono, monospace' },
        hoverinfo: 'label+value+percent',
    }];

    const layout = {
        paper_bgcolor: 'transparent',
        plot_bgcolor: 'transparent',
        margin: { t: 10, b: 10, l: 10, r: 10 },
        showlegend: true,
        legend: {
            font: { color: '#8892b0', size: 11, family: 'DM Sans, sans-serif' },
            orientation: 'h',
            y: -0.1,
        },
        height: 160,
    };

    const config = { displayModeBar: false, responsive: true };

    if (!pieChartInitialized) {
        Plotly.newPlot('pie-chart', data, layout, config);
        pieChartInitialized = true;
    } else {
        Plotly.react('pie-chart', data, layout, config);
    }
}

// ============================================================
// 이벤트 로그
// ============================================================

function addLogEntry(time, type, message) {
    const typeColors = {
        '신호': 'text-indigo-400',
        '체결': 'text-emerald-400',
        '리스크': 'text-amber-400',
        '오류': 'text-red-400',
        '시스템': 'text-slate-400',
    };

    const colorClass = typeColors[type] || 'text-gray-400';

    logEntries.unshift({ time, type, message });
    if (logEntries.length > MAX_LOG_ENTRIES) {
        logEntries.pop();
    }

    const logEl = document.getElementById('event-log');
    const entry = document.createElement('div');
    entry.className = 'log-entry';
    entry.innerHTML = `<span class="text-gray-500 mono text-xs">${time}</span> <span class="${colorClass}">[${type}]</span> ${message}`;

    logEl.prepend(entry);

    // 최대 항목 수 유지
    while (logEl.children.length > MAX_LOG_ENTRIES) {
        logEl.removeChild(logEl.lastChild);
    }

    document.getElementById('log-count').textContent = logEntries.length + '건';
}

// ============================================================
// 초기화
// ============================================================

document.addEventListener('DOMContentLoaded', () => {
    // 포지션 정렬 클릭
    document.querySelectorAll('.sortable-th').forEach(th => {
        th.addEventListener('click', () => {
            const key = th.dataset.sort;
            if (positionSortKey === key) {
                positionSortDir = positionSortDir === 'desc' ? 'asc' : 'desc';
            } else {
                positionSortKey = key;
                positionSortDir = key === 'name' ? 'asc' : 'desc';
            }
            renderSortedPositions();
        });
    });

    // SSE 연결
    sse.connect();

    // 초기 데이터 로드
    api('/api/portfolio').then(data => {
        sse._dispatch('portfolio', data);
    }).catch(() => {});

    api('/api/risk').then(data => {
        sse._dispatch('risk', data);
    }).catch(() => {});

    api('/api/positions').then(data => {
        updatePositionsTable(data);
    }).catch(() => {});

    api('/api/status').then(data => {
        sse._dispatch('status', data);
    }).catch(() => {});

    addLogEntry(formatTime(new Date().toISOString()), '시스템', '대시보드 연결됨');
});
