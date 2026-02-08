/**
 * AI Trader v2 - 공통 유틸리티
 * SSE 연결, API 호출, 포맷팅
 */

// ============================================================
// SSE 연결 관리
// ============================================================
class SSEClient {
    constructor() {
        this.eventSource = null;
        this.handlers = {};
        this.reconnectDelay = 1000;
        this.maxReconnectDelay = 30000;
        this.connected = false;
    }

    connect() {
        if (this.eventSource) {
            this.eventSource.close();
        }

        this.eventSource = new EventSource('/api/stream');

        this.eventSource.onopen = () => {
            this.connected = true;
            this.reconnectDelay = 1000;
            console.log('[SSE] 연결됨');
            this._updateConnectionStatus(true);
        };

        this.eventSource.onerror = () => {
            this.connected = false;
            this._updateConnectionStatus(false);
            this.eventSource.close();

            // 재연결
            setTimeout(() => this.connect(), this.reconnectDelay);
            this.reconnectDelay = Math.min(this.reconnectDelay * 2, this.maxReconnectDelay);
        };

        // 이벤트 타입별 리스너
        const eventTypes = ['status', 'portfolio', 'positions', 'risk', 'events'];
        eventTypes.forEach(type => {
            this.eventSource.addEventListener(type, (e) => {
                try {
                    const data = JSON.parse(e.data);
                    this._dispatch(type, data);
                } catch (err) {
                    console.error(`[SSE] ${type} 파싱 오류:`, err);
                }
            });
        });
    }

    on(eventType, handler) {
        if (!this.handlers[eventType]) {
            this.handlers[eventType] = [];
        }
        this.handlers[eventType].push(handler);
    }

    _dispatch(eventType, data) {
        const handlers = this.handlers[eventType] || [];
        handlers.forEach(h => {
            try { h(data); } catch(e) { console.error(e); }
        });
    }

    _updateConnectionStatus(connected) {
        const el = document.getElementById('sb-status');
        if (!el) return;
        if (connected) {
            el.innerHTML = '<span class="status-dot green"></span> 연결됨';
        } else {
            el.innerHTML = '<span class="status-dot red"></span> 연결 끊김';
        }
    }
}

// 전역 SSE 클라이언트
const sse = new SSEClient();

// ============================================================
// API 호출
// ============================================================
async function api(path) {
    const resp = await fetch(path);
    if (!resp.ok) throw new Error(`API error: ${resp.status}`);
    return resp.json();
}

// ============================================================
// 포맷팅
// ============================================================
function formatNumber(n, decimals = 0) {
    if (n === null || n === undefined || isNaN(n)) return '--';
    return Number(n).toLocaleString('ko-KR', {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
    });
}

function formatCurrency(n) {
    if (n === null || n === undefined || isNaN(n)) return '--';
    return formatNumber(n, 0) + '원';
}

function formatPct(n, decimals = 2) {
    if (n === null || n === undefined || isNaN(n)) return '--';
    const prefix = n > 0 ? '+' : '';
    return prefix + Number(n).toFixed(decimals) + '%';
}

function formatPnl(n) {
    if (n === null || n === undefined || isNaN(n)) return '--';
    const prefix = n > 0 ? '+' : '';
    return prefix + formatNumber(n, 0) + '원';
}

function pnlClass(n) {
    if (n > 0) return 'text-profit';
    if (n < 0) return 'text-loss';
    return '';
}

function formatTime(isoString) {
    if (!isoString) return '--';
    const d = new Date(isoString);
    return d.toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function formatDate(isoString) {
    if (!isoString) return '--';
    const d = new Date(isoString);
    return d.toLocaleDateString('ko-KR', { year: 'numeric', month: '2-digit', day: '2-digit' });
}

function formatDuration(seconds) {
    if (!seconds && seconds !== 0) return '--';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
}

function sessionLabel(session) {
    const map = {
        'pre_market': '프리장',
        'regular': '정규장',
        'next': '넥스트장',
        'after_hours': '시간외',
        'closed': '마감',
    };
    return map[session] || session;
}

// ============================================================
// 네비게이션 활성화
// ============================================================
document.addEventListener('DOMContentLoaded', () => {
    const path = window.location.pathname;
    document.querySelectorAll('.nav-pill').forEach(link => {
        const page = link.dataset.page;
        if (
            (page === 'index' && path === '/') ||
            (page !== 'index' && path === '/' + page)
        ) {
            link.classList.add('active');
        }
    });

    // SSE 연결 (status-bar 업데이트)
    sse.on('status', (data) => {
        const sbSession = document.getElementById('sb-session');
        const sbUptime = document.getElementById('sb-uptime');
        if (sbSession) sbSession.textContent = sessionLabel(data.session);
        if (sbUptime) sbUptime.textContent = formatDuration(data.uptime_seconds);
    });
});
