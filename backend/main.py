from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from authlib.integrations.starlette_client import OAuth
from pydantic import BaseModel
import pdfplumber, pytesseract
from PIL import Image
import io, re, os, secrets

app = FastAPI(title="Adit Pay Statement Analyser API")

# ── Session ────────────────────────────────────────────────────────────────────
SESSION_SECRET = os.getenv("SESSION_SECRET", secrets.token_hex(32))
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, https_only=False,
                   same_site="none")   # needed for cross-origin cookie on Render

# ── CORS — allow the frontend Render service ───────────────────────────────────
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL, "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Google OAuth ───────────────────────────────────────────────────────────────
GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
BACKEND_URL          = os.getenv("BACKEND_URL", "http://localhost:8000")
ALLOWED_DOMAIN       = "adit.com"

oauth = OAuth()
oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


def get_current_user(request: Request):
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


# ── Auth routes ────────────────────────────────────────────────────────────────

@app.get("/auth/login")
async def login(request: Request):
    redirect_uri = f"{BACKEND_URL}/auth/callback"
    return await oauth.google.authorize_redirect(request, redirect_uri)


@app.get("/auth/callback")
async def auth_callback(request: Request):
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception:
        return RedirectResponse(url=f"{FRONTEND_URL}/?error=auth_failed")

    user_info = token.get("userinfo") or {}
    email: str = user_info.get("email", "")

    if not email.lower().endswith(f"@{ALLOWED_DOMAIN}"):
        return RedirectResponse(url=f"{FRONTEND_URL}/?error=unauthorized_domain&email={email}")

    request.session["user"] = {
        "email":   email,
        "name":    user_info.get("name", email.split("@")[0]),
        "picture": user_info.get("picture", ""),
    }
    return RedirectResponse(url=FRONTEND_URL)


@app.get("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url=FRONTEND_URL)


@app.get("/api/me")
async def me(request: Request):
    user = request.session.get("user")
    if not user:
        return JSONResponse({"authenticated": False})
    return JSONResponse({"authenticated": True, "user": user})


# ── Calculation logic ──────────────────────────────────────────────────────────
ADIT_RATE_CP, ADIT_AUTH_CP = 0.0225, 0.20
ADIT_RATE_ON, ADIT_AUTH_ON = 0.0290, 0.30


def calc_cp(amount, count):
    tf, af = amount * ADIT_RATE_CP, count * ADIT_AUTH_CP
    return {"type": "Card Present", "amount": amount, "count": count,
            "trn_fee": round(tf,2), "auth_fee": round(af,2),
            "total_fee": round(tf+af,2), "rate_label": "2.25% + $0.20"}


def calc_online(amount, count):
    tf, af = amount * ADIT_RATE_ON, count * ADIT_AUTH_ON
    return {"type": "Online (Card Not Present)", "amount": amount, "count": count,
            "trn_fee": round(tf,2), "auth_fee": round(af,2),
            "total_fee": round(tf+af,2), "rate_label": "2.90% + $0.30"}


def build_analysis(existing_merchant, total_amount, total_count, total_fees_paid, card_present_pct, mode):
    if mode == "card_present_only":
        row = calc_cp(total_amount, total_count)
        rows, adit_total = [row], row["total_fee"]
    else:
        op = 1.0 - card_present_pct
        r_cp = calc_cp(total_amount * card_present_pct, total_count * card_present_pct)
        r_on = calc_online(total_amount * op, total_count * op)
        rows  = [r_cp, r_on]
        adit_total = r_cp["total_fee"] + r_on["total_fee"]

    avg_pct = adit_total / total_amount if total_amount else 0
    ex_pct  = total_fees_paid / total_amount if total_amount else 0
    return {
        "existing_merchant":    existing_merchant,
        "total_amount":         round(total_amount, 2),
        "total_count":          total_count,
        "total_fees_paid":      round(total_fees_paid, 2),
        "existing_avg_fee_pct": round(ex_pct * 100, 4),
        "card_present_pct":     round(card_present_pct * 100, 1),
        "mode":                 mode,
        "adit_rows":            rows,
        "adit_total_fee":       round(adit_total, 2),
        "adit_avg_fee_pct":     round(avg_pct * 100, 4),
        "savings":              round(total_fees_paid - adit_total, 2),
    }


# ── PDF / Image extraction ─────────────────────────────────────────────────────

def extract_pdf(data):
    text = ""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                text += t + "\n"
    return text


def extract_image(data):
    return pytesseract.image_to_string(Image.open(io.BytesIO(data)))


def parse_statement(raw):
    text = raw.replace(",", "").lower()
    total_amount = next((float(m.group(1)) for pat in [
        r"total\s+(?:trn|transaction|sale|gross)\s+(?:amt|amount)[^\d]*(\d+\.?\d*)",
        r"gross\s+sales[^\d]*(\d+\.?\d*)", r"total\s+sales[^\d]*(\d+\.?\d*)",
        r"total\s+amount[^\d]*(\d+\.?\d*)",
    ] if (m := re.search(pat, text))), None)

    total_count = next((int(m.group(1)) for pat in [
        r"(?:no|number|num)\s+(?:of\s+)?(?:trn|transaction)[^\d]*(\d+)",
        r"transaction\s+count[^\d]*(\d+)",
    ] if (m := re.search(pat, text))), None)

    total_fees = next((float(m.group(1)) for pat in [
        r"total\s+(?:fees?|fee\s+paid|trn\s+fee)[^\d]*(\d+\.?\d*)",
        r"processing\s+fee[^\d]*(\d+\.?\d*)",
    ] if (m := re.search(pat, text))), None)

    merchant = "Unknown"
    for pat in [r"merchant\s*(?:name)?[:\s]+([A-Za-z0-9 &.'-]+)",
                r"dba[:\s]+([A-Za-z0-9 &.'-]+)"]:
        m = re.search(pat, raw, re.IGNORECASE)
        if m:
            merchant = m.group(1).strip()[:40]
            break

    return {"merchant": merchant, "total_amount": total_amount,
            "total_count": total_count, "total_fees": total_fees,
            "raw_text": raw[:3000]}


# ── Pydantic ───────────────────────────────────────────────────────────────────

class ManualInput(BaseModel):
    existing_merchant: str
    total_amount:      float
    total_count:       int
    total_fees_paid:   float
    card_present_pct:  float
    mode:              str = "template"


# ── API routes ─────────────────────────────────────────────────────────────────

@app.post("/api/upload")
async def upload_statement(file: UploadFile = File(...), user=Depends(get_current_user)):
    data  = await file.read()
    fname = (file.filename or "").lower()
    ct    = file.content_type or ""
    try:
        if "pdf" in ct or fname.endswith(".pdf"):
            raw = extract_pdf(data)
        elif any(fname.endswith(e) for e in [".png",".jpg",".jpeg",".tiff",".bmp",".webp"]):
            raw = extract_image(data)
        else:
            raise HTTPException(status_code=400, detail="Unsupported file type.")
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Could not read file: {e}")
    return {"extracted": parse_statement(raw), "message": "Review extracted values."}


@app.post("/api/calculate")
async def calculate(inp: ManualInput, user=Depends(get_current_user)):
    try:
        return build_analysis(inp.existing_merchant, inp.total_amount, inp.total_count,
                              inp.total_fees_paid, inp.card_present_pct / 100.0, inp.mode)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.get("/health")
async def health():
    return {"status": "ok"}
import os
import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
