/**
 * AI Trader v2 - US Market Tab
 * ai-trader-us API(8081) 프록시를 통한 미국 주식 대시보드
 *
 * XSS 방지: 모든 API 응답 데이터는 esc() 함수를 통해 이스케이프 처리됨
 */

const US_STATUS_URL    = "/api/us-proxy/api/us/status";
const US_PORTFOLIO_URL = "/api/us-proxy/api/us/portfolio";
const US_POSITIONS_URL = "/api/us-proxy/api/us/positions";
const US_SIGNALS_URL   = "/api/us-proxy/api/us/signals";

function esc(s) {
    if (s == null) return "";
    const d = document.createElement("div");
    d.textContent = String(s);
    return d.innerHTML;
}

function fmtUSD(v) {
    return "$" + (v || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtPct(v) {
    return (v >= 0 ? "+" : "") + (v || 0).toFixed(2) + "%";
}

async function fetchUSData() {
    try {
        const [statusRes, portfolioRes, positionsRes, signalsRes] = await Promise.all([
            fetch(US_STATUS_URL),
            fetch(US_PORTFOLIO_URL),
            fetch(US_POSITIONS_URL),
            fetch(US_SIGNALS_URL),
        ]);
        const [status, portfolio, positions, signals] = await Promise.all([
            statusRes.json(),
            portfolioRes.json(),
            positionsRes.json(),
            signalsRes.json(),
        ]);

        if (status.error || status.offline) {
            renderOffline();
            return;
        }

        renderStatus(status);
        renderPortfolio(portfolio);
        renderPositions(positions);
        renderSignals(signals);
    } catch (e) {
        renderOffline();
    }
}

function renderStatus(status) {
    const botEl = document.getElementById("us-bot-status");
    const sessionEl = document.getElementById("us-session");
    const updatedEl = document.getElementById("us-updated");

    const running = status.running !== false;
    const dotClass = running ? "green" : "red";
    const label = running ? "Running" : "Offline";
    botEl.innerHTML = '<span class="status-dot ' + dotClass + '"></span> ' + esc(label);

    const session = status.session || status.market_session || "--";
    sessionEl.textContent = session;

    const now = new Date();
    updatedEl.textContent = now.toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function renderPortfolio(p) {
    const data = p.error ? {} : p;
    const totalEl = document.getElementById("us-total-value");
    const cashEl = document.getElementById("us-cash");
    const posValEl = document.getElementById("us-positions-value");
    const pnlEl = document.getElementById("us-daily-pnl");

    totalEl.textContent = fmtUSD(data.total_value || data.total_equity);
    cashEl.textContent = fmtUSD(data.cash);
    posValEl.textContent = fmtUSD(data.positions_value || data.stock_value);

    const pnl = data.daily_pnl || 0;
    pnlEl.textContent = fmtUSD(pnl);
    pnlEl.className = "value " + (pnl >= 0 ? "text-profit" : "text-loss");
}

function renderPositions(positions) {
    const tbody = document.getElementById("us-positions-body");
    const items = Array.isArray(positions) ? positions : (positions.positions || []);

    if (!items.length) {
        tbody.textContent = "";
        const tr = document.createElement("tr");
        const td = document.createElement("td");
        td.setAttribute("colspan", "6");
        td.setAttribute("style", "padding: 40px 0; text-align: center; color: var(--text-muted); font-size: 0.85rem;");
        td.textContent = "보유 종목 없음";
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
    }

    tbody.textContent = "";
    items.forEach(function(pos) {
        const pnlPct = pos.unrealized_pnl_pct || pos.pnl_pct || 0;
        const pnlClass = pnlPct >= 0 ? "text-profit" : "text-loss";
        const marketVal = pos.market_value || pos.current_value || 0;

        const tr = document.createElement("tr");
        tr.setAttribute("style", "border-bottom: 1px solid var(--border-subtle);");

        const tdSymbol = document.createElement("td");
        tdSymbol.setAttribute("style", "padding: 10px 12px 10px 0; font-weight: 600;");
        tdSymbol.textContent = pos.symbol || pos.ticker || "";

        const tdStrategy = document.createElement("td");
        tdStrategy.setAttribute("style", "padding: 10px 12px 10px 0; color: var(--text-secondary); font-size: 0.8rem;");
        tdStrategy.textContent = pos.strategy || "--";

        const tdQty = document.createElement("td");
        tdQty.className = "mono";
        tdQty.setAttribute("style", "padding: 10px 12px 10px 0; text-align: right;");
        tdQty.textContent = pos.quantity || pos.shares || "";

        const tdAvg = document.createElement("td");
        tdAvg.className = "mono";
        tdAvg.setAttribute("style", "padding: 10px 12px 10px 0; text-align: right;");
        tdAvg.textContent = fmtUSD(pos.avg_price || pos.average_price);

        const tdPnl = document.createElement("td");
        tdPnl.className = "mono " + pnlClass;
        tdPnl.setAttribute("style", "padding: 10px 12px 10px 0; text-align: right; font-weight: 600;");
        tdPnl.textContent = fmtPct(pnlPct);

        const tdVal = document.createElement("td");
        tdVal.className = "mono";
        tdVal.setAttribute("style", "padding: 10px 0 10px 0; text-align: right;");
        tdVal.textContent = fmtUSD(marketVal);

        tr.appendChild(tdSymbol);
        tr.appendChild(tdStrategy);
        tr.appendChild(tdQty);
        tr.appendChild(tdAvg);
        tr.appendChild(tdPnl);
        tr.appendChild(tdVal);
        tbody.appendChild(tr);
    });
}

function renderSignals(signals) {
    const tbody = document.getElementById("us-signals-body");
    const items = Array.isArray(signals) ? signals : (signals.signals || []);

    if (!items.length) {
        tbody.textContent = "";
        const tr = document.createElement("tr");
        const td = document.createElement("td");
        td.setAttribute("colspan", "6");
        td.setAttribute("style", "padding: 40px 0; text-align: center; color: var(--text-muted); font-size: 0.85rem;");
        td.textContent = "시그널 없음";
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
    }

    tbody.textContent = "";
    items.forEach(function(sig) {
        var score = sig.score || 0;
        var scoreColor = score >= 70 ? "var(--accent-green)" : (score >= 50 ? "var(--accent-amber)" : "var(--text-muted)");
        var dir = (sig.direction || sig.side || "").toUpperCase();

        var time = sig.time || sig.timestamp || sig.created_at || "--";
        if (time.length > 19) time = time.substring(11, 19);
        else if (time.length > 8 && time.includes("T")) time = time.split("T")[1].substring(0, 8);

        const tr = document.createElement("tr");
        tr.setAttribute("style", "border-bottom: 1px solid var(--border-subtle);");

        const tdTime = document.createElement("td");
        tdTime.className = "mono";
        tdTime.setAttribute("style", "padding: 10px 12px 10px 0; font-size: 0.8rem; color: var(--text-secondary);");
        tdTime.textContent = time;

        const tdSymbol = document.createElement("td");
        tdSymbol.setAttribute("style", "padding: 10px 12px 10px 0; font-weight: 600;");
        tdSymbol.textContent = sig.symbol || sig.ticker || "";

        const tdStrategy = document.createElement("td");
        tdStrategy.setAttribute("style", "padding: 10px 12px 10px 0; color: var(--text-secondary); font-size: 0.8rem;");
        tdStrategy.textContent = sig.strategy || "--";

        const tdScore = document.createElement("td");
        tdScore.className = "mono";
        tdScore.setAttribute("style", "padding: 10px 12px 10px 0; text-align: right; font-weight: 600; color: " + scoreColor + ";");
        tdScore.textContent = score;

        const tdDir = document.createElement("td");
        tdDir.setAttribute("style", "padding: 10px 12px 10px 0;");
        const dirSpan = document.createElement("span");
        if (dir === "BUY") {
            dirSpan.className = "badge badge-green";
            dirSpan.textContent = "BUY";
        } else if (dir === "SELL") {
            dirSpan.className = "badge badge-red";
            dirSpan.textContent = "SELL";
        } else {
            dirSpan.className = "badge badge-blue";
            dirSpan.textContent = dir || "--";
        }
        tdDir.appendChild(dirSpan);

        const tdReason = document.createElement("td");
        tdReason.setAttribute("style", "padding: 10px 0 10px 0; color: var(--text-secondary); font-size: 0.8rem; max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;");
        tdReason.textContent = sig.reason || sig.description || "--";

        tr.appendChild(tdTime);
        tr.appendChild(tdSymbol);
        tr.appendChild(tdStrategy);
        tr.appendChild(tdScore);
        tr.appendChild(tdDir);
        tr.appendChild(tdReason);
        tbody.appendChild(tr);
    });
}

function renderOffline() {
    // Status bar
    var botEl = document.getElementById("us-bot-status");
    botEl.textContent = "";
    var dot = document.createElement("span");
    dot.className = "status-dot red";
    botEl.appendChild(dot);
    botEl.appendChild(document.createTextNode(" Offline"));

    document.getElementById("us-session").textContent = "--";
    var now = new Date();
    document.getElementById("us-updated").textContent = now.toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });

    // Summary cards
    document.getElementById("us-total-value").textContent = "--";
    document.getElementById("us-cash").textContent = "--";
    document.getElementById("us-positions-value").textContent = "--";
    var pnlEl = document.getElementById("us-daily-pnl");
    pnlEl.textContent = "--";
    pnlEl.className = "value";

    // Tables
    ["us-positions-body", "us-signals-body"].forEach(function(id) {
        var el = document.getElementById(id);
        el.textContent = "";
        var tr = document.createElement("tr");
        var td = document.createElement("td");
        td.setAttribute("colspan", "6");
        td.setAttribute("style", "padding: 40px 0; text-align: center; color: var(--text-muted); font-size: 0.85rem;");
        td.textContent = "API 서버 오프라인";
        tr.appendChild(td);
        el.appendChild(tr);
    });
}

document.addEventListener("DOMContentLoaded", function() {
    fetchUSData();
    setInterval(fetchUSData, 30000);
});
