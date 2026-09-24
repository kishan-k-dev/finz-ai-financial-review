async function upload() {
    const f = document.getElementById('file').files[0];

    if (!f) {
        return alert('Choose the dataset first');
    }

    try {
        const r = await fetch('/api/upload-raw', {
            method: 'POST',
            headers: {
                'Content-Type': f.type || 'application/octet-stream',
                'X-Filename': f.name
            },
            body: f
        });

        const d = await r.json();

        document.getElementById('status').textContent =
            d.ok
                ? `Loaded ${d.rows} transactions`
                : `Error: ${d.detail || 'Upload failed'}`;

        if (d.ok) {
            refresh();
        }
    } catch (err) {
        document.getElementById('status').textContent =
            `Error: ${err.message}`;
    }
}


async function refresh() {
    await Promise.all([
        loadPnl(),
        loadVariance(),
        loadTx()
    ]);
}


async function loadPnl() {
    const d = await (await fetch('/api/pnl')).json();

    document.getElementById('pnl').innerHTML =
        d.length
            ? `<table class="table">
                <tr>
                    <th>Month</th>
                    <th>Revenue</th>
                    <th>COGS</th>
                    <th>Gross</th>
                    <th>Payroll</th>
                    <th>Opex</th>
                    <th>Operating Profit</th>
                </tr>` +
                d.map(x =>
                    `<tr>
                        <td>${x.month}</td>
                        <td>${fmt(x.revenue)}</td>
                        <td>${fmt(x.cogs)}</td>
                        <td>${fmt(x.gross_profit)}</td>
                        <td>${fmt(x.payroll)}</td>
                        <td>${fmt(x.operating_expenses)}</td>
                        <td><b>${fmt(x.operating_profit)}</b></td>
                    </tr>`
                ).join('') +
                `</table>`
            : 'Upload data first.';
}


async function loadVariance() {
    const d = await (await fetch('/api/variance')).json();

    document.getElementById('variance').innerHTML =
        d.length
            ? d.map(x =>
                `<div class="row">
                    <b>${x.from_month} → ${x.to_month}</b><br>
                    Profit change: ${fmt(x.profit_change)}<br>
                    Revenue: ${fmt(x.revenue_change)} |
                    COGS: ${fmt(x.cogs_change)} |
                    Payroll: ${fmt(x.payroll_change)} |
                    Opex: ${fmt(x.opex_change)}
                </div>`
            ).join('')
            : 'Upload at least two months.';
}


async function loadTx() {
    const d = await (await fetch('/api/transactions')).json();

    document.getElementById('tx').innerHTML =
        d.length
            ? `<table class="table">
                <tr>
                    <th>Date</th>
                    <th>Description</th>
                    <th>Amount</th>
                    <th>Category</th>
                    <th>Review</th>
                </tr>` +
                d.slice(0, 200).map(x =>
                    `<tr class="${x.is_review ? 'review' : ''}">
                        <td>${x.tx_date}</td>
                        <td>${esc(x.description)}</td>
                        <td>${fmt(x.amount)}</td>
                        <td>
                            <select onchange="cat(${x.id}, this.value)">
                                ${[
                                    'Revenue',
                                    'Cost of Goods Sold',
                                    'Payroll',
                                    'Operating Expenses'
                                ].map(c =>
                                    `<option ${c === x.category ? 'selected' : ''}>${c}</option>`
                                ).join('')}
                            </select>
                        </td>
                        <td>${x.is_review ? '⚠️ Review' : ''}</td>
                    </tr>`
                ).join('') +
                `</table>`
            : 'Upload data first.';
}


async function cat(id, category) {
    await fetch(`/api/transactions/${id}/category`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({ category })
    });

    refresh();
}


async function ask(q) {
    const question = q || document.getElementById('q').value;

    if (!question) {
        return;
    }

    const r = await fetch('/api/chat', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({ question })
    });

    const d = await r.json();

    document.getElementById('answer').textContent =
        d.answer + '\n\nEvidence: ' + d.evidence;
}


function fmt(x) {
    return Number(x || 0).toLocaleString(undefined, {
        maximumFractionDigits: 2
    });
}


function esc(s) {
    return String(s).replace(
        /[&<>"']/g,
        m => ({
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
            '"': '&quot;',
            "'": '&#39;'
        }[m])
    );
}


refresh();