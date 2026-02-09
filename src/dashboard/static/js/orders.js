/**
 * AI Trader v2 - 주문 내역 페이지
 */

// ============================================================
// 대기 주문 렌더링 (SSE 실시간)
// ============================================================

function renderPendingOrders(orders) {
    const section = document.getElementById('pending-section');
    const list = document.getElementById('pending-list');
    const countEl = document.getElementById('pending-count');

    if (!orders || orders.length === 0) {
        section.style.display = 'none';
        return;
    }

    section.style.display = 'block';
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
// 주문 이벤트 히스토리 렌더링
// ============================================================

function renderOrderHistory(events) {
    const tbody = document.getElementById('orders-body');
    const countEl = document.getElementById('history-count');
    const totalEl = document.getElementById('orders-total');

    if (!events || events.length === 0) {
        tbody.innerHTML = '<tr><td colspan="3" style="padding: 40px 0; text-align: center; color: var(--text-muted); font-size: 0.85rem;">주문 관련 이벤트 없음</td></tr>';
        countEl.textContent = '0건';
        totalEl.textContent = '0건';
        return;
    }

    countEl.textContent = events.length + '건';
    totalEl.textContent = events.length + '건';

    const typeColors = {
        '체결': 'badge-green',
        '주문': 'badge-blue',
        '취소': 'badge-red',
        '폴백': 'badge-yellow',
        '신호': 'badge-purple',
    };

    // 최신순 정렬
    const sorted = [...events].reverse();

    const rows = sorted.map(evt => {
        const time = evt.time ? formatTime(evt.time) : '--';
        const evtType = evt.type || '--';
        const message = evt.message || '';

        // 유형 매칭
        let badgeCls = 'badge-blue';
        for (const [key, cls] of Object.entries(typeColors)) {
            if (evtType.includes(key) || message.includes(key)) {
                badgeCls = cls;
                break;
            }
        }

        return `<tr class="border-b" style="border-color: rgba(99,102,241,0.08);">
            <td class="py-2 pr-3 mono" style="font-size: 0.78rem; color: var(--text-secondary); white-space: nowrap;">${time}</td>
            <td class="py-2 pr-3"><span class="badge ${badgeCls}">${evtType}</span></td>
            <td class="py-2" style="font-size: 0.82rem; color: var(--text-primary);">${message}</td>
        </tr>`;
    }).join('');

    tbody.innerHTML = rows;
}

// ============================================================
// SSE 핸들러
// ============================================================

sse.on('pending_orders', (data) => {
    renderPendingOrders(data);
});

// ============================================================
// 초기화
// ============================================================

document.addEventListener('DOMContentLoaded', () => {
    sse.connect();

    // 대기 주문 로드
    api('/api/orders/pending').then(data => {
        renderPendingOrders(data);
    }).catch(() => {});

    // 주문 히스토리 로드
    api('/api/orders/history').then(data => {
        renderOrderHistory(data);
    }).catch(() => {});

    // 30초마다 히스토리 갱신
    setInterval(() => {
        api('/api/orders/history').then(data => {
            renderOrderHistory(data);
        }).catch(() => {});
    }, 30000);
});
