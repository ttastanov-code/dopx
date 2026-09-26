// static/js/charts.js
// Единый вид графиков дашборда и админки (Chart.js 4): короткие даты, мягкая сетка, цвета темы.

(function () {
    const MONTHS = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];

    // Цвет текста/сетки из темы daisyUI; в админке (без токенов) — нейтральный серый.
    // Повторная инициализация (мягкое обновление страницы) — старый график на canvas убираем.
    function destroyExisting(el) {
        const prev = el && window.Chart && Chart.getChart(el);
        if (prev) prev.destroy();
    }

    function themeColor(alpha) {
        const raw = getComputedStyle(document.documentElement).getPropertyValue('--color-base-content').trim();
        return raw ? `color-mix(in oklab, ${raw} ${Math.round(alpha * 100)}%, transparent)` : `rgba(100, 116, 139, ${alpha})`;
    }

    // "2026-09-11" -> "11.09" на оси, "11 сен 2026" в подсказке.
    function shortDate(value) {
        const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(value || '');
        return m ? `${m[3]}.${m[2]}` : value;
    }
    function longDate(value) {
        const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(value || '');
        return m ? `${Number(m[3])} ${MONTHS[Number(m[2]) - 1]} ${m[1]}` : value;
    }

    function baseOptions() {
        const text = themeColor(0.55);
        const grid = themeColor(0.08);
        return {
            responsive: true,
            maintainAspectRatio: true,
            interaction: { mode: 'index', intersect: false },
            plugins: {
                legend: { display: false },
                tooltip: {
                    padding: 10,
                    cornerRadius: 10,
                    displayColors: false,
                    titleFont: { weight: '600' },
                    callbacks: { title: (items) => longDate(items[0] && items[0].label) },
                },
            },
            scales: {
                x: {
                    grid: { display: false },
                    border: { display: false },
                    ticks: {
                        color: text,
                        maxRotation: 0,
                        autoSkip: true,
                        maxTicksLimit: 7,
                        callback: function (value) { return shortDate(this.getLabelForValue(value)); },
                    },
                },
                y: {
                    beginAtZero: true,
                    grid: { color: grid },
                    border: { display: false },
                    ticks: { color: text, precision: 0, maxTicksLimit: 5 },
                },
            },
        };
    }

    // Верх шкалы — на 15% выше максимума (без округления до «красивого» деления),
    // иначе ровная линия уезжает на середину шкалы.
    function fitYAxis(options, values) {
        const max = Math.max(0, ...values);
        options.scales.y.bounds = 'data';
        options.scales.y.max = max === 0 ? 1 : max * 1.15;
        // Деления строим сами: целые, не больше пяти, без дробного «верха» вроде 1.15.
        const top = Math.max(1, Math.floor(max));
        const rough = top / 4;
        const magnitude = Math.pow(10, Math.floor(Math.log10(Math.max(rough, 1))));
        const step = Math.max(1, [1, 2, 5, 10].map((m) => m * magnitude).find((v) => v >= rough) || magnitude * 10);
        options.scales.y.afterBuildTicks = (axis) => {
            const ticks = [];
            for (let v = 0; v <= top; v += step) ticks.push({ value: v });
            axis.ticks = ticks;
        };
    }

    function line(canvasId, points, opts) {
        const el = document.getElementById(canvasId);
        destroyExisting(el);
        if (!el || typeof Chart === 'undefined') return null;
        const valueKey = opts.valueKey || (Object.prototype.hasOwnProperty.call(points[0] || {}, 'active_users') ? 'active_users' : 'count');
        const values = points.map((p) => p[valueKey]);
        const options = baseOptions();
        fitYAxis(options, values);
        return new Chart(el.getContext('2d'), {
            type: 'line',
            data: {
                labels: points.map((p) => p.period),
                datasets: [{
                    label: opts.label,
                    data: values,
                    borderColor: opts.color,
                    backgroundColor: (ctx) => {
                        const area = ctx.chart.chartArea;
                        if (!area) return opts.color + '22';
                        const g = ctx.chart.ctx.createLinearGradient(0, area.top, 0, area.bottom);
                        g.addColorStop(0, opts.color + '40');
                        g.addColorStop(1, opts.color + '00');
                        return g;
                    },
                    borderWidth: 2,
                    tension: 0.35,
                    fill: true,
                    pointRadius: 0,
                    pointHoverRadius: 4,
                    pointHoverBackgroundColor: opts.color,
                }],
            },
            options: options,
        });
    }

    function bar(canvasId, labels, values, opts) {
        const el = document.getElementById(canvasId);
        destroyExisting(el);
        if (!el || typeof Chart === 'undefined') return null;
        const options = baseOptions();
        options.scales.x.ticks.callback = function (value) { return this.getLabelForValue(value); };
        options.plugins.tooltip.callbacks = {};
        fitYAxis(options, values);
        return new Chart(el.getContext('2d'), {
            type: 'bar',
            data: {
                labels: labels,
                datasets: [{ label: opts.label, data: values, backgroundColor: opts.color, borderRadius: 6, maxBarThickness: 36 }],
            },
            options: options,
        });
    }

    function doughnut(canvasId, labels, values, colors) {
        const el = document.getElementById(canvasId);
        destroyExisting(el);
        if (!el || typeof Chart === 'undefined') return null;
        return new Chart(el.getContext('2d'), {
            type: 'doughnut',
            data: { labels: labels, datasets: [{ data: values, backgroundColor: colors, borderWidth: 0, hoverOffset: 6 }] },
            options: {
                responsive: true,
                cutout: '68%',
                plugins: {
                    legend: { position: 'bottom', labels: { color: themeColor(0.7), usePointStyle: true, boxWidth: 8, padding: 14 } },
                    tooltip: { padding: 10, cornerRadius: 10 },
                },
            },
        });
    }

    window.dopxCharts = { line: line, bar: bar, doughnut: doughnut };
})();
