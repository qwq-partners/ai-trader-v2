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
    wr.textContent = closed > 0 ? stats.win_rate.toFixed(1) + '%' : '--';
    wr.className = 'stat-value mono ' + (stats.win_rate >= 50 ? 'text-profit' : stats.win_rate > 0 ? 'text-loss' : '');

    const wl = document.getElementById('perf-wl');
    wl.textContent = closed > 0 ? `${stats.wins}/${stats.losses}` : (open > 0 ? `보유 ${open}건` : '--');

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
        yaxis: { color: '#8892b0', title: '승률 (%)', gridcolor: 'rgba(99,102,241,0.08)' },
        yaxis2: { color: '#a78bfa', title: '거래 수', overlaying: 'y', side: 'right', gridcolor: 'transparent' },
        legend: { font: { color: '#8892b0', size: 11, family: 'DM Sans, sans-serif' }, orientation: 'h', y: 1.15 },
        height: 280,
        font: { color: '#e2e8f0', family: 'DM Sans, sans-serif' },
    };

    Plotly.newPlot('strategy-chart', data, layout, { displayModeBar: false, responsive: true });
}

function renderExitPnlChart(byExitType) {
    const keys = Object.keys(byExitType);
    if (keys.length === 0) {
        document.getElementById('exit-pnl-chart').innerHTML = '<div class="flex items-center justify-center h-full text-gray-500 text-sm">데이터 없음</div>';
        return;
    }

    const exitLabels = {
        take_profit: '익절',
        stop_loss: '손절',
        trailing: '트레일링',
        manual: '수동',
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
        margin: { t: 10, b: 40, l: 50, r: 10 },
        xaxis: { color: '#8892b0' },
        yaxis: { color: '#8892b0', title: '평균 수익률 (%)', gridcolor: 'rgba(99,102,241,0.08)', zeroline: true, zerolinecolor: 'rgba(99,102,241,0.2)' },
        height: 280,
        font: { color: '#e2e8f0', family: 'DM Sans, sans-serif' },
    };

    Plotly.newPlot('exit-pnl-chart', data, layout, { displayModeBar: false, responsive: true });
}

function renderStrategyTable(byStrategy) {
    const tbody = document.getElementById('strategy-table-body');
    const keys = Object.keys(byStrategy);

    if (keys.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" class="py-8 text-center text-gray-500">데이터 없음</td></tr>';
        return;
    }

    const strategyNames = {
        momentum_breakout: '모멘텀 브레이크아웃',
        theme_chasing: '테마 추종',
        gap_and_go: '갭상승 추종',
        mean_reversion: '평균 회귀',
    };

    const rows = keys.map(k => {
        const s = byStrategy[k];
        const pnlCls = s.total_pnl > 0 ? 'text-profit' : s.total_pnl < 0 ? 'text-loss' : '';
        const wrCls = s.win_rate >= 50 ? 'text-profit' : 'text-loss';

        return `<tr class="border-b" style="border-color:rgba(99,102,241,0.08)">
            <td class="py-2 pr-4 font-medium text-white">${strategyNames[k] || k}</td>
            <td class="py-2 pr-4 text-right mono">${s.trades}</td>
            <td class="py-2 pr-4 text-right mono">${s.wins}</td>
            <td class="py-2 pr-4 text-right mono ${wrCls}">${s.win_rate.toFixed(1)}%</td>
            <td class="py-2 pr-4 text-right mono ${pnlCls}">${formatPnl(s.total_pnl)}</td>
        </tr>`;
    }).join('');

    tbody.innerHTML = rows;
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
