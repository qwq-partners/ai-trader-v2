/**
 * AI Trader v2 - 성과 분석 페이지
 */

let currentDays = 1;

async function loadStats(days) {
    currentDays = days;
    try {
        const stats = await api(`/api/trades/stats?days=${days}`);
        renderSummary(stats);
        renderStrategyChart(stats.by_strategy || {});
        renderExitPnlChart(stats.by_exit_type || {});
        renderStrategyTable(stats.by_strategy || {});
    } catch (e) {
        console.error('성과 로드 오류:', e);
    }

    // 에퀴티 커브 (기간 매핑)
    try {
        const curveDays = days <= 1 ? 7 : days;  // 오늘만이면 최소 7일 표시
        const curve = await api(`/api/equity-curve?days=${curveDays}`);
        renderEquityCurve(curve);
    } catch (e) {
        console.error('에퀴티 커브 오류:', e);
    }
}

function renderEquityCurve(data) {
    const el = document.getElementById('equity-curve-chart');
    if (!data || data.length === 0) {
        el.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:0.85rem;">거래 데이터 없음</div>';
        return;
    }

    const dates = data.map(d => d.date);
    const dailyPnl = data.map(d => d.pnl);
    const cumPnl = data.map(d => d.cumulative_pnl);

    const traces = [
        {
            x: dates,
            y: cumPnl,
            type: 'scatter',
            mode: 'lines+markers',
            name: '누적 손익',
            line: { color: '#6366f1', width: 2.5 },
            marker: { size: 5 },
            fill: 'tozeroy',
            fillcolor: 'rgba(99,102,241,0.08)',
        },
        {
            x: dates,
            y: dailyPnl,
            type: 'bar',
            name: '일별 손익',
            marker: {
                color: dailyPnl.map(v => v >= 0 ? 'rgba(52,211,153,0.6)' : 'rgba(248,113,113,0.6)'),
            },
            yaxis: 'y2',
        }
    ];

    const layout = {
        paper_bgcolor: 'transparent',
        plot_bgcolor: 'transparent',
        margin: { t: 10, b: 40, l: 60, r: 60 },
        xaxis: { color: '#8892b0', gridcolor: 'rgba(99,102,241,0.06)' },
        yaxis: { color: '#8892b0', gridcolor: 'rgba(99,102,241,0.08)', zeroline: true, zerolinecolor: 'rgba(99,102,241,0.2)' },
        yaxis2: { color: '#a78bfa', overlaying: 'y', side: 'right', gridcolor: 'transparent' },
        legend: { font: { color: '#8892b0', size: 11, family: 'DM Sans, sans-serif' }, orientation: 'h', y: 1.12 },
        height: 280,
        font: { color: '#e2e8f0', family: 'DM Sans, sans-serif' },
    };

    Plotly.react('equity-curve-chart', traces, layout, { displayModeBar: false, responsive: true });

    // 핵심 지표 계산
    const totalGross = dailyPnl.filter(v => v > 0).reduce((s, v) => s + v, 0);
    const totalLoss = Math.abs(dailyPnl.filter(v => v < 0).reduce((s, v) => s + v, 0));
    const pf = totalLoss > 0 ? (totalGross / totalLoss).toFixed(2) : (totalGross > 0 ? '∞' : '--');

    // Max Drawdown
    let peak = 0;
    let maxDD = 0;
    cumPnl.forEach(v => {
        if (v > peak) peak = v;
        const dd = peak - v;
        if (dd > maxDD) maxDD = dd;
    });

    document.getElementById('eq-max-profit').textContent = totalGross > 0 ? formatPnl(totalGross) : '--';
    document.getElementById('eq-max-loss').textContent = totalLoss > 0 ? formatPnl(-totalLoss) : '--';
    document.getElementById('eq-pf').textContent = pf;
    document.getElementById('eq-mdd').textContent = maxDD > 0 ? formatPnl(-maxDD) : '--';
}

function renderSummary(stats) {
    const closed = stats.total_trades || 0;
    const open = stats.open_trades || 0;
    const all = stats.all_trades || closed;

    // 총 거래: 청산 + 보유중 구분 표시
    const totalEl = document.getElementById('perf-total');
    if (open > 0) {
        totalEl.innerHTML = `${all} <span style="font-size:0.65rem; color:var(--text-muted); font-weight:400;">(보유${open})</span>`;
    } else {
        totalEl.textContent = all;
    }

    const wr = document.getElementById('perf-winrate');
    const winRate = stats.win_rate || 0;
    wr.textContent = closed > 0 ? winRate.toFixed(1) + '%' : '--';
    wr.className = 'stat-value mono ' + (winRate >= 50 ? 'text-profit' : winRate > 0 ? 'text-loss' : '');

    const wl = document.getElementById('perf-wl');
    wl.textContent = closed > 0 ? `${stats.wins || 0}/${stats.losses || 0}` : (open > 0 ? `보유 ${open}건` : '--');

    // 총 손익: 청산 손익 + 미실현 손익
    const totalPnl = (stats.total_pnl || 0) + (stats.open_pnl || 0);
    const pnl = document.getElementById('perf-pnl');
    pnl.textContent = totalPnl !== 0 ? formatPnl(totalPnl) : '--';
    pnl.className = 'stat-value mono ' + pnlClass(totalPnl);

    // 평균 수익률: 청산 있으면 청산 기준, 없으면 보유중 기준
    const avgPnl = document.getElementById('perf-avg-pnl');
    const avgPnlVal = closed > 0 ? stats.avg_pnl_pct : (open > 0 ? stats.open_avg_pnl_pct : 0);
    avgPnl.textContent = avgPnlVal ? formatPct(avgPnlVal) : '--';
    avgPnl.className = 'stat-value mono ' + pnlClass(avgPnlVal);

    document.getElementById('perf-avg-hold').textContent =
        stats.avg_holding_minutes ? Math.round(stats.avg_holding_minutes) + '분' : '--';
}

function renderStrategyChart(byStrategy) {
    const keys = Object.keys(byStrategy);
    if (keys.length === 0) {
        document.getElementById('strategy-chart').innerHTML = '<div class="flex items-center justify-center h-full text-gray-500 text-sm">데이터 없음</div>';
        return;
    }

    const strategyNames = {
        momentum_breakout: '모멘텀',
        theme_chasing: '테마추종',
        gap_and_go: '갭상승',
        mean_reversion: '평균회귀',
        sepa_trend: 'SEPA',
        rsi2_reversal: 'RSI2',
    };

    const labels = keys.map(k => strategyNames[k] || k);
    const winRates = keys.map(k => byStrategy[k].win_rate || 0);
    const tradeCounts = keys.map(k => byStrategy[k].trades || 0);

    const data = [
        {
            x: labels,
            y: winRates,
            type: 'bar',
            name: '승률 (%)',
            marker: { color: '#6366f1' },
            yaxis: 'y',
        },
        {
            x: labels,
            y: tradeCounts,
            type: 'bar',
            name: '거래 수',
            marker: { color: '#a78bfa' },
            yaxis: 'y2',
        }
    ];

    const layout = {
        paper_bgcolor: 'transparent',
        plot_bgcolor: 'transparent',
        margin: { t: 10, b: 40, l: 50, r: 50 },
        barmode: 'group',
        xaxis: { color: '#8892b0' },
        yaxis: { color: '#8892b0', gridcolor: 'rgba(99,102,241,0.08)' },
        yaxis2: { color: '#a78bfa', overlaying: 'y', side: 'right', gridcolor: 'transparent' },
        legend: { font: { color: '#8892b0', size: 11, family: 'DM Sans, sans-serif' }, orientation: 'h', y: 1.15 },
        height: 280,
        font: { color: '#e2e8f0', family: 'DM Sans, sans-serif' },
    };

    Plotly.react('strategy-chart', data, layout, { displayModeBar: false, responsive: true });
}

function renderExitPnlChart(byExitType) {
    const keys = Object.keys(byExitType);
    if (keys.length === 0) {
        document.getElementById('exit-pnl-chart').innerHTML = '<div class="flex items-center justify-center h-full text-gray-500 text-sm">데이터 없음</div>';
        return;
    }

    const exitLabels = {
        take_profit: '익절',
        first_take_profit: '1차익절',
        second_take_profit: '2차익절',
        third_take_profit: '3차익절',
        stop_loss: '손절',
        trailing: '트레일링',
        breakeven: '본전',
        manual: '수동',
        kis_sync: '동기화',
    };

    const labels = keys.map(k => exitLabels[k] || k);
    const avgPnls = keys.map(k => byExitType[k].avg_pnl_pct || 0);
    const counts = keys.map(k => byExitType[k].trades || 0);

    const colors = avgPnls.map(v => v >= 0 ? '#34d399' : '#f87171');

    const data = [{
        x: labels,
        y: avgPnls,
        type: 'bar',
        marker: { color: colors },
        text: counts.map(c => `${c}건`),
        textposition: 'auto',
        textfont: { color: '#e2e8f0', size: 11, family: 'JetBrains Mono, monospace' },
    }];

    const layout = {
        paper_bgcolor: 'transparent',
        plot_bgcolor: 'transparent',
        margin: { t: 24, b: 40, l: 50, r: 10 },
        xaxis: { color: '#8892b0' },
        yaxis: { color: '#8892b0', gridcolor: 'rgba(99,102,241,0.08)', zeroline: true, zerolinecolor: 'rgba(99,102,241,0.2)' },
        annotations: [
            { xref: 'paper', yref: 'paper', x: 0, y: 1.08, xanchor: 'left', yanchor: 'bottom', text: '평균 수익률 (%)', showarrow: false, font: { color: '#8892b0', size: 10 } },
        ],
        height: 280,
        font: { color: '#e2e8f0', family: 'DM Sans, sans-serif' },
    };

    Plotly.react('exit-pnl-chart', data, layout, { displayModeBar: false, responsive: true });
}

function renderStrategyTable(byStrategy) {
    const tbody = document.getElementById('strategy-table-body');
    const keys = Object.keys(byStrategy);

    if (keys.length === 0) {
        tbody.textContent = '';
        const tr = document.createElement('tr');
        const td = document.createElement('td');
        td.colSpan = 6;
        td.style.cssText = 'padding:40px 0; text-align:center; color:var(--text-muted); font-size:0.85rem;';
        td.textContent = '데이터 없음';
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
    }

    const strategyNames = {
        momentum_breakout: '모멘텀 브레이크아웃',
        theme_chasing: '테마 추종',
        gap_and_go: '갭상승 추종',
        mean_reversion: '평균 회귀',
        sepa_trend: 'SEPA 추세',
        rsi2_reversal: 'RSI2 반전',
    };

    const fragment = document.createDocumentFragment();
    keys.forEach(k => {
        const s = byStrategy[k];
        const pnlCls = s.total_pnl > 0 ? 'text-profit' : s.total_pnl < 0 ? 'text-loss' : '';
        const wrCls = s.win_rate >= 50 ? 'text-profit' : 'text-loss';
        const losses = (s.trades || 0) - (s.wins || 0);
        const avgPct = s.avg_pnl_pct != null ? s.avg_pnl_pct : (s.trades > 0 ? (s.total_pnl / s.trades) : 0);
        const avgCls = avgPct > 0 ? 'text-profit' : avgPct < 0 ? 'text-loss' : '';

        const tr = document.createElement('tr');
        tr.className = 'border-b';
        tr.style.borderColor = 'rgba(99,102,241,0.08)';

        const tdName = document.createElement('td');
        tdName.className = 'py-2 pr-4 font-medium';
        tdName.style.color = '#fff';
        tdName.textContent = strategyNames[k] || k;

        const tdTrades = document.createElement('td');
        tdTrades.className = 'py-2 pr-4 text-right mono';
        tdTrades.textContent = s.trades;

        const tdWL = document.createElement('td');
        tdWL.className = 'py-2 pr-4 text-right mono';
        const winSpan = document.createElement('span');
        winSpan.className = 'text-profit';
        winSpan.textContent = s.wins;
        const lossSpan = document.createElement('span');
        lossSpan.className = 'text-loss';
        lossSpan.textContent = losses;
        tdWL.appendChild(winSpan);
        tdWL.appendChild(document.createTextNode(' / '));
        tdWL.appendChild(lossSpan);

        const tdWR = document.createElement('td');
        tdWR.className = 'py-2 pr-4 text-right mono ' + wrCls;
        tdWR.textContent = s.win_rate.toFixed(1) + '%';

        const tdPnl = document.createElement('td');
        tdPnl.className = 'py-2 pr-4 text-right mono ' + pnlCls;
        tdPnl.textContent = formatPnl(s.total_pnl);

        const tdAvg = document.createElement('td');
        tdAvg.className = 'py-2 text-right mono ' + avgCls;
        tdAvg.textContent = formatPct(avgPct);

        tr.append(tdName, tdTrades, tdWL, tdWR, tdPnl, tdAvg);
        fragment.appendChild(tr);
    });

    tbody.textContent = '';
    tbody.appendChild(fragment);
}

// 탭 이벤트
document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        loadStats(parseInt(btn.dataset.days));
    });
});

// 초기화
document.addEventListener('DOMContentLoaded', () => {
    loadStats(1);
    sse.connect();
});
