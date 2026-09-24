import os, sqlite3, json, re
from pathlib import Path
from typing import Optional
import pandas as pd
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi import Request
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()
BASE = Path(__file__).resolve().parent.parent
DB = BASE / "finz.db"

app = FastAPI(title="Finz AI Financial Review MVP")
app.mount("/static", StaticFiles(directory=BASE/"app"/"static"), name="static")
templates = Jinja2Templates(directory=BASE/"app"/"templates")

def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c = conn()
    c.execute("""CREATE TABLE IF NOT EXISTS transactions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tx_date TEXT, description TEXT, amount REAL,
        category TEXT, confidence REAL, is_review INTEGER DEFAULT 0
    )""")
    c.commit(); c.close()
init_db()

CATEGORY_RULES = [
    ("Revenue", ["sale","sales","revenue","restaurant","customer","pos","payment received","deposit","income"]),
    ("Cost of Goods Sold", ["food","produce","meat","vegetable","supplier","ingredient","inventory","grocery","beverage","coffee"]),
    ("Payroll", ["payroll","salary","wage","employee","staff","pay"]),
    ("Operating Expenses", ["rent","lease","electric","electricity","gas","water","internet","insurance","marketing","advertising","software","bank fee","fee","repair","maintenance","office","tax"]),
]

def guess_category(desc, amount):
    s = str(desc).lower()
    # Credit/inflow is generally revenue unless description strongly suggests transfer/refund.
    for cat, words in CATEGORY_RULES:
        if any(w in s for w in words):
            return cat, 0.95
    if amount > 0:
        return "Revenue", 0.70
    return "Operating Expenses", 0.45

def normalize(df):
    df = df.copy()
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    cols = set(df.columns)
    def pick(candidates):
        for x in candidates:
            if x in cols: return x
        for c in df.columns:
            if any(x in c for x in candidates): return c
        return None
    date_col = pick(["date","transaction_date","trans_date","posted_date"])
    desc_col = pick(["description","details","memo","merchant","transaction_description","name"])
    amt_col = pick(["amount","transaction_amount","value","total"])
    debit_col = pick(["debit","withdrawal","outflow"])
    credit_col = pick(["credit","deposit","inflow"])
    if not date_col or not desc_col:
        raise ValueError(f"Could not identify date/description columns. Columns found: {list(df.columns)}")
    if not amt_col and not (debit_col or credit_col):
        raise ValueError(f"Could not identify amount columns. Columns found: {list(df.columns)}")
    out = pd.DataFrame()
    out["tx_date"] = pd.to_datetime(df[date_col], errors="coerce")
    out["description"] = df[desc_col].fillna("").astype(str)
    if amt_col:
        out["amount"] = pd.to_numeric(df[amt_col].astype(str).str.replace(",","",regex=False).str.replace("$","",regex=False), errors="coerce")
    else:
        debit = pd.to_numeric(df[debit_col], errors="coerce").fillna(0) if debit_col else 0
        credit = pd.to_numeric(df[credit_col], errors="coerce").fillna(0) if credit_col else 0
        out["amount"] = credit - debit
    out = out.dropna(subset=["tx_date","amount"]).reset_index(drop=True)
    cats = out.apply(lambda r: guess_category(r["description"], float(r["amount"])), axis=1)
    out["category"] = [x[0] for x in cats]
    out["confidence"] = [x[1] for x in cats]
    out["is_review"] = (out["confidence"] < 0.6).astype(int)
    return out

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

from fastapi import Request

@app.post("/api/upload-raw")
async def upload_raw(request: Request):
    try:
        raw = await request.body()

        if not raw:
            raise HTTPException(status_code=400, detail="Uploaded file is empty.")

        filename = request.headers.get("x-filename", "upload.csv").lower()

        from io import BytesIO

        if filename.endswith(".xlsx"):
            df = pd.read_excel(BytesIO(raw), engine="openpyxl")
        elif filename.endswith(".csv"):
            df = pd.read_csv(BytesIO(raw))
        else:
            raise HTTPException(
                status_code=400,
                detail="Please upload a .xlsx or .csv file."
            )

        norm = normalize(df)

        c = conn()
        c.execute("DELETE FROM transactions")
        c.executemany(
            """INSERT INTO transactions(
                tx_date,
                description,
                amount,
                category,
                confidence,
                is_review
            ) VALUES(?,?,?,?,?,?)""",
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

        print(f"RAW UPLOAD: successfully stored {len(norm)} transactions")

        return {
            "ok": True,
            "rows": len(norm),
            "columns": [str(col) for col in df.columns]
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"RAW UPLOAD ERROR: {type(e).__name__}: {e}")
        raise HTTPException(
            status_code=400,
            detail=f"Upload failed: {type(e).__name__}: {e}"
        )
@app.get("/api/transactions")
def transactions(limit:int=500):
    c=conn()
    rows=[dict(r) for r in c.execute("SELECT * FROM transactions ORDER BY tx_date DESC LIMIT ?",(limit,))]
    c.close()
    return rows

@app.post("/api/transactions/{tx_id}/category")
def update_category(tx_id:int, body:dict):
    category = body.get("category","").strip()
    if not category: raise HTTPException(400,"Category required")
    c=conn()
    c.execute("UPDATE transactions SET category=?, confidence=1.0, is_review=0 WHERE id=?", (category,tx_id))
    c.commit(); c.close()
    return {"ok":True}

def pnl():
    c=conn()
    df=pd.read_sql_query("SELECT tx_date,amount,category FROM transactions",c)
    c.close()
    if df.empty: return []
    df["tx_date"]=pd.to_datetime(df["tx_date"])
    df["month"]=df["tx_date"].dt.to_period("M").astype(str)
    # Revenue is positive income; expense categories are negative/outflow.
    g=df.groupby(["month","category"])["amount"].sum().unstack(fill_value=0)
    result=[]
    for month,row in g.iterrows():
        rev=float(row.get("Revenue",0))
        cogs=abs(float(row.get("Cost of Goods Sold",0)))
        payroll=abs(float(row.get("Payroll",0)))
        opex=abs(float(row.get("Operating Expenses",0)))
        gross=rev-cogs
        op=gross-payroll-opex
        result.append({"month":month,"revenue":rev,"cogs":cogs,"gross_profit":gross,
                       "payroll":payroll,"operating_expenses":opex,"operating_profit":op})
    return result

@app.get("/api/pnl")
def get_pnl():
    return pnl()

@app.get("/api/variance")
def variance():
    rows=pnl()
    out=[]
    for a,b in zip(rows, rows[1:]):
        out.append({
            "from_month":a["month"],"to_month":b["month"],
            "profit_change":round(b["operating_profit"]-a["operating_profit"],2),
            "revenue_change":round(b["revenue"]-a["revenue"],2),
            "cogs_change":round(b["cogs"]-a["cogs"],2),
            "payroll_change":round(b["payroll"]-a["payroll"],2),
            "opex_change":round(b["operating_expenses"]-a["operating_expenses"],2),
        })
    return out

class ChatIn(BaseModel):
    question: str

def context_for_ai():
    c=conn()
    tx=[dict(r) for r in c.execute("SELECT id,tx_date,description,amount,category FROM transactions ORDER BY tx_date").fetchall()]
    c.close()
    return {"pnl":pnl(),"transactions":tx[:1000]}

@app.post("/api/chat")
def chat(body:ChatIn):
    ctx=context_for_ai()
    q=body.question.lower()
    # Deterministic answers for common demo questions. No LLM is allowed to invent totals.
    if "revenue" in q:
        months=ctx["pnl"]
        if months:
            parts=[f'{m["month"]}: {m["revenue"]:.2f}' for m in months]
            return {"answer":"Verified revenue from transaction data: " + "; ".join(parts), "evidence":"P&L calculated directly from stored transactions."}
    if "payroll" in q:
        parts=[f'{m["month"]}: {m["payroll"]:.2f}' for m in ctx["pnl"]]
        return {"answer":"Verified payroll by month: " + "; ".join(parts), "evidence":"Payroll category totals calculated from transactions."}
    if "profit" in q or "variance" in q or "change" in q:
        vs=variance()
        if vs:
            x=vs[-1]
            return {"answer":f'Operating profit changed by {x["profit_change"]:.2f} from {x["from_month"]} to {x["to_month"]}. Revenue change: {x["revenue_change"]:.2f}; COGS change: {x["cogs_change"]:.2f}; Payroll change: {x["payroll_change"]:.2f}; Operating expense change: {x["opex_change"]:.2f}.',
                    "evidence":"Variance calculated deterministically from monthly P&L."}
    # Optional LLM: send only verified context; model is asked not to calculate.
    key=os.getenv("OPENAI_API_KEY")
    if key:
        try:
            from openai import OpenAI
            client=OpenAI(api_key=key)
            prompt=("You are a financial analyst assistant. Use ONLY the supplied verified context. "
                    "Do not invent or recalculate financial totals. Explain the supplied figures and cite transaction IDs when possible. "
                    "If the context cannot answer, say so.\nQUESTION:\n"+body.question+"\nCONTEXT:\n"+json.dumps(ctx))
            r=client.chat.completions.create(model=os.getenv("OPENAI_MODEL","gpt-4o-mini"),
                                             messages=[{"role":"user","content":prompt}],temperature=0)
            return {"answer":r.choices[0].message.content,"evidence":"Answer grounded in verified application data."}
        except Exception as e:
            return {"answer":"AI provider error. The deterministic financial APIs are still available.","evidence":str(e)}
    return {"answer":"I can answer after the transaction data is loaded. Try: 'What was our revenue?', 'How much did we spend on payroll?', or 'Why did profit change?'.",
            "evidence":"No LLM key configured; deterministic financial endpoints are available."}
