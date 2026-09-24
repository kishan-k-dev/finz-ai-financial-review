import os
import sqlite3
import json
import base64
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


# ============================================================
# FASTAPI APP
# ============================================================

app = FastAPI(
    title="Finz AI Financial Review MVP"
)

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
            tx_date TEXT,
            description TEXT,
            amount REAL,
            category TEXT,
            confidence REAL,
            is_review INTEGER DEFAULT 0
        )
        """
    )

    c.commit()
    c.close()


init_db()


# ============================================================
# CATEGORY RULES
# ============================================================

# IMPORTANT:
# More specific rules come before generic rules.
# This prevents things like "POS/software subscription"
# from being incorrectly classified as Revenue.

REVENUE_WORDS = [
    "pos batch deposit",
    "food sales",
    "beverage sales",
    "sales deposit",
    "customer payment",
    "customer receipt",
    "catering invoice payment",
    "restaurant sales",
    "revenue"
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
    "disposables"
]

PAYROLL_WORDS = [
    "payroll",
    "salary",
    "wage",
    "wages",
    "employee wages",
    "staff wages",
    "payroll taxes",
    "employee benefits"
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
    "professional services"
]


def guess_category(desc, amount):
    """
    Deterministically classify a transaction.

    Positive amounts are generally income.
    Negative amounts are generally expenses.

    Specific description rules are checked first.
    """

    s = str(desc).lower().strip()

    # --------------------------------------------------------
    # REVENUE
    # --------------------------------------------------------

    if any(word in s for word in REVENUE_WORDS):
        return "Revenue", 0.95

    # --------------------------------------------------------
    # COGS
    # --------------------------------------------------------

    if any(word in s for word in COGS_WORDS):
        return "Cost of Goods Sold", 0.95

    # --------------------------------------------------------
    # PAYROLL
    # --------------------------------------------------------

    if any(word in s for word in PAYROLL_WORDS):
        return "Payroll", 0.95

    # --------------------------------------------------------
    # OPERATING EXPENSES
    # --------------------------------------------------------

    if any(word in s for word in OPEX_WORDS):
        return "Operating Expenses", 0.95

    # --------------------------------------------------------
    # FALLBACK
    # --------------------------------------------------------

    if amount > 0:
        return "Revenue", 0.70

    return "Operating Expenses", 0.45


# ============================================================
# DATA NORMALIZATION
# ============================================================

def normalize(df):
    """
    Convert different CSV/XLSX column formats into:

    tx_date
    description
    amount
    category
    confidence
    is_review
    """

    df = df.copy()

    # Normalize column names
    df.columns = [
        str(c).strip().lower().replace(" ", "_")
        for c in df.columns
    ]

    cols = set(df.columns)

    def pick(candidates):
        # Exact match
        for x in candidates:
            if x in cols:
                return x

        # Partial match
        for c in df.columns:
            if any(x in c for x in candidates):
                return c

        return None

    date_col = pick([
        "date",
        "transaction_date",
        "trans_date",
        "posted_date"
    ])

    desc_col = pick([
        "description",
        "details",
        "memo",
        "merchant",
        "transaction_description",
        "name"
    ])

    amt_col = pick([
        "amount",
        "transaction_amount",
        "value",
        "total"
    ])

    debit_col = pick([
        "debit",
        "withdrawal",
        "outflow"
    ])

    credit_col = pick([
        "credit",
        "deposit",
        "inflow"
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

    # Date
    out["tx_date"] = pd.to_datetime(
        df[date_col],
        errors="coerce"
    )

    # Description
    out["description"] = (
        df[desc_col]
        .fillna("")
        .astype(str)
    )

    # Amount
    if amt_col:

        out["amount"] = pd.to_numeric(
            df[amt_col]
            .astype(str)
            .str.replace(",", "", regex=False)
            .str.replace("$", "", regex=False)
            .str.strip(),
            errors="coerce"
        )

    else:

        if debit_col:
            debit = pd.to_numeric(
                df[debit_col],
                errors="coerce"
            ).fillna(0)
        else:
            debit = 0

        if credit_col:
            credit = pd.to_numeric(
                df[credit_col],
                errors="coerce"
            ).fillna(0)
        else:
            credit = 0

        out["amount"] = credit - debit

    # Remove invalid rows
    out = out.dropna(
        subset=["tx_date", "amount"]
    ).reset_index(drop=True)

    # Categorization
    cats = out.apply(
        lambda r: guess_category(
            r["description"],
            float(r["amount"])
        ),
        axis=1
    )

    out["category"] = [
        x[0]
        for x in cats
    ]

    out["confidence"] = [
        x[1]
        for x in cats
    ]

    # Low confidence transactions require review
    out["is_review"] = (
        out["confidence"] < 0.60
    ).astype(int)

    return out


# ============================================================
# HOME PAGE
# ============================================================

@app.get("/", response_class=HTMLResponse)
def home(request: Request):

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request
        }
    )


# ============================================================
# STANDARD MULTIPART UPLOAD
# ============================================================

@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):

    try:

        filename = (
            file.filename or ""
        ).lower()

        raw = await file.read()

        if not raw:
            raise HTTPException(
                status_code=400,
                detail="Uploaded file is empty."
            )

        if filename.endswith(".xlsx"):

            df = pd.read_excel(
                BytesIO(raw),
                engine="openpyxl"
            )

        elif filename.endswith(".csv"):

            df = pd.read_csv(
                BytesIO(raw)
            )

        else:

            raise HTTPException(
                status_code=400,
                detail="Please upload a .xlsx or .csv file."
            )

        norm = normalize(df)

        c = conn()

        c.execute(
            "DELETE FROM transactions"
        )

        c.executemany(
            """
            INSERT INTO transactions(
                tx_date,
                description,
                amount,
                category,
                confidence,
                is_review
            )
            VALUES(?,?,?,?,?,?)
            """,
            [
                (
                    r.tx_date.strftime("%Y-%m-%d"),
                    r.description,
                    float(r.amount),
                    r.category,
                    float(r.confidence),
                    int(r.is_review)
                )
                for r in norm.itertuples()
            ]
        )

        c.commit()
        c.close()

        print(
            f"UPLOAD: successfully stored {len(norm)} transactions"
        )

        return {
            "ok": True,
            "rows": len(norm),
            "columns": [
                str(col)
                for col in df.columns
            ]
        }

    except HTTPException:
        raise

    except Exception as e:

        print(
            f"UPLOAD ERROR: {type(e).__name__}: {e}"
        )

        raise HTTPException(
            status_code=400,
            detail=(
                f"Upload failed: "
                f"{type(e).__name__}: {e}"
            )
        )


# ============================================================
# JSON / BASE64 UPLOAD
# ============================================================

class FileUploadRequest(BaseModel):
    filename: str
    data: str


@app.post("/api/upload-json")
async def upload_json(payload: FileUploadRequest):

    try:

        if not payload.data:

            raise HTTPException(
                status_code=400,
                detail="Uploaded file is empty."
            )

        # Decode Base64
        try:

            raw = base64.b64decode(
                payload.data,
                validate=True
            )

        except Exception:

            raise HTTPException(
                status_code=400,
                detail="Invalid Base64 file data."
            )

        if not raw:

            raise HTTPException(
                status_code=400,
                detail="Uploaded file is empty."
            )

        filename = (
            payload.filename or ""
        ).lower()

        # Read Excel
        if filename.endswith(".xlsx"):

            df = pd.read_excel(
                BytesIO(raw),
                engine="openpyxl"
            )

        # Read CSV
        elif filename.endswith(".csv"):

            df = pd.read_csv(
                BytesIO(raw)
            )

        else:

            raise HTTPException(
                status_code=400,
                detail="Please upload a .xlsx or .csv file."
            )

        # Normalize
        norm = normalize(df)

        # Store transactions
        c = conn()

        c.execute(
            "DELETE FROM transactions"
        )

        c.executemany(
            """
            INSERT INTO transactions(
                tx_date,
                description,
                amount,
                category,
                confidence,
                is_review
            )
            VALUES(?,?,?,?,?,?)
            """,
            [
                (
                    r.tx_date.strftime("%Y-%m-%d"),
                    r.description,
                    float(r.amount),
                    r.category,
                    float(r.confidence),
                    int(r.is_review)
                )
                for r in norm.itertuples()
            ]
        )

        c.commit()
        c.close()

        print(
            f"JSON UPLOAD: successfully stored "
            f"{len(norm)} transactions"
        )

        return {
            "ok": True,
            "rows": len(norm),
            "columns": [
                str(col)
                for col in df.columns
            ]
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
            )
        )


# ============================================================
# RAW UPLOAD
# ============================================================

@app.post("/api/upload-raw")
async def upload_raw(request: Request):

    try:

        raw = await request.body()

        if not raw:

            raise HTTPException(
                status_code=400,
                detail="Uploaded file is empty."
            )

        filename = request.headers.get(
            "x-filename",
            "upload.csv"
        ).lower()

        if filename.endswith(".xlsx"):

            df = pd.read_excel(
                BytesIO(raw),
                engine="openpyxl"
            )

        elif filename.endswith(".csv"):

            df = pd.read_csv(
                BytesIO(raw)
            )

        else:

            raise HTTPException(
                status_code=400,
                detail="Please upload a .xlsx or .csv file."
            )

        norm = normalize(df)

        c = conn()

        c.execute(
            "DELETE FROM transactions"
        )

        c.executemany(
            """
            INSERT INTO transactions(
                tx_date,
                description,
                amount,
                category,
                confidence,
                is_review
            )
            VALUES(?,?,?,?,?,?)
            """,
            [
                (
                    r.tx_date.strftime("%Y-%m-%d"),
                    r.description,
                    float(r.amount),
                    r.category,
                    float(r.confidence),
                    int(r.is_review)
                )
                for r in norm.itertuples()
            ]
        )

        c.commit()
        c.close()

        print(
            f"RAW UPLOAD: successfully stored "
            f"{len(norm)} transactions"
        )

        return {
            "ok": True,
            "rows": len(norm),
            "columns": [
                str(col)
                for col in df.columns
            ]
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
            )
        )


# ============================================================
# TRANSACTIONS
# ============================================================

@app.get("/api/transactions")
def transactions(limit: int = 500):

    c = conn()

    rows = [
        dict(r)
        for r in c.execute(
            """
            SELECT *
            FROM transactions
            ORDER BY tx_date DESC
            LIMIT ?
            """,
            (limit,)
        )
    ]

    c.close()

    return rows


# ============================================================
# UPDATE TRANSACTION CATEGORY
# ============================================================

@app.post("/api/transactions/{tx_id}/category")
def update_category(
    tx_id: int,
    body: dict
):

    category = body.get(
        "category",
        ""
    ).strip()

    allowed = {
        "Revenue",
        "Cost of Goods Sold",
        "Payroll",
        "Operating Expenses"
    }

    if category not in allowed:

        raise HTTPException(
            status_code=400,
            detail="Invalid category."
        )

    c = conn()

    c.execute(
        """
        UPDATE transactions
        SET
            category=?,
            confidence=1.0,
            is_review=0
        WHERE id=?
        """,
        (
            category,
            tx_id
        )
    )

    c.commit()
    c.close()

    return {
        "ok": True
    }


# ============================================================
# P&L CALCULATION
# ============================================================

def pnl():

    c = conn()

    df = pd.read_sql_query(
        """
        SELECT
            tx_date,
            amount,
            category
        FROM transactions
        """,
        c
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

    # Group by month/category
    grouped = (
        df.groupby(
            ["month", "category"]
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
                0
            )
        )

        cogs = abs(
            float(
                row.get(
                    "Cost of Goods Sold",
                    0
                )
            )
        )

        payroll = abs(
            float(
                row.get(
                    "Payroll",
                    0
                )
            )
        )

        opex = abs(
            float(
                row.get(
                    "Operating Expenses",
                    0
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
                "operating_profit": operating_profit
            }
        )

    return result


@app.get("/api/pnl")
def get_pnl():

    return pnl()


# ============================================================
# VARIANCE
# ============================================================

@app.get("/api/variance")
def variance():

    rows = pnl()

    out = []

    for a, b in zip(
        rows,
        rows[1:]
    ):

        out.append(
            {
                "from_month":
                    a["month"],

                "to_month":
                    b["month"],

                "profit_change":
                    round(
                        b["operating_profit"]
                        - a["operating_profit"],
                        2
                    ),

                "revenue_change":
                    round(
                        b["revenue"]
                        - a["revenue"],
                        2
                    ),

                "cogs_change":
                    round(
                        b["cogs"]
                        - a["cogs"],
                        2
                    ),

                "payroll_change":
                    round(
                        b["payroll"]
                        - a["payroll"],
                        2
                    ),

                "opex_change":
                    round(
                        b["operating_expenses"]
                        - a["operating_expenses"],
                        2
                    )
            }
        )

    return out


# ============================================================
# AI CONTEXT
# ============================================================

def context_for_ai():

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
            ORDER BY tx_date
            """
        ).fetchall()
    ]

    c.close()

    return {
        "pnl": pnl(),
        "transactions": tx[:1000]
    }


# ============================================================
# AI CHAT
# ============================================================

class ChatIn(BaseModel):
    question: str


@app.post("/api/chat")
def chat(body: ChatIn):

    ctx = context_for_ai()

    q = body.question.lower().strip()

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
                "answer":
                    "Verified revenue from transaction data: "
                    + "; ".join(parts),

                "evidence":
                    "P&L calculated directly from stored transactions."
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

        return {
            "answer":
                "Verified payroll by month: "
                + "; ".join(parts),

            "evidence":
                "Payroll category totals calculated from transactions."
        }

    # --------------------------------------------------------
    # PROFIT / VARIANCE
    # --------------------------------------------------------

    if (
        "profit" in q
        or "variance" in q
        or "change" in q
    ):

        vs = variance()

        if vs:

            x = vs[-1]

            return {
                "answer":
                    f'Operating profit changed by '
                    f'{x["profit_change"]:.2f} '
                    f'from {x["from_month"]} '
                    f'to {x["to_month"]}. '
                    f'Revenue change: '
                    f'{x["revenue_change"]:.2f}; '
                    f'COGS change: '
                    f'{x["cogs_change"]:.2f}; '
                    f'Payroll change: '
                    f'{x["payroll_change"]:.2f}; '
                    f'Operating expense change: '
                    f'{x["opex_change"]:.2f}.',

                "evidence":
                    "Variance calculated deterministically "
                    "from monthly P&L."
            }

    # --------------------------------------------------------
    # OPTIONAL OPENAI
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
                "You are a financial analyst assistant. "
                "Use ONLY the supplied verified context. "
                "Do not invent or recalculate financial totals. "
                "Explain the supplied figures and cite "
                "transaction IDs when possible. "
                "If the context cannot answer, say so.\n\n"
                "QUESTION:\n"
                + body.question
                + "\n\nCONTEXT:\n"
                + json.dumps(ctx)
            )

            response = client.chat.completions.create(
                model=os.getenv(
                    "OPENAI_MODEL",
                    "gpt-4o-mini"
                ),
                messages=[
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                temperature=0
            )

            return {
                "answer":
                    response.choices[0].message.content,

                "evidence":
                    "Answer grounded in verified application data."
            }

        except Exception as e:

            return {
                "answer":
                    "AI provider error. "
                    "The deterministic financial APIs "
                    "are still available.",

                "evidence":
                    str(e)
            }

    # --------------------------------------------------------
    # DEFAULT RESPONSE
    # --------------------------------------------------------

    return {
        "answer":
            "I can answer after the transaction data "
            "is loaded. Try: 'What was our revenue?', "
            "'How much did we spend on payroll?', "
            "or 'Why did profit change?'.",

        "evidence":
            "No LLM key configured; deterministic "
            "financial endpoints are available."
    }