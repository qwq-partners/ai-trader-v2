/**
 * AI Trader v2 - 테마/스크리닝 페이지 v4
 * - MarketFilter (통합/KR/US) 지원
 * - 스크리닝 결과 상위 20개 + 스크롤
 * - 뉴스 원문 링크 지원
 */

// ── 마켓 필터 ──────────────────────────────────────────────────────────────
function applyThemesMarketFilter(filter) {
    const krSec = document.getElementById("kr-themes-section");
    const usSec = document.getElementById("us-themes-section");
    if (!krSec || !usSec) return;
    if (filter === "all") {
        krSec.style.display = "";
        usSec.style.display = "";
    } else if (filter === "us") {
        krSec.style.display = "none";
        usSec.style.display = "";
    } else {
        krSec.style.display = "";
        usSec.style.display = "none";
    }
}

// ── KR 테마 ────────────────────────────────────────────────────────────────
async function loadThemes() {
    try {
        const themes = await api('/api/themes');
        renderThemes(themes);
    } catch (e) {
        console.error('테마 로드 오류:', e);
    }
}

async function loadScreening() {
    try {
        const results = await api('/api/screening');
        renderScreening(results);
    } catch (e) {
        console.error('스크리닝 로드 오류:', e);
    }
}

function renderThemes(themes) {
    const grid = document.getElementById('themes-grid');
    if (!themes || themes.length === 0) {
        grid.innerHTML = '<div class="card p-6 text-center text-gray-500 col-span-full">감지된 테마 없음</div>';
        return;
    }

    const cards = themes.map(theme => {
        const scoreColor = theme.score >= 80 ? '#34d399' : theme.score >= 60 ? '#fbbf24' : '#6366f1';
        const scorePct = Math.min(theme.score, 100);

        const keywords = (theme.keywords || []).slice(0, 5).map(k =>
            `<span class="badge badge-purple">${esc(k)}</span>`
        ).join(' ');

        const stocks = (theme.related_stocks || []).slice(0, 6).map(s =>
            `<span class="text-xs mono text-gray-400">${esc(s)}</span>`
        ).join(', ');

        const timeStr = theme.detected_at ? formatTime(theme.detected_at) : '--';

        // 뉴스: news_items(URL포함) 우선, 폴백 news_titles
        const newsItems = theme.news_items && theme.news_items.length > 0
            ? theme.news_items
            : (theme.news_titles || []).map(t => ({ title: t, url: "" }));

        const newsHtml = newsItems.length > 0
            ? `<div class="mt-2 pt-2 border-t" style="border-color:rgba(99,102,241,0.08)">
                <div class="text-xs text-gray-500 mb-1">관련 뉴스 ${newsItems.length}건</div>
                ${newsItems.slice(0, 5).map(item => {
                    const title = esc(item.title || item);
                    const url = item.url || "";
                    return url
                        ? `<div class="text-xs mb-0.5" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
                             <a href="${esc(url)}" target="_blank" rel="noopener"
                                style="color:var(--accent-blue);text-decoration:none;"
                                title="${title}">• ${title}</a>
                           </div>`
                        : `<div class="text-xs text-gray-400 mb-0.5" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;"
                               title="${title}">• ${title}</div>`;
                }).join('')}
               </div>`
            : `<div class="mt-2 pt-2 border-t" style="border-color:rgba(99,102,241,0.08)">
                  <div class="text-xs text-gray-500">뉴스 0건</div>
               </div>`;

        return `<div class="card p-4 theme-card">
            <div class="flex items-start justify-between mb-2">
                <h3 class="font-semibold text-white">${esc(theme.name)}</h3>
                <span class="mono text-lg font-bold" style="color:${scoreColor}">${theme.score.toFixed(0)}</span>
            </div>
            <div class="score-bar mb-3">
                <div class="score-fill" style="width:${scorePct}%; background:${scoreColor}"></div>
            </div>
            <div class="flex flex-wrap gap-1 mb-2">${keywords}</div>
            <div class="text-xs text-gray-500 mb-1">관련 종목</div>
            <div class="mb-2">${stocks || '<span class="text-xs text-gray-500">없음</span>'}</div>
            ${newsHtml}
            <div class="flex justify-end text-xs text-gray-500 mt-1">
                <span>${timeStr}</span>
            </div>
        </div>`;
    }).join('');

    grid.innerHTML = cards;
}

function renderScreening(results) {
    const tbody = document.getElementById('screening-body');
    const countEl = document.getElementById('screening-count');

    if (!results || results.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" class="py-8 text-center text-gray-500">스크리닝 결과 없음</td></tr>';
        if (countEl) countEl.textContent = '';
        return;
    }

    // 상위 20개만 표시
    const TOP = 20;
    if (countEl) countEl.textContent = `상위 ${Math.min(results.length, TOP)} / 전체 ${results.length}`;

    const rows = results.slice(0, TOP).map(s => {
        const changeCls = s.change_pct > 0 ? 'text-profit' : s.change_pct < 0 ? 'text-loss' : '';
        const scoreBadge = s.score >= 80 ? 'badge-green' : s.score >= 60 ? 'badge-yellow' : 'badge-blue';
        const reasons = (s.reasons || []).slice(0, 3).map(r => esc(r)).join(', ');

        return `<tr class="border-b hover:bg-dark-700/30" style="border-color:#31324420">
            <td class="py-2 pr-3 font-medium text-white">${esc(s.name || s.symbol)}</td>
            <td class="py-2 pr-3 text-right mono">${formatNumber(s.price)}</td>
            <td class="py-2 pr-3 text-right mono ${changeCls}">${formatPct(s.change_pct)}</td>
            <td class="py-2 pr-3 text-right mono col-hide-mobile">${s.volume_ratio ? s.volume_ratio.toFixed(1) + 'x' : '--'}</td>
            <td class="py-2 pr-3 text-right"><span class="badge ${scoreBadge}">${s.score.toFixed(0)}</span></td>
            <td class="py-2 text-xs text-gray-400 col-hide-mobile" style="max-width:200px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${reasons || '--'}</td>
        </tr>`;
    }).join('');

    tbody.innerHTML = rows;
}

// ── US 테마 ────────────────────────────────────────────────────────────────
async function loadUSThemes() {
    try {
        const themes = await api('/api/us-proxy/api/us/themes');
        renderUSThemes(themes);
    } catch (e) {
        console.warn('US 테마 로드 실패 (봇 오프라인?):', e);
        const grid = document.getElementById('us-themes-grid');
        if (grid) {
            grid.textContent = '';
            const msg = document.createElement('div');
            msg.className = 'theme-card';
            msg.style.cssText = 'text-align:center;color:var(--text-muted);padding:40px 20px;grid-column:1/-1;';
            msg.textContent = 'US 봇 오프라인 — 테마 데이터 없음';
            grid.appendChild(msg);
        }
    }
}

function renderUSThemes(themes) {
    const grid = document.getElementById('us-themes-grid');
    if (!grid) return;

    if (!themes || themes.length === 0) {
        grid.textContent = '';
        const msg = document.createElement('div');
        msg.className = 'theme-card';
        msg.style.cssText = 'text-align:center;color:var(--text-muted);padding:40px 20px;grid-column:1/-1;';
        msg.textContent = '감지된 US 테마 없음';
        grid.appendChild(msg);
        return;
    }

    const cards = themes.map(theme => {
        const scoreColor = theme.score >= 80 ? '#34d399' : theme.score >= 60 ? '#fbbf24' : '#a78bfa';
        const scorePct = Math.min(theme.score, 100);

        const keywords = (theme.keywords || []).slice(0, 5).map(k =>
            `<span class="badge badge-purple">${esc(k)}</span>`
        ).join(' ');

        const stocks = (theme.related_stocks || []).slice(0, 8).map(s =>
            `<span class="text-xs mono text-gray-400">${esc(s)}</span>`
        ).join(', ');

        const timeStr = theme.detected_at ? formatTime(theme.detected_at) : '--';

        // US는 news_headlines / news_items 모두 지원
        const rawNews = theme.news_items && theme.news_items.length > 0
            ? theme.news_items
            : (theme.news_headlines || []).map(t => ({ title: t, url: "" }));

        const newsHtml = rawNews.length > 0
            ? `<div class="mt-2 pt-2 border-t" style="border-color:rgba(99,102,241,0.08)">
                <div class="text-xs text-gray-500 mb-1">관련 뉴스 ${rawNews.length}건</div>
                ${rawNews.slice(0, 5).map(item => {
                    const title = esc(typeof item === 'string' ? item : (item.title || ''));
                    const url = (typeof item === 'object' && item.url) ? esc(item.url) : "";
                    return url
                        ? `<div class="text-xs mb-0.5" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
                             <a href="${url}" target="_blank" rel="noopener"
                                style="color:var(--accent-blue);text-decoration:none;"
                                title="${title}">• ${title}</a>
                           </div>`
                        : `<div class="text-xs text-gray-400 mb-0.5" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;"
                               title="${title}">• ${title}</div>`;
                }).join('')}
               </div>`
            : `<div class="mt-2 pt-2 border-t" style="border-color:rgba(99,102,241,0.08)">
                  <div class="text-xs text-gray-500">뉴스 0건</div>
               </div>`;

        return `<div class="card p-4 theme-card">
            <div class="flex items-start justify-between mb-2">
                <h3 class="font-semibold text-white">${esc(theme.name)}</h3>
                <span class="mono text-lg font-bold" style="color:${scoreColor}">${Number(theme.score).toFixed(0)}</span>
            </div>
            <div class="score-bar mb-3">
                <div class="score-fill" style="width:${scorePct}%; background:${scoreColor}"></div>
            </div>
            <div class="flex flex-wrap gap-1 mb-2">${keywords}</div>
            <div class="text-xs text-gray-500 mb-1">관련 종목</div>
            <div class="mb-2">${stocks || '<span class="text-xs text-gray-500">없음</span>'}</div>
            ${newsHtml}
            <div class="flex justify-end text-xs text-gray-500 mt-1">
                <span>${esc(timeStr)}</span>
            </div>
        </div>`;
    }).join('');

    grid.innerHTML = cards;
}

// ── US 스크리닝 ────────────────────────────────────────────────────────────
async function loadUSScreening() {
    try {
        const results = await api('/api/us-proxy/api/us/screening');
        renderUSScreening(results);
    } catch (e) {
        console.warn('US 스크리닝 로드 실패 (봇 오프라인?):', e);
        const tbody = document.getElementById('us-screening-body');
        if (tbody) {
            tbody.textContent = '';
            const tr = document.createElement('tr');
            const td = document.createElement('td');
            td.colSpan = 9;
            td.style.cssText = 'padding:40px 0;text-align:center;color:var(--text-muted);font-size:0.85rem;';
            td.textContent = 'US 봇 오프라인 — 스크리닝 데이터 없음';
            tr.appendChild(td);
            tbody.appendChild(tr);
        }
    }
}

function renderUSScreening(results) {
    const tbody = document.getElementById('us-screening-body');
    const countEl = document.getElementById('us-screening-count');
    if (!tbody) return;

    if (!results || results.length === 0) {
        tbody.textContent = '';
        const tr = document.createElement('tr');
        const td = document.createElement('td');
        td.colSpan = 9;
        td.style.cssText = 'padding:40px 0;text-align:center;color:var(--text-muted);font-size:0.85rem;';
        td.textContent = 'US 스크리닝 결과 없음';
        tr.appendChild(td);
        tbody.appendChild(tr);
        if (countEl) countEl.textContent = '';
        return;
    }

    const TOP = 20;
    if (countEl) countEl.textContent = `상위 ${Math.min(results.length, TOP)} / 전체 ${results.length}`;

    const flagColors = {
        'VOL_SURGE': 'badge-yellow', '52W_HIGH': 'badge-green',
        'BREAKOUT': 'badge-green', 'MOMENTUM': 'badge-blue',
        'OVERSOLD': 'badge-red', 'OVERBOUGHT': 'badge-purple',
    };

    const rows = results.slice(0, TOP).map(s => {
        const changeCls = s.change_pct > 0 ? 'text-profit' : s.change_pct < 0 ? 'text-loss' : '';
        const totalScore = s.total_score || s.score || 0;
        const scoreBadge = totalScore >= 100 ? 'badge-green' : totalScore >= 80 ? 'badge-yellow' : 'badge-blue';
        const flags = (s.flags || []).map(f =>
            `<span class="badge ${flagColors[f] || 'badge-blue'}" style="font-size:0.6rem;padding:2px 6px;">${esc(f)}</span>`
        ).join(' ');

        // Finviz 기관 거래 컬럼
        const fz = s.finviz_meta || {};
        const instTrans = fz.inst_trans != null ? fz.inst_trans : null;
        const instCls = instTrans >= 2 ? 'text-profit' : instTrans <= -2 ? 'text-loss' : 'text-gray-400';
        const instStr = instTrans != null ? (instTrans >= 0 ? '+' : '') + instTrans.toFixed(2) + '%' : '--';

        // 목표가 상승 여지
        const upside = fz.target_upside != null ? fz.target_upside : null;
        const upsideCls = upside >= 30 ? 'text-profit' : upside >= 10 ? 'text-gray-300' : 'text-gray-500';
        const upsideStr = upside != null ? (upside >= 0 ? '+' : '') + upside.toFixed(1) + '%' : '--';

        // 점수 표시: 기술점수 + Finviz 보너스 (보너스가 있으면 표시)
        const bonus = s.finviz_bonus || 0;
        const bonusStr = bonus !== 0 ? ` <span style="font-size:0.65rem;color:${bonus>0?'#34d399':'#f87171'}">${bonus>0?'+':''}${bonus.toFixed(0)}</span>` : '';
        const scoreHtml = `<span class="badge ${scoreBadge}">${Number(totalScore).toFixed(0)}</span>${bonusStr}`;

        return `<tr class="border-b hover:bg-dark-700/30" style="border-color:#31324420">
            <td class="py-2 pr-3 font-medium text-white">${esc(s.symbol)}</td>
            <td class="py-2 pr-3 text-right mono">$${Number(s.price).toFixed(2)}</td>
            <td class="py-2 pr-3 text-right mono ${changeCls}">${formatPct(s.change_pct)}</td>
            <td class="py-2 pr-3 text-right mono col-hide-mobile">${s.vol_ratio ? Number(s.vol_ratio).toFixed(1) + 'x' : '--'}</td>
            <td class="py-2 pr-3 text-right mono col-hide-mobile">${s.rsi ? Number(s.rsi).toFixed(0) : '--'}</td>
            <td class="py-2 pr-3 text-right mono col-hide-mobile ${instCls}">${instStr}</td>
            <td class="py-2 pr-3 text-right mono col-hide-mobile ${upsideCls}">${upsideStr}</td>
            <td class="py-2 pr-3 text-right">${scoreHtml}</td>
            <td class="py-2 text-xs col-hide-mobile">${flags || '--'}</td>
        </tr>`;
    }).join('');

    tbody.innerHTML = rows;
}

// ── 이벤트 바인딩 ──────────────────────────────────────────────────────────
document.getElementById('btn-refresh-themes').addEventListener('click', loadThemes);
document.getElementById('btn-refresh-screening').addEventListener('click', loadScreening);
document.getElementById('btn-refresh-us-themes').addEventListener('click', loadUSThemes);
document.getElementById('btn-refresh-us-screening').addEventListener('click', loadUSScreening);

// ── 초기화 ─────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    // 마켓 필터 렌더링
    const filterBar = document.getElementById('themes-filter-bar');
    if (filterBar && typeof MarketFilter !== 'undefined') {
        MarketFilter.render(filterBar, (filter) => {
            applyThemesMarketFilter(filter);
        });
        applyThemesMarketFilter(MarketFilter.get());
    }

    // 다른 탭에서 필터 변경 시 동기화
    document.addEventListener('market_filter_change', (e) => {
        applyThemesMarketFilter(e.detail.filter);
    });

    loadThemes();
    loadScreening();
    loadUSThemes();
    loadUSScreening();
    sse.connect();
});
