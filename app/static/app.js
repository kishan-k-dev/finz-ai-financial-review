// ============================================================
// FINZ - BROWSER SESSION ISOLATION
// ============================================================
//
// Every browser context gets its own unique session ID.
//
// Normal Chrome:
// finz_session_id = ABC123
//
// Incognito:
// finz_session_id = XYZ789
//
// The session is sent in:
// 1. X-Finz-Session header
// 2. ?session_id= URL parameter
//
// The URL parameter is intentional. It prevents cached API
// responses from being reused between different sessions.
// ============================================================


function getSessionId() {
    let id = localStorage.getItem("finz_session_id");

    if (!id) {

        if (
            window.crypto &&
            typeof crypto.randomUUID === "function"
        ) {
            id = crypto.randomUUID();

        } else {
            id =
                "finz-" +
                Date.now() +
                "-" +
                Math.random()
                    .toString(36)
                    .slice(2);
        }

        localStorage.setItem(
            "finz_session_id",
            id
        );
    }

    return id;
}


// ============================================================
// API FETCH HELPER
// ============================================================
//
// Every API request goes through this function.
//
// It automatically:
// - Adds X-Finz-Session
// - Adds session_id to API URL
// - Disables browser cache
// - Uses same-origin credentials
// ============================================================


function apiFetch(url, options = {}) {

    const sessionId = getSessionId();

    const headers = new Headers(
        options.headers || {}
    );

    headers.set(
        "X-Finz-Session",
        sessionId
    );


    // --------------------------------------------------------
    // Add session_id to the URL
    // --------------------------------------------------------
    //
    // Example:
    //
    // /api/pnl
    //
    // becomes:
    //
    // /api/pnl?session_id=abc123
    //
    // If the URL already has parameters, use & instead.
    //

    let requestUrl = url;

    if (
        url.startsWith("/api/") &&
        !url.includes("session_id=")
    ) {

        const separator =
            url.includes("?")
                ? "&"
                : "?";

        requestUrl =
            `${url}${separator}session_id=${encodeURIComponent(sessionId)}`;
    }


    return fetch(
        requestUrl,
        {
            ...options,

            headers: headers,

            credentials: "same-origin",

            cache: "no-store"
        }
    );
}


// ============================================================
// CREATE SESSION
// ============================================================

getSessionId();

console.log(
    "Finz session:",
    getSessionId()
);


// ============================================================
// FILE UPLOAD
// ============================================================

async function upload() {

    const fileInput =
        document.getElementById("file");

    const f =
        fileInput.files[0];


    if (!f) {

        alert(
            "Choose the dataset first"
        );

        return;
    }


    const status =
        document.getElementById("status");

    status.textContent =
        "Uploading...";


    try {

        // ----------------------------------------------------
        // Convert file to Base64
        // ----------------------------------------------------

        const base64 =
            await new Promise(
                (resolve, reject) => {

                    const reader =
                        new FileReader();


                    reader.onload = () => {

                        try {

                            const parts =
                                reader.result.split(",");


                            if (
                                parts.length < 2
                            ) {

                                reject(
                                    new Error(
                                        "Could not read the file."
                                    )
                                );

                                return;
                            }


                            resolve(
                                parts[1]
                            );

                        } catch (error) {

                            reject(error);
                        }
                    };


                    reader.onerror = () => {

                        reject(
                            new Error(
                                "Failed to read the selected file."
                            )
                        );
                    };


                    reader.readAsDataURL(f);
                }
            );


        // ----------------------------------------------------
        // Upload
        // ----------------------------------------------------

        const r =
            await apiFetch(
                "/api/upload-json",
                {
                    method: "POST",

                    headers: {
                        "Content-Type":
                            "application/json"
                    },

                    body: JSON.stringify({
                        filename: f.name,
                        data: base64
                    })
                }
            );


        const responseText =
            await r.text();


        let d = null;


        if (responseText) {

            try {

                d =
                    JSON.parse(
                        responseText
                    );

            } catch (error) {

                status.textContent =
                    `Upload failed: HTTP ${r.status} ${r.statusText}`;


                console.error(
                    "Invalid JSON response:",
                    responseText
                );

                return;
            }
        }


        if (!r.ok) {

            status.textContent =
                `Upload failed: ${
                    d?.detail ||
                    `HTTP ${r.status}`
                }`;

            return;
        }


        if (
            d &&
            d.ok
        ) {

            status.textContent =
                `Loaded ${d.rows} transactions`;


            await refresh();

            return;
        }


        status.textContent =
            "Upload failed: Unexpected server response.";


    } catch (error) {

        console.error(
            "Upload error:",
            error
        );


        status.textContent =
            `Upload failed: ${error.message}`;
    }
}


// ============================================================
// REFRESH EVERYTHING
// ============================================================

async function refresh() {

    await Promise.all([
        loadPnl(),
        loadVariance(),
        loadTx()
    ]);
}


// ============================================================
// MONTHLY P&L
// ============================================================

async function loadPnl() {

    try {

        const r =
            await apiFetch(
                "/api/pnl"
            );


        if (!r.ok) {

            throw new Error(
                `HTTP ${r.status}`
            );
        }


        const d =
            await r.json();


        document.getElementById(
            "pnl"
        ).innerHTML =

            d.length

                ? `

                    <table class="table">

                        <tr>

                            <th>
                                Month
                            </th>

                            <th>
                                Revenue
                            </th>

                            <th>
                                COGS
                            </th>

                            <th>
                                Gross
                            </th>

                            <th>
                                Payroll
                            </th>

                            <th>
                                Opex
                            </th>

                            <th>
                                Operating Profit
                            </th>

                        </tr>


                        ${d.map(x => `

                            <tr>

                                <td>
                                    ${esc(x.month)}
                                </td>

                                <td>
                                    ${fmt(x.revenue)}
                                </td>

                                <td>
                                    ${fmt(x.cogs)}
                                </td>

                                <td>
                                    ${fmt(x.gross_profit)}
                                </td>

                                <td>
                                    ${fmt(x.payroll)}
                                </td>

                                <td>
                                    ${fmt(x.operating_expenses)}
                                </td>

                                <td>

                                    <b>
                                        ${fmt(
                                            x.operating_profit
                                        )}
                                    </b>

                                </td>

                            </tr>

                        `).join("")}

                    </table>

                `

                : "Upload data first.";


    } catch (error) {

        console.error(
            "P&L error:",
            error
        );


        document.getElementById(
            "pnl"
        ).textContent =
            "Unable to load P&L.";
    }
}


// ============================================================
// VARIANCE
// ============================================================

async function loadVariance() {

    try {

        const r =
            await apiFetch(
                "/api/variance"
            );


        if (!r.ok) {

            throw new Error(
                `HTTP ${r.status}`
            );
        }


        const d =
            await r.json();


        document.getElementById(
            "variance"
        ).innerHTML =

            d.length

                ? d.map(x => `

                    <div class="row">

                        <b>
                            ${esc(
                                x.from_month
                            )}
                            →
                            ${esc(
                                x.to_month
                            )}
                        </b>

                        <br>

                        Profit change:
                        ${fmt(
                            x.profit_change
                        )}

                        <br>

                        Revenue:
                        ${fmt(
                            x.revenue_change
                        )}

                        |

                        COGS:
                        ${fmt(
                            x.cogs_change
                        )}

                        |

                        Payroll:
                        ${fmt(
                            x.payroll_change
                        )}

                        |

                        Opex:
                        ${fmt(
                            x.opex_change
                        )}

                    </div>

                `).join("")

                : "Upload at least two months.";


    } catch (error) {

        console.error(
            "Variance error:",
            error
        );


        document.getElementById(
            "variance"
        ).textContent =
            "Unable to load variance.";
    }
}


// ============================================================
// TRANSACTIONS
// ============================================================

async function loadTx() {

    try {

        const r =
            await apiFetch(
                "/api/transactions"
            );


        if (!r.ok) {

            throw new Error(
                `HTTP ${r.status}`
            );
        }


        const d =
            await r.json();


        document.getElementById(
            "tx"
        ).innerHTML =

            d.length

                ? `

                    <table class="table">

                        <tr>

                            <th>
                                Date
                            </th>

                            <th>
                                Description
                            </th>

                            <th>
                                Amount
                            </th>

                            <th>
                                Category
                            </th>

                            <th>
                                Review
                            </th>

                        </tr>


                        ${d.slice(
                            0,
                            200
                        ).map(x => `

                            <tr
                                class="${
                                    x.is_review
                                        ? "review"
                                        : ""
                                }"
                            >

                                <td>
                                    ${esc(
                                        x.tx_date
                                    )}
                                </td>


                                <td>
                                    ${esc(
                                        x.description
                                    )}
                                </td>


                                <td>
                                    ${fmt(
                                        x.amount
                                    )}
                                </td>


                                <td>

                                    <select
                                        onchange="cat(
                                            ${x.id},
                                            this.value
                                        )"
                                    >

                                        ${
                                            [
                                                "Revenue",
                                                "Cost of Goods Sold",
                                                "Payroll",
                                                "Operating Expenses"
                                            ]
                                            .map(
                                                category => `

                                                    <option
                                                        value="${esc(
                                                            category
                                                        )}"

                                                        ${
                                                            category ===
                                                            x.category
                                                                ? "selected"
                                                                : ""
                                                        }
                                                    >

                                                        ${esc(
                                                            category
                                                        )}

                                                    </option>

                                                `
                                            )
                                            .join("")
                                        }

                                    </select>

                                </td>


                                <td>

                                    ${
                                        x.is_review
                                            ? "⚠️ Review"
                                            : ""
                                    }

                                </td>

                            </tr>

                        `).join("")}

                    </table>

                `

                : "Upload data first.";


    } catch (error) {

        console.error(
            "Transaction error:",
            error
        );


        document.getElementById(
            "tx"
        ).textContent =
            "Unable to load transactions.";
    }
}


// ============================================================
// CHANGE TRANSACTION CATEGORY
// ============================================================

async function cat(
    id,
    category
) {

    try {

        const r =
            await apiFetch(
                `/api/transactions/${id}/category`,
                {
                    method: "POST",

                    headers: {
                        "Content-Type":
                            "application/json"
                    },

                    body: JSON.stringify({
                        category:
                            category
                    })
                }
            );


        const text =
            await r.text();


        if (!r.ok) {

            console.error(
                "Category update failed:",
                text
            );

            return;
        }


        await refresh();


    } catch (error) {

        console.error(
            "Category update error:",
            error
        );
    }
}


// ============================================================
// AI ANALYST
// ============================================================

async function ask(q) {

    const question =
        q ||
        document.getElementById(
            "q"
        ).value;


    if (!question) {
        return;
    }


    const answerBox =
        document.getElementById(
            "answer"
        );


    answerBox.textContent =
        "Analyzing...";


    try {

        const r =
            await apiFetch(
                "/api/chat",
                {
                    method: "POST",

                    headers: {
                        "Content-Type":
                            "application/json"
                    },

                    body: JSON.stringify({
                        question:
                            question
                    })
                }
            );


        const text =
            await r.text();


        let d = null;


        if (text) {

            try {

                d =
                    JSON.parse(
                        text
                    );

            } catch (error) {

                answerBox.textContent =
                    `AI request failed: HTTP ${r.status}`;

                console.error(
                    "Invalid AI response:",
                    text
                );

                return;
            }
        }


        if (!r.ok) {

            answerBox.textContent =
                d?.detail ||
                `AI request failed: HTTP ${r.status}`;

            return;
        }


        if (!d) {

            answerBox.textContent =
                "AI returned an empty response.";

            return;
        }


        answerBox.textContent =
            `${d.answer || ""}\n\nEvidence: ${
                d.evidence || ""
            }`;


    } catch (error) {

        console.error(
            "AI request error:",
            error
        );


        answerBox.textContent =
            `AI request failed: ${error.message}`;
    }
}


// ============================================================
// FORMAT NUMBER
// ============================================================

function fmt(x) {

    return Number(
        x || 0
    ).toLocaleString(
        undefined,
        {
            maximumFractionDigits: 2
        }
    );
}


// ============================================================
// ESCAPE HTML
// ============================================================

function esc(s) {

    return String(
        s ?? ""
    ).replace(
        /[&<>"']/g,

        m => ({
            "&": "&amp;",
            "<": "&lt;",
            ">": "&gt;",
            '"': "&quot;",
            "'": "&#39;"
        }[m])
    );
}


// ============================================================
// INITIAL LOAD
// ============================================================

window.addEventListener(
    "load",
    async () => {

        // IMPORTANT:
        //
        // DO NOT RESET THE DATABASE HERE.
        //
        // The session is handled by:
        //
        // X-Finz-Session
        //
        // and:
        //
        // ?session_id=
        //
        // Every browser context gets its own ID.


        console.log(
            "Finz session:",
            getSessionId()
        );


        await refresh();
    }
);