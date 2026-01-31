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
            `<span class="badge badge-purple">${k}</span>`
        ).join(' ');

        const stocks = (theme.related_stocks || []).slice(0, 6).map(s =>
            `<span class="text-xs mono text-gray-400">${s}</span>`
        ).join(', ');

        const timeStr = theme.detected_at ? formatTime(theme.detected_at) : '--';

        return `<div class="card p-4 theme-card">
            <div class="flex items-start justify-between mb-2">
                <h3 class="font-semibold text-white">${theme.name}</h3>
                <span class="mono text-lg font-bold" style="color:${scoreColor}">${theme.score.toFixed(0)}</span>
            </div>
            <div class="score-bar mb-3">
                <div class="score-fill" style="width:${scorePct}%; background:${scoreColor}"></div>
            </div>
            <div class="flex flex-wrap gap-1 mb-2">${keywords}</div>
            <div class="text-xs text-gray-500 mb-1">관련 종목</div>
            <div class="mb-2">${stocks || '<span class="text-xs text-gray-500">없음</span>'}</div>
            <div class="flex justify-between text-xs text-gray-500 mt-3 pt-2 border-t" style="border-color:rgba(99,102,241,0.08)">
                <span>뉴스 ${theme.news_count || 0}건</span>
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
        const reasons = (s.reasons || []).slice(0, 3).join(', ');

        return `<tr class="border-b hover:bg-dark-700/30" style="border-color:#31324420">
            <td class="py-2 pr-3 font-medium text-white">${s.symbol} ${s.name ? `<span class="text-xs text-gray-500">${s.name}</span>` : ''}</td>
            <td class="py-2 pr-3 text-right mono">${formatNumber(s.price)}</td>
            <td class="py-2 pr-3 text-right mono ${changeCls}">${formatPct(s.change_pct)}</td>
            <td class="py-2 pr-3 text-right mono">${s.volume_ratio ? s.volume_ratio.toFixed(1) + 'x' : '--'}</td>
            <td class="py-2 pr-3 text-right"><span class="badge ${scoreBadge}">${s.score.toFixed(0)}</span></td>
            <td class="py-2 text-xs text-gray-400 max-w-xs truncate">${reasons || '--'}</td>
        </tr>`;
    }).join('');

    tbody.innerHTML = rows;
}

// 이벤트
document.getElementById('btn-refresh-themes').addEventListener('click', loadThemes);
document.getElementById('btn-refresh-screening').addEventListener('click', loadScreening);

// 초기화
document.addEventListener('DOMContentLoaded', () => {
    loadThemes();
    loadScreening();
    sse.connect();
});
