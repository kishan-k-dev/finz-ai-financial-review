import os
import sqlite3
import json
import base64
import uuid
from pathlib import Path
from io import BytesIO

import pandas as pd
from dotenv import load_dotenv
from pydantic import BaseModel

from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "finz.db"

app = FastAPI(title="Finz AI Financial Review MVP")


# ============================================================
# NO-CACHE MIDDLEWARE
# ============================================================

@app.middleware("http")
async def no_cache_middleware(request: Request, call_next):
    response = await call_next(request)

    path = request.url.path

    if (
        path == "/"
        or path.startswith("/api/")
        or path.startswith("/static/")
    ):
        response.headers["Cache-Control"] = (
            "no-store, no-cache, must-revalidate, max-age=0, private"
        )
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"

    # This helps confirm Railway is serving the new backend.
    response.headers["X-Finz-Session-Isolation"] = "v4"

    return response


# ============================================================
# STATIC FILES + TEMPLATES
# ============================================================

app.mount(
    "/static",
    StaticFiles(directory=BASE / "app" / "static"),
    name="static"
)

templates = Jinja2Templates(
    directory=BASE / "app" / "templates"
)


# ============================================================
# DATABASE
# ============================================================

def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = conn()

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS transactions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            tx_date TEXT,
            description TEXT,
            amount REAL,
            category TEXT,
            confidence REAL,
            is_review INTEGER DEFAULT 0
        )
        """
    )

    # Upgrade older databases that don't have session_id.
    columns = [
        row["name"]
        for row in c.execute(
            "PRAGMA table_info(transactions)"
        ).fetchall()
    ]

    if "session_id" not in columns:
        c.execute(
            "ALTER TABLE transactions ADD COLUMN session_id TEXT"
        )

    c.commit()
    c.close()


init_db()


# ============================================================
# SESSION ISOLATION
# ============================================================

def get_session_id(request: Request):
    """Get the browser/session ID.

    Priority:
    1. URL query parameter:
       /api/pnl?session_id=abc123

       This is the strongest isolation mechanism because
       different browsers generate different API URLs.

    2. X-Finz-Session request header.

    3. finz_session cookie for older clients.
    """

    # --------------------------------------------------------
    # 1. URL QUERY PARAMETER
    # --------------------------------------------------------

    session_id = request.query_params.get("session_id")

    if session_id:
        session_id = session_id.strip()

        if session_id:
            return session_id

    # --------------------------------------------------------
    # 2. CUSTOM HEADER
    # --------------------------------------------------------

    session_id = request.headers.get("X-Finz-Session")

    if session_id:
        session_id = session_id.strip()

        if session_id:
            return session_id

    # --------------------------------------------------------
    # 3. COOKIE FALLBACK
    # --------------------------------------------------------

    session_id = request.cookies.get("finz_session")

    if session_id:
        session_id = session_id.strip()

        if session_id:
            return session_id

    return None


def ensure_session(response, request):
    """Ensure a browser session is available without creating cookies."""

    # Session is controlled by JavaScript localStorage.
    # We intentionally do NOT create or use a cookie.

    return get_session_id(request)


# ============================================================
# CATEGORY RULES
# ============================================================

REVENUE_WORDS = [
    "pos batch deposit",
    "food sales",
    "beverage sales",
    "sales deposit",
    "customer payment",
    "customer receipt",
    "catering invoice payment",
    "restaurant sales",
    "revenue",
]


COGS_WORDS = [
    "food inventory",
    "beverage inventory",
    "food purchase",
    "beverage purchase",
    "ingredient",
    "ingredients",
    "produce",
    "meat",
    "vegetable",
    "supplier",
    "inventory",
    "grocery",
    "coffee",
    "to-go packaging",
    "packaging",
    "disposables",
]


PAYROLL_WORDS = [
    "payroll",
    "salary",
    "wage",
    "wages",
    "employee wages",
    "staff wages",
    "payroll taxes",
    "employee benefits",
]


OPEX_WORDS = [
    "rent",
    "lease",
    "electric",
    "electricity",
    "gas",
    "water",
    "internet",
    "phone",
    "insurance",
    "marketing",
    "advertising",
    "software",
    "subscription",
    "pos/software",
    "pos software",
    "accounting",
    "bookkeeping",
    "bank fee",
    "bank fees",
    "merchant fee",
    "repair",
    "repairs",
    "maintenance",
    "office",
    "office supplies",
    "tax",
    "utilities",
    "legal",
    "professional services",
]


def guess_category(desc, amount):
    s = str(desc).lower().strip()

    if any(word in s for word in REVENUE_WORDS):
        return "Revenue", 0.95

    if any(word in s for word in COGS_WORDS):
        return "Cost of Goods Sold", 0.95

    if any(word in s for word in PAYROLL_WORDS):
        return "Payroll", 0.95

    if any(word in s for word in OPEX_WORDS):
        return "Operating Expenses", 0.95

    # Positive amount with no matching description.
    if amount > 0:
        return "Revenue", 0.70

    # Negative amount with no matching description.
    return "Operating Expenses", 0.45


# ============================================================
# DATA NORMALIZATION
# ============================================================

def normalize(df):
    df = df.copy()

    # Normalize column names.
    df.columns = [
        str(c).strip().lower().replace(" ", "_")
        for c in df.columns
    ]

    cols = set(df.columns)

    def pick(candidates):
        # Exact match first.
        for x in candidates:
            if x in cols:
                return x

        # Partial match second.
        for c in df.columns:
            if any(x in c for x in candidates):
                return c

        return None

    date_col = pick([
        "date",
        "transaction_date",
        "trans_date",
        "posted_date",
    ])

    desc_col = pick([
        "description",
        "details",
        "memo",
        "merchant",
        "transaction_description",
        "name",
    ])

    amt_col = pick([
        "amount",
        "transaction_amount",
        "value",
        "total",
    ])

    debit_col = pick([
        "debit",
        "withdrawal",
        "outflow",
    ])

    credit_col = pick([
        "credit",
        "deposit",
        "inflow",
    ])

    if not date_col or not desc_col:
        raise ValueError(
            "Could not identify date/description columns. "
            f"Columns found: {list(df.columns)}"
        )

    if not amt_col and not (debit_col or credit_col):
        raise ValueError(
            "Could not identify amount columns. "
            f"Columns found: {list(df.columns)}"
        )

    out = pd.DataFrame()

    # --------------------------------------------------------
    # DATE
    # --------------------------------------------------------

    out["tx_date"] = pd.to_datetime(
        df[date_col],
        errors="coerce",
    )

    # --------------------------------------------------------
    # DESCRIPTION
    # --------------------------------------------------------

    out["description"] = (
        df[desc_col]
        .fillna("")
        .astype(str)
    )

    # --------------------------------------------------------
    # AMOUNT
    # --------------------------------------------------------

    if amt_col:
        out["amount"] = pd.to_numeric(
            df[amt_col]
            .astype(str)
            .str.replace(",", "", regex=False)
            .str.replace("$", "", regex=False)
            .str.strip(),
            errors="coerce",
        )

    else:
        if debit_col:
            debit = pd.to_numeric(
                df[debit_col],
                errors="coerce",
            ).fillna(0)
        else:
            debit = 0

        if credit_col:
            credit = pd.to_numeric(
                df[credit_col],
                errors="coerce",
            ).fillna(0)
        else:
            credit = 0

        out["amount"] = credit - debit

    # Remove invalid rows.
    out = out.dropna(
        subset=["tx_date", "amount"]
    ).reset_index(drop=True)

    # --------------------------------------------------------
    # CATEGORIZATION
    # --------------------------------------------------------

    cats = out.apply(
        lambda r: guess_category(
            r["description"],
            float(r["amount"]),
        ),
        axis=1,
    )

    out["category"] = [
        x[0]
        for x in cats
    ]

    out["confidence"] = [
        x[1]
        for x in cats
    ]

    out["is_review"] = (
        out["confidence"] < 0.60
    ).astype(int)

    return out


# ============================================================
# HOME PAGE
# ============================================================

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    response = templates.TemplateResponse(
        "index.html",
        {
            "request": request
        },
    )

    ensure_session(
        response,
        request,
    )

    return response


# ============================================================
# FILE READER
# ============================================================

def dataframe_from_bytes(raw, filename):
    filename = (filename or "").lower()

    if filename.endswith(".xlsx"):
        return pd.read_excel(
            BytesIO(raw),
            engine="openpyxl",
        )

    if filename.endswith(".csv"):
        return pd.read_csv(
            BytesIO(raw)
        )

    raise HTTPException(
        status_code=400,
        detail="Please upload a .xlsx or .csv file.",
    )


# ============================================================
# STORE DATA
# ============================================================

def store_dataframe(df, session_id):
    norm = normalize(df)

    c = conn()

    # VERY IMPORTANT:
    # Delete ONLY this session's old transactions.
    c.execute(
        """
        DELETE FROM transactions
        WHERE session_id=?
        """,
        (session_id,),
    )

    rows = []

    for r in norm.itertuples():
        rows.append(
            (
                session_id,
                r.tx_date.strftime("%Y-%m-%d"),
                r.description,
                float(r.amount),
                r.category,
                float(r.confidence),
                int(r.is_review),
            )
        )

    c.executemany(
        """
        INSERT INTO transactions(
            session_id,
            tx_date,
            description,
            amount,
            category,
            confidence,
            is_review
        )
        VALUES(?,?,?,?,?,?,?)
        """,
        rows,
    )

    c.commit()
    c.close()

    return norm


# ============================================================
# STANDARD MULTIPART UPLOAD
# ============================================================

@app.post("/api/upload")
async def upload(
    request: Request,
    file: UploadFile = File(...),
):
    session_id = get_session_id(request)

    if not session_id:
        raise HTTPException(
            status_code=400,
            detail="Session not initialized. Refresh the page.",
        )

    try:
        filename = (
            file.filename or ""
        ).lower()

        raw = await file.read()

        if not raw:
            raise HTTPException(
                status_code=400,
                detail="Uploaded file is empty.",
            )

        df = dataframe_from_bytes(
            raw,
            filename,
        )

        norm = store_dataframe(
            df,
            session_id,
        )

        print(
            f"UPLOAD: session={session_id[:8]} "
            f"stored {len(norm)} transactions"
        )

        return {
            "ok": True,
            "rows": len(norm),
            "columns": [
                str(col)
                for col in df.columns
            ],
        }

    except HTTPException:
        raise

    except Exception as e:
        print(
            f"UPLOAD ERROR: "
            f"{type(e).__name__}: {e}"
        )

        raise HTTPException(
            status_code=400,
            detail=(
                f"Upload failed: "
                f"{type(e).__name__}: {e}"
            ),
        )


# ============================================================
# JSON / BASE64 UPLOAD
# ============================================================

class FileUploadRequest(BaseModel):
    filename: str
    data: str


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
def health():
    return {
        "status": "ok",
        "session_isolation": "v4",
    }


# ============================================================
# DEBUG SESSION
# ============================================================

@app.get("/api/debug/session")
def debug_session(request: Request):
    query_session = request.query_params.get(
        "session_id"
    )

    header_session = request.headers.get(
        "X-Finz-Session"
    )

    cookie_session = request.cookies.get(
        "finz_session"
    )

    effective_session = get_session_id(
        request
    )

    if not effective_session:
        return {
            "query_session": (
                query_session[:12]
                if query_session
                else None
            ),
            "header_session": (
                header_session[:12]
                if header_session
                else None
            ),
            "cookie_session": (
                cookie_session[:12]
                if cookie_session
                else None
            ),
            "effective_session": None,
            "rows": 0,
            "isolation": "v4",
        }

    c = conn()

    row = c.execute(
        """
        SELECT COUNT(*) AS count
        FROM transactions
        WHERE session_id=?
        """,
        (effective_session,),
    ).fetchone()

    c.close()

    return {
        "query_session": (
            query_session[:12]
            if query_session
            else None
        ),
        "header_session": (
            header_session[:12]
            if header_session
            else None
        ),
        "cookie_session": (
            cookie_session[:12]
            if cookie_session
            else None
        ),
        "effective_session": (
            effective_session[:12]
        ),
        "rows": int(row["count"]),
        "isolation": "v4",
    }


# ============================================================
# BASE64 JSON UPLOAD
# ============================================================

@app.post("/api/upload-json")
async def upload_json(
    request: Request,
    payload: FileUploadRequest,
):
    session_id = get_session_id(request)

    if not session_id:
        raise HTTPException(
            status_code=400,
            detail="Session not initialized. Refresh the page.",
        )

    try:

        if not payload.data:
            raise HTTPException(
                status_code=400,
                detail="Uploaded file is empty.",
            )

        # ----------------------------------------------------
        # DECODE BASE64
        # ----------------------------------------------------

        try:
            raw = base64.b64decode(
                payload.data,
                validate=True,
            )

        except Exception:
            raise HTTPException(
                status_code=400,
                detail="Invalid Base64 file data.",
            )

        if not raw:
            raise HTTPException(
                status_code=400,
                detail="Uploaded file is empty.",
            )

        # ----------------------------------------------------
        # READ DATAFRAME
        # ----------------------------------------------------

        df = dataframe_from_bytes(
            raw,
            payload.filename,
        )

        # ----------------------------------------------------
        # STORE ONLY FOR THIS SESSION
        # ----------------------------------------------------

        norm = store_dataframe(
            df,
            session_id,
        )

        print(
            f"JSON UPLOAD: session={session_id[:8]} "
            f"stored {len(norm)} transactions"
        )

        return {
            "ok": True,
            "rows": len(norm),
            "columns": [
                str(col)
                for col in df.columns
            ],
        }

    except HTTPException:
        raise

    except Exception as e:

        print(
            f"JSON UPLOAD ERROR: "
            f"{type(e).__name__}: {e}"
        )

        raise HTTPException(
            status_code=400,
            detail=(
                f"Upload failed: "
                f"{type(e).__name__}: {e}"
            ),
        )


# ============================================================
# RAW UPLOAD
# ============================================================

@app.post("/api/upload-raw")
async def upload_raw(request: Request):
    session_id = get_session_id(request)

    if not session_id:
        raise HTTPException(
            status_code=400,
            detail="Session not initialized. Refresh the page.",
        )

    try:

        raw = await request.body()

        if not raw:
            raise HTTPException(
                status_code=400,
                detail="Uploaded file is empty.",
            )

        filename = request.headers.get(
            "x-filename",
            "upload.csv",
        ).lower()

        df = dataframe_from_bytes(
            raw,
            filename,
        )

        norm = store_dataframe(
            df,
            session_id,
        )

        print(
            f"RAW UPLOAD: session={session_id[:8]} "
            f"stored {len(norm)} transactions"
        )

        return {
            "ok": True,
            "rows": len(norm),
            "columns": [
                str(col)
                for col in df.columns
            ],
        }

    except HTTPException:
        raise

    except Exception as e:

        print(
            f"RAW UPLOAD ERROR: "
            f"{type(e).__name__}: {e}"
        )

        raise HTTPException(
            status_code=400,
            detail=(
                f"Upload failed: "
                f"{type(e).__name__}: {e}"
            ),
        )


# ============================================================
# TRANSACTIONS
# ============================================================

@app.get("/api/transactions")
def transactions(
    request: Request,
    limit: int = 500,
):
    session_id = get_session_id(request)

    if not session_id:
        return []

    c = conn()

    rows = [
        dict(r)
        for r in c.execute(
            """
            SELECT
                id,
                tx_date,
                description,
                amount,
                category,
                confidence,
                is_review
            FROM transactions
            WHERE session_id=?
            ORDER BY tx_date DESC
            LIMIT ?
            """,
            (
                session_id,
                limit,
            ),
        )
    ]

    c.close()

    return rows


# ============================================================
# UPDATE TRANSACTION CATEGORY
# ============================================================

@app.post("/api/transactions/{tx_id}/category")
def update_category(
    request: Request,
    tx_id: int,
    body: dict,
):
    session_id = get_session_id(request)

    if not session_id:
        raise HTTPException(
            status_code=400,
            detail="Session not initialized.",
        )

    category = body.get(
        "category",
        "",
    ).strip()

    allowed = {
        "Revenue",
        "Cost of Goods Sold",
        "Payroll",
        "Operating Expenses",
    }

    if category not in allowed:
        raise HTTPException(
            status_code=400,
            detail="Invalid category.",
        )

    c = conn()

    c.execute(
        """
        UPDATE transactions
        SET
            category=?,
            confidence=1.0,
            is_review=0
        WHERE
            id=?
            AND session_id=?
        """,
        (
            category,
            tx_id,
            session_id,
        ),
    )

    c.commit()
    c.close()

    return {
        "ok": True
    }


# ============================================================
# P&L CALCULATION
# ============================================================

def pnl(session_id):

    if not session_id:
        return []

    c = conn()

    df = pd.read_sql_query(
        """
        SELECT
            tx_date,
            amount,
            category
        FROM transactions
        WHERE session_id=?
        """,
        c,
        params=(session_id,),
    )

    c.close()

    if df.empty:
        return []

    df["tx_date"] = pd.to_datetime(
        df["tx_date"]
    )

    df["month"] = (
        df["tx_date"]
        .dt.to_period("M")
        .astype(str)
    )

    grouped = (
        df.groupby(
            [
                "month",
                "category",
            ]
        )["amount"]
        .sum()
        .unstack(
            fill_value=0
        )
    )

    result = []

    for month, row in grouped.iterrows():

        revenue = float(
            row.get(
                "Revenue",
                0,
            )
        )

        cogs = abs(
            float(
                row.get(
                    "Cost of Goods Sold",
                    0,
                )
            )
        )

        payroll = abs(
            float(
                row.get(
                    "Payroll",
                    0,
                )
            )
        )

        opex = abs(
            float(
                row.get(
                    "Operating Expenses",
                    0,
                )
            )
        )

        gross = (
            revenue
            - cogs
        )

        operating_profit = (
            gross
            - payroll
            - opex
        )

        result.append(
            {
                "month": month,
                "revenue": revenue,
                "cogs": cogs,
                "gross_profit": gross,
                "payroll": payroll,
                "operating_expenses": opex,
                "operating_profit": operating_profit,
            }
        )

    return result


# ============================================================
# RESET CURRENT SESSION ONLY
# ============================================================

@app.post("/api/reset")
def reset_data(request: Request):

    session_id = get_session_id(request)

    if not session_id:
        return {
            "ok": True,
            "deleted": 0,
        }

    c = conn()

    c.execute(
        """
        DELETE FROM transactions
        WHERE session_id=?
        """,
        (session_id,),
    )

    deleted = c.rowcount

    c.commit()
    c.close()

    print(
        f"RESET: session={session_id[:8]} "
        f"deleted={deleted}"
    )

    return {
        "ok": True,
        "deleted": deleted,
    }


# ============================================================
# P&L API
# ============================================================

@app.get("/api/pnl")
def get_pnl(request: Request):

    session_id = get_session_id(request)

    return pnl(
        session_id
    )


# ============================================================
# VARIANCE
# ============================================================

@app.get("/api/variance")
def variance(request: Request):

    session_id = get_session_id(request)

    rows = pnl(
        session_id
    )

    out = []

    for a, b in zip(
        rows,
        rows[1:],
    ):

        out.append(
            {
                "from_month": a["month"],
                "to_month": b["month"],

                "profit_change": round(
                    b["operating_profit"]
                    - a["operating_profit"],
                    2,
                ),

                "revenue_change": round(
                    b["revenue"]
                    - a["revenue"],
                    2,
                ),

                "cogs_change": round(
                    b["cogs"]
                    - a["cogs"],
                    2,
                ),

                "payroll_change": round(
                    b["payroll"]
                    - a["payroll"],
                    2,
                ),

                "opex_change": round(
                    b["operating_expenses"]
                    - a["operating_expenses"],
                    2,
                ),
            }
        )

    return out


# ============================================================
# AI CONTEXT
# ============================================================

def context_for_ai(session_id):

    if not session_id:
        return {
            "pnl": [],
            "transactions": [],
        }

    c = conn()

    tx = [
        dict(r)
        for r in c.execute(
            """
            SELECT
                id,
                tx_date,
                description,
                amount,
                category
            FROM transactions
            WHERE session_id=?
            ORDER BY tx_date
            """,
            (session_id,),
        ).fetchall()
    ]

    c.close()

    return {
        "pnl": pnl(
            session_id
        ),
        "transactions": tx[:1000],
    }


# ============================================================
# AI CHAT
# ============================================================

class ChatIn(BaseModel):
    question: str


@app.post("/api/chat")
def chat(
    request: Request,
    body: ChatIn,
):

    session_id = get_session_id(request)

    ctx = context_for_ai(
        session_id
    )

    q = (
        body.question
        .lower()
        .strip()
    )

    # --------------------------------------------------------
    # REVENUE
    # --------------------------------------------------------

    if "revenue" in q:

        months = ctx["pnl"]

        if months:

            parts = [
                f'{m["month"]}: '
                f'{m["revenue"]:.2f}'
                for m in months
            ]

            return {
                "answer": (
                    "Verified revenue from "
                    "transaction data: "
                    + "; ".join(parts)
                ),
                "evidence": (
                    "P&L calculated directly "
                    "from stored transactions."
                ),
            }

    # --------------------------------------------------------
    # PAYROLL
    # --------------------------------------------------------

    if "payroll" in q:

        parts = [
            f'{m["month"]}: '
            f'{m["payroll"]:.2f}'
            for m in ctx["pnl"]
        ]

        if parts:

            return {
                "answer": (
                    "Verified payroll by month: "
                    + "; ".join(parts)
                ),
                "evidence": (
                    "Payroll category totals "
                    "calculated from transactions."
                ),
            }

    # --------------------------------------------------------
    # PROFIT / VARIANCE
    # --------------------------------------------------------

    if (
        "profit" in q
        or "variance" in q
        or "change" in q
    ):

        rows = ctx["pnl"]

        if len(rows) >= 2:

            a = rows[-2]
            b = rows[-1]

            profit_change = (
                b["operating_profit"]
                - a["operating_profit"]
            )

            revenue_change = (
                b["revenue"]
                - a["revenue"]
            )

            cogs_change = (
                b["cogs"]
                - a["cogs"]
            )

            payroll_change = (
                b["payroll"]
                - a["payroll"]
            )

            opex_change = (
                b["operating_expenses"]
                - a["operating_expenses"]
            )

            return {
                "answer": (
                    f'Operating profit changed by '
                    f'{profit_change:.2f} '
                    f'from {a["month"]} '
                    f'to {b["month"]}. '

                    f'Revenue change: '
                    f'{revenue_change:.2f}; '

                    f'COGS change: '
                    f'{cogs_change:.2f}; '

                    f'Payroll change: '
                    f'{payroll_change:.2f}; '

                    f'Operating expense change: '
                    f'{opex_change:.2f}.'
                ),

                "evidence": (
                    "Variance calculated "
                    "deterministically from "
                    "monthly P&L."
                ),
            }

    # --------------------------------------------------------
    # OPENAI
    # --------------------------------------------------------

    key = os.getenv(
        "OPENAI_API_KEY"
    )

    if key:

        try:

            from openai import OpenAI

            client = OpenAI(
                api_key=key
            )

            prompt = (
                "You are a financial analyst "
                "assistant. "

                "Use ONLY the supplied verified "
                "context. "

                "Do not invent or recalculate "
                "financial totals. "

                "Explain the supplied figures "
                "and cite transaction IDs "
                "when possible. "

                "If the context cannot answer, "
                "say so.\n\n"

                "QUESTION:\n"
                + body.question
                + "\n\nCONTEXT:\n"
                + json.dumps(ctx)
            )

            response = (
                client.chat.completions.create(
                    model=os.getenv(
                        "OPENAI_MODEL",
                        "gpt-4o-mini",
                    ),
                    messages=[
                        {
                            "role": "user",
                            "content": prompt,
                        }
                    ],
                    temperature=0,
                )
            )

            return {
                "answer": (
                    response
                    .choices[0]
                    .message
                    .content
                ),
                "evidence": (
                    "Answer grounded in "
                    "verified application data."
                ),
            }

        except Exception as e:

            return {
                "answer": (
                    "AI provider error. "
                    "The deterministic "
                    "financial APIs are "
                    "still available."
                ),
                "evidence": str(e),
            }

    # --------------------------------------------------------
    # DEFAULT
    # --------------------------------------------------------

    return {
        "answer": (
            "I can answer after the "
            "transaction data is loaded. "
            "Try: 'What was our revenue?', "
            "'How much did we spend on "
            "payroll?', or "
            "'Why did profit change?'."
        ),

        "evidence": (
            "No LLM key configured; "
            "deterministic financial "
            "endpoints are available."
        ),
    }