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
        const [evoRes, histRes, codeEvoRes] = await Promise.allSettled([
            fetch('/api/evolution').then(r => r.json()),
            fetch('/api/evolution/history').then(r => r.json()),
            fetch('/api/code-evolution').then(r => r.json()),
        ]);

        const evo = evoRes.status === 'fulfilled' ? evoRes.value : {};
        const history = histRes.status === 'fulfilled' ? histRes.value : [];
        const codeEvo = codeEvoRes.status === 'fulfilled' ? codeEvoRes.value : {};

        if (evoRes.status === 'rejected') console.error('Evolution data load error:', evoRes.reason);
        if (histRes.status === 'rejected') console.error('Evolution history load error:', histRes.reason);
        if (codeEvoRes.status === 'rejected') console.error('Code evolution load error:', codeEvoRes.reason);

        renderSummary(evo.summary);
        renderInsights(evo.insights);
        renderRecommendations(evo.parameter_adjustments || []);  // AI 추천
        renderChangesTable(evo.parameter_changes);  // 적용된 변경
        renderAvoid(evo.avoid_situations);
        renderFocus(evo.focus_opportunities);
        renderOutlook(evo.next_week_outlook);
        renderHistory(history);

        // 코드 진화 데이터 렌더링
        renderErrorPatterns(codeEvo.error_patterns || []);
        renderCodeEvoSummary(codeEvo.summary || {});
        renderCodeEvoHistory(codeEvo.history || []);
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

function renderRecommendations(recs) {
    const el = document.getElementById('evo-recommendations');
    if (!recs || recs.length === 0) {
        el.innerHTML = '<div style="padding: 20px 0; text-align: center; color: var(--text-muted); font-size: 0.85rem;">추천 내용 없음</div>';
        currentRecommendations = [];
        return;
    }

    // 전역 변수에 저장 (반영 버튼에서 사용)
    currentRecommendations = recs;

    el.innerHTML = recs.map((rec, idx) => {
        const confPct = rec.confidence != null ? Math.round(rec.confidence * 100) : 0;
        return `<div class="recommendation-card">
            <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 12px;">
                <div style="flex: 1;">
                    <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 6px;">
                        <span class="badge badge-purple" style="font-size:0.65rem;">${escapeHtml(rec.strategy || '')}</span>
                        <span class="rec-param">${escapeHtml(rec.parameter || '')}</span>
                    </div>
                    <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 8px;">
                        <span class="mono" style="font-size:0.85rem;color:var(--text-muted);">${formatValue(rec.current_value)}</span>
                        <span class="arrow-to">&rarr;</span>
                        <span class="mono" style="font-size:0.9rem;color:var(--accent-green);font-weight:600;">${formatValue(rec.suggested_value)}</span>
                        <div class="conf-gauge">
                            <div class="conf-bar-bg" style="width: 60px;">
                                <div class="conf-bar-fill ${confBarColor(confPct)}" style="width:${confPct}%"></div>
                            </div>
                            <span class="mono" style="font-size:0.72rem;">${confPct}%</span>
                        </div>
                    </div>
                    <div class="rec-reason">${escapeHtml(rec.reason || '이유 없음')}</div>
                </div>
                <button class="btn-apply" onclick="applyParameterChange(${idx})" data-rec-idx="${idx}">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                    반영
                </button>
            </div>
        </div>`;
    }).join('');
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
// 파라미터 변경 반영
// ----------------------------------------------------------

let currentRecommendations = [];

async function applyParameterChange(idx) {
    if (!currentRecommendations[idx]) {
        alert('추천 데이터를 찾을 수 없습니다.');
        return;
    }

    const rec = currentRecommendations[idx];
    const confirmMsg = `파라미터 변경을 반영하시겠습니까?\n\n` +
        `전략: ${rec.strategy}\n` +
        `파라미터: ${rec.parameter}\n` +
        `${rec.current_value} → ${rec.suggested_value}\n\n` +
        `변경 후 봇이 자동 재시작됩니다.`;

    if (!confirm(confirmMsg)) {
        return;
    }

    const btn = event.target.closest('.btn-apply');
    btn.disabled = true;
    btn.textContent = '적용 중...';

    try {
        const response = await fetch('/api/evolution/apply', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                strategy: rec.strategy,
                parameter: rec.parameter,
                new_value: rec.suggested_value,
                reason: rec.reason,
            }),
        });

        const result = await response.json();

        if (response.ok && result.success) {
            alert('파라미터가 적용되었습니다.\n봇이 재시작됩니다...');
            // 3초 후 새로고침 (봇 재시작 대기)
            setTimeout(() => window.location.reload(), 3000);
        } else {
            alert('적용 실패: ' + (result.message || '알 수 없는 오류'));
            btn.disabled = false;
            btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg> 반영';
        }
    } catch (e) {
        console.error('Apply parameter error:', e);
        alert('적용 중 오류 발생: ' + e.message);
        btn.disabled = false;
        btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg> 반영';
    }
}

// ----------------------------------------------------------
// 코드 진화 렌더링 함수들
// ----------------------------------------------------------

function renderErrorPatterns(patterns) {
    const el = document.getElementById('error-patterns');
    if (!patterns || patterns.length === 0) {
        el.innerHTML = '<div style="padding: 20px 0; text-align: center; color: var(--text-muted); font-size: 0.85rem;">에러 없음 ✓</div>';
        return;
    }

    el.innerHTML = patterns.map(p => {
        const msg = escapeHtml(p.message || '').substring(0, 120);
        const count = p.count || 0;
        return `<div class="error-item">
            <span style="flex: 1;">${msg}</span>
            <span class="error-count">${count}회</span>
        </div>`;
    }).join('');
}

function renderCodeEvoSummary(summary) {
    document.getElementById('code-evo-total').textContent = summary.total || 0;
    document.getElementById('code-evo-success').textContent = summary.successful || 0;
    document.getElementById('code-evo-failed').textContent = summary.failed || 0;
    document.getElementById('code-evo-merged').textContent = summary.auto_merged || 0;
}

function renderCodeEvoHistory(history) {
    const el = document.getElementById('code-evo-history');
    if (!history || history.length === 0) {
        el.innerHTML = '<div style="padding: 20px 0; text-align: center; color: var(--text-muted); font-size: 0.85rem;">이력 없음</div>';
        return;
    }

    el.innerHTML = history.slice(0, 5).map(h => {
        const ts = h.timestamp ? formatDateTime(new Date(h.timestamp)) : '--';
        const status = h.success ? 'success' : 'failed';
        const statusText = h.success ? '성공' : '실패';
        const trigger = escapeHtml(h.trigger || '');
        const message = escapeHtml(h.message || '').substring(0, 100);
        const changedFiles = h.changed_files_count || 0;
        const prUrl = h.pr_url || '';
        const merged = h.auto_merged ? ' • 자동 머지됨' : '';

        return `<div class="code-evo-item">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                <div style="display: flex; align-items: center; gap: 8px;">
                    <span class="code-evo-status ${status}">${statusText}</span>
                    <span class="mono" style="font-size: 0.75rem; color: var(--text-muted);">${ts}</span>
                    <span class="badge badge-blue" style="font-size: 0.6rem;">${trigger}</span>
                </div>
                ${changedFiles > 0 ? `<span style="font-size: 0.72rem; color: var(--text-muted);">${changedFiles}개 파일 변경</span>` : ''}
            </div>
            <div style="font-size: 0.82rem; color: var(--text-secondary); margin-bottom: 6px;">${message}</div>
            ${prUrl ? `<div style="font-size: 0.72rem;">
                <a href="${prUrl}" target="_blank" style="color: var(--accent-blue); text-decoration: none;">
                    🔗 PR 보기
                </a>
                <span style="color: var(--text-muted);">${merged}</span>
            </div>` : ''}
        </div>`;
    }).join('');
}

// ----------------------------------------------------------
// 초기 로드
// ----------------------------------------------------------

document.addEventListener('DOMContentLoaded', () => {
    loadEvolution();
    sse.connect();
});
