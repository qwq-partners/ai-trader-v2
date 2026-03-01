/**
 * AI Trader v2 - 테마/스크리닝 페이지
 */

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
            ${(theme.news_titles && theme.news_titles.length > 0) ? `
            <div class="mt-2 pt-2 border-t" style="border-color:rgba(99,102,241,0.08)">
                <div class="text-xs text-gray-500 mb-1">관련 뉴스 ${theme.news_count || theme.news_titles.length}건</div>
                ${theme.news_titles.slice(0, 5).map(t =>
                    `<div class="text-xs text-gray-400 truncate mb-0.5" title="${esc(t)}">• ${esc(t)}</div>`
                ).join('')}
            </div>` : `
            <div class="mt-2 pt-2 border-t" style="border-color:rgba(99,102,241,0.08)">
                <div class="text-xs text-gray-500">뉴스 ${theme.news_count || 0}건</div>
            </div>`}
            <div class="flex justify-end text-xs text-gray-500 mt-1">
                <span>${timeStr}</span>
            </div>
        </div>`;
    }).join('');

    grid.innerHTML = cards;
}

function renderScreening(results) {
    const tbody = document.getElementById('screening-body');

    if (!results || results.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" class="py-8 text-center text-gray-500">스크리닝 결과 없음</td></tr>';
        return;
    }

    const rows = results.slice(0, 50).map(s => {
        const changeCls = s.change_pct > 0 ? 'text-profit' : s.change_pct < 0 ? 'text-loss' : '';
        const scoreBadge = s.score >= 80 ? 'badge-green' : s.score >= 60 ? 'badge-yellow' : 'badge-blue';
        const reasons = (s.reasons || []).slice(0, 3).map(r => esc(r)).join(', ');

        return `<tr class="border-b hover:bg-dark-700/30" style="border-color:#31324420">
            <td class="py-2 pr-3 font-medium text-white">${esc(s.symbol)} ${s.name ? `<span class="text-xs text-gray-500">${esc(s.name)}</span>` : ''}</td>
            <td class="py-2 pr-3 text-right mono">${formatNumber(s.price)}</td>
            <td class="py-2 pr-3 text-right mono ${changeCls}">${formatPct(s.change_pct)}</td>
            <td class="py-2 pr-3 text-right mono col-hide-mobile">${s.volume_ratio ? s.volume_ratio.toFixed(1) + 'x' : '--'}</td>
            <td class="py-2 pr-3 text-right"><span class="badge ${scoreBadge}">${s.score.toFixed(0)}</span></td>
            <td class="py-2 text-xs text-gray-400 max-w-xs truncate col-hide-mobile">${reasons || '--'}</td>
        </tr>`;
    }).join('');

    tbody.innerHTML = rows;
}

// ============================================================
// US 테마
// NOTE: 모든 동적 문자열은 esc() 함수로 XSS 이스케이프 처리됨
// esc()는 common.js에서 document.createElement('div').textContent 사용
// ============================================================
async function loadUSThemes() {
    try {
        const themes = await api('/api/us-proxy/api/us/themes');
        renderUSThemes(themes);
    } catch (e) {
        console.warn('US 테마 로드 실패 (봇 오프라인?):', e);
        const grid = document.getElementById('us-themes-grid');
        if (grid) grid.textContent = '';
        if (grid) {
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

    // 테마 카드는 서버 데이터(Finnhub API)에서 오지만 모든 동적 값은 esc()로 이스케이프
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

        const headlines = (theme.news_headlines || []);

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
            ${headlines.length > 0 ? `
            <div class="mt-2 pt-2 border-t" style="border-color:rgba(99,102,241,0.08)">
                <div class="text-xs text-gray-500 mb-1">관련 뉴스 ${theme.news_count || headlines.length}건</div>
                ${headlines.slice(0, 5).map(t =>
                    `<div class="text-xs text-gray-400 truncate mb-0.5" title="${esc(t)}">• ${esc(t)}</div>`
                ).join('')}
            </div>` : `
            <div class="mt-2 pt-2 border-t" style="border-color:rgba(99,102,241,0.08)">
                <div class="text-xs text-gray-500">뉴스 ${theme.news_count || 0}건</div>
            </div>`}
            <div class="flex justify-end text-xs text-gray-500 mt-1">
                <span>${esc(timeStr)}</span>
            </div>
        </div>`;
    }).join('');

    grid.innerHTML = cards;
}

// ============================================================
// US 스크리닝
// ============================================================
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
            td.colSpan = 8;
            td.style.cssText = 'padding:40px 0;text-align:center;color:var(--text-muted);font-size:0.85rem;';
            td.textContent = 'US 봇 오프라인 — 스크리닝 데이터 없음';
            tr.appendChild(td);
            tbody.appendChild(tr);
        }
    }
}

function renderUSScreening(results) {
    const tbody = document.getElementById('us-screening-body');
    if (!tbody) return;

    if (!results || results.length === 0) {
        tbody.textContent = '';
        const tr = document.createElement('tr');
        const td = document.createElement('td');
        td.colSpan = 8;
        td.style.cssText = 'padding:40px 0;text-align:center;color:var(--text-muted);font-size:0.85rem;';
        td.textContent = 'US 스크리닝 결과 없음';
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
    }

    const flagColors = {
        'VOL_SURGE': 'badge-yellow',
        '52W_HIGH': 'badge-green',
        'BREAKOUT': 'badge-green',
        'MOMENTUM': 'badge-blue',
        'OVERSOLD': 'badge-red',
        'OVERBOUGHT': 'badge-purple',
    };

    // 스크리닝 데이터는 서버 계산 결과이며 모든 동적 값은 esc()로 이스케이프
    const rows = results.slice(0, 50).map(s => {
        const changeCls = s.change_pct > 0 ? 'text-profit' : s.change_pct < 0 ? 'text-loss' : '';
        const scoreBadge = s.score >= 80 ? 'badge-green' : s.score >= 60 ? 'badge-yellow' : 'badge-blue';
        const flags = (s.flags || []).map(f =>
            `<span class="badge ${flagColors[f] || 'badge-blue'}" style="font-size:0.6rem;padding:2px 6px;">${esc(f)}</span>`
        ).join(' ');

        return `<tr class="border-b hover:bg-dark-700/30" style="border-color:#31324420">
            <td class="py-2 pr-3 font-medium text-white">${esc(s.symbol)}</td>
            <td class="py-2 pr-3 text-right mono">$${Number(s.price).toFixed(2)}</td>
            <td class="py-2 pr-3 text-right mono ${changeCls}">${formatPct(s.change_pct)}</td>
            <td class="py-2 pr-3 text-right mono col-hide-mobile">${s.vol_ratio ? Number(s.vol_ratio).toFixed(1) + 'x' : '--'}</td>
            <td class="py-2 pr-3 text-right mono col-hide-mobile">${s.rsi ? Number(s.rsi).toFixed(0) : '--'}</td>
            <td class="py-2 pr-3 text-right mono col-hide-mobile">${s.pct_from_52w_high != null ? formatPct(s.pct_from_52w_high) : '--'}</td>
            <td class="py-2 pr-3 text-right"><span class="badge ${scoreBadge}">${Number(s.score).toFixed(0)}</span></td>
            <td class="py-2 text-xs col-hide-mobile">${flags || '--'}</td>
        </tr>`;
    }).join('');

    tbody.innerHTML = rows;
}

// 이벤트 — KR
document.getElementById('btn-refresh-themes').addEventListener('click', loadThemes);
document.getElementById('btn-refresh-screening').addEventListener('click', loadScreening);

// 이벤트 — US
document.getElementById('btn-refresh-us-themes').addEventListener('click', loadUSThemes);
document.getElementById('btn-refresh-us-screening').addEventListener('click', loadUSScreening);

// 초기화
document.addEventListener('DOMContentLoaded', () => {
    loadThemes();
    loadScreening();
    loadUSThemes();
    loadUSScreening();
    sse.connect();
});
