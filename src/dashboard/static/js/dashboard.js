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

sse.on('pending_orders', (data) => {
    renderPendingOrders(data);
});

sse.on('health_checks', (data) => {
    renderHealthChecks(data, true);
});

sse.on('external_accounts', (data) => {
    renderExternalAccounts(data);
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
// 대기 주문 카드 렌더링
// ============================================================

function renderPendingOrders(orders) {
    const card = document.getElementById('pending-orders-card');
    const list = document.getElementById('pending-orders-list');
    const countEl = document.getElementById('pending-orders-count');

    if (!orders || orders.length === 0) {
        card.style.display = 'none';
        return;
    }

    card.style.display = 'block';
    countEl.textContent = orders.length + '건';

    const items = orders.map(o => {
        const sideCls = o.side === 'SELL' ? 'badge-red' : 'badge-blue';
        const sideLabel = o.side === 'SELL' ? '매도' : '매수';
        const gaugeColor = o.progress_pct >= 80 ? 'var(--accent-red)' : 'var(--accent-blue)';
        const elapsed = o.elapsed_seconds;
        const elapsedStr = elapsed >= 60 ? `${Math.floor(elapsed / 60)}분 ${elapsed % 60}초` : `${elapsed}초`;
        const remainStr = o.remaining_seconds >= 60 ? `${Math.floor(o.remaining_seconds / 60)}분 ${o.remaining_seconds % 60}초` : `${o.remaining_seconds}초`;

        return `<div style="background: var(--bg-elevated); border: 1px solid var(--border-subtle); border-radius: 10px; padding: 12px 16px;">
            <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px;">
                <div style="display: flex; align-items: center; gap: 8px;">
                    <span style="font-weight: 600; font-size: 0.88rem; color: var(--text-primary);">${o.name || o.symbol}</span>
                    <span style="font-size: 0.72rem; color: var(--text-muted);">${o.symbol}</span>
                    <span class="badge ${sideCls}">${sideLabel}</span>
                </div>
                <span class="mono" style="font-size: 0.78rem; color: var(--text-secondary);">${o.quantity}주</span>
            </div>
            <div style="display: flex; align-items: center; gap: 12px;">
                <div style="flex: 1; background: rgba(99,102,241,0.08); border-radius: 4px; height: 6px; overflow: hidden;">
                    <div style="width: ${o.progress_pct}%; height: 100%; background: ${gaugeColor}; border-radius: 4px; transition: width 0.3s;"></div>
                </div>
                <span class="mono" style="font-size: 0.72rem; color: ${o.progress_pct >= 80 ? 'var(--accent-red)' : 'var(--text-muted)'}; white-space: nowrap;">
                    ${elapsedStr} / ${o.timeout_seconds}초
                </span>
            </div>
            ${o.progress_pct >= 80 ? '<div style="margin-top: 6px; font-size: 0.72rem; color: var(--accent-amber);">시장가 폴백 임박 (잔여 ' + remainStr + ')</div>' : ''}
        </div>`;
    }).join('');

    list.innerHTML = items;
}

// ============================================================
// 헬스체크 카드 렌더링
// ============================================================

function renderHealthChecks(checks, failedOnly) {
    const card = document.getElementById('health-checks-card');
    const grid = document.getElementById('health-checks-grid');
    const badge = document.getElementById('health-checks-badge');
    const dot = document.getElementById('health-dot');

    if (!checks || checks.length === 0) {
        card.style.display = 'none';
        return;
    }

    card.style.display = 'block';

    // SSE는 실패 항목만 전달, API는 전체 항목 전달
    const failed = failedOnly ? checks : checks.filter(c => !c.ok);
    const hasCritical = checks.some(c => c.level === 'critical' && !c.ok);
    const hasWarning = checks.some(c => c.level === 'warning' && !c.ok);

    // 배지 + 도트 색상
    if (hasCritical) {
        badge.textContent = failed.length + '건 이상';
        badge.style.color = 'var(--accent-red)';
        badge.style.background = 'rgba(248,113,113,0.08)';
        badge.style.borderColor = 'rgba(248,113,113,0.12)';
        dot.style.background = 'var(--accent-red)';
        dot.style.boxShadow = '0 0 8px var(--accent-red)';
    } else if (hasWarning) {
        badge.textContent = failed.length + '건 주의';
        badge.style.color = 'var(--accent-amber)';
        badge.style.background = 'rgba(251,191,36,0.08)';
        badge.style.borderColor = 'rgba(251,191,36,0.12)';
        dot.style.background = 'var(--accent-amber)';
        dot.style.boxShadow = '0 0 8px var(--accent-amber)';
    } else {
        badge.textContent = '정상';
        badge.style.color = 'var(--accent-green)';
        badge.style.background = 'rgba(52,211,153,0.08)';
        badge.style.borderColor = 'rgba(52,211,153,0.12)';
        dot.style.background = 'var(--accent-green)';
        dot.style.boxShadow = '0 0 8px var(--accent-green)';
    }

    // 그리드 아이템 렌더링
    const items = checks.map(c => {
        const isOk = c.ok !== false;
        let color, bg, border, icon;
        if (!isOk && c.level === 'critical') {
            color = 'var(--accent-red)'; bg = 'rgba(248,113,113,0.06)'; border = 'rgba(248,113,113,0.15)'; icon = '\u26d4';
        } else if (!isOk && c.level === 'warning') {
            color = 'var(--accent-amber)'; bg = 'rgba(251,191,36,0.06)'; border = 'rgba(251,191,36,0.15)'; icon = '\u26a0\ufe0f';
        } else {
            color = 'var(--accent-green)'; bg = 'rgba(52,211,153,0.04)'; border = 'rgba(52,211,153,0.08)'; icon = '\u2705';
        }

        const nameMap = {
            event_loop_stall: '\uc774\ubca4\ud2b8 \ub8e8\ud504',
            ws_feed: 'WebSocket',
            daily_loss: '\uc77c\uc77c \uc190\uc775',
            pending_deadlock: 'Pending',
            memory: '\uba54\ubaa8\ub9ac',
            queue_saturation: '\uc774\ubca4\ud2b8 \ud050',
            broker: '\ube0c\ub85c\ucee4',
            rolling_perf: '\ub864\ub9c1 \uc131\uacfc',
        };
        const label = nameMap[c.name] || c.name;
        const valStr = c.value != null ? `<span class="mono" style="font-size:0.72rem; color:var(--text-secondary);">${typeof c.value === 'number' ? c.value.toFixed(1) : c.value}</span>` : '';

        return `<div style="background:${bg}; border:1px solid ${border}; border-radius:10px; padding:10px 12px;">
            <div style="display:flex; align-items:center; gap:6px; margin-bottom:4px;">
                <span style="font-size:0.75rem;">${icon}</span>
                <span style="font-size:0.78rem; font-weight:500; color:${isOk ? 'var(--text-primary)' : color};">${label}</span>
                ${valStr}
            </div>
            <div style="font-size:0.72rem; color:${isOk ? 'var(--text-muted)' : color};">${c.message}</div>
        </div>`;
    }).join('');

    grid.innerHTML = items;
}

// ============================================================
// 외부 계좌 카드 렌더링
// ============================================================

function renderExternalAccounts(accounts) {
    const section = document.getElementById('external-accounts-section');
    const container = document.getElementById('external-accounts-container');

    if (!accounts || accounts.length === 0) {
        section.style.display = 'none';
        return;
    }

    // 포지션이 있는 계좌만 표시할지 여부 (빈 계좌도 요약은 표시)
    section.style.display = 'block';

    const cards = accounts.map(acct => {
        const s = acct.summary || {};
        const positions = acct.positions || [];
        const hasError = !!acct.error;

        // 계좌 요약
        const totalEquity = s.total_equity || 0;
        const stockValue = s.stock_value || 0;
        const deposit = s.deposit || 0;
        const unrealizedPnl = s.unrealized_pnl || 0;
        const purchaseAmt = s.purchase_amount || 0;
        const pnlPct = purchaseAmt > 0 ? (unrealizedPnl / purchaseAmt * 100) : 0;

        // 포지션 테이블 행
        let posRows = '';
        if (positions.length > 0) {
            posRows = positions.map(p => {
                const cls = pnlClass(p.pnl);
                return `<tr style="border-bottom:1px solid var(--border-subtle);">
                    <td class="py-1 pr-3" style="font-size:0.82rem; font-weight:500; color:var(--text-primary); white-space:nowrap;">${p.name || p.symbol} <span style="color:var(--text-muted); font-size:0.68rem;">${p.symbol}</span></td>
                    <td class="py-1 pr-3 text-right mono" style="font-size:0.82rem;">${formatNumber(p.current_price)}</td>
                    <td class="py-1 pr-3 text-right mono" style="font-size:0.82rem; color:var(--text-secondary);">${formatNumber(p.avg_price)}</td>
                    <td class="py-1 pr-3 text-right mono" style="font-size:0.82rem;">${p.qty}</td>
                    <td class="py-1 pr-3 text-right mono" style="font-size:0.82rem;">${formatCurrency(p.eval_amt)}</td>
                    <td class="py-1 pr-3 text-right mono ${cls}" style="font-size:0.82rem;">${formatPnl(p.pnl)}</td>
                    <td class="py-1 text-right mono ${cls}" style="font-size:0.82rem; font-weight:600;">${formatPct(p.pnl_pct)}</td>
                </tr>`;
            }).join('');
        } else {
            posRows = '<tr><td colspan="7" style="padding:20px 0; text-align:center; color:var(--text-muted); font-size:0.82rem;">보유 종목 없음</td></tr>';
        }

        return `<div class="card" style="padding: 24px; margin-bottom: 16px;">
            <div class="card-header">
                <span class="dot" style="background: var(--accent-purple); box-shadow: 0 0 8px var(--accent-purple);"></span>
                ${acct.name} 계좌
                <span style="margin-left: 8px; font-size: 0.68rem; color: var(--text-muted); font-family: 'JetBrains Mono', monospace; letter-spacing: 0;">${acct.cano}</span>
                ${hasError ? '<span class="badge badge-red" style="margin-left:auto;">오류</span>' : `<span style="margin-left:auto; font-size:0.7rem; color:var(--text-muted); font-family:\'JetBrains Mono\',monospace; background:var(--bg-elevated); padding:3px 10px; border-radius:6px; border:1px solid var(--border-subtle);">${positions.length}종목</span>`}
            </div>

            <!-- 요약 -->
            <div style="display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:16px;">
                <div>
                    <div style="font-size:0.68rem; color:var(--text-muted); text-transform:uppercase; letter-spacing:0.08em; margin-bottom:4px;">총자산</div>
                    <div class="mono" style="font-size:0.95rem; font-weight:600; color:var(--text-primary);">${formatCurrency(totalEquity)}</div>
                </div>
                <div>
                    <div style="font-size:0.68rem; color:var(--text-muted); text-transform:uppercase; letter-spacing:0.08em; margin-bottom:4px;">예수금</div>
                    <div class="mono" style="font-size:0.95rem; font-weight:500; color:var(--text-secondary);">${formatCurrency(deposit)}</div>
                </div>
                <div>
                    <div style="font-size:0.68rem; color:var(--text-muted); text-transform:uppercase; letter-spacing:0.08em; margin-bottom:4px;">주식평가</div>
                    <div class="mono" style="font-size:0.95rem; font-weight:500; color:var(--text-secondary);">${formatCurrency(stockValue)}</div>
                </div>
                <div>
                    <div style="font-size:0.68rem; color:var(--text-muted); text-transform:uppercase; letter-spacing:0.08em; margin-bottom:4px;">평가손익</div>
                    <div class="mono ${pnlClass(unrealizedPnl)}" style="font-size:0.95rem; font-weight:600;">${formatPnl(unrealizedPnl)} <span style="font-size:0.72rem;">(${formatPct(pnlPct)})</span></div>
                </div>
            </div>

            <!-- 포지션 테이블 -->
            ${hasError ? `<div style="padding:12px; background:rgba(248,113,113,0.06); border:1px solid rgba(248,113,113,0.12); border-radius:8px; margin-bottom:12px;">
                <div style="color:var(--accent-red); font-size:0.78rem;">조회 실패: ${acct.error}</div>
            </div>` : ''}

            ${positions.length > 0 ? `<div style="overflow-x:auto;">
                <table style="width:100%; text-align:left; border-collapse:collapse;">
                    <thead>
                        <tr style="border-bottom:1px solid var(--border-subtle);">
                            <th style="padding:0 10px 8px 0; font-size:0.68rem; color:var(--text-muted); text-transform:uppercase; letter-spacing:0.08em;">종목</th>
                            <th style="padding:0 10px 8px 0; text-align:right; font-size:0.68rem; color:var(--text-muted); text-transform:uppercase; letter-spacing:0.08em;">현재가</th>
                            <th style="padding:0 10px 8px 0; text-align:right; font-size:0.68rem; color:var(--text-muted); text-transform:uppercase; letter-spacing:0.08em;">평균가</th>
                            <th style="padding:0 10px 8px 0; text-align:right; font-size:0.68rem; color:var(--text-muted); text-transform:uppercase; letter-spacing:0.08em;">수량</th>
                            <th style="padding:0 10px 8px 0; text-align:right; font-size:0.68rem; color:var(--text-muted); text-transform:uppercase; letter-spacing:0.08em;">평가금액</th>
                            <th style="padding:0 10px 8px 0; text-align:right; font-size:0.68rem; color:var(--text-muted); text-transform:uppercase; letter-spacing:0.08em;">손익</th>
                            <th style="padding:0 0 8px 0; text-align:right; font-size:0.68rem; color:var(--text-muted); text-transform:uppercase; letter-spacing:0.08em;">손익률</th>
                        </tr>
                    </thead>
                    <tbody>${posRows}</tbody>
                </table>
            </div>` : `<div style="text-align:center; padding:12px 0; color:var(--text-muted); font-size:0.82rem;">보유 종목 없음</div>`}
        </div>`;
    }).join('');

    container.innerHTML = cards;
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

    // 대기 주문 초기 로드
    api('/api/orders/pending').then(data => {
        renderPendingOrders(data);
    }).catch(() => {});

    // 헬스체크 초기 로드
    api('/api/health-checks').then(data => {
        renderHealthChecks(data, false);
    }).catch(() => {});
    // 30초마다 헬스체크 갱신
    setInterval(() => {
        api('/api/health-checks').then(data => {
            renderHealthChecks(data, false);
        }).catch(() => {});
    }, 30000);

    // 외부 계좌 초기 로드
    api('/api/accounts/positions').then(data => {
        renderExternalAccounts(data);
    }).catch(() => {});

    // 프리마켓 데이터 로드
    loadPremarket();
    // 30초마다 프리마켓 갱신
    setInterval(loadPremarket, 30000);

    addLogEntry(formatTime(new Date().toISOString()), '시스템', '대시보드 연결됨');
});
