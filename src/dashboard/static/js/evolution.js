/**
 * AI Trader v2 - 거래 리뷰 페이지 JS
 * 일일 거래 복기 + LLM 종합 평가 + 변경 이력
 */

// ----------------------------------------------------------
// 상태 관리
// ----------------------------------------------------------
let availableDates = [];
let currentDateIdx = -1;
let currentRecommendations = [];

// ----------------------------------------------------------
// 유틸리티
// ----------------------------------------------------------
function escapeHtml(str) {
    if (!str) return '';
    const d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
}

function formatValue(v) {
    if (v == null) return '--';
    if (typeof v === 'number') {
        return Number.isInteger(v) ? String(v) : v.toFixed(2);
    }
    return escapeHtml(String(v));
}

function assessmentBadge(assess) {
    if (!assess) return '<span class="badge badge-blue">--</span>';
    const upper = assess.toUpperCase();
    switch (upper) {
        case 'GOOD': case 'EXCELLENT':
            return '<span class="badge badge-green">' + upper + '</span>';
        case 'FAIR':
            return '<span class="badge badge-yellow">' + upper + '</span>';
        case 'POOR':
            return '<span class="badge badge-red">' + upper + '</span>';
        case 'NO_DATA':
            return '<span class="badge badge-blue">N/A</span>';
        default:
            return '<span class="badge badge-blue">' + escapeHtml(upper) + '</span>';
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

function exitTypeBadge(exitType) {
    if (!exitType) return '';
    const map = {
        'stop_loss': ['badge-red', '손절'],
        'first_take_profit': ['badge-green', '1차익절'],
        'trailing_stop': ['badge-yellow', '트레일링'],
        'breakeven_stop': ['badge-blue', '본전'],
        'manual': ['badge-purple', '수동'],
        'eod_close': ['badge-cyan', '장마감'],
    };
    const [cls, label] = map[exitType] || ['badge-blue', escapeHtml(exitType)];
    return '<span class="badge badge-exit ' + cls + '">' + label + '</span>';
}

function formatDateTime(d) {
    if (!(d instanceof Date) || isNaN(d)) return '--';
    const pad = n => String(n).padStart(2, '0');
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
}

// ----------------------------------------------------------
// 날짜 네비게이션
// ----------------------------------------------------------
async function loadAvailableDates() {
    try {
        const data = await api('/api/daily-review/dates');
        availableDates = data.dates || [];
    } catch (e) {
        console.error('[리뷰] 날짜 목록 로드 실패:', e);
        availableDates = [];
    }
}

function navigateDate(direction) {
    const newIdx = currentDateIdx + direction;
    if (newIdx < 0 || newIdx >= availableDates.length) return;
    currentDateIdx = newIdx;
    updateDateNav();
    loadDailyReview(availableDates[currentDateIdx]);
}

function updateDateNav() {
    const label = document.getElementById('date-label');
    const prevBtn = document.getElementById('btn-prev');
    const nextBtn = document.getElementById('btn-next');

    if (currentDateIdx >= 0 && currentDateIdx < availableDates.length) {
        label.textContent = availableDates[currentDateIdx];
    } else {
        label.textContent = '--';
    }

    prevBtn.disabled = currentDateIdx <= 0;
    nextBtn.disabled = currentDateIdx >= availableDates.length - 1;
}

function loadCurrentDate() {
    if (availableDates.length > 0) {
        currentDateIdx = availableDates.length - 1;
        updateDateNav();
        loadDailyReview(availableDates[currentDateIdx]);
    }
    loadChangeHistory();
}

// ----------------------------------------------------------
// 데이터 로드
// ----------------------------------------------------------
async function loadDailyReview(dateStr) {
    const btn = document.getElementById('btn-refresh');
    btn.classList.add('loading');

    try {
        const data = await api('/api/daily-review?date=' + dateStr);
        const report = data.trade_report;
        const llmReview = data.llm_review;

        renderSummaryCards(report ? report.summary : null, llmReview);
        renderTradeCards(report ? report.trades : [], llmReview ? llmReview.trade_reviews : []);
        renderStrategyPerformance(report ? report.strategy_performance : null);
        renderLLMReview(llmReview, !!report);

    } catch (e) {
        console.error('[리뷰] 로드 실패:', e);
    } finally {
        btn.classList.remove('loading');
    }
}

async function loadChangeHistory() {
    try {
        const history = await api('/api/evolution/history');
        renderChangeHistory(history);
    } catch (e) {
        console.error('[리뷰] 변경 이력 로드 실패:', e);
    }
}

// ----------------------------------------------------------
// 요약 카드 렌더링
// ----------------------------------------------------------
function renderSummaryCards(summary, llmReview) {
    const winrateEl = document.getElementById('sum-winrate');
    const pfEl = document.getElementById('sum-pf');
    const pnlEl = document.getElementById('sum-pnl');
    const assessEl = document.getElementById('sum-assessment');

    if (!summary || summary.total_trades === 0) {
        winrateEl.textContent = '--';
        winrateEl.style.color = 'var(--text-muted)';
        pfEl.textContent = '--';
        pfEl.style.color = 'var(--text-muted)';
        pnlEl.textContent = '--';
        pnlEl.style.color = 'var(--text-muted)';
        assessEl.innerHTML = '<span class="badge badge-blue" style="font-size:0.85rem;">거래 없음</span>';
        return;
    }

    // 승률
    const wr = summary.win_rate;
    winrateEl.textContent = wr.toFixed(1) + '%';
    winrateEl.style.color = wr >= 50 ? 'var(--accent-green)' : wr >= 40 ? 'var(--accent-amber)' : 'var(--accent-red)';

    // 손익비
    const pf = summary.profit_factor;
    pfEl.textContent = pf.toFixed(2);
    pfEl.style.color = pf >= 1.5 ? 'var(--accent-green)' : pf >= 1.0 ? 'var(--accent-amber)' : 'var(--accent-red)';

    // 총손익
    const totalPnl = summary.total_pnl;
    pnlEl.textContent = (totalPnl >= 0 ? '+' : '') + Number(totalPnl).toLocaleString('ko-KR') + '원';
    pnlEl.style.color = totalPnl >= 0 ? 'var(--accent-green)' : 'var(--accent-red)';

    // 평가
    if (llmReview && llmReview.assessment) {
        assessEl.innerHTML = assessmentBadge(llmReview.assessment);
        assessEl.querySelector('.badge').style.fontSize = '0.85rem';
    } else {
        assessEl.innerHTML = '<span class="badge badge-blue" style="font-size:0.85rem;">평가 대기</span>';
    }
}

// ----------------------------------------------------------
// 거래별 상세 복기 카드
// ----------------------------------------------------------
function renderTradeCards(trades, tradeReviews) {
    const container = document.getElementById('trade-cards-container');

    if (!trades || trades.length === 0) {
        container.innerHTML = '<div class="placeholder-text">거래 데이터 없음</div>';
        return;
    }

    // LLM trade_reviews를 symbol 기준으로 매핑
    const reviewMap = {};
    if (tradeReviews && tradeReviews.length > 0) {
        tradeReviews.forEach(r => {
            if (r.symbol) reviewMap[r.symbol] = r;
        });
    }

    const cards = trades.map((t, idx) => {
        const isWin = t.pnl >= 0;
        const cardClass = isWin ? 'win' : 'loss';
        const pnlColor = isWin ? 'var(--accent-green)' : 'var(--accent-red)';
        const review = reviewMap[t.symbol];

        // 진입/청산 시간 포맷
        let entryTime = '--';
        let exitTime = '--';
        if (t.entry_time) {
            try {
                const d = new Date(t.entry_time);
                entryTime = d.toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' });
            } catch(e) { entryTime = t.entry_time; }
        }
        if (t.exit_time) {
            try {
                const d = new Date(t.exit_time);
                exitTime = d.toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' });
            } catch(e) { exitTime = t.exit_time; }
        }

        // 지표 태그
        let indicatorTags = '';
        const indicators = t.indicators_at_entry || {};
        const indKeys = Object.keys(indicators).slice(0, 6);
        if (indKeys.length > 0) {
            indicatorTags = '<div class="trade-indicators">' +
                indKeys.map(k => {
                    let v = indicators[k];
                    if (typeof v === 'number') v = Number.isInteger(v) ? v : v.toFixed(2);
                    return '<span class="trade-indicator"><span class="ind-label">' + escapeHtml(k) + '</span><span class="ind-value">' + escapeHtml(String(v)) + '</span></span>';
                }).join('') +
                '</div>';
        }

        // LLM 리뷰 블록
        let reviewBlock = '';
        if (review) {
            if (review.review) {
                reviewBlock += '<div class="trade-llm-block"><strong style="color:var(--accent-blue);font-size:0.72rem;text-transform:uppercase;letter-spacing:0.06em;">복기</strong><br>' + escapeHtml(review.review) + '</div>';
            }
            if (review.lesson) {
                reviewBlock += '<div class="trade-llm-block"><strong style="color:var(--accent-amber);font-size:0.72rem;text-transform:uppercase;letter-spacing:0.06em;">교훈</strong><br>' + escapeHtml(review.lesson) + '</div>';
            }
        }

        const delay = Math.min(idx * 0.06, 0.5);

        return '<div class="trade-card ' + cardClass + ' trade-card-animate" style="animation-delay:' + delay + 's;">' +
            '<div class="trade-card-header">' +
                '<div class="trade-card-header-left">' +
                    '<span class="trade-name">' + escapeHtml(t.name || t.symbol) + '</span>' +
                    '<span class="trade-symbol">' + escapeHtml(t.symbol) + '</span>' +
                    '<span class="badge badge-purple" style="font-size:0.65rem;">' + escapeHtml(t.strategy || '') + '</span>' +
                    exitTypeBadge(t.exit_type) +
                '</div>' +
                '<span class="trade-pnl" style="color:' + pnlColor + ';">' + (t.pnl >= 0 ? '+' : '') + Number(t.pnl).toLocaleString('ko-KR') + '원 (' + (t.pnl_pct >= 0 ? '+' : '') + t.pnl_pct.toFixed(2) + '%)</span>' +
            '</div>' +
            '<div class="trade-detail-row">' +
                '<span class="label">진입</span>' +
                '<span class="mono" style="font-size:0.82rem;">' + entryTime + '</span>' +
                '<span class="mono" style="color:var(--text-secondary);">@ ' + Number(t.entry_price).toLocaleString('ko-KR') + '원</span>' +
                '<span style="color:var(--text-muted);margin:0 4px;">|</span>' +
                '<span class="label">청산</span>' +
                '<span class="mono" style="font-size:0.82rem;">' + exitTime + '</span>' +
                '<span class="mono" style="color:var(--text-secondary);">@ ' + Number(t.exit_price).toLocaleString('ko-KR') + '원</span>' +
                '<span style="color:var(--text-muted);margin:0 4px;">|</span>' +
                '<span class="label">보유</span>' +
                '<span class="mono" style="font-size:0.82rem;">' + (t.holding_minutes || 0) + '분</span>' +
                '<span style="color:var(--text-muted);margin:0 4px;">|</span>' +
                '<span class="label">수량</span>' +
                '<span class="mono" style="font-size:0.82rem;">' + (t.quantity || 0) + '주</span>' +
            '</div>' +
            indicatorTags +
            reviewBlock +
        '</div>';
    }).join('');

    container.innerHTML = cards;
}

// ----------------------------------------------------------
// 전략별 성과 비교
// ----------------------------------------------------------
function renderStrategyPerformance(stratPerf) {
    const tbody = document.getElementById('strat-perf-body');

    if (!stratPerf || Object.keys(stratPerf).length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="placeholder-text">데이터 없음</td></tr>';
        return;
    }

    const rows = Object.entries(stratPerf).map(([strategy, perf]) => {
        const pnlColor = perf.total_pnl >= 0 ? 'var(--accent-green)' : 'var(--accent-red)';
        const wrColor = perf.win_rate >= 50 ? 'var(--accent-green)' : perf.win_rate >= 40 ? 'var(--accent-amber)' : 'var(--accent-red)';

        return '<tr style="border-bottom: 1px solid var(--border-subtle);">' +
            '<td style="padding:10px 12px 10px 0;"><span class="badge badge-purple" style="font-size:0.65rem;">' + escapeHtml(strategy) + '</span></td>' +
            '<td style="padding:10px 12px 10px 0;text-align:right;" class="mono">' + perf.trades + '</td>' +
            '<td style="padding:10px 12px 10px 0;text-align:right;color:var(--accent-green);" class="mono">' + perf.wins + '</td>' +
            '<td style="padding:10px 12px 10px 0;text-align:right;color:var(--accent-red);" class="mono">' + perf.losses + '</td>' +
            '<td style="padding:10px 12px 10px 0;text-align:right;color:' + wrColor + ';" class="mono">' + perf.win_rate.toFixed(1) + '%</td>' +
            '<td style="padding:10px 12px 10px 0;text-align:right;color:' + pnlColor + ';" class="mono">' + (perf.total_pnl >= 0 ? '+' : '') + Number(perf.total_pnl).toLocaleString('ko-KR') + '원</td>' +
            '<td style="padding:10px 0;text-align:right;color:' + pnlColor + ';" class="mono">' + (perf.avg_pnl_pct >= 0 ? '+' : '') + perf.avg_pnl_pct.toFixed(2) + '%</td>' +
        '</tr>';
    }).join('');

    tbody.innerHTML = rows;
}

// ----------------------------------------------------------
// LLM 종합 평가 렌더링
// ----------------------------------------------------------
function renderLLMReview(llmReview, hasTradeReport) {
    const reviewSection = document.getElementById('llm-review-section');
    const waitingSection = document.getElementById('llm-waiting-section');

    if (!llmReview) {
        reviewSection.style.display = 'none';
        waitingSection.style.display = hasTradeReport ? 'block' : 'none';
        return;
    }

    reviewSection.style.display = 'block';
    waitingSection.style.display = 'none';

    // 인사이트
    const insightsEl = document.getElementById('llm-insights');
    const insights = llmReview.insights || [];
    if (insights.length > 0) {
        insightsEl.innerHTML = insights.map((text, i) =>
            '<div class="insight-item"><span class="insight-num">' + (i + 1) + '</span>' + escapeHtml(text) + '</div>'
        ).join('');
    } else {
        insightsEl.innerHTML = '<div style="color:var(--text-muted);font-size:0.82rem;">인사이트 없음</div>';
    }

    // 회피 패턴
    const avoidEl = document.getElementById('llm-avoid');
    const avoidPatterns = llmReview.avoid_patterns || [];
    if (avoidPatterns.length > 0) {
        avoidEl.innerHTML = avoidPatterns.map(item => {
            const text = typeof item === 'string' ? item : (item.description || JSON.stringify(item));
            return '<div class="avoid-item">' + escapeHtml(text) + '</div>';
        }).join('');
    } else {
        avoidEl.innerHTML = '<div style="color:var(--text-muted);font-size:0.82rem;">없음</div>';
    }

    // 집중 기회
    const focusEl = document.getElementById('llm-focus');
    const focusOps = llmReview.focus_opportunities || [];
    if (focusOps.length > 0) {
        focusEl.innerHTML = focusOps.map(item => {
            const text = typeof item === 'string' ? item : (item.description || JSON.stringify(item));
            return '<div class="focus-item">' + escapeHtml(text) + '</div>';
        }).join('');
    } else {
        focusEl.innerHTML = '<div style="color:var(--text-muted);font-size:0.82rem;">없음</div>';
    }

    // 파라미터 추천
    const paramsSection = document.getElementById('llm-params-section');
    const recsEl = document.getElementById('llm-recommendations');
    const suggestions = llmReview.parameter_suggestions || [];
    currentRecommendations = suggestions;

    if (suggestions.length > 0) {
        paramsSection.style.display = 'block';
        recsEl.innerHTML = suggestions.map((rec, idx) => {
            const confPct = rec.confidence != null ? Math.round(rec.confidence * 100) : 0;
            return '<div class="recommendation-card">' +
                '<div style="display:flex;justify-content:space-between;align-items:flex-start;">' +
                    '<div style="flex:1;">' +
                        '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">' +
                            '<span class="badge badge-purple" style="font-size:0.65rem;">' + escapeHtml(rec.strategy || '') + '</span>' +
                            '<span class="rec-param">' + escapeHtml(rec.parameter || '') + '</span>' +
                        '</div>' +
                        '<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">' +
                            '<span class="mono" style="font-size:0.85rem;color:var(--text-muted);">' + formatValue(rec.current_value) + '</span>' +
                            '<span class="arrow-to">&rarr;</span>' +
                            '<span class="mono" style="font-size:0.9rem;color:var(--accent-green);font-weight:600;">' + formatValue(rec.suggested_value) + '</span>' +
                            '<div class="conf-gauge">' +
                                '<div class="conf-bar-bg" style="width:60px;"><div class="conf-bar-fill ' + confBarColor(confPct) + '" style="width:' + confPct + '%;"></div></div>' +
                                '<span class="mono" style="font-size:0.72rem;">' + confPct + '%</span>' +
                            '</div>' +
                        '</div>' +
                        '<div class="rec-reason">' + escapeHtml(rec.reason || '') + '</div>' +
                    '</div>' +
                    '<button class="btn-apply" onclick="applyParameterChange(event,' + idx + ')" data-rec-idx="' + idx + '">' +
                        '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>' +
                        ' 반영' +
                    '</button>' +
                '</div>' +
            '</div>';
        }).join('');
    } else {
        paramsSection.style.display = 'none';
    }
}

// ----------------------------------------------------------
// 변경 이력
// ----------------------------------------------------------
function renderChangeHistory(history) {
    const tbody = document.getElementById('evo-history-body');

    if (!history || history.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" class="placeholder-text">변경 이력 없음</td></tr>';
        return;
    }

    const sorted = [...history].sort((a, b) => {
        const ta = a.timestamp || '';
        const tb = b.timestamp || '';
        return tb.localeCompare(ta);
    });

    tbody.innerHTML = sorted.map(ch => {
        const ts = ch.timestamp ? formatDateTime(new Date(ch.timestamp)) : '--';
        return '<tr style="border-bottom:1px solid var(--border-subtle);">' +
            '<td style="padding:10px 12px 10px 0;font-size:0.75rem;color:var(--text-muted);" class="mono">' + ts + '</td>' +
            '<td style="padding:10px 12px 10px 0;"><span class="badge badge-purple" style="font-size:0.65rem;">' + escapeHtml(ch.strategy || '') + '</span></td>' +
            '<td style="padding:10px 12px 10px 0;font-size:0.82rem;" class="mono">' + escapeHtml(ch.parameter || '') + '</td>' +
            '<td style="padding:10px 12px 10px 0;text-align:right;" class="mono">' + formatValue(ch.as_is) + '</td>' +
            '<td style="padding:10px 8px;text-align:center;" class="arrow-to">&rarr;</td>' +
            '<td style="padding:10px 12px 10px 0;text-align:right;color:var(--accent-cyan);" class="mono">' + formatValue(ch.to_be) + '</td>' +
            '<td style="padding:10px 12px 10px 0;font-size:0.8rem;color:var(--text-secondary);max-width:200px;">' + escapeHtml(ch.reason || '') + '</td>' +
            '<td style="padding:10px 0;text-align:center;">' + effectBadge(ch.is_effective) + '</td>' +
        '</tr>';
    }).join('');
}

// ----------------------------------------------------------
// 파라미터 변경 반영
// ----------------------------------------------------------
async function applyParameterChange(event, idx) {
    if (!currentRecommendations[idx]) return;

    const rec = currentRecommendations[idx];
    const confirmMsg = '파라미터 변경을 반영하시겠습니까?\n\n' +
        '전략: ' + rec.strategy + '\n' +
        '파라미터: ' + rec.parameter + '\n' +
        rec.current_value + ' \u2192 ' + rec.suggested_value + '\n\n' +
        '변경 후 봇이 자동 재시작됩니다.';

    if (!confirm(confirmMsg)) return;

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
            btn.textContent = '적용됨';
            btn.style.background = 'var(--accent-green)';
            setTimeout(() => window.location.reload(), 3000);
        } else {
            btn.disabled = false;
            btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg> 반영';
        }
    } catch (e) {
        console.error('[리뷰] 파라미터 반영 오류:', e);
        btn.disabled = false;
        btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg> 반영';
    }
}

// ----------------------------------------------------------
// 초기화
// ----------------------------------------------------------
document.addEventListener('DOMContentLoaded', async () => {
    sse.connect();

    // 날짜 목록 로드
    await loadAvailableDates();

    if (availableDates.length > 0) {
        // 최신 날짜로 시작
        currentDateIdx = availableDates.length - 1;
        updateDateNav();
        loadDailyReview(availableDates[currentDateIdx]);
    } else {
        document.getElementById('date-label').textContent = '데이터 없음';
    }

    // 변경 이력 로드
    loadChangeHistory();
});
