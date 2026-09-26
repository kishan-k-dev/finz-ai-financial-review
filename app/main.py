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

from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Header
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

    response.headers["X-Finz-Session-Isolation"] = "v5"
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
    # Checks query parameter first
    session_id = request.query_params.get("session_id")
    if session_id and session_id.strip():
        return session_id.strip()

    # Checks X-Finz-Session header next
    session_id = request.headers.get("X-Finz-Session")
    if session_id and session_id.strip():
        return session_id.strip()

    # Checks cookies last
    session_id = request.cookies.get("finz_session")
    if session_id and session_id.strip():
        return session_id.strip()

    return None

# ============================================================
# CATEGORY RULES
# ============================================================

REVENUE_WORDS = [
    "pos batch deposit", "food sales", "beverage sales", "sales deposit",
    "customer payment", "customer receipt", "catering invoice payment",
    "restaurant sales", "revenue",
]

COGS_WORDS = [
    "food inventory", "beverage inventory", "food purchase", "beverage purchase",
    "ingredient", "ingredients", "produce", "meat", "vegetable", "supplier",
    "inventory", "grocery", "coffee", "to-go packaging", "packaging", "disposables",
]

PAYROLL_WORDS = [
    "payroll", "salary", "wage", "wages", "employee wages", "staff wages",
    "payroll taxes", "employee benefits",
]

OPEX_WORDS = [
    "rent", "lease", "electric", "electricity", "gas", "water", "internet",
    "phone", "insurance", "marketing", "advertising", "software", "subscription",
    "pos/software", "pos software", "accounting", "bookkeeping", "bank fee",
    "bank fees", "merchant fee", "repair", "repairs", "maintenance", "office",
    "office supplies", "tax", "utilities", "legal", "professional services",
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
    if amount > 0:
        return "Revenue", 0.70
    return "Operating Expenses", 0.45

# ============================================================
# DATA NORMALIZATION
# ============================================================

def normalize(df):
    df = df.copy()
    df.columns = [
        str(c).strip().lower().replace(" ", "_")
        for c in df.columns
    ]
    cols = set(df.columns)

    def pick(candidates):
        for x in candidates:
            if x in cols:
                return x
        for c in df.columns:
            if any(x in c for x in candidates):
                return c
        return None

    date_col = pick(["date", "transaction_date", "trans_date", "posted_date"])
    desc_col = pick(["description", "details", "memo", "merchant", "transaction_description", "name"])
    amt_col = pick(["amount", "transaction_amount", "value", "total"])
    debit_col = pick(["debit", "withdrawal", "outflow"])
    credit_col = pick(["credit", "deposit", "inflow"])

    if not date_col or not desc_col:
        raise ValueError(f"Could not identify date/description columns. Columns found: {list(df.columns)}")

    if not amt_col and not (debit_col or credit_col):
        raise ValueError(f"Could not identify amount columns. Columns found: {list(df.columns)}")

    out = pd.DataFrame()
    out["tx_date"] = pd.to_datetime(df[date_col], errors="coerce")
    out["description"] = df[desc_col].fillna("").astype(str)

    if amt_col:
        out["amount"] = pd.to_numeric(
            df[amt_col].astype(str).str.replace(",", "", regex=False).str.replace("$", "", regex=False).str.strip(),
            errors="coerce",
        )
    else:
        debit = pd.to_numeric(df[debit_col], errors="coerce").fillna(0) if debit_col else 0
        credit = pd.to_numeric(df[credit_col], errors="coerce").fillna(0) if credit_col else 0
        out["amount"] = credit - debit

    out = out.dropna(subset=["tx_date", "amount"]).reset_index(drop=True)

    cats = out.apply(lambda r: guess_category(r["description"], float(r["amount"])), axis=1)
    out["category"] = [x[0] for x in cats]
    out["confidence"] = [x[1] for x in cats]
    out["is_review"] = (out["confidence"] < 0.60).astype(int)
    return out

# ============================================================
# HOME PAGE
# ============================================================

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

def dataframe_from_bytes(raw, filename):
    filename = (filename or "").lower()
    if filename.endswith(".xlsx"):
        return pd.read_excel(BytesIO(raw), engine="openpyxl")
    if filename.endswith(".csv"):
        return pd.read_csv(BytesIO(raw))
    raise HTTPException(status_code=400, detail="Please upload a .xlsx or .csv file.")

def store_dataframe(df, session_id):
    norm = normalize(df)
    c = conn()
    c.execute("DELETE FROM transactions WHERE session_id=?", (session_id,))
    rows = [
        (
            session_id,
            r.tx_date.strftime("%Y-%m-%d"),
            r.description,
            float(r.amount),
            r.category,
            float(r.confidence),
            int(r.is_review),
        )
        for r in norm.itertuples()
    ]
    c.executemany(
        """
        INSERT INTO transactions(session_id, tx_date, description, amount, category, confidence, is_review)
        VALUES(?,?,?,?,?,?,?)
        """,
        rows,
    )
    c.commit()
    c.close()
    return norm

class FileUploadRequest(BaseModel):
    filename: str
    data: str

@app.get("/health")
def health():
    return {"status": "ok", "session_isolation": "v5"}

@app.post("/api/upload-json")
async def upload_json(request: Request, payload: FileUploadRequest):
    session_id = get_session_id(request)
    if not session_id:
        raise HTTPException(status_code=400, detail="Session not initialized. Refresh the page.")
    try:
        if not payload.data:
            raise HTTPException(status_code=400, detail="Uploaded file is empty.")
        raw = base64.b64decode(payload.data, validate=True)
        df = dataframe_from_bytes(raw, payload.filename)
        norm = store_dataframe(df, session_id)
        return {"ok": True, "rows": len(norm), "columns": [str(col) for col in df.columns]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Upload failed: {type(e).__name__}: {e}")

@app.get("/api/transactions")
def transactions(request: Request, limit: int = 500):
    session_id = get_session_id(request)
    if not session_id:
        return []
    c = conn()
    rows = [
        dict(r) for r in c.execute(
            """
            SELECT id, tx_date, description, amount, category, confidence, is_review
            FROM transactions WHERE session_id=? ORDER BY tx_date DESC LIMIT ?
            """,
            (session_id, limit),
        )
    ]
    c.close()
    return rows

@app.post("/api/transactions/{tx_id}/category")
def update_category(request: Request, tx_id: int, body: dict):
    session_id = get_session_id(request)
    if not session_id:
        raise HTTPException(status_code=400, detail="Session not initialized.")
    category = body.get("category", "").strip()
    allowed = {"Revenue", "Cost of Goods Sold", "Payroll", "Operating Expenses"}
    if category not in allowed:
        raise HTTPException(status_code=400, detail="Invalid category.")
    c = conn()
    c.execute(
        "UPDATE transactions SET category=?, confidence=1.0, is_review=0 WHERE id=? AND session_id=?",
        (category, tx_id, session_id),
    )
    c.commit()
    c.close()
    return {"ok": True}

def pnl(session_id):
    if not session_id:
        return []
    c = conn()
    df = pd.read_sql_query("SELECT tx_date, amount, category FROM transactions WHERE session_id=?", c, params=(session_id,))
    c.close()
    if df.empty:
        return []
    df["tx_date"] = pd.to_datetime(df["tx_date"])
    df["month"] = df["tx_date"].dt.to_period("M").astype(str)
    grouped = df.groupby(["month", "category"])["amount"].sum().unstack(fill_value=0)
    result = []
    for month, row in grouped.iterrows():
        revenue = float(row.get("Revenue", 0))
        cogs = abs(float(row.get("Cost of Goods Sold", 0)))
        payroll = abs(float(row.get("Payroll", 0)))
        opex = abs(float(row.get("Operating Expenses", 0)))
        gross = revenue - cogs
        operating_profit = gross - payroll - opex
        result.append({
            "month": month, "revenue": revenue, "cogs": cogs,
            "gross_profit": gross, "payroll": payroll,
            "operating_expenses": opex, "operating_profit": operating_profit
        })
    return result

@app.get("/api/pnl")
def get_pnl(request: Request):
    return pnl(get_session_id(request))

@app.get("/api/variance")
def variance(request: Request):
    rows = pnl(get_session_id(request))
    out = []
    for a, b in zip(rows, rows[1:]):
        out.append({
            "from_month": a["month"], "to_month": b["month"],
            "profit_change": round(b["operating_profit"] - a["operating_profit"], 2),
            "revenue_change": round(b["revenue"] - a["revenue"], 2),
            "cogs_change": round(b["cogs"] - a["cogs"], 2),
            "payroll_change": round(b["payroll"] - a["payroll"], 2),
            "opex_change": round(b["operating_expenses"] - a["operating_expenses"], 2),
        })
    return out

class ChatIn(BaseModel):
    question: str

@app.post("/api/chat")
def chat(request: Request, body: ChatIn):
    session_id = get_session_id(request)
    c = conn()
    tx = [dict(r) for r in c.execute("SELECT id, tx_date, description, amount, category FROM transactions WHERE session_id=? ORDER BY tx_date", (session_id,)).fetchall()]
    c.close()
    ctx = {"pnl": pnl(session_id), "transactions": tx[:1000]}
    
    return {
        "answer": f"Analysis complete for your dataset containing {len(tx)} rows.",
        "evidence": "Deterministic calculation from session records."
    }