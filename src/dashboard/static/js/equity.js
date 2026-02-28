/**
 * AI Trader v2 - 자산 히스토리 페이지
 */

let equityChartInitialized = false;
let lastSnapshots = [];

// ============================================================
// 데이터 로드
// ============================================================

async function loadEquityByRange(dateFrom, dateTo) {
    try {
        const data = await api(`/api/equity-history?from=${dateFrom}&to=${dateTo}`);
        lastSnapshots = data.snapshots || [];
        renderSummary(data.summary || {}, lastSnapshots);
        renderEquityChart(lastSnapshots);
        renderEquityTable(lastSnapshots);
    } catch (e) {
        console.error('[자산] 히스토리 로드 실패:', e);
    }
}

async function loadPositionDetail(dateStr, rowEl) {
    try {
        const data = await api(`/api/equity-history/positions?date=${dateStr}`);
        renderPositionDetail(data, rowEl);
    } catch (e) {
        console.error('[자산] 포지션 상세 로드 실패:', e);
    }
}

// ============================================================
// 요약 카드
// ============================================================

function renderSummary(summary, snapshots) {
    const summaryEl = document.getElementById('equity-summary');
    const count = snapshots.length;
    summaryEl.textContent = count > 0 ? `${count}일 데이터` : '데이터 없음';

    // 기간 수익률
    const returnEl = document.getElementById('sum-return');
    const periodReturn = summary.period_return_pct;
    if (periodReturn != null) {
        returnEl.textContent = formatPct(periodReturn);
        returnEl.className = 'mono ' + pnlClass(periodReturn);
    } else {
        returnEl.textContent = '--';
        returnEl.className = 'mono';
    }

    // 최대 낙폭
    const ddEl = document.getElementById('sum-drawdown');
    const maxDD = summary.max_drawdown_pct;
    if (maxDD != null) {
        ddEl.textContent = formatPct(maxDD);
        ddEl.className = 'mono text-loss';
    } else {
        ddEl.textContent = '--';
        ddEl.className = 'mono';
    }

    // 평균 일일 손익
    const avgEl = document.getElementById('sum-avg-pnl');
    const avgPnl = summary.avg_daily_pnl;
    if (avgPnl != null) {
        avgEl.textContent = formatPnl(avgPnl);
        avgEl.className = 'mono ' + pnlClass(avgPnl);
    } else {
        avgEl.textContent = '--';
        avgEl.className = 'mono';
    }

    // 데이터 일수
    document.getElementById('sum-days').textContent = count + '일';
}

// ============================================================
// 총자산 차트 (Plotly)
// ============================================================

function renderEquityChart(snapshots) {
    if (!snapshots || snapshots.length === 0) {
        const el = document.getElementById('equity-chart');
        el.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:0.85rem;">데이터 없음</div>';
        return;
    }

    const dates = snapshots.map(s => s.date);
    const equities = snapshots.map(s => s.total_equity);

    // 마커 색상: 전일 대비 상승=초록, 하락=빨강, 첫날=파랑
    const markerColors = snapshots.map((s, i) => {
        if (i === 0) return '#6366f1';
        return s.total_equity >= snapshots[i - 1].total_equity ? '#34d399' : '#f87171';
    });

    // 호버 텍스트
    const hoverTexts = snapshots.map((s, i) => {
        const prevEquity = i > 0 ? snapshots[i - 1].total_equity : s.total_equity;
        const change = s.total_equity - prevEquity;
        const sign = change >= 0 ? '+' : '';
        const pctSign = s.daily_pnl_pct >= 0 ? '+' : '';
        return `<b>${s.date}</b><br>` +
            `총자산  <b>${Number(s.total_equity).toLocaleString('ko-KR')}</b>원<br>` +
            `변동  ${sign}${Number(change).toLocaleString('ko-KR')}원 (${pctSign}${s.daily_pnl_pct}%)`;
    });

    // Y축 범위: 데이터 min/max 기준 ±3% 패딩
    const minEquity = Math.min(...equities);
    const maxEquity = Math.max(...equities);
    const padding = (maxEquity - minEquity) * 0.3 || maxEquity * 0.02;
    const yMin = minEquity - padding;
    const yMax = maxEquity + padding;

    // 영역 채우기용 베이스라인 trace
    const baseLine = {
        x: dates,
        y: dates.map(() => yMin),
        type: 'scatter',
        mode: 'lines',
        line: { width: 0 },
        showlegend: false,
        hoverinfo: 'skip',
    };

    const trace = {
        x: dates,
        y: equities,
        type: 'scatter',
        mode: 'lines+markers',
        name: '총자산',
        line: { color: '#6366f1', width: 2.5, shape: 'spline' },
        marker: {
            color: markerColors,
            size: 9,
            line: { color: '#1a1a2e', width: 2 },
            symbol: 'circle',
        },
        fill: 'tonexty',
        fillcolor: 'rgba(99,102,241,0.08)',
        hovertext: hoverTexts,
        hoverinfo: 'text',
    };

    const layout = {
        paper_bgcolor: 'transparent',
        plot_bgcolor: 'transparent',
        margin: { t: 10, b: 40, l: 70, r: 20 },
        xaxis: {
            color: '#5a6480',
            gridcolor: 'rgba(99,102,241,0.06)',
            tickfont: { size: 11, family: 'JetBrains Mono, monospace', color: '#5a6480' },
            showspikes: true,
            spikemode: 'across',
            spikethickness: 1,
            spikecolor: 'rgba(99,102,241,0.3)',
            spikedash: 'dot',
        },
        yaxis: {
            color: '#5a6480',
            gridcolor: 'rgba(99,102,241,0.06)',
            tickfont: { size: 11, family: 'JetBrains Mono, monospace', color: '#5a6480' },
            tickformat: ',.0f',
            ticksuffix: '',
            range: [yMin, yMax],
            showspikes: true,
            spikemode: 'across',
            spikethickness: 1,
            spikecolor: 'rgba(99,102,241,0.3)',
            spikedash: 'dot',
        },
        showlegend: false,
        hovermode: 'closest',
        hoverlabel: {
            bgcolor: '#1a1a2e',
            bordercolor: 'rgba(99,102,241,0.4)',
            font: { color: '#e2e8f0', size: 12.5, family: 'DM Sans, sans-serif' },
            align: 'left',
        },
    };

    const config = { displayModeBar: false, responsive: true };

    if (!equityChartInitialized) {
        Plotly.newPlot('equity-chart', [baseLine, trace], layout, config);
        equityChartInitialized = true;
    } else {
        Plotly.react('equity-chart', [baseLine, trace], layout, config);
    }
}

// ============================================================
// 일자별 테이블
// ============================================================

function renderEquityTable(snapshots) {
    const tbody = document.getElementById('equity-table-body');
    const countEl = document.getElementById('table-count');

    if (!snapshots || snapshots.length === 0) {
        tbody.innerHTML = '<tr><td colspan="9" style="padding: 40px 0; text-align: center; color: var(--text-muted); font-size: 0.85rem;">데이터 없음</td></tr>';
        countEl.textContent = '0일';
        return;
    }

    countEl.textContent = snapshots.length + '일';

    // 최신순 정렬
    const sorted = [...snapshots].reverse();

    const rows = sorted.map((s, idx) => {
        const pnlCls = pnlClass(s.daily_pnl);
        const hasPositions = s.positions && s.positions.length > 0;
        const expandIcon = hasPositions ? '&#9654;' : '';

        return `<tr class="border-b" style="border-color: rgba(99,102,241,0.08);" data-date="${s.date}" data-has-positions="${hasPositions}">
            <td class="py-2 pr-2" style="text-align: center;">
                ${hasPositions ? `<button class="expand-btn" onclick="togglePositionDetail(this, '${s.date}')" title="포지션 상세">${expandIcon}</button>` : ''}
            </td>
            <td class="py-2 pr-3 mono" style="font-size: 0.82rem; color: var(--text-secondary); white-space: nowrap;">${s.date}</td>
            <td class="py-2 pr-3 text-right mono" style="font-weight: 500;">${formatNumber(s.total_equity)}<span style="font-size:0.68rem; color:var(--text-muted);">원</span></td>
            <td class="py-2 pr-3 text-right mono ${pnlCls}">${formatPnl(s.daily_pnl)}</td>
            <td class="py-2 pr-3 text-right mono ${pnlCls}" style="font-weight: 500;">${formatPct(s.daily_pnl_pct)}</td>
            <td class="py-2 pr-3 text-right mono col-hide-mobile" style="color: var(--text-secondary);">${s.cash > 0 ? formatNumber(s.cash) + '<span style="font-size:0.68rem; color:var(--text-muted);">원</span>' : '--'}</td>
            <td class="py-2 pr-3 text-right mono col-hide-mobile" style="color: var(--text-secondary);">${s.position_count}</td>
            <td class="py-2 pr-3 text-right mono col-hide-mobile" style="color: var(--text-secondary);">${s.trades_count}</td>
            <td class="py-2 text-right mono" style="color: var(--text-secondary);">${s.trades_count > 0 ? s.win_rate.toFixed(0) + '%' : '--'}</td>
        </tr>`;
    }).join('');

    tbody.innerHTML = rows;
}

// ============================================================
// 포지션 상세 (expandable row)
// ============================================================

function togglePositionDetail(btn, dateStr) {
    const tr = btn.closest('tr');
    const nextRow = tr.nextElementSibling;

    // 이미 열려있으면 닫기
    if (nextRow && nextRow.classList.contains('position-detail-row')) {
        nextRow.remove();
        btn.innerHTML = '&#9654;';
        return;
    }

    btn.innerHTML = '&#9660;';

    // 캐시된 데이터에서 찾기
    const snapshot = lastSnapshots.find(s => s.date === dateStr);
    if (snapshot && snapshot.positions && snapshot.positions.length > 0) {
        insertPositionDetailRow(tr, snapshot.positions);
    } else {
        // API 호출
        const detailRow = document.createElement('tr');
        detailRow.className = 'position-detail-row';
        detailRow.innerHTML = '<td colspan="9"><div class="position-detail-content" style="color:var(--text-muted); font-size:0.82rem;">로딩 중...</div></td>';
        tr.after(detailRow);

        api(`/api/equity-history/positions?date=${dateStr}`).then(data => {
            if (data && data.positions && data.positions.length > 0) {
                detailRow.remove();
                insertPositionDetailRow(tr, data.positions);
            } else {
                detailRow.innerHTML = '<td colspan="9"><div class="position-detail-content" style="color:var(--text-muted); font-size:0.82rem;">포지션 데이터 없음</div></td>';
            }
        }).catch(() => {
            detailRow.innerHTML = '<td colspan="9"><div class="position-detail-content" style="color:var(--text-muted); font-size:0.82rem;">조회 실패</div></td>';
        });
    }
}

function insertPositionDetailRow(afterRow, positions) {
    // 수익률 내림차순 정렬 (높은→낮은)
    positions.sort((a, b) => (b.pnl_pct ?? 0) - (a.pnl_pct ?? 0));

    const posTable = positions.map(p => {
        const cls = pnlClass(p.pnl);
        return `<tr style="border-bottom: 1px solid var(--border-subtle);">
            <td class="py-1 pr-3" style="font-size:0.78rem; font-weight:500; color:var(--text-primary); white-space:nowrap;">${esc(p.name || p.symbol)} <span style="color:var(--text-muted); font-size:0.65rem;">${esc(p.symbol)}</span></td>
            <td class="py-1 pr-3 text-right mono" style="font-size:0.78rem;">${p.quantity}</td>
            <td class="py-1 pr-3 text-right mono" style="font-size:0.78rem; color:var(--text-secondary);">${formatNumber(p.avg_price)}</td>
            <td class="py-1 pr-3 text-right mono" style="font-size:0.78rem;">${formatNumber(p.current_price)}</td>
            <td class="py-1 pr-3 text-right mono" style="font-size:0.78rem;">${formatNumber(p.market_value)}<span style="font-size:0.65rem; color:var(--text-muted);">원</span></td>
            <td class="py-1 pr-3 text-right mono ${cls}" style="font-size:0.78rem;">${formatPnl(p.pnl)}</td>
            <td class="py-1 text-right mono ${cls}" style="font-size:0.78rem; font-weight:600;">${formatPct(p.pnl_pct)}</td>
        </tr>`;
    }).join('');

    const detailRow = document.createElement('tr');
    detailRow.className = 'position-detail-row';
    detailRow.innerHTML = `<td colspan="9" class="position-detail-row">
        <div class="position-detail-content">
            <table style="width:100%; text-align:left; border-collapse:collapse;">
                <thead>
                    <tr style="border-bottom:1px solid var(--border-subtle);">
                        <th style="padding:0 10px 6px 0; font-size:0.65rem;">종목</th>
                        <th style="padding:0 10px 6px 0; text-align:right; font-size:0.65rem;">수량</th>
                        <th style="padding:0 10px 6px 0; text-align:right; font-size:0.65rem;">평균가</th>
                        <th style="padding:0 10px 6px 0; text-align:right; font-size:0.65rem;">현재가</th>
                        <th style="padding:0 10px 6px 0; text-align:right; font-size:0.65rem;">평가액</th>
                        <th style="padding:0 10px 6px 0; text-align:right; font-size:0.65rem;">손익</th>
                        <th style="padding:0 0 6px 0; text-align:right; font-size:0.65rem;">수익률</th>
                    </tr>
                </thead>
                <tbody>${posTable}</tbody>
            </table>
        </div>
    </td>`;
    afterRow.after(detailRow);
}

// ============================================================
// 초기화
// ============================================================

// ============================================================
// US 자산
// ============================================================

async function loadUSEquity() {
    try {
        const [portfolio, positions, status] = await Promise.all([
            fetch('/api/us-proxy/api/us/portfolio').then(r => r.ok ? r.json() : null),
            fetch('/api/us-proxy/api/us/positions').then(r => r.ok ? r.json() : null),
            fetch('/api/us-proxy/api/us/status').then(r => r.ok ? r.json() : null).catch(() => null),
        ]);
        // 모의거래 배지 표시
        const isPaper = status && (status.paper_trading === true || status.broker === 'alpaca_paper' || status.env === 'dev');
        const badge = document.getElementById('us-eq-paper-badge');
        const note = document.getElementById('us-eq-paper-note');
        if (badge) badge.style.display = isPaper ? 'inline-block' : 'none';
        if (note) note.style.display = isPaper ? 'inline-block' : 'none';
        renderUSEquity(portfolio, positions);
    } catch (e) {
        console.error('[자산] US 데이터 로드 실패:', e);
    }
}

function renderUSEquity(portfolio, positions) {
    // 요약 카드
    if (portfolio) {
        const fmt = v => '$' + Number(v || 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        document.getElementById('us-eq-total').textContent = fmt(portfolio.total_value);
        document.getElementById('us-eq-cash').textContent = fmt(portfolio.cash);
        const posVal = (portfolio.total_value || 0) - (portfolio.cash || 0);
        document.getElementById('us-eq-positions').textContent = fmt(posVal);
        const pnlEl = document.getElementById('us-eq-daily-pnl');
        const dailyPnl = portfolio.daily_pnl || 0;
        pnlEl.textContent = (dailyPnl >= 0 ? '+' : '') + fmt(dailyPnl);
        pnlEl.className = 'mono ' + (dailyPnl >= 0 ? 'text-profit' : 'text-loss');
    }

    // 포지션 테이블
    const tbody = document.getElementById('us-eq-positions-body');
    const countEl = document.getElementById('us-eq-pos-count');

    if (!positions || positions.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" style="padding:40px 0;text-align:center;color:var(--text-muted);font-size:0.85rem;">보유 포지션 없음</td></tr>';
        countEl.textContent = '0개';
        return;
    }

    countEl.textContent = positions.length + '개';
    const rows = positions.map(p => {
        const pnl = (p.unrealized_pnl || 0);
        const pnlPct = (p.unrealized_pnl_pct || 0);
        const cls = pnl >= 0 ? 'text-profit' : 'text-loss';
        const fmtUsd = v => '$' + Number(v || 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        return `<tr style="border-bottom: 1px solid var(--border-subtle);">
            <td class="py-2 pr-3" style="font-weight:500;">${esc(p.symbol)}</td>
            <td class="py-2 pr-3 text-right mono">${p.quantity}</td>
            <td class="py-2 pr-3 text-right mono" style="color:var(--text-secondary);">${fmtUsd(p.avg_price)}</td>
            <td class="py-2 pr-3 text-right mono">${fmtUsd(p.current_price)}</td>
            <td class="py-2 pr-3 text-right mono">${fmtUsd(p.market_value)}</td>
            <td class="py-2 pr-3 text-right mono ${cls}">${(pnl >= 0 ? '+' : '') + fmtUsd(pnl)}</td>
            <td class="py-2 text-right mono ${cls}" style="font-weight:600;">${(pnlPct >= 0 ? '+' : '') + pnlPct.toFixed(2)}%</td>
        </tr>`;
    }).join('');
    tbody.innerHTML = rows;
}

function applyEquityMarketFilter(filter) {
    const krSec = document.getElementById('kr-equity-section');
    const usSec = document.getElementById('us-equity-section');
    if (!krSec || !usSec) return;
    if (filter === 'all') { krSec.style.display = ''; usSec.style.display = ''; }
    else if (filter === 'kr') { krSec.style.display = ''; usSec.style.display = 'none'; }
    else { krSec.style.display = 'none'; usSec.style.display = ''; }
}

document.addEventListener('DOMContentLoaded', async () => {
    sse.connect();

    // Market Filter
    const mfBar = document.getElementById('market-filter-bar');
    if (mfBar && typeof MarketFilter !== 'undefined') {
        MarketFilter.render(mfBar, (val) => applyEquityMarketFilter(val));
        applyEquityMarketFilter(MarketFilter.get());
        loadUSEquity();
        setInterval(loadUSEquity, 30000);
    }

    const dateFromInput = document.getElementById('date-from');
    const dateToInput = document.getElementById('date-to');
    const searchBtn = document.getElementById('date-search-btn');

    // 오늘 날짜
    const today = new Date().toISOString().slice(0, 10);
    dateToInput.value = today;

    // 초기 로드: oldest_date를 가져와서 from 기본값 설정
    try {
        const data = await api(`/api/equity-history?days=9999`);
        const oldest = (data.summary && data.summary.oldest_date) || today;
        dateFromInput.value = oldest;
        // 가져온 데이터로 바로 렌더링
        lastSnapshots = data.snapshots || [];
        renderSummary(data.summary || {}, lastSnapshots);
        renderEquityChart(lastSnapshots);
        renderEquityTable(lastSnapshots);
    } catch (e) {
        console.error('[자산] 초기 로드 실패:', e);
        dateFromInput.value = today;
    }

    // 조회 버튼 클릭
    searchBtn.addEventListener('click', () => {
        const dateFrom = dateFromInput.value;
        const dateTo = dateToInput.value;
        if (dateFrom && dateTo) {
            loadEquityByRange(dateFrom, dateTo);
        }
    });

    // Enter 키로도 조회
    dateFromInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') searchBtn.click();
    });
    dateToInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') searchBtn.click();
    });
});
