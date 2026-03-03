/**
 * AI Trader v2 - 메인 대시보드 실시간 업데이트
 */

const logEntries = [];
const MAX_LOG_ENTRIES = 50;

// 포지션 정렬 상태
let positionSortKey = 'unrealized_pnl_pct';
let positionSortDir = 'desc';
let lastPositions = [];

// 마켓별 데이터 캐시 (필터 전환 시 즉시 반영용)
let cachedKRPortfolio = null;
let cachedUSPortfolio = null;
let cachedUSSignals = null;
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
    updateKRPositionsSummary(data);
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
    const total = cash + stock;
    const cashPct = total > 0 ? Math.round(cash / total * 100) : 0;
    const barEl = document.getElementById('p-cash-bar');
    if (barEl) barEl.style.width = cashPct + '%';
}

function updateUSPieChart(cash, stock) {
    if (cash === 0 && stock === 0) return;
    const total = cash + stock;
    const cashPct = total > 0 ? Math.round(cash / total * 100) : 100;
    const barEl = document.getElementById('us-cash-bar');
    if (barEl) barEl.style.width = cashPct + '%';
}

// ============================================================
// 포지션 요약 (마켓 카드 내)
// ============================================================

function updateKRPositionsSummary(positions) {
    const el = document.getElementById('kr-pos-summary-list');
    if (!el) return;
    if (!positions || positions.length === 0) {
        el.textContent = '포지션 없음';
        return;
    }
    el.textContent = '';
    positions.slice(0, 5).forEach(pos => {
        const div = document.createElement('div');
        div.style.cssText = 'display:flex;justify-content:space-between;padding:1px 0;';
        const name = document.createElement('span');
        name.textContent = pos.name || pos.symbol;
        name.style.cssText = 'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:60%;';
        const pct = document.createElement('span');
        const val = pos.unrealized_pnl_pct ?? 0;
        pct.textContent = (val >= 0 ? '+' : '') + val.toFixed(2) + '%';
        pct.className = 'mono';
        pct.style.color = val >= 0 ? 'var(--acc-green)' : 'var(--acc-red)';
        div.append(name, pct);
        el.appendChild(div);
    });
    if (positions.length > 5) {
        const more = document.createElement('div');
        more.style.cssText = 'font-size:.7rem;color:var(--text-muted);text-align:right;';
        more.textContent = '외 ' + (positions.length - 5) + '개';
        el.appendChild(more);
    }
}

function updateUSPositionsSummary(positions) {
    const el = document.getElementById('us-pos-summary-list');
    if (!el) return;
    if (!positions || positions.length === 0) {
        el.textContent = '포지션 없음';
        return;
    }
    el.textContent = '';
    positions.slice(0, 5).forEach(pos => {
        const div = document.createElement('div');
        div.style.cssText = 'display:flex;justify-content:space-between;padding:1px 0;';
        const name = document.createElement('span');
        name.textContent = pos.name || pos.symbol;
        name.style.cssText = 'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:60%;';
        const pct = document.createElement('span');
        const val = pos.pnl_pct ?? 0;
        pct.textContent = (val >= 0 ? '+' : '') + val.toFixed(2) + '%';
        pct.className = 'mono';
        pct.style.color = val >= 0 ? 'var(--acc-green)' : 'var(--acc-red)';
        div.append(name, pct);
        el.appendChild(div);
    });
    if (positions.length > 5) {
        const more = document.createElement('div');
        more.style.cssText = 'font-size:.7rem;color:var(--text-muted);text-align:right;';
        more.textContent = '외 ' + (positions.length - 5) + '개';
        el.appendChild(more);
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

function toggleHealthChecks() {
    const grid = document.getElementById('health-checks-grid');
    const chevron = document.getElementById('health-checks-chevron');
    const isCollapsed = grid.style.display === 'none';
    grid.style.display = isCollapsed ? 'grid' : 'none';
    if (chevron) chevron.style.transform = isCollapsed ? 'rotate(180deg)' : 'rotate(0deg)';
}

function renderHealthChecks(checks, failedOnly) {
    const card = document.getElementById('health-checks-card');
    const grid = document.getElementById('health-checks-grid');
    const badge = document.getElementById('health-checks-badge');
    const dot = document.getElementById('health-dot');
    const chevron = document.getElementById('health-checks-chevron');

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

    // critical 이상이면 자동 펼침
    if (hasCritical && grid.style.display === 'none') {
        grid.style.display = 'grid';
        if (chevron) chevron.style.transform = 'rotate(180deg)';
    }
    // 이미 펼쳐진 상태면 display:grid 유지 (콘텐츠 업데이트만)
    if (grid.style.display !== 'none') {
        grid.style.display = 'grid';
    }
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
        const isUSD = acct.currency === 'USD';
        const fmt = isUSD ? formatUSD : formatCurrency;
        const fmtPnl = isUSD ? (n => formatUSD(n, 2)) : formatPnl;

        // 계좌 요약
        const totalEquity = s.total_equity || 0;
        const stockValue = s.stock_value || 0;
        const deposit = s.deposit;
        const unrealizedPnl = s.unrealized_pnl || 0;
        const purchaseAmt = s.purchase_amount || 0;
        const pnlPct = purchaseAmt > 0 ? (unrealizedPnl / purchaseAmt * 100) : 0;

        // 포지션 테이블 행
        let posRows = '';
        if (positions.length > 0) {
            posRows = positions.map(p => {
                const cls = pnlClass(p.pnl);
                const qtyDisplay = Number.isInteger(p.qty) ? p.qty : Number(p.qty).toFixed(4);
                const fmtPrice = isUSD ? formatUSD : formatNumber;
                return `<tr style="border-bottom:1px solid var(--border-subtle);">
                    <td class="py-1 pr-3" style="font-size:0.82rem; font-weight:500; color:var(--text-primary); white-space:nowrap;">${esc(p.name || p.symbol)} <span style="color:var(--text-muted); font-size:0.68rem;">${esc(p.symbol)}</span></td>
                    <td class="py-1 pr-3 text-right mono" style="font-size:0.82rem;">${fmtPrice(p.current_price, isUSD ? 2 : 0)}</td>
                    <td class="py-1 pr-3 text-right mono" style="font-size:0.82rem; color:var(--text-secondary);">${fmtPrice(p.avg_price, isUSD ? 2 : 0)}</td>
                    <td class="py-1 pr-3 text-right mono" style="font-size:0.82rem;">${qtyDisplay}</td>
                    <td class="py-1 pr-3 text-right mono" style="font-size:0.82rem;">${fmt(p.eval_amt)}</td>
                    <td class="py-1 pr-3 text-right mono ${cls}" style="font-size:0.82rem;">${fmtPnl(p.pnl)}</td>
                    <td class="py-1 text-right mono ${cls}" style="font-size:0.82rem; font-weight:600;">${formatPct(p.pnl_pct)}</td>
                </tr>`;
            }).join('');
        } else {
            posRows = '<tr><td colspan="7" style="padding:20px 0; text-align:center; color:var(--text-muted); font-size:0.82rem;">보유 종목 없음</td></tr>';
        }

        // 예수금 표시 (해외계좌는 deposit=null일 수 있음)
        const depositDisplay = deposit !== null && deposit !== undefined ? fmt(deposit) : '--';

        return `<div class="card" style="padding: 24px; margin-bottom: 16px;">
            <div class="card-header">
                <span class="dot" style="background: var(--accent-purple); box-shadow: 0 0 8px var(--accent-purple);"></span>
                ${esc(acct.name)} 계좌
                <span style="margin-left: 8px; font-size: 0.68rem; color: var(--text-muted); font-family: 'JetBrains Mono', monospace; letter-spacing: 0;">${esc(acct.cano)}</span>
                ${isUSD ? '<span class="badge badge-blue" style="margin-left:8px;">USD</span>' : ''}
                ${s.cached ? '<span class="badge badge-yellow" style="margin-left:8px;" title="' + esc(s.cached_at || '') + '">캐시</span>' : ''}
                ${hasError ? '<span class="badge badge-red" style="margin-left:auto;">오류</span>' : `<span style="margin-left:auto; font-size:0.7rem; color:var(--text-muted); font-family:\'JetBrains Mono\',monospace; background:var(--bg-elevated); padding:3px 10px; border-radius:6px; border:1px solid var(--border-subtle);">${positions.length}종목</span>`}
            </div>

            <!-- 요약 -->
            <div style="display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:16px;">
                <div>
                    <div style="font-size:0.68rem; color:var(--text-muted); text-transform:uppercase; letter-spacing:0.08em; margin-bottom:4px;">총자산</div>
                    <div class="mono" style="font-size:0.95rem; font-weight:600; color:var(--text-primary);">${fmt(totalEquity)}</div>
                </div>
                <div>
                    <div style="font-size:0.68rem; color:var(--text-muted); text-transform:uppercase; letter-spacing:0.08em; margin-bottom:4px;">예수금</div>
                    <div class="mono" style="font-size:0.95rem; font-weight:500; color:var(--text-secondary);">${depositDisplay}</div>
                </div>
                <div>
                    <div style="font-size:0.68rem; color:var(--text-muted); text-transform:uppercase; letter-spacing:0.08em; margin-bottom:4px;">주식평가</div>
                    <div class="mono" style="font-size:0.95rem; font-weight:500; color:var(--text-secondary);">${fmt(stockValue)}</div>
                </div>
                <div>
                    <div style="font-size:0.68rem; color:var(--text-muted); text-transform:uppercase; letter-spacing:0.08em; margin-bottom:4px;">평가손익</div>
                    <div class="mono ${pnlClass(unrealizedPnl)}" style="font-size:0.95rem; font-weight:600;">${fmtPnl(unrealizedPnl)} <span style="font-size:0.72rem;">(${formatPct(pnlPct)})</span></div>
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
        const [status, portfolio, positions, signals, risk, extOvs] = await Promise.all([
            fetch("/api/us-proxy/api/us/status").then(r => r.json()).catch(() => ({ offline: true })),
            fetch("/api/us-proxy/api/us/portfolio").then(r => r.json()).catch(() => ({ offline: true })),
            fetch("/api/us-proxy/api/us/positions").then(r => r.json()).catch(() => []),
            fetch("/api/us-proxy/api/us/signals").then(r => r.ok ? r.json() : []).catch(() => []),
            fetch("/api/us-proxy/api/us/risk").then(r => r.json()).catch(() => null),
            fetch("/api/accounts/overseas").then(r => r.json()).catch(() => ({ positions: [], summary: {} })),
        ]);

        // 외부 계좌 해외 포지션을 US 포지션에 병합
        const mergedPositions = [...(positions || [])];
        if (extOvs.positions && extOvs.positions.length > 0) {
            for (const p of extOvs.positions) {
                mergedPositions.push({
                    symbol: p.symbol,
                    name: p.name,
                    strategy: 'IRP',
                    current_price: p.current_price,
                    avg_price: p.avg_price,
                    quantity: p.qty,
                    market_value: p.eval_amt,
                    pnl: p.pnl,
                    pnl_pct: p.pnl_pct,
                    entry_time: null,
                    stage: null,
                });
            }
        }

        // 외부 계좌 해외 자산을 US 포트폴리오에 병합
        // 총자산 = 주문가능금액(deposit) + 주식평가금액(stock_value)
        const mergedPortfolio = { ...portfolio };
        const ovsSummary = extOvs.summary || {};
        const ovsStockValue = ovsSummary.stock_value || 0;
        const ovsDeposit = ovsSummary.deposit || 0;
        const ovsTotal = ovsDeposit + ovsStockValue;

        if (!mergedPortfolio.offline && !mergedPortfolio.error) {
            mergedPortfolio.total_value = (mergedPortfolio.total_value || 0) + ovsTotal;
            mergedPortfolio.cash = (mergedPortfolio.cash || 0) + ovsDeposit;
        } else if (ovsTotal > 0) {
            // US 엔진 오프라인이지만 IRP 해외 데이터는 있음
            mergedPortfolio.offline = false;
            mergedPortfolio.error = false;
            mergedPortfolio.total_value = ovsTotal;
            mergedPortfolio.cash = ovsDeposit;
            mergedPortfolio.daily_pnl = 0;
        }

        renderUSStatus(status);
        renderUSPortfolio(mergedPortfolio);
        renderUSPositions(mergedPositions);
        renderUSSignals(signals);
        renderUSRisk(risk);
        cachedUSPortfolio = mergedPortfolio;
        cachedUSSignals = signals;
        updatePortfolioCard();
    } catch (e) {
        console.warn("[US] 데이터 로드 실패:", e);
    }
}

function renderUSStatus(s) {
    const dot = document.getElementById("us-status-dot");
    if (!dot) return;
    if (s.offline || s.error) {
        dot.style.background = "var(--acc-red)";
        dot.style.boxShadow = "0 0 8px var(--acc-red)";
        return;
    }
    const running = s.running;
    dot.style.background = running ? "var(--acc-green)" : "var(--text-muted)";
    dot.style.boxShadow = running ? "0 0 8px var(--acc-green)" : "none";
}

function renderUSPortfolio(p) {
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
    if (p.offline || p.error) {
        set("us-total-value", "--"); set("us-cash", "--");
        set("us-stock-value", "--"); set("us-daily-pnl", "--");
        set("us-cash-pct", "");
        return;
    }
    const total = p.total_value || 0;
    const cash  = p.cash || 0;
    const stock = Math.max(0, total - cash);
    const fmt   = v => "$" + v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

    set("us-total-value", fmt(total));
    set("us-cash",        fmt(cash));
    set("us-stock-value", fmt(stock));

    // Cash %
    const cashPct = total > 0 ? Math.round(cash / total * 100) : 100;
    set("us-cash-pct", "(" + cashPct + "%)");

    // Daily PnL
    const pnl   = p.daily_pnl || 0;
    const sign  = pnl >= 0 ? "+" : "";
    const pnlEl = document.getElementById("us-daily-pnl");
    if (pnlEl) {
        pnlEl.textContent = sign + fmt(Math.abs(pnl));
        pnlEl.style.color = pnl >= 0 ? "var(--acc-green)" : "var(--acc-red)";
    }

    // Cash bar
    updateUSPieChart(cash, stock);
}

function renderUSPositions(positions) {
    const tbody  = document.getElementById("us-positions-body");
    updateUSPositionsSummary(positions);
    if (!tbody) return;
    if (!positions || positions.length === 0) {
        // Note: innerHTML used with static trusted content only (no user input)
        tbody.innerHTML = '<tr><td colspan="10" style="padding:20px 0;text-align:center;color:var(--text-muted);font-size:0.82rem;">보유 종목 없음</td></tr>';
        return;
    }
    const fmtUsd = v => '$' + Number(v || 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    const now = new Date();
    // All dynamic values escaped via esc() — safe from XSS (data from own backend API)
    tbody.innerHTML = positions.map(p => {
        const pnl = p.pnl || 0;
        const pnlPct = p.pnl_pct || 0;
        const pCls = pnlClass(pnl);
        const stageLabel = exitStageLabel(p.stage ? { stage: p.stage } : null);
        // 보유시간
        let holdStr = '--';
        if (p.entry_time) {
            const entry = new Date(p.entry_time);
            const diffMin = Math.floor((now - entry) / 60000);
            if (diffMin >= 60) {
                holdStr = Math.floor(diffMin / 60) + 'h ' + (diffMin % 60) + 'm';
            } else {
                holdStr = diffMin + 'm';
            }
        }
        return '<tr class="border-b" style="border-color:rgba(99,102,241,0.08)">' +
            '<td class="py-2 pr-3 font-medium text-white" style="white-space:nowrap;">' + esc(p.symbol) + (p.name ? ' <span style="color:var(--text-muted);font-size:0.72rem;font-weight:400;">' + esc(p.name) + '</span>' : '') + '</td>' +
            '<td class="py-2 pr-3" style="font-size:0.75rem;color:var(--accent-purple);">' + esc(p.strategy || '--') + '</td>' +
            '<td class="py-2 pr-3 text-right mono">' + fmtUsd(p.current_price) + '</td>' +
            '<td class="col-avg-price py-2 pr-3 text-right mono text-gray-400">' + fmtUsd(p.avg_price) + '</td>' +
            '<td class="col-quantity py-2 pr-3 text-right mono">' + p.quantity + '</td>' +
            '<td class="col-market-value py-2 pr-3 text-right mono" style="color:var(--text-secondary);">' + fmtUsd(p.market_value) + '</td>' +
            '<td class="py-2 pr-3 text-right mono ' + pCls + '">' + (pnl >= 0 ? '+' : '-') + '$' + Math.abs(pnl).toFixed(2) + '</td>' +
            '<td class="py-2 pr-3 text-right mono ' + pCls + '">' + formatPct(pnlPct) + '</td>' +
            '<td class="col-holding py-2 pr-3 mono" style="font-size:0.75rem;color:var(--text-secondary);">' + holdStr + '</td>' +
            '<td class="py-2">' + stageLabel + '</td>' +
        '</tr>';
    }).join('');
}

function renderUSSignals(signals) {
    const container = document.getElementById("us-signals-section");
    if (!container) return;
    if (!signals || signals.length === 0) {
        container.textContent = "";
        return;
    }
    const items = signals.slice(0, 20);
    const rows = items.map(s => {
        const time = esc((s.timestamp || "").slice(11, 19));
        const isBuy = (s.side || "").toLowerCase() === "buy";
        const badgeCls = isBuy ? "badge-red" : "badge-blue";
        const badgeText = isBuy ? "매수" : "매도";
        const score = s.score != null ? Number(s.score).toFixed(0) : "-";
        return '<tr style="border-bottom:1px solid var(--border-subtle);">' +
            '<td style="padding:6px 10px 6px 0;" class="mono" title="' + esc(s.timestamp) + '">' + time + '</td>' +
            '<td style="padding:6px 10px 6px 0;font-weight:600;" class="mono">' + esc(s.symbol) + '</td>' +
            '<td style="padding:6px 10px 6px 0;font-size:0.78rem;color:var(--text-secondary);">' + esc(s.strategy || "-") + '</td>' +
            '<td style="padding:6px 10px 6px 0;" class="mono">' + esc(score) + '</td>' +
            '<td style="padding:6px 10px 6px 0;"><span class="badge ' + badgeCls + '">' + badgeText + '</span></td>' +
            '<td style="padding:6px 0;font-size:0.75rem;color:var(--text-muted);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="' + esc(s.reason) + '">' + esc(s.reason || "") + '</td>' +
        '</tr>';
    }).join("");

    // 모든 동적 값은 esc()로 이스케이프됨 (XSS 방지)
    container.innerHTML =
        '<div class="card" style="padding:16px 20px;">' +
            '<div style="font-size:0.82rem;color:var(--text-muted);margin-bottom:10px;font-weight:600;">🇺🇸 스크리닝 시그널 <span class="mono" style="font-size:0.7rem;color:var(--text-muted);margin-left:6px;">' + items.length + '건</span></div>' +
            '<div style="overflow-x:auto;max-height:320px;overflow-y:auto;scrollbar-width:thin;scrollbar-color:rgba(99,102,241,0.15) transparent;">' +
            '<table style="width:100%;text-align:left;border-collapse:collapse;">' +
                '<thead><tr style="border-bottom:1px solid var(--border-subtle);">' +
                    '<th style="padding:0 10px 8px 0;font-size:0.7rem;color:var(--text-muted);font-weight:500;">시간</th>' +
                    '<th style="padding:0 10px 8px 0;font-size:0.7rem;color:var(--text-muted);font-weight:500;">종목</th>' +
                    '<th style="padding:0 10px 8px 0;font-size:0.7rem;color:var(--text-muted);font-weight:500;">전략</th>' +
                    '<th style="padding:0 10px 8px 0;font-size:0.7rem;color:var(--text-muted);font-weight:500;">점수</th>' +
                    '<th style="padding:0 10px 8px 0;font-size:0.7rem;color:var(--text-muted);font-weight:500;">방향</th>' +
                    '<th style="padding:0 0 8px 0;font-size:0.7rem;color:var(--text-muted);font-weight:500;">사유</th>' +
                '</tr></thead>' +
                '<tbody>' + rows + '</tbody>' +
            '</table></div>' +
        '</div>';
}

// ============================================================
// US 리스크 렌더링
// ============================================================

function renderUSRisk(risk) {
    if (!risk) return;
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };

    // 거래 가능
    const canTrade = document.getElementById('us-can-trade');
    if (canTrade) {
        canTrade.textContent = risk.can_trade ? 'Yes' : 'No';
        canTrade.className = 'badge ' + (risk.can_trade ? 'badge-green' : 'badge-red');
    }
    // 일일 손실
    const usDailyLossEl = document.getElementById('us-daily-loss');
    if (usDailyLossEl) {
        usDailyLossEl.textContent = formatPct(risk.daily_loss_pct);
        usDailyLossEl.style.color = risk.daily_loss_pct < 0 ? 'var(--acc-red)' : '';
    }
    set('us-daily-loss-limit', '-' + risk.daily_loss_limit_pct + '%');
    // 거래 횟수
    set('us-trades', risk.daily_trades);
    // 포지션
    set('us-r-positions', risk.position_count);
    set('us-r-max-positions', risk.max_positions);
    // 연속 손실
    const consec = document.getElementById('us-consecutive');
    if (consec) {
        consec.textContent = risk.consecutive_losses;
        consec.style.color = risk.consecutive_losses >= 3 ? 'var(--acc-red)' : '';
    }
    // 신호 생성
    set('us-signals-count', risk.signals_generated);
    // WS 구독
    set('us-ws-sub', risk.ws_subscribed);
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
    // 새 레이아웃: KR 카드는 항상 KR 전용 데이터만 표시
    // US 카드는 renderUSPortfolio()에서 독립 업데이트
    const kr = cachedKRPortfolio;
    const equityEl    = document.getElementById('p-equity');
    const cashEl      = document.getElementById('p-cash');
    const cashPctEl   = document.getElementById('p-cash-pct');
    const stockEl     = document.getElementById('p-stock');
    const dailyPnlEl  = document.getElementById('p-daily-pnl');
    const breakdownEl = document.getElementById('p-pnl-breakdown');

    if (!kr) { _updateCardFilterLabel(); return; }

    if (equityEl) equityEl.textContent = formatCurrency(kr.total_equity);
    if (cashEl)   cashEl.textContent   = formatCurrency(kr.cash);

    const krCashPct = kr.cash_ratio != null ? (kr.cash_ratio * 100).toFixed(0) : '--';
    if (cashPctEl) cashPctEl.textContent = '(' + krCashPct + '%)';
    if (stockEl)  stockEl.textContent   = formatCurrency(kr.total_position_value);

    if (dailyPnlEl) {
        dailyPnlEl.textContent = '';
        const pnlText = document.createTextNode(formatPnl(kr.daily_pnl) + ' ');
        const pnlPctSpan = document.createElement('span');
        pnlPctSpan.style.cssText = 'font-size:0.72rem;color:var(--text-muted);';
        pnlPctSpan.textContent = '(' + formatPct(kr.daily_pnl_pct) + ')';
        dailyPnlEl.appendChild(pnlText);
        dailyPnlEl.appendChild(pnlPctSpan);
        dailyPnlEl.className = 'mono font-semibold ' + pnlClass(kr.daily_pnl);
    }

    _renderKRBreakdown(kr, breakdownEl);
    updatePieChart(kr.cash || 0, kr.total_position_value || 0);
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
    // 새 레이아웃: KR/US 카드가 분리되어 필터 레이블 불필요
}

function updateRiskCard() {
    // 새 레이아웃: 리스크 섹션은 항상 KR 카드 내부 — KR 데이터만 표시
    const kr = cachedKRRisk;
    if (!kr) return;

    const canTrade      = document.getElementById('r-can-trade');
    const dailyLoss     = document.getElementById('r-daily-loss');
    const dailyLossLim  = document.getElementById('r-daily-loss-limit');
    const lossGauge     = document.getElementById('r-loss-gauge');
    const trades        = document.getElementById('r-trades');
    const maxTrades     = document.getElementById('r-max-trades');
    const tradesGauge   = document.getElementById('r-trades-gauge');
    const positions     = document.getElementById('r-positions');
    const maxPositions  = document.getElementById('r-max-positions');
    const posGauge      = document.getElementById('r-positions-gauge');
    const consec        = document.getElementById('r-consecutive');

    // 거래 가능
    if (canTrade) {
        canTrade.textContent = kr.can_trade ? 'Yes' : 'No';
        canTrade.className = 'badge ' + (kr.can_trade ? 'badge-green' : 'badge-red');
    }
    // 일일 손실
    if (dailyLoss) {
        dailyLoss.textContent = formatPct(kr.daily_loss_pct);
        dailyLoss.style.color = kr.daily_loss_pct < 0 ? 'var(--acc-red)' : '';
    }
    if (dailyLossLim) dailyLossLim.textContent = '-' + kr.daily_loss_limit_pct + '%';
    if (lossGauge) {
        const lossPct = Math.min(Math.abs(kr.daily_loss_pct) / kr.daily_loss_limit_pct * 100, 100);
        lossGauge.style.width = lossPct + '%';
        lossGauge.className = 'gauge-fill ' + (lossPct > 70 ? 'bg-red-500' : lossPct > 40 ? 'bg-yellow-500' : 'bg-green-500');
    }
    // 거래 횟수
    if (trades) trades.textContent = kr.daily_trades;
    if (maxTrades) maxTrades.textContent = kr.daily_max_trades;
    if (tradesGauge) tradesGauge.style.width = Math.min(kr.daily_trades / kr.daily_max_trades * 100, 100) + '%';
    // 포지션
    if (positions) positions.textContent = kr.position_count;
    if (maxPositions) maxPositions.textContent = kr.max_positions;
    if (posGauge) posGauge.style.width = Math.min(kr.position_count / kr.max_positions * 100, 100) + '%';
    // 연속 손실
    if (consec) {
        consec.textContent = kr.consecutive_losses;
        consec.style.color = kr.consecutive_losses >= 3 ? 'var(--acc-red)' : '';
    }
}

function _setRiskGaugesVisible(visible) {
    const gauges = document.querySelectorAll('.card.card-inner.animate-in.delay-2 .gauge-bg');
    gauges.forEach(function(g) { g.style.opacity = visible ? '1' : '0.3'; });
}

function applyMarketFilter(filter) {
    const usCard   = document.getElementById("us-market-card");
    const krCard   = document.getElementById("kr-market-card");
    const krSec    = document.getElementById("kr-positions-section");
    const usPosF   = document.getElementById("us-positions-full");
    const extSec   = document.getElementById("external-accounts-section");
    const usSec    = document.getElementById("us-summary-section");
    if (usSec) usSec.style.display = "none"; // 구 컨테이너 항상 숨김

    const showUS = filter === "all" || filter === "us";
    const showKR = filter === "all" || filter === "kr";

    if (usCard)  usCard.style.display  = showUS ? "" : "none";
    if (krCard)  krCard.style.display  = showKR ? "" : "none";
    if (krSec)   krSec.style.display   = showKR ? "" : "none";
    // us-positions-full: US 마켓 표시 여부에 따라 제어 (KR positions-section과 동일 방식)
    if (usPosF) usPosF.style.display = showUS ? "" : "none";
    const usSignalsSec = document.getElementById("us-signals-section");
    if (usSignalsSec) usSignalsSec.style.display = showUS ? "" : "none";
    if (extSec) {
        if (showKR) extSec.style.removeProperty("display");
        else extSec.style.display = "none";
    }

    // markets-grid: 단일 필터 시 1열로
    const grid = document.querySelector(".markets-grid");
    if (grid) grid.style.gridTemplateColumns = (showUS && showKR) ? "1fr 1fr" : "1fr";

    updatePortfolioCard();
    updateRiskCard();
}
