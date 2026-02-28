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

// 마켓별 데이터 캐시 (필터 전환 시 즉시 반영용)
let cachedKRPortfolio = null;
let cachedUSPortfolio = null;
let cachedKRRisk = null;

// ============================================================
// SSE 이벤트 핸들러
// ============================================================

sse.on('portfolio', (data) => {
    cachedKRPortfolio = data;
    updatePortfolioCard();
});

sse.on('risk', (data) => {
    cachedKRRisk = data;
    updateRiskCard();
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
        tbody.innerHTML = '<tr><td colspan="11" class="py-8 text-center text-gray-500">보유 포지션 없음</td></tr>';
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
        // 수수료 포함 순손익 (unrealized_pnl_net)을 우선 사용
        const netPnl = pos.unrealized_pnl_net ?? pos.unrealized_pnl;
        const netPct = pos.unrealized_pnl_net_pct ?? pos.unrealized_pnl_pct;
        const pnlCls = pnlClass(netPnl);
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

        return `<tr class="border-b" style="border-color:rgba(99,102,241,0.08)">
            <td class="py-2 pr-3 font-medium text-white" style="white-space:nowrap;">${esc(pos.name || pos.symbol)} <span style="color:var(--text-muted); font-size:0.72rem; font-weight:400;">${esc(pos.symbol)}</span></td>
            <td class="py-2 pr-3" style="font-size:0.75rem; color:var(--accent-purple);">${esc(stName)}</td>
            <td class="py-2 pr-3 text-right mono">${formatNumber(pos.current_price)}</td>
            <td class="col-avg-price py-2 pr-3 text-right mono text-gray-400">${formatNumber(pos.avg_price)}</td>
            <td class="col-quantity py-2 pr-3 text-right mono">${pos.quantity}</td>
            <td class="col-market-value py-2 pr-3 text-right mono" style="color:var(--text-secondary);">${formatNumber(pos.market_value || (pos.current_price * pos.quantity))}</td>
            <td class="py-2 pr-3 text-right mono ${pnlCls}" title="평가손익: ${formatPnl(pos.unrealized_pnl)}">${formatPnl(netPnl)}</td>
            <td class="py-2 pr-3 text-right mono ${pnlCls}" title="평가수익률: ${formatPct(pos.unrealized_pnl_pct)}">${formatPct(netPct)}</td>
            <td class="col-holding py-2 pr-3 mono" style="font-size:0.75rem; color:var(--text-secondary);">${holdStr}</td>
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
        '신호': 'color: #818cf8;',
        '체결': 'color: #34d399;',
        '주문': 'color: #60a5fa;',
        '리스크': 'color: #fbbf24;',
        '오류': 'color: #f87171;',
        '시스템': 'color: #94a3b8;',
    };

    const colorStyle = typeColors[type] || 'color: #9ca3af;';

    logEntries.unshift({ time, type, message });
    if (logEntries.length > MAX_LOG_ENTRIES) {
        logEntries.pop();
    }

    const logEl = document.getElementById('event-log');
    const entry = document.createElement('div');
    entry.className = 'log-entry';
    entry.style.cssText = 'padding: 3px 0; border-bottom: 1px solid rgba(99,102,241,0.05);';

    const timeSpan = document.createElement('span');
    timeSpan.className = 'mono';
    timeSpan.style.cssText = 'color: #6b7280; font-size: 0.75rem; margin-right: 6px;';
    timeSpan.textContent = time;

    const typeSpan = document.createElement('span');
    typeSpan.style.cssText = colorStyle + ' font-weight: 600; font-size: 0.78rem; margin-right: 6px;';
    typeSpan.textContent = '[' + type + ']';

    const msgSpan = document.createElement('span');
    msgSpan.style.cssText = 'font-size: 0.82rem; color: var(--text-primary);';
    msgSpan.textContent = message;

    entry.append(timeSpan, typeSpan, msgSpan);
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
                    <span style="font-weight: 600; font-size: 0.88rem; color: var(--text-primary);">${esc(o.name || o.symbol)}</span>
                    <span style="font-size: 0.72rem; color: var(--text-muted);">${esc(o.symbol)}</span>
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
        const valStr = c.value != null ? `<span class="mono" style="font-size:0.72rem; color:var(--text-secondary);">${typeof c.value === 'number' ? c.value.toFixed(1) : esc(c.value)}</span>` : '';

        return `<div style="background:${bg}; border:1px solid ${border}; border-radius:10px; padding:10px 12px;">
            <div style="display:flex; align-items:center; gap:6px; margin-bottom:4px;">
                <span style="font-size:0.75rem;">${icon}</span>
                <span style="font-size:0.78rem; font-weight:500; color:${isOk ? 'var(--text-primary)' : color};">${esc(label)}</span>
                ${valStr}
            </div>
            <div style="font-size:0.72rem; color:${isOk ? 'var(--text-muted)' : color};">${esc(c.message)}</div>
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
                    <td class="py-1 pr-3" style="font-size:0.82rem; font-weight:500; color:var(--text-primary); white-space:nowrap;">${esc(p.name || p.symbol)} <span style="color:var(--text-muted); font-size:0.68rem;">${esc(p.symbol)}</span></td>
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
                ${esc(acct.name)} 계좌
                <span style="margin-left: 8px; font-size: 0.68rem; color: var(--text-muted); font-family: 'JetBrains Mono', monospace; letter-spacing: 0;">${esc(acct.cano)}</span>
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
                <div style="color:var(--accent-red); font-size:0.78rem;">조회 실패: ${esc(acct.error)}</div>
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
// 주문 이벤트 히스토리
// ============================================================

function renderOrderHistory(events) {
    const card = document.getElementById('order-history-card');
    const tbody = document.getElementById('order-history-body');
    const countEl = document.getElementById('order-history-count');

    if (!events || events.length === 0) {
        card.style.display = 'none';
        return;
    }

    card.style.display = 'block';
    countEl.textContent = events.length + '건';

    const typeColors = {
        '체결': 'badge-green',
        '주문': 'badge-blue',
        '취소': 'badge-red',
        '폴백': 'badge-yellow',
        '신호': 'badge-purple',
        '오류': 'badge-red',
        '리스크': 'badge-yellow',
    };

    // 매수/매도 강조 색상
    const sideStyle = {
        '매수': 'color: var(--accent-blue); font-weight: 600;',
        '매도': 'font-weight: 600;',
    };

    // 최신순 정렬, 최대 30건
    const sorted = [...events].reverse().slice(0, 30);

    const fragment = document.createDocumentFragment();
    sorted.forEach(evt => {
        const tr = document.createElement('tr');
        tr.style.borderBottom = '1px solid rgba(99,102,241,0.08)';

        const time = evt.time ? formatTime(evt.time) : '--';
        const evtType = evt.type || '--';
        const message = evt.message || '';

        let badgeCls = 'badge-blue';
        for (const [key, cls] of Object.entries(typeColors)) {
            if (evtType === key) { badgeCls = cls; break; }
        }

        // 시간
        const tdTime = document.createElement('td');
        tdTime.className = 'py-2 pr-3 mono';
        tdTime.style.cssText = 'font-size:0.78rem; color:var(--text-secondary); white-space:nowrap;';
        tdTime.textContent = time;

        // 유형 배지
        const tdType = document.createElement('td');
        tdType.className = 'py-2 pr-3';
        tdType.style.whiteSpace = 'nowrap';
        const badge = document.createElement('span');
        badge.className = 'badge ' + badgeCls;
        badge.textContent = evtType;
        tdType.appendChild(badge);

        // 메시지 (매수/매도 강조)
        const tdMsg = document.createElement('td');
        tdMsg.className = 'py-2';
        tdMsg.style.cssText = 'font-size:0.82rem; color:var(--text-primary);';

        // 메시지에서 매도 손익 강조
        const isSell = message.includes('매도');
        if (isSell && evtType === '체결') {
            tdMsg.style.color = '#f87171';
        } else if (message.includes('매수') && evtType === '체결') {
            tdMsg.style.color = 'var(--accent-blue)';
        }
        tdMsg.textContent = message;

        tr.append(tdTime, tdType, tdMsg);
        fragment.appendChild(tr);
    });

    tbody.textContent = '';
    tbody.appendChild(fragment);
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
            <div style="font-size:0.78rem; font-weight:500; color:var(--text-primary); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${esc(s.name || s.symbol)}</div>
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

    // 주문 히스토리 로드
    api('/api/orders/history').then(data => {
        renderOrderHistory(data);
    }).catch(() => {});
    // 30초마다 주문 히스토리 갱신
    setInterval(() => {
        api('/api/orders/history').then(data => {
            renderOrderHistory(data);
        }).catch(() => {});
    }, 30000);

    addLogEntry(formatTime(new Date().toISOString()), '시스템', '대시보드 연결됨');

    // 마켓 필터 바 렌더링
    const filterBar = document.getElementById("market-filter-bar");
    if (filterBar) {
        MarketFilter.render(filterBar, (filter) => {
            applyMarketFilter(filter);
            if (filter !== "kr") loadUSData();
        });
    }

    // 초기 필터 적용
    const initFilter = MarketFilter.get();
    applyMarketFilter(initFilter);
    if (initFilter !== "kr") {
        loadUSData();
        setInterval(loadUSData, 30000);
    }

    document.addEventListener("market_filter_change", (e) => {
        applyMarketFilter(e.detail.filter);
    });
});

// ============================================================
// 마켓 필터 통합 (US 데이터)
// ============================================================

async function loadUSData() {
    try {
        const [status, portfolio, positions] = await Promise.all([
            fetch("/api/us-proxy/api/us/status").then(r => r.json()).catch(() => ({ offline: true })),
            fetch("/api/us-proxy/api/us/portfolio").then(r => r.json()).catch(() => ({ offline: true })),
            fetch("/api/us-proxy/api/us/positions").then(r => r.json()).catch(() => []),
        ]);
        renderUSStatus(status);
        renderUSPortfolio(portfolio);
        renderUSPositions(positions);
        cachedUSPortfolio = portfolio;
        updatePortfolioCard();
    } catch (e) {
        console.warn("[US] 데이터 로드 실패:", e);
    }
}

function renderUSStatus(s) {
    const statusEl = document.getElementById("us-bot-status");
    const sessionEl = document.getElementById("us-bot-session");
    const paperBadge = document.getElementById("us-paper-badge");
    const assetBadge = document.getElementById("us-asset-badge");
    const brokerEl = document.getElementById("us-bot-broker");
    if (!statusEl) return;

    if (s.offline || s.error) {
        statusEl.innerHTML = '<span style="color:var(--accent-red);">● 오프라인</span>';
        if (sessionEl) sessionEl.textContent = "API 연결 불가";
        return;
    }

    const running = s.running;
    statusEl.innerHTML = running
        ? '<span style="color:var(--accent-green);">● Running</span>'
        : '<span style="color:var(--text-muted);">● Stopped</span>';
    const sessionMap = { regular: "정규장", pre_market: "프리마켓", after_hours: "애프터마켓", closed: "장 마감" };
    if (sessionEl) sessionEl.textContent = sessionMap[s.session] || s.session || "-";

    // 모의(paper) 거래 여부 표시
    const isPaper = s.paper_trading === true || s.broker === "alpaca_paper" || s.env === "dev";
    if (paperBadge) paperBadge.style.display = isPaper ? "inline-block" : "none";
    if (assetBadge) assetBadge.style.display = isPaper ? "inline-block" : "none";
    if (brokerEl) {
        if (isPaper) {
            const brokerName = s.broker === "alpaca_paper" ? "Alpaca Paper" : (s.broker || "모의");
            brokerEl.textContent = `모의거래 (${brokerName}) — KR 총자산에 미포함`;
            brokerEl.style.display = "block";
        } else {
            brokerEl.style.display = "none";
        }
    }
}

function renderUSPortfolio(p) {
    const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
    if (p.offline || p.error) {
        set("us-total-value", "-"); set("us-cash", "-"); set("us-daily-pnl", "-");
        return;
    }
    set("us-total-value", "$" + (p.total_value || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
    set("us-cash", "$" + (p.cash || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
    const pnl = p.daily_pnl || 0;
    const pnlEl = document.getElementById("us-daily-pnl");
    if (pnlEl) {
        const sign = pnl >= 0 ? "+" : "";
        pnlEl.textContent = sign + "$" + Math.abs(pnl).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        pnlEl.style.color = pnl >= 0 ? "var(--accent-green)" : "var(--accent-red)";
    }
}

function renderUSPositions(positions) {
    const tbody = document.getElementById("us-positions-body");
    if (!tbody) return;
    if (!positions || positions.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" style="padding:20px 0;text-align:center;color:var(--text-muted);font-size:0.82rem;">보유 종목 없음</td></tr>';
        return;
    }
    tbody.innerHTML = positions.map(p => {
        const pnlPct = p.pnl_pct || 0;
        const pnlColor = pnlPct >= 0 ? "var(--accent-green)" : "var(--accent-red)";
        const sign = pnlPct >= 0 ? "+" : "";
        return '<tr style="border-bottom:1px solid var(--border-subtle);">' +
            '<td style="padding:8px 10px 8px 0;"><span class="mono" style="font-weight:600;">' + esc(p.symbol) + '</span>' + (p.name ? '<br><span style="font-size:0.72rem;color:var(--text-muted);">' + esc(p.name) + '</span>' : '') + '</td>' +
            '<td style="padding:8px 10px 8px 0;font-size:0.78rem;color:var(--text-secondary);">' + esc(p.strategy || "-") + '</td>' +
            '<td style="padding:8px 10px 8px 0;" class="mono">' + p.quantity + '</td>' +
            '<td style="padding:8px 10px 8px 0;" class="mono">$' + (p.avg_price||0).toFixed(2) + '</td>' +
            '<td style="padding:8px 10px 8px 0;color:' + pnlColor + ';" class="mono">' + sign + pnlPct.toFixed(2) + '%</td>' +
            '<td style="padding:8px 10px 8px 0;" class="mono">$' + (p.market_value||0).toLocaleString("en-US",{minimumFractionDigits:2}) + '</td>' +
        '</tr>';
    }).join("");
}

// ============================================================
// 마켓 필터 기반 포트폴리오/리스크 카드 업데이트
// ============================================================

function formatUSD(n) {
    if (n === null || n === undefined || isNaN(n)) return '$--';
    return (n < 0 ? '-' : '') + '$' + Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function formatUSDSigned(n) {
    if (n === null || n === undefined || isNaN(n)) return '$--';
    const sign = n > 0 ? '+' : '';
    return sign + '$' + Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function updatePortfolioCard() {
    const filter = MarketFilter.get();
    const kr = cachedKRPortfolio;
    const us = cachedUSPortfolio;

    const equityEl = document.getElementById('p-equity');
    const cashEl = document.getElementById('p-cash');
    const cashPctEl = document.getElementById('p-cash-pct');
    const stockEl = document.getElementById('p-stock');
    const dailyPnlEl = document.getElementById('p-daily-pnl');
    const breakdownEl = document.getElementById('p-pnl-breakdown');

    if (filter === 'kr' || filter === 'all') {
        // KR 데이터 렌더
        if (kr) {
            const krEquity = formatCurrency(kr.total_equity);
            const krCash = formatCurrency(kr.cash);
            const krCashPct = kr.cash_ratio != null ? (kr.cash_ratio * 100).toFixed(0) : '--';
            const krStock = formatCurrency(kr.total_position_value);

            if (filter === 'kr') {
                equityEl.textContent = krEquity;
                cashEl.textContent = krCash;
                cashPctEl.textContent = '(' + krCashPct + '%)';
                stockEl.textContent = krStock;

                dailyPnlEl.textContent = '';
                const pnlText = document.createTextNode(formatPnl(kr.daily_pnl) + ' ');
                const pnlSpan = document.createElement('span');
                pnlSpan.style.cssText = 'font-size:0.72rem; color:var(--text-muted);';
                pnlSpan.textContent = '(' + formatPct(kr.daily_pnl_pct) + ')';
                dailyPnlEl.appendChild(pnlText);
                dailyPnlEl.appendChild(pnlSpan);
                dailyPnlEl.className = 'mono font-semibold ' + pnlClass(kr.daily_pnl);

                _renderKRBreakdown(kr, breakdownEl);
                updatePieChart(kr.cash, kr.total_position_value);
            } else {
                // "all" 모드 — KR + US 분리 표시 (DOM API 사용)
                const usVal = (us && !us.offline && !us.error) ? us : null;
                const usEquityStr = usVal ? formatUSD(usVal.total_value || 0) : '$--';
                const usCashStr = usVal ? formatUSD(usVal.cash || 0) : '$--';
                const usStockStr = usVal ? formatUSD((usVal.total_value || 0) - (usVal.cash || 0)) : '$--';
                const usPnlVal = usVal ? (usVal.daily_pnl || 0) : 0;
                const usPnlStr = usVal ? formatUSDSigned(usPnlVal) : '$--';
                const usPnlPct = usVal && usVal.total_value ? (usPnlVal / usVal.total_value * 100) : 0;

                // 총자산: KR / US
                equityEl.textContent = '';
                _appendFlagValue(equityEl, '\u{1F1F0}\u{1F1F7}', krEquity, '0.85em');
                _appendSep(equityEl, '0.55em');
                _appendFlagValue(equityEl, '\u{1F1FA}\u{1F1F8}', usEquityStr, '0.85em');

                // 현금
                cashEl.textContent = '';
                _appendFlagValue(cashEl, '\u{1F1F0}\u{1F1F7}', krCash);
                _appendSep(cashEl);
                _appendFlagValue(cashEl, '\u{1F1FA}\u{1F1F8}', usCashStr);
                cashPctEl.textContent = '(' + krCashPct + '%)';

                // 주식평가
                stockEl.textContent = '';
                _appendFlagValue(stockEl, '\u{1F1F0}\u{1F1F7}', krStock);
                _appendSep(stockEl);
                _appendFlagValue(stockEl, '\u{1F1FA}\u{1F1F8}', usStockStr);

                // 일일 손익
                dailyPnlEl.textContent = '';
                const krPnlSpan = document.createElement('span');
                krPnlSpan.className = pnlClass(kr.daily_pnl);
                krPnlSpan.textContent = '\u{1F1F0}\u{1F1F7} ' + formatPnl(kr.daily_pnl);
                const krPctSpan = document.createElement('span');
                krPctSpan.style.cssText = 'font-size:0.72rem; color:var(--text-muted);';
                krPctSpan.textContent = ' (' + formatPct(kr.daily_pnl_pct) + ')';
                dailyPnlEl.appendChild(krPnlSpan);
                dailyPnlEl.appendChild(krPctSpan);
                _appendSep(dailyPnlEl);
                const usPnlSpan = document.createElement('span');
                usPnlSpan.className = pnlClass(usPnlVal);
                usPnlSpan.textContent = '\u{1F1FA}\u{1F1F8} ' + usPnlStr;
                const usPctSpan = document.createElement('span');
                usPctSpan.style.cssText = 'font-size:0.72rem; color:var(--text-muted);';
                usPctSpan.textContent = ' (' + (usVal ? formatPct(usPnlPct) : '--') + ')';
                dailyPnlEl.appendChild(usPnlSpan);
                dailyPnlEl.appendChild(usPctSpan);
                dailyPnlEl.className = 'mono font-semibold';

                _renderKRBreakdown(kr, breakdownEl);
                updatePieChart(kr.cash, kr.total_position_value);
            }
        }
    } else if (filter === 'us') {
        // US 전용 모드
        const usVal = (us && !us.offline && !us.error) ? us : null;
        if (usVal) {
            equityEl.textContent = formatUSD(usVal.total_value || 0);
            cashEl.textContent = formatUSD(usVal.cash || 0);
            const usCashPct = usVal.total_value ? ((usVal.cash || 0) / usVal.total_value * 100).toFixed(0) : '--';
            cashPctEl.textContent = '(' + usCashPct + '%)';
            stockEl.textContent = formatUSD((usVal.total_value || 0) - (usVal.cash || 0));

            const pnl = usVal.daily_pnl || 0;
            const pnlPct = usVal.total_value ? (pnl / usVal.total_value * 100) : 0;
            dailyPnlEl.textContent = '';
            const pnlText = document.createTextNode(formatUSDSigned(pnl) + ' ');
            const pctSpan = document.createElement('span');
            pctSpan.style.cssText = 'font-size:0.72rem; color:var(--text-muted);';
            pctSpan.textContent = '(' + formatPct(pnlPct) + ')';
            dailyPnlEl.appendChild(pnlText);
            dailyPnlEl.appendChild(pctSpan);
            dailyPnlEl.className = 'mono font-semibold ' + pnlClass(pnl);

            if (breakdownEl) breakdownEl.textContent = '';
            updatePieChart(usVal.cash || 0, (usVal.total_value || 0) - (usVal.cash || 0));
        } else {
            equityEl.textContent = '$--';
            cashEl.textContent = '$--';
            cashPctEl.textContent = '';
            stockEl.textContent = '$--';
            dailyPnlEl.textContent = '$--';
            dailyPnlEl.className = 'mono font-semibold';
            if (breakdownEl) breakdownEl.textContent = '';
        }
    }

    _updateCardFilterLabel();
}

/** KR 실현/미실현 분리 표시 (DOM API) */
function _renderKRBreakdown(data, el) {
    if (!el) return;
    if (data.realized_daily_pnl || data.unrealized_pnl) {
        el.textContent = '';
        const unrealizedNet = data.unrealized_pnl_net ?? data.unrealized_pnl;
        const netLabel = data.unrealized_pnl_net != null ? '\uBBF8\uC2E4\uD604(\uC21C)' : '\uBBF8\uC2E4\uD604';
        const netTitle = data.unrealized_pnl_net != null
            ? '\uC218\uC218\uB8CC \uD3EC\uD568: ' + formatPnl(unrealizedNet) + ' / \uD3C9\uAC00: ' + formatPnl(data.unrealized_pnl)
            : '';

        const realLabel = document.createElement('span');
        realLabel.style.color = 'var(--text-muted)';
        realLabel.textContent = '\uC2E4\uD604 ';
        const realVal = document.createElement('span');
        realVal.className = 'mono ' + pnlClass(data.realized_daily_pnl);
        realVal.textContent = formatPnl(data.realized_daily_pnl);

        const sep = document.createElement('span');
        sep.style.cssText = 'color:var(--text-muted); margin:0 6px;';
        sep.textContent = '|';

        const unLabel = document.createElement('span');
        unLabel.style.color = 'var(--text-muted)';
        if (netTitle) unLabel.title = netTitle;
        unLabel.textContent = netLabel + ' ';
        const unVal = document.createElement('span');
        unVal.className = 'mono ' + pnlClass(unrealizedNet);
        if (netTitle) unVal.title = netTitle;
        unVal.textContent = formatPnl(unrealizedNet);

        el.append(realLabel, realVal, sep, unLabel, unVal);
    } else {
        el.textContent = '';
    }
}

/** 플래그+값 DOM 요소 추가 */
function _appendFlagValue(parent, flag, value, flagSize) {
    const flagSpan = document.createElement('span');
    if (flagSize) flagSpan.style.fontSize = flagSize;
    flagSpan.textContent = flag + ' ';
    const valSpan = document.createElement('span');
    valSpan.textContent = value;
    parent.appendChild(flagSpan);
    parent.appendChild(valSpan);
}

/** 구분자(/) DOM 요소 추가 */
function _appendSep(parent, fontSize) {
    const sep = document.createElement('span');
    sep.style.cssText = 'color:var(--text-muted); margin:0 4px;' + (fontSize ? ' font-size:' + fontSize + ';' : '');
    sep.textContent = '/';
    parent.appendChild(sep);
}

/** 카드 제목에 필터 플래그 표시 */
function _updateCardFilterLabel() {
    const filter = MarketFilter.get();
    const headerEl = document.querySelector('.card.card-inner.animate-in.delay-1 .card-header');
    if (!headerEl) return;
    const flagMap = { kr: ' \u{1F1F0}\u{1F1F7}', us: ' \u{1F1FA}\u{1F1F8}', all: ' \u{1F310}' };
    const flag = flagMap[filter] || '';
    headerEl.childNodes.forEach(node => {
        if (node.nodeType === 3 && node.textContent.trim().startsWith('\uD3EC\uD2B8\uD3F4\uB9AC\uC624')) {
            node.textContent = '\n                    \uD3EC\uD2B8\uD3F4\uB9AC\uC624 \uC694\uC57D' + flag + '\n                ';
        }
    });
}

function updateRiskCard() {
    const filter = MarketFilter.get();
    const kr = cachedKRRisk;
    const us = cachedUSPortfolio;

    const canTrade = document.getElementById('r-can-trade');
    const dailyLoss = document.getElementById('r-daily-loss');
    const dailyLossLimit = document.getElementById('r-daily-loss-limit');
    const lossGauge = document.getElementById('r-loss-gauge');
    const trades = document.getElementById('r-trades');
    const maxTrades = document.getElementById('r-max-trades');
    const tradesGauge = document.getElementById('r-trades-gauge');
    const positions = document.getElementById('r-positions');
    const maxPositions = document.getElementById('r-max-positions');
    const positionsGauge = document.getElementById('r-positions-gauge');
    const consec = document.getElementById('r-consecutive');

    if (filter === 'kr' || filter === 'all') {
        if (!kr) return;

        // 거래 가능
        if (kr.can_trade) {
            canTrade.textContent = 'Yes';
            canTrade.className = 'badge badge-green';
        } else {
            canTrade.textContent = 'No';
            canTrade.className = 'badge badge-red';
        }

        // 일일 손실
        dailyLoss.textContent = formatPct(kr.daily_loss_pct);
        dailyLossLimit.textContent = '-' + kr.daily_loss_limit_pct + '%';
        const lossPct = Math.min(Math.abs(kr.daily_loss_pct) / kr.daily_loss_limit_pct * 100, 100);
        lossGauge.style.width = lossPct + '%';
        lossGauge.className = 'gauge-fill ' + (lossPct > 70 ? 'bg-red-500' : lossPct > 40 ? 'bg-yellow-500' : 'bg-green-500');

        // 거래 횟수
        trades.textContent = kr.daily_trades;
        maxTrades.textContent = kr.daily_max_trades;
        const tradesPct = Math.min(kr.daily_trades / kr.daily_max_trades * 100, 100);
        tradesGauge.style.width = tradesPct + '%';

        // 포지션 수 — "all" 모드에서 US 포지션 병기
        const usVal = (us && !us.offline && !us.error) ? us : null;
        if (filter === 'all' && usVal && usVal.positions_count != null) {
            positions.textContent = kr.position_count + ' + ' + usVal.positions_count;
        } else {
            positions.textContent = kr.position_count;
        }
        maxPositions.textContent = kr.max_positions;
        const posPct = Math.min(kr.position_count / kr.max_positions * 100, 100);
        positionsGauge.style.width = posPct + '%';

        // 연속 손실
        consec.textContent = kr.consecutive_losses;
        consec.className = 'mono' + (kr.consecutive_losses >= 3 ? ' text-loss' : '');

        _setRiskGaugesVisible(true);
    } else if (filter === 'us') {
        // US 모드 — 리스크 API 없으므로 최소 표시
        canTrade.textContent = '-';
        canTrade.className = 'badge badge-blue';

        dailyLoss.textContent = '-';
        dailyLossLimit.textContent = '-';
        lossGauge.style.width = '0%';

        const usVal = (us && !us.offline && !us.error) ? us : null;
        trades.textContent = '-';
        maxTrades.textContent = '-';
        tradesGauge.style.width = '0%';

        positions.textContent = usVal && usVal.positions_count != null ? usVal.positions_count : '-';
        maxPositions.textContent = '-';
        positionsGauge.style.width = '0%';

        consec.textContent = '-';
        consec.className = 'mono';

        _setRiskGaugesVisible(false);
    }
}

function _setRiskGaugesVisible(visible) {
    const gauges = document.querySelectorAll('.card.card-inner.animate-in.delay-2 .gauge-bg');
    gauges.forEach(function(g) { g.style.opacity = visible ? '1' : '0.3'; });
}

function applyMarketFilter(filter) {
    const usSec = document.getElementById("us-summary-section");
    const krSec = document.getElementById("kr-positions-section");
    const extSec = document.getElementById("external-accounts-section");
    if (!usSec) return;
    if (filter === "all") {
        usSec.style.display = "block";
        if (krSec) krSec.style.display = "block";
        if (extSec) extSec.style.removeProperty("display");
    } else if (filter === "us") {
        usSec.style.display = "block";
        if (krSec) krSec.style.display = "none";
        if (extSec) extSec.style.display = "none";
    } else {
        usSec.style.display = "none";
        if (krSec) krSec.style.display = "block";
        if (extSec) extSec.style.removeProperty("display");
    }
    // 포트폴리오/리스크 카드 갱신
    updatePortfolioCard();
    updateRiskCard();
}
