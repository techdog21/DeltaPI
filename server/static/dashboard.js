function encryptAndSubmit() {
    let days = document.getElementById('daysInput').value;
    fetch('/encrypt_days?days=' + encodeURIComponent(days))
        .then(res => res.json())
        .then(data => {
            document.getElementById('tokenInput').value = data.token;
            document.querySelector('form').submit();
        })
        .catch(() => alert('Encryption error'));
}

function toggleTheme() {
    var current = document.documentElement.getAttribute('data-theme') || 'dark';
    var next = current === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    document.cookie = 'theme=' + next + ';path=/;max-age=31536000';
    location.reload();
}

// ---- Progressive load ----
// The server returns an instant shell full of placeholders; the real content
// arrives in two stages fetched here: /panels (everything from the local DB —
// fast) is swapped into .dashboard wholesale, then /external (the eight
// third-party weather/environment APIs — slow) fills its slots by id. A slow
// upstream API can therefore only ever delay the Weather/Environment bodies,
// never the page or the local data.

var _lastExternal = null;   // last /external payload, re-applied after a panels swap

function applyExternal(d) {
    if (!d || d.error) return;
    var wb = document.getElementById('weatherBody');
    var eb = document.getElementById('envBody');
    var fc = document.getElementById('solarFc');
    var op = document.getElementById('outlookPill');
    if (wb) wb.innerHTML = d.weather_html;
    if (eb) eb.innerHTML = d.environment_html;
    if (fc) fc.innerHTML = d.solar_fc_html;
    if (op) { op.className = 'pill ' + d.outlook_class; op.textContent = d.outlook_label; }
}

function loadExternal() {
    fetch('/external' + window.location.search, { cache: 'no-store' })
        .then(function(r) { return r.json(); })
        .then(function(d) { _lastExternal = d; applyExternal(d); })
        .catch(function() {});
}

var _panelsRetry = null;
function loadPanels() {
    if (_panelsRetry) { clearTimeout(_panelsRetry); _panelsRetry = null; }
    fetch('/panels' + window.location.search, { cache: 'no-store' })
        .then(function(r) {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.text();
        })
        .then(function(html) {
            var cur = document.querySelector('.dashboard');
            if (cur) {
                cur.innerHTML = html;
                initCharts();
                applyExternal(_lastExternal);   // keep the last weather while a fresh one loads
            }
            loadExternal();
        })
        .catch(function() { _panelsRetry = setTimeout(loadPanels, 10000); });
}

// In-place refresh (auto-refresh timer): same two-stage fetch — no full reload,
// so the header controls and scroll position are untouched and nothing flashes.
function refreshDashboard() {
    loadPanels();
}

var _refreshTimer = null;
function setAutoRefresh(seconds) {
    if (_refreshTimer) clearInterval(_refreshTimer);
    _refreshTimer = null;
    localStorage.setItem('autoRefresh', seconds);
    if (seconds > 0) {
        _refreshTimer = setInterval(refreshDashboard, seconds * 1000);
    }
}
(function() {
    var saved = localStorage.getItem('autoRefresh') || '0';
    var sel = document.getElementById('autoRefresh');
    if (sel) sel.value = saved;
    if (parseInt(saved) > 0) setAutoRefresh(parseInt(saved));
})();

// ---- Charts: rebuilt from the #dash-data JSON island so each panels swap can redraw them ----
var _charts = [];
function initCharts() {
    var el = document.getElementById('dash-data');
    if (!el || typeof Chart === 'undefined') return;
    var D = JSON.parse(el.textContent);
    var style = getComputedStyle(document.documentElement);
    var gridColor = style.getPropertyValue('--grid-color').trim();
    var textMuted = style.getPropertyValue('--text-muted').trim();
    var cv = function(n) { return style.getPropertyValue(n).trim(); };
    var chartOpts = function(yMin, yMax, stepSize) { return {
        responsive: true, maintainAspectRatio: false, animation: false,
        plugins: { legend: { display: false } },
        elements: { point: { radius: 0 }, line: { borderWidth: 1.5 } },
        scales: { x: { display: false },
            y: { min: yMin, max: yMax, ticks: { stepSize: stepSize, font: { size: 9 }, color: textMuted }, grid: { color: gridColor } } }
    }; };
    var flat = function(arr, val) { return arr.map(function() { return val; }); };
    _charts.forEach(function(c) { c.destroy(); });
    _charts = [];
    function mk(id, cfg) { var e = document.getElementById(id); if (e) _charts.push(new Chart(e, cfg)); }
    mk('chartPower', { type: 'line', data: { labels: D.timestamps, datasets: [{ data: D.powers, borderColor: cv('--chart-power'), fill: false, tension: 0.1 }] }, options: chartOpts(0, 305, 50) });
    mk('chartVoltage', { type: 'line', data: { labels: D.voltage_timestamps, datasets: [{ data: D.voltage_values, borderColor: cv('--chart-voltage'), backgroundColor: cv('--chart-voltage-fill'), fill: true, tension: 0.3, spanGaps: false }] }, options: chartOpts(12.5, 14.6, 0.5) });
    mk('chartH20', { type: 'line', data: { labels: D.h20_days, datasets: [{ data: D.h20_values, borderColor: cv('--chart-h20'), backgroundColor: cv('--chart-h20-fill'), fill: true, tension: 0.2, pointRadius: 2 }] }, options: chartOpts(0, D.h20_ymax, D.h20_ymax / 4) });
    mk('chartSOC', { type: 'line', data: { labels: D.batt_times, datasets: [
        { data: D.batt_soc_values, borderColor: cv('--chart-voltage'), backgroundColor: cv('--chart-voltage-fill'), fill: true, tension: 0.3, pointRadius: 0 },
        { data: flat(D.batt_times, D.SOC_DANGER), borderColor: cv('--pill-red'), borderDash: [4,3], fill: false, pointRadius: 0 }
    ] }, options: chartOpts(0, 100, 20) });
    mk('chartLoad', { type: 'line', data: { labels: D.batt_times, datasets: [{ data: D.batt_load_values, borderColor: cv('--chart-power'), fill: false, tension: 0.2, pointRadius: 0 }] }, options: chartOpts(0, null, null) });
    mk('chartCharge', { type: 'line', data: { labels: D.timestamps, datasets: [{ data: D.charge_powers, borderColor: cv('--chart-h21-border'), fill: false, tension: 0.1 }] }, options: chartOpts(0, null, null) });
    mk('chartTemp', { type: 'line', data: { labels: D.batt_times, datasets: [
        { data: D.batt_temp_values, borderColor: cv('--chart-power'), fill: false, tension: 0.3, pointRadius: 0, spanGaps: true },
        { data: flat(D.batt_times, D.FREEZE_F), borderColor: cv('--pill-red'), borderDash: [4,3], fill: false, pointRadius: 0 }
    ] }, options: chartOpts(null, null, null) });
    mk('chartConsDaily', { type: 'bar', data: { labels: D.cons_days, datasets: [{ data: D.cons_values, backgroundColor: cv('--chart-h21'), borderColor: cv('--chart-power'), borderWidth: 1 }] }, options: chartOpts(0, null, null) });
    // Pi CPU temp (°C) + fan (%) share a 0-100 axis; both sit naturally in range.
    mk('chartPiTemp', { type: 'line', data: { labels: D.pi_times, datasets: [
        { data: D.pi_temp_vals, borderColor: cv('--chart-power'), fill: false, tension: 0.3, pointRadius: 0, spanGaps: true },
        { data: D.pi_fan_vals, borderColor: cv('--chart-voltage'), fill: false, tension: 0.3, pointRadius: 0, spanGaps: true }
    ] }, options: chartOpts(0, 100, 20) });
    mk('chartPiMem', { type: 'line', data: { labels: D.pi_times, datasets: [{ data: D.pi_mem_vals, borderColor: cv('--chart-h20'), backgroundColor: cv('--chart-h20-fill'), fill: true, tension: 0.3, pointRadius: 0, spanGaps: true }] }, options: chartOpts(0, null, null) });
    mk('chartPiLoad', { type: 'line', data: { labels: D.pi_times, datasets: [{ data: D.pi_load_vals, borderColor: cv('--chart-voltage'), fill: false, tension: 0.3, pointRadius: 0, spanGaps: true }] }, options: chartOpts(0, null, null) });
    // Pi disk (GB used) — anchored at 0 so the line height tracks actual fill; watch it creep up.
    mk('chartPiDisk', { type: 'line', data: { labels: D.pi_times, datasets: [{ data: D.pi_disk_vals, borderColor: cv('--chart-h21-border'), fill: false, tension: 0.3, pointRadius: 0, spanGaps: true }] }, options: chartOpts(0, null, null) });
}
initCharts();
loadPanels();
