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

    // 일일 손익 (실현 + 미실현)
    const dailyPnl = document.getElementById('p-daily-pnl');
    dailyPnl.innerHTML = formatPnl(data.daily_pnl) + ` <span style="font-size:0.72rem; color:var(--text-muted);">(${formatPct(data.daily_pnl_pct)})</span>`;
    dailyPnl.className = 'mono font-semibold ' + pnlClass(data.daily_pnl);

    // 실현/미실현 분리 표시
    const breakdownEl = document.getElementById('p-pnl-breakdown');
    if (breakdownEl && (data.realized_daily_pnl || data.unrealized_pnl)) {
        breakdownEl.innerHTML =
            `<span style="color:var(--text-muted);">실현</span> <span class="mono ${pnlClass(data.realized_daily_pnl)}">${formatPnl(data.realized_daily_pnl)}</span>` +
            ` <span style="color:var(--text-muted); margin:0 6px;">|</span> ` +
            `<span style="color:var(--text-muted);">미실현</span> <span class="mono ${pnlClass(data.unrealized_pnl)}">${formatPnl(data.unrealized_pnl)}</span>`;
    }

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

sse.on('events', (data) => {
    if (Array.isArray(data)) {
        data.forEach(evt => {
            addLogEntry(formatTime(evt.time), evt.type, evt.message);
        });
    }
});

// ============================================================
// 포지션 테이블
// ============================================================

function updatePositionsTable(positions) {
    if (positions) lastPositions = positions;
    renderSortedPositions();
}

const strategyNames = {
    momentum_breakout: '모멘텀',
    theme_chasing: '테마',
    gap_and_go: '갭상승',
    mean_reversion: '평균회귀',
};

function renderSortedPositions() {
    const tbody = document.getElementById('positions-body');
    const positions = lastPositions;

    if (!positions || positions.length === 0) {
        tbody.innerHTML = '<tr><td colspan="10" class="py-8 text-center text-gray-500">보유 포지션 없음</td></tr>';
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

    const now = new Date();
    const rows = sorted.map(pos => {
        const pnlCls = pnlClass(pos.unrealized_pnl);
        const stageLabel = exitStageLabel(pos.exit_state);
        const stName = strategyNames[pos.strategy] || pos.strategy || '--';

        // 보유시간
        let holdStr = '--';
        if (pos.entry_time) {
            const entry = new Date(pos.entry_time);
            const diffMin = Math.floor((now - entry) / 60000);
            if (diffMin >= 60) {
                holdStr = `${Math.floor(diffMin / 60)}h ${diffMin % 60}m`;
            } else {
                holdStr = `${diffMin}m`;
            }
        }

        // 손절/목표
        const slTp = (pos.stop_loss || pos.take_profit)
            ? `<span style="color:var(--accent-red);font-size:0.72rem;">${pos.stop_loss ? formatNumber(pos.stop_loss) : '--'}</span>` +
              `<span style="color:var(--text-muted);font-size:0.72rem;"> / </span>` +
              `<span style="color:var(--accent-green);font-size:0.72rem;">${pos.take_profit ? formatNumber(pos.take_profit) : '--'}</span>`
            : '--';

        return `<tr class="border-b" style="border-color:rgba(99,102,241,0.08)">
            <td class="py-2 pr-3 font-medium text-white" style="white-space:nowrap;">${pos.name || pos.symbol} <span style="color:var(--text-muted); font-size:0.72rem; font-weight:400;">${pos.symbol}</span></td>
            <td class="py-2 pr-3" style="font-size:0.75rem; color:var(--accent-purple);">${stName}</td>
            <td class="py-2 pr-3 text-right mono">${formatNumber(pos.current_price)}</td>
            <td class="py-2 pr-3 text-right mono text-gray-400">${formatNumber(pos.avg_price)}</td>
            <td class="py-2 pr-3 text-right mono">${pos.quantity}</td>
            <td class="py-2 pr-3 text-right mono ${pnlCls}">${formatPnl(pos.unrealized_pnl)}</td>
            <td class="py-2 pr-3 text-right mono ${pnlCls}">${formatPct(pos.unrealized_pnl_pct)}</td>
            <td class="py-2 pr-3 text-right mono" style="white-space:nowrap;">${slTp}</td>
            <td class="py-2 pr-3 mono" style="font-size:0.75rem; color:var(--text-secondary);">${holdStr}</td>
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
// 프리마켓 (NXT) 표시
// ============================================================

async function loadPremarket() {
    try {
        const data = await api('/api/premarket');
        renderPremarket(data);
    } catch (e) {
        // 프리장 시간이 아니면 무시
    }
}

function renderPremarket(data) {
    const card = document.getElementById('premarket-card');
    const grid = document.getElementById('premarket-grid');
    const countEl = document.getElementById('premarket-count');

    if (!data || !data.available || !data.stocks || data.stocks.length === 0) {
        card.style.display = 'none';
        return;
    }

    card.style.display = 'block';
    countEl.textContent = data.count + '종목';

    const items = data.stocks.slice(0, 20).map(s => {
        const cls = s.pre_change_pct >= 0 ? 'text-profit' : 'text-loss';
        const bgCls = s.pre_change_pct >= 0 ? 'rgba(52,211,153,0.06)' : 'rgba(248,113,113,0.06)';
        const borderCls = s.pre_change_pct >= 0 ? 'rgba(52,211,153,0.12)' : 'rgba(248,113,113,0.12)';
        return `<div style="background:${bgCls}; border:1px solid ${borderCls}; border-radius:10px; padding:10px 12px;">
            <div style="font-size:0.78rem; font-weight:500; color:var(--text-primary); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${s.name || s.symbol}</div>
            <div style="display:flex; justify-content:space-between; align-items:baseline; margin-top:4px;">
                <span class="mono" style="font-size:0.82rem;">${formatNumber(s.pre_price)}</span>
                <span class="mono ${cls}" style="font-size:0.82rem; font-weight:600;">${formatPct(s.pre_change_pct)}</span>
            </div>
        </div>`;
    }).join('');

    grid.innerHTML = items;
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

    // 프리마켓 데이터 로드
    loadPremarket();
    // 30초마다 프리마켓 갱신
    setInterval(loadPremarket, 30000);

    addLogEntry(formatTime(new Date().toISOString()), '시스템', '대시보드 연결됨');
});
