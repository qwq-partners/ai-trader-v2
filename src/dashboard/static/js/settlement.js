/**
 * AI Trader v2 - 일일 정산 (KIS 체결 기반)
 */

let currentDate = new Date().toISOString().slice(0, 10);

// HTML 이스케이프 (XSS 방지)
function esc(s) {
    if (s === null || s === undefined) return '';
    const d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
}

document.addEventListener('DOMContentLoaded', () => {
    const dateInput = document.getElementById('settlement-date');
    dateInput.value = currentDate;
    dateInput.addEventListener('change', (e) => {
        currentDate = e.target.value;
        loadSettlement();
    });
    loadSettlement();
});

async function loadSettlement() {
    const loading = document.getElementById('loading-indicator');
    loading.style.display = 'inline';
    try {
        const data = await api(`/api/daily-settlement?date=${encodeURIComponent(currentDate)}`);
        if (data.error) {
            showError(esc(data.error));
            return;
        }
        renderSummary(data.summary);
        renderSells(data.sells);
        renderBuys(data.buys);
        renderHoldings(data.holdings);
    } catch (e) {
        showError('데이터 조회 실패: ' + esc(e.message));
    } finally {
        loading.style.display = 'none';
    }
}

function emptyRow(cols, msg) {
    return `<tr><td colspan="${cols}" style="padding:40px 0;text-align:center;color:var(--text-muted);font-size:0.85rem;">${msg}</td></tr>`;
}

function showError(msg) {
    document.getElementById('sells-tbody').textContent = '';
    document.getElementById('buys-tbody').textContent = '';
    document.getElementById('holdings-tbody').textContent = '';
    [['sells-tbody',7],['buys-tbody',6],['holdings-tbody',5]].forEach(([id,cols]) => {
        const tbody = document.getElementById(id);
        const tr = document.createElement('tr');
        const td = document.createElement('td');
        td.colSpan = cols;
        td.style.cssText = 'padding:40px 0;text-align:center;color:var(--accent-red);font-size:0.85rem;';
        td.textContent = msg;
        tr.appendChild(td);
        tbody.appendChild(tr);
    });
}

function renderSummary(s) {
    const el = (id) => document.getElementById(id);

    const realized = el('s-realized');
    realized.textContent = formatPnl(s.realized_pnl);
    realized.className = 'stat-value ' + pnlClass(s.realized_pnl);

    const unrealized = el('s-unrealized');
    unrealized.textContent = formatPnl(s.unrealized_pnl);
    unrealized.className = 'stat-value ' + pnlClass(s.unrealized_pnl);

    const total = el('s-total-pnl');
    total.textContent = formatPnl(s.total_pnl);
    total.className = 'stat-value ' + pnlClass(s.total_pnl);

    // 승/패 (DOM 조작)
    const wl = el('s-winloss');
    wl.textContent = '';
    const winSpan = document.createElement('span');
    winSpan.className = 'text-profit';
    winSpan.textContent = s.win_count;
    const sep = document.createTextNode(' / ');
    const lossSpan = document.createElement('span');
    lossSpan.className = 'text-loss';
    lossSpan.textContent = s.loss_count;
    wl.appendChild(winSpan);
    wl.appendChild(sep);
    wl.appendChild(lossSpan);

    el('s-buysell').textContent = `${s.buy_count} / ${s.sell_count}`;
    el('s-buy-total').textContent = formatCurrency(s.total_buy_amount);
    el('s-sell-total').textContent = formatCurrency(s.total_sell_amount);
}

function buildRow(cells) {
    const tr = document.createElement('tr');
    tr.style.borderBottom = '1px solid var(--border-subtle)';
    cells.forEach(c => {
        const td = document.createElement('td');
        td.style.cssText = c.style || '';
        if (c.className) td.className = c.className;
        if (c.html) {
            // 신뢰된 데이터(숫자/포맷)만 사용
            td.innerHTML = c.html;
        } else {
            td.textContent = c.text || '';
        }
        tr.appendChild(td);
    });
    return tr;
}

function buildTotalRow(cols, label, valueHtml) {
    const tr = document.createElement('tr');
    tr.style.borderTop = '2px solid var(--border-accent)';
    const tdLabel = document.createElement('td');
    tdLabel.colSpan = cols;
    tdLabel.style.cssText = 'padding:12px 0;text-align:right;font-weight:600;color:var(--text-secondary);font-size:0.85rem;';
    tdLabel.textContent = label;
    const tdVal = document.createElement('td');
    tdVal.style.cssText = 'padding:12px 0;text-align:right;';
    tdVal.innerHTML = valueHtml;
    tr.appendChild(tdLabel);
    tr.appendChild(tdVal);
    return tr;
}

function renderSells(sells) {
    const tbody = document.getElementById('sells-tbody');
    tbody.textContent = '';

    if (!sells || sells.length === 0) {
        const tr = document.createElement('tr');
        const td = document.createElement('td');
        td.colSpan = 7;
        td.style.cssText = 'padding:40px 0;text-align:center;color:var(--text-muted);font-size:0.85rem;';
        td.textContent = '매도 체결 없음';
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
    }

    let totalPnl = 0;
    for (const s of sells) {
        const feeTax = s.fee + s.tax;
        totalPnl += s.pnl;
        const isWin = s.pnl >= 0;
        const badgeCls = isWin ? 'badge-sell-win' : 'badge-sell-loss';

        const pnlCell = s.entry_price > 0
            ? `<span class="badge ${esc(badgeCls)}">${esc(formatPct(s.pnl_pct))}</span>
               <div class="mono ${esc(pnlClass(s.pnl))}" style="font-size:0.85rem;font-weight:600;margin-top:2px;">${esc(formatPnl(s.pnl))}</div>`
            : '<span style="color:var(--text-muted);font-size:0.8rem;">진입가 불명</span>';

        tbody.appendChild(buildRow([
            { text: s.time, style: 'padding:10px 12px 10px 0;font-size:0.8rem;color:var(--text-secondary);', className: 'mono' },
            { html: `<div style="font-weight:600;font-size:0.85rem;">${esc(s.name)}</div><div style="font-size:0.7rem;color:var(--text-muted);">${esc(s.symbol)}</div>`, style: 'padding:10px 12px 10px 0;' },
            { text: formatNumber(s.quantity), style: 'padding:10px 12px 10px 0;text-align:right;', className: 'mono' },
            { text: formatNumber(s.price), style: 'padding:10px 12px 10px 0;text-align:right;', className: 'mono' },
            { text: s.entry_price > 0 ? formatNumber(s.entry_price) : '-', style: 'padding:10px 12px 10px 0;text-align:right;color:var(--text-secondary);', className: 'mono' },
            { text: formatNumber(feeTax), style: 'padding:10px 12px 10px 0;text-align:right;font-size:0.8rem;color:var(--text-muted);', className: 'mono' },
            { html: pnlCell, style: 'padding:10px 0;text-align:right;' },
        ]));
    }

    tbody.appendChild(buildTotalRow(
        6, '합계',
        `<div class="mono ${esc(pnlClass(totalPnl))}" style="font-size:1rem;font-weight:700;">${esc(formatPnl(totalPnl))}</div>`
    ));
}

function renderBuys(buys) {
    const tbody = document.getElementById('buys-tbody');
    tbody.textContent = '';

    if (!buys || buys.length === 0) {
        const tr = document.createElement('tr');
        const td = document.createElement('td');
        td.colSpan = 6;
        td.style.cssText = 'padding:40px 0;text-align:center;color:var(--text-muted);font-size:0.85rem;';
        td.textContent = '매수 체결 없음';
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
    }

    let totalAmount = 0;
    for (const b of buys) {
        totalAmount += b.amount;
        tbody.appendChild(buildRow([
            { text: b.time, style: 'padding:10px 12px 10px 0;font-size:0.8rem;color:var(--text-secondary);', className: 'mono' },
            { html: `<div style="font-weight:600;font-size:0.85rem;">${esc(b.name)}</div><div style="font-size:0.7rem;color:var(--text-muted);">${esc(b.symbol)}</div>`, style: 'padding:10px 12px 10px 0;' },
            { text: formatNumber(b.quantity), style: 'padding:10px 12px 10px 0;text-align:right;', className: 'mono' },
            { text: formatNumber(b.price), style: 'padding:10px 12px 10px 0;text-align:right;', className: 'mono' },
            { text: formatCurrency(b.amount), style: 'padding:10px 12px 10px 0;text-align:right;', className: 'mono' },
            { text: formatNumber(b.fee), style: 'padding:10px 0;text-align:right;font-size:0.8rem;color:var(--text-muted);', className: 'mono' },
        ]));
    }

    tbody.appendChild(buildTotalRow(
        4, '합계',
        `<div class="mono" style="font-size:1rem;font-weight:700;">${esc(formatCurrency(totalAmount))}</div>`
    ));
    // 빈 마지막 칸
    const lastRow = tbody.lastChild;
    const emptyTd = document.createElement('td');
    lastRow.appendChild(emptyTd);
}

function renderHoldings(holdings) {
    const tbody = document.getElementById('holdings-tbody');
    tbody.textContent = '';

    if (!holdings || holdings.length === 0) {
        const tr = document.createElement('tr');
        const td = document.createElement('td');
        td.colSpan = 5;
        td.style.cssText = 'padding:40px 0;text-align:center;color:var(--text-muted);font-size:0.85rem;';
        td.textContent = '보유 종목 없음';
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
    }

    let totalUnrealized = 0;
    for (const h of holdings) {
        totalUnrealized += h.unrealized_pnl;
        const badgeCls = h.unrealized_pnl >= 0 ? 'badge-sell-win' : 'badge-sell-loss';

        tbody.appendChild(buildRow([
            { html: `<div style="font-weight:600;font-size:0.85rem;">${esc(h.name)}</div><div style="font-size:0.7rem;color:var(--text-muted);">${esc(h.symbol)}</div>`, style: 'padding:10px 12px 10px 0;' },
            { text: formatNumber(h.quantity), style: 'padding:10px 12px 10px 0;text-align:right;', className: 'mono' },
            { text: formatNumber(h.avg_price), style: 'padding:10px 12px 10px 0;text-align:right;', className: 'mono' },
            { text: formatNumber(h.current_price), style: 'padding:10px 12px 10px 0;text-align:right;', className: 'mono' },
            { html: `<span class="badge ${esc(badgeCls)}">${esc(formatPct(h.unrealized_pct))}</span>
                     <div class="mono ${esc(pnlClass(h.unrealized_pnl))}" style="font-size:0.85rem;font-weight:600;margin-top:2px;">${esc(formatPnl(h.unrealized_pnl))}</div>`,
              style: 'padding:10px 0;text-align:right;' },
        ]));
    }

    tbody.appendChild(buildTotalRow(
        4, '합계',
        `<div class="mono ${esc(pnlClass(totalUnrealized))}" style="font-size:1rem;font-weight:700;">${esc(formatPnl(totalUnrealized))}</div>`
    ));
}
