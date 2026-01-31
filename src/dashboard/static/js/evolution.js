/**
 * AI Trader v2 - 진화 리포트 페이지 JS
 */

// ----------------------------------------------------------
// 데이터 로드
// ----------------------------------------------------------

async function loadEvolution() {
    const btn = document.getElementById('btn-refresh');
    btn.classList.add('loading');

    try {
        const [evoRes, histRes] = await Promise.allSettled([
            fetch('/api/evolution').then(r => r.json()),
            fetch('/api/evolution/history').then(r => r.json()),
        ]);

        const evo = evoRes.status === 'fulfilled' ? evoRes.value : {};
        const history = histRes.status === 'fulfilled' ? histRes.value : [];

        if (evoRes.status === 'rejected') console.error('Evolution data load error:', evoRes.reason);
        if (histRes.status === 'rejected') console.error('Evolution history load error:', histRes.reason);

        renderSummary(evo.summary);
        renderInsights(evo.insights);
        renderChangesTable(evo.parameter_changes);
        renderAvoid(evo.avoid_situations);
        renderFocus(evo.focus_opportunities);
        renderOutlook(evo.next_week_outlook);
        renderHistory(history);
    } catch (e) {
        console.error('Evolution load error:', e);
    } finally {
        btn.classList.remove('loading');
    }
}

// ----------------------------------------------------------
// 렌더링 함수들
// ----------------------------------------------------------

function renderSummary(s) {
    if (!s) return;
    document.getElementById('evo-version').textContent = 'v' + (s.version || 0);
    document.getElementById('evo-total').textContent = s.total_evolutions || 0;
    document.getElementById('evo-success').textContent = s.successful_changes || 0;
    document.getElementById('evo-rollback').textContent = s.rolled_back_changes || 0;

    const lastEl = document.getElementById('evo-last');
    if (s.last_evolution) {
        const d = new Date(s.last_evolution);
        lastEl.textContent = formatDateTime(d);
    } else {
        lastEl.textContent = '--';
    }

    // Assessment badge
    const assessEl = document.getElementById('evo-assessment');
    const assess = (s.assessment || 'unknown').toUpperCase();
    assessEl.textContent = assess;
    assessEl.className = 'badge ' + assessmentBadgeClass(assess);

    // Confidence
    const conf = Math.round((s.confidence || 0) * 100);
    document.getElementById('evo-confidence').textContent = conf + '%';
    document.getElementById('evo-conf-bar').style.width = conf + '%';
}

function renderInsights(insights) {
    const el = document.getElementById('evo-insights');
    if (!insights || insights.length === 0) {
        el.innerHTML = '<div style="padding: 20px 0; text-align: center; color: var(--text-muted); font-size: 0.85rem;">인사이트 없음</div>';
        return;
    }

    el.innerHTML = insights.map((text, i) =>
        `<div class="insight-item"><span class="insight-num">${i + 1}</span>${escapeHtml(text)}</div>`
    ).join('');
}

function renderChangesTable(changes) {
    const tbody = document.getElementById('evo-changes-body');
    if (!changes || changes.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" style="padding: 30px 0; text-align: center; color: var(--text-muted); font-size: 0.85rem;">파라미터 변경 없음</td></tr>';
        return;
    }

    tbody.innerHTML = changes.map(ch => {
        const confPct = ch.confidence != null ? Math.round(ch.confidence * 100) : null;
        const confHtml = confPct != null
            ? `<div class="conf-gauge"><div class="conf-bar-bg"><div class="conf-bar-fill ${confBarColor(confPct)}" style="width:${confPct}%"></div></div><span class="mono" style="font-size:0.72rem;">${confPct}%</span></div>`
            : '<span style="color:var(--text-muted);">--</span>';

        return `<tr style="border-bottom: 1px solid var(--border-subtle);">
            <td style="padding: 10px 12px 10px 0;"><span class="badge badge-purple" style="font-size:0.65rem;">${escapeHtml(ch.strategy || '')}</span></td>
            <td style="padding: 10px 12px 10px 0;" class="mono" style="font-size:0.82rem;">${escapeHtml(ch.parameter || '')}</td>
            <td style="padding: 10px 12px 10px 0; text-align: right;" class="mono">${formatValue(ch.as_is)}</td>
            <td style="padding: 10px 8px; text-align: center;" class="arrow-to">&rarr;</td>
            <td style="padding: 10px 12px 10px 0; text-align: right; color: var(--accent-cyan);" class="mono">${formatValue(ch.to_be)}</td>
            <td style="padding: 10px 12px 10px 0; font-size: 0.8rem; color: var(--text-secondary); max-width: 240px;">${escapeHtml(ch.reason || '')}</td>
            <td style="padding: 10px 12px 10px 0; text-align: center;">${confHtml}</td>
            <td style="padding: 10px 0; text-align: center;">${effectBadge(ch.is_effective)}</td>
        </tr>`;
    }).join('');
}

function renderAvoid(items) {
    const el = document.getElementById('evo-avoid');
    if (!items || items.length === 0) {
        el.innerHTML = '<div style="padding: 20px 0; text-align: center; color: var(--text-muted); font-size: 0.85rem;">회피 패턴 없음</div>';
        return;
    }

    el.innerHTML = items.map(item => {
        const desc = typeof item === 'string' ? item : (item.description || JSON.stringify(item));
        const count = item.count ? ` <span class="mono" style="color:var(--accent-amber);font-size:0.75rem;">(${item.count}회)</span>` : '';
        const typeTag = item.type ? `<span class="badge badge-yellow" style="font-size:0.6rem;margin-right:8px;">${escapeHtml(item.type)}</span>` : '';
        return `<div class="avoid-item">${typeTag}${escapeHtml(desc)}${count}</div>`;
    }).join('');
}

function renderFocus(items) {
    const el = document.getElementById('evo-focus');
    if (!items || items.length === 0) {
        el.innerHTML = '<div style="padding: 20px 0; text-align: center; color: var(--text-muted); font-size: 0.85rem;">집중 기회 없음</div>';
        return;
    }

    el.innerHTML = items.map(item => {
        const desc = typeof item === 'string' ? item : (item.description || JSON.stringify(item));
        return `<div class="focus-item">${escapeHtml(desc)}</div>`;
    }).join('');
}

function renderOutlook(text) {
    const el = document.getElementById('evo-outlook');
    el.textContent = text || '--';
}

function renderHistory(history) {
    const tbody = document.getElementById('evo-history-body');
    if (!history || history.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" style="padding: 30px 0; text-align: center; color: var(--text-muted); font-size: 0.85rem;">변경 이력 없음</td></tr>';
        return;
    }

    // 최신순 정렬
    const sorted = [...history].sort((a, b) => {
        const ta = a.timestamp || '';
        const tb = b.timestamp || '';
        return tb.localeCompare(ta);
    });

    tbody.innerHTML = sorted.map(ch => {
        const ts = ch.timestamp ? formatDateTime(new Date(ch.timestamp)) : '--';
        return `<tr style="border-bottom: 1px solid var(--border-subtle);">
            <td style="padding: 10px 12px 10px 0;" class="mono" style="font-size:0.75rem;color:var(--text-muted);">${ts}</td>
            <td style="padding: 10px 12px 10px 0;"><span class="badge badge-purple" style="font-size:0.65rem;">${escapeHtml(ch.strategy || '')}</span></td>
            <td style="padding: 10px 12px 10px 0; font-size:0.82rem;" class="mono">${escapeHtml(ch.parameter || '')}</td>
            <td style="padding: 10px 12px 10px 0; text-align: right;" class="mono">${formatValue(ch.as_is)}</td>
            <td style="padding: 10px 8px; text-align: center;" class="arrow-to">&rarr;</td>
            <td style="padding: 10px 12px 10px 0; text-align: right; color: var(--accent-cyan);" class="mono">${formatValue(ch.to_be)}</td>
            <td style="padding: 10px 12px 10px 0; font-size: 0.8rem; color: var(--text-secondary); max-width: 200px;">${escapeHtml(ch.reason || '')}</td>
            <td style="padding: 10px 0; text-align: center;">${effectBadge(ch.is_effective)}</td>
        </tr>`;
    }).join('');
}

// ----------------------------------------------------------
// 유틸리티
// ----------------------------------------------------------

function escapeHtml(str) {
    const d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
}

function formatDateTime(d) {
    if (!(d instanceof Date) || isNaN(d)) return '--';
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function formatValue(v) {
    if (v == null) return '--';
    if (typeof v === 'number') {
        return Number.isInteger(v) ? String(v) : v.toFixed(2);
    }
    return escapeHtml(String(v));
}

function assessmentBadgeClass(assess) {
    switch (assess) {
        case 'GOOD': case 'EXCELLENT': return 'badge-green';
        case 'FAIR': return 'badge-yellow';
        case 'POOR': return 'badge-red';
        default: return 'badge-blue';
    }
}

function confBarColor(pct) {
    if (pct >= 70) return 'bg-green-500';
    if (pct >= 40) return 'bg-yellow-500';
    return 'bg-red-500';
}

function effectBadge(isEffective) {
    if (isEffective === true) return '<span class="eff-badge eff-effective">효과적</span>';
    if (isEffective === false) return '<span class="eff-badge eff-ineffective">비효과적</span>';
    return '<span class="eff-badge eff-pending">평가중</span>';
}

// ----------------------------------------------------------
// 초기 로드
// ----------------------------------------------------------

document.addEventListener('DOMContentLoaded', () => {
    loadEvolution();
    sse.connect();
});
