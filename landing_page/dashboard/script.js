// Dashboard Script

// Open demo dashboard
document
    .querySelectorAll(".back-trigger-btn")
    .forEach((btn) => {
        btn.addEventListener("click", () => {
            window.location.href = "../index.html";
        });
    });


// Chart defaults for dark mode
Chart.defaults.color = '#9CA3AF';
Chart.defaults.font.family = "'Outfit', sans-serif";
Chart.defaults.scale.grid.color = 'rgba(255, 255, 255, 0.05)';
Chart.defaults.plugins.tooltip.backgroundColor = 'rgba(11, 14, 20, 0.9)';
Chart.defaults.plugins.tooltip.padding = 12;
Chart.defaults.plugins.tooltip.cornerRadius = 8;
Chart.defaults.plugins.tooltip.borderColor = 'rgba(255, 255, 255, 0.1)';
Chart.defaults.plugins.tooltip.borderWidth = 1;

// Colors
const colors = {
    cyan: '#00f7ff',
    purple: '#662c91',
    pink: '#FF6661',
    blue: '#1d4bcaff',
    indigo: '#560591',
    teal: '#008C7A',
    red: '#ff4934',
    orange: '#ff7b00'
};

const gradientColors = Object.values(colors);

// Main Execution
document.addEventListener('DOMContentLoaded', async () => {
    try {
        // Load data from the locally included syntheticData variable (from sabinet_fake.js)
        const data = syntheticData;

        const metrics = processLegalMetrics(data);
        renderKPIs(metrics.kpis);
        renderCharts(metrics);

    } catch (error) {
        console.error("Failed to load or process data:", error);
        document.getElementById('kpi-container').innerHTML = `<div class="kpi-card glass-card" style="grid-column: 1/-1; color: #F43F5E; text-align: center;">Error loading data: ${error.message}. Please serve this directory with a local web server (e.g. python -m http.server).</div>`;
    }
});

// Data Processing Logic (Ported from sabinet_aggregator.py)
function extractCaseYear(caseNumber) {
    if (!caseNumber) return null;
    const match = caseNumber.match(/(20\d{2})|\/(\d{2})$|\/(\d{4})/);
    if (!match) return null;

    let rawYear = match[0].replace(/\//g, '');
    if (rawYear.length === 2) {
        return parseInt("20" + rawYear, 10);
    }
    return parseInt(rawYear, 10);
}

function processLegalMetrics(jsonData) {
    const records = [];
    const allSubjects = [];
    const allKeywords = [];
    const subjectByYear = {};

    jsonData.forEach(caseObj => {
        const caseYear = extractCaseYear(caseObj.case_number);
        let durationYears = null;

        if (caseObj.judgment_date) {
            const judgmentYear = new Date(caseObj.judgment_date).getFullYear();
            if (caseYear) {
                durationYears = Math.max(0, judgmentYear - caseYear);
            }
        }

        records.push({
            case_number: caseObj.case_number,
            court: caseObj.court,
            division: caseObj.division_location,
            judge: caseObj.judge,
            reportable: caseObj.reportable,
            outcome: caseObj.result ? caseObj.result.outcome_type : 'Unknown',
            costs_order: caseObj.result ? caseObj.result.costs_order : 'None',
            duration_years: durationYears
        });

        if (caseObj.subjects) {
            allSubjects.push(...caseObj.subjects);
            if (caseYear) {
                if (!subjectByYear[caseYear]) subjectByYear[caseYear] = {};
                caseObj.subjects.forEach(sub => {
                    subjectByYear[caseYear][sub] = (subjectByYear[caseYear][sub] || 0) + 1;
                });
            }
        }
        if (caseObj.ai_metadata && caseObj.ai_metadata.keywords) {
            allKeywords.push(...caseObj.ai_metadata.keywords);
        }
    });

    // Aggregations
    const totalCases = records.length;

    // Average duration
    const durationSum = records.reduce((acc, r) => acc + (r.duration_years || 0), 0);
    const durationCount = records.filter(r => r.duration_years !== null).length;
    const avgDuration = durationCount > 0 ? (durationSum / durationCount).toFixed(1) : 0;

    // Reportable ratio
    const reportableCount = records.filter(r => r.reportable).length;
    const reportablePct = totalCases > 0 ? Math.round((reportableCount / totalCases) * 100) : 0;

    // Counts utility
    const countBy = (arr, key) => {
        const counts = {};
        arr.forEach(item => {
            const val = item[key];
            counts[val] = (counts[val] || 0) + 1;
        });
        return counts;
    };

    const countArray = (arr) => {
        const counts = {};
        arr.forEach(val => {
            counts[val] = (counts[val] || 0) + 1;
        });
        return counts;
    };

    const sortObject = (obj, limit = 10) => {
        return Object.entries(obj)
            .sort((a, b) => b[1] - a[1])
            .slice(0, limit)
            .reduce((acc, [k, v]) => { acc[k] = v; return acc; }, {});
    };

    const judgeMatrix = {};
    records.forEach(r => {
        if (!judgeMatrix[r.judge]) judgeMatrix[r.judge] = {};
        judgeMatrix[r.judge][r.outcome] = (judgeMatrix[r.judge][r.outcome] || 0) + 1;
    });

    return {
        kpis: {
            total_cases_analyzed: totalCases,
            average_case_lifecycle_years: avgDuration,
            reportable_ratio: reportablePct
        },
        court_workload: {
            by_court_type: sortObject(countBy(records, 'court')),
            by_division: sortObject(countBy(records, 'division'), 5)
        },
        litigation_trends: {
            top_outcomes: sortObject(countBy(records, 'outcome')),
            top_subject_matters: sortObject(countArray(allSubjects), 5),
            trending_keywords: sortObject(countArray(allKeywords), 8),
            subject_by_year: subjectByYear
        },
        judge_analytics: {
            case_volumes: sortObject(countBy(records, 'judge'), 10),
            judge_outcome_matrix: judgeMatrix
        }
    };
}

function renderKPIs(kpis) {
    const container = document.getElementById('kpi-container');
    container.innerHTML = `
        <div class="kpi-card glass-card">
            <div class="kpi-title"><i class="fa-solid fa-folder-open"></i> Total Cases Analyzed</div>
            <div class="kpi-value">${kpis.total_cases_analyzed}</div>
            <div class="kpi-sub">In Synthetic Dataset</div>
        </div>
        <div class="kpi-card glass-card">
            <div class="kpi-title"><i class="fa-solid fa-clock-rotate-left"></i> Avg Case Lifecycle</div>
            <div class="kpi-value">${kpis.average_case_lifecycle_years} <span style="font-size:1rem;font-weight:400;color:var(--text-muted)">Yrs</span></div>
            <div class="kpi-sub">From Initiation to Judgment</div>
        </div>
        <div class="kpi-card glass-card">
            <div class="kpi-title"><i class="fa-solid fa-file-signature"></i> Reportable Ratio</div>
            <div class="kpi-value">${kpis.reportable_ratio}%</div>
            <div class="kpi-sub">Cases marked as reportable</div>
        </div>
        <div class="kpi-card glass-card">
            <div class="kpi-title"><i class="fa-solid fa-chart-line"></i> Data Status</div>
            <div class="kpi-value" style="color:var(--accent-1); font-size:1.8rem; margin-top:0.5rem">Up to Date</div>
            <div class="kpi-sub">Aggregated live</div>
        </div>
    `;
}

function renderCharts(metrics) {
    // Top Outcomes (Doughnut)
    createChart('outcomesChart', 'doughnut', {
        labels: Object.keys(metrics.litigation_trends.top_outcomes),
        datasets: [{
            data: Object.values(metrics.litigation_trends.top_outcomes),
            backgroundColor: gradientColors,
            borderWidth: 0,
            hoverOffset: 4
        }]
    }, {
        cutout: '70%',
        plugins: { legend: { position: 'right' } }
    });

    // Court Workload by Division (Bar)
    createChart('courtWorkloadChart', 'bar', {
        labels: Object.keys(metrics.court_workload.by_division),
        datasets: [{
            label: 'Cases',
            data: Object.values(metrics.court_workload.by_division),
            backgroundColor: gradientColors,
            borderRadius: 6,
            borderWidth: 0
        }]
    }, {
        plugins: { legend: { display: false } },
        scales: { y: { beginAtZero: true, ticks: { stepSize: 1 } } }
    });

    // Top Subject Matters (Horizontal Bar)
    createChart('subjectsChart', 'bar', {
        labels: Object.keys(metrics.litigation_trends.top_subject_matters),
        datasets: [{
            label: 'Occurrences',
            data: Object.values(metrics.litigation_trends.top_subject_matters),
            backgroundColor: gradientColors,
            borderRadius: 6,
        }]
    }, {
        indexAxis: 'y',
        plugins: { legend: { display: false } },
        scales: { x: { beginAtZero: true, ticks: { stepSize: 1 } } }
    });

    // Trending Keywords (Polar Area)
    createChart('keywordsChart', 'polarArea', {
        labels: Object.keys(metrics.litigation_trends.trending_keywords),
        datasets: [{
            data: Object.values(metrics.litigation_trends.trending_keywords),
            backgroundColor: [
                'rgba(0, 247, 255, 0.6)',
                'rgba(102, 44, 145, 0.6)',
                'rgba(255, 102, 97, 0.6)',
                'rgba(29, 75, 202, 0.6)',
                'rgba(86, 5, 145, 0.6)',
                'rgba(0, 140, 122, 0.6)',
                'rgba(255, 73, 52, 0.6)',
                'rgba(255, 123, 0, 0.6)'
            ],
            borderWidth: 1,
            borderColor: 'rgba(255,255,255,0.1)'
        }]
    }, {
        scales: { r: { ticks: { display: false } } },
        plugins: { legend: { position: 'right' } }
    });

    // Judge Analytics (Stacked Bar)
    const judges = Object.keys(metrics.judge_analytics.case_volumes);
    const outcomesSet = new Set();
    judges.forEach(j => {
        Object.keys(metrics.judge_analytics.judge_outcome_matrix[j]).forEach(o => outcomesSet.add(o));
    });
    const outcomesArray = Array.from(outcomesSet);

    // Granted Outcome Percentage by Judge
    const judgeGrantedPct = judges.map(j => {
        let total = 0;
        let granted = 0;
        Object.entries(metrics.judge_analytics.judge_outcome_matrix[j]).forEach(([outcome, count]) => {
            total += count;
            if (outcome && outcome.toLowerCase().includes('granted')) {
                granted += count;
            }
        });
        return total > 0 ? ((granted / total) * 100).toFixed(1) : 0;
    });

    createChart('grantedPercentageChart', 'bar', {
        labels: judges,
        datasets: [{
            label: '% Granted Outcome',
            data: judgeGrantedPct,
            backgroundColor: gradientColors,
            borderRadius: 6
        }]
    }, {
        plugins: { legend: { display: false } },
        scales: { y: { beginAtZero: true, max: 100, ticks: { callback: function (value) { return value + "%" } } } }
    });

    // Subject Matter Trends over Time
    const years = Object.keys(metrics.litigation_trends.subject_by_year).sort();
    const top3Subjects = Object.keys(metrics.litigation_trends.top_subject_matters).slice(0, 3);
    const subjectDatasets = top3Subjects.map((sub, idx) => {
        return {
            label: sub,
            data: years.map(y => metrics.litigation_trends.subject_by_year[y][sub] || 0),
            borderColor: gradientColors[idx % gradientColors.length],
            backgroundColor: 'transparent',
            borderWidth: 2,
            tension: 0.4
        };
    });

    createChart('subjectTimelineChart', 'line', {
        labels: years,
        datasets: subjectDatasets
    }, {
        plugins: { legend: { position: 'top' } },
        scales: { y: { beginAtZero: true, ticks: { stepSize: 1 } } }
    });

    const datasets = outcomesArray.map((outcome, idx) => {
        return {
            label: outcome,
            data: judges.map(j => metrics.judge_analytics.judge_outcome_matrix[j][outcome] || 0),
            backgroundColor: gradientColors[idx % gradientColors.length],
            borderRadius: 4
        };
    });

    createChart('judgeAnalyticsChart', 'bar', {
        labels: judges,
        datasets: datasets
    }, {
        plugins: { legend: { position: 'top' } },
        scales: {
            x: { stacked: true },
            y: { stacked: true, beginAtZero: true, ticks: { stepSize: 1 } }
        }
    });
}

function createChart(id, type, data, options = {}) {
    const ctx = document.getElementById(id).getContext('2d');
    return new Chart(ctx, {
        type: type,
        data: data,
        options: {
            responsive: true,
            maintainAspectRatio: false,
            ...options
        }
    });
}

function getGradient(ctx, color1, color2, horizontal = false) {
    let gradient;
    if (horizontal) {
        gradient = ctx.createLinearGradient(0, 0, 400, 0);
    } else {
        gradient = ctx.createLinearGradient(0, 0, 0, 400);
    }
    gradient.addColorStop(0, color1);
    gradient.addColorStop(1, color2);
    return gradient;
}
