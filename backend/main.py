from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from authlib.integrations.starlette_client import OAuth
from pydantic import BaseModel
import pdfplumber, pytesseract
from PIL import Image
import io, re, os, secrets

app = FastAPI(title="Adit Pay Statement Analyser API")

# ── Session ─────────────────────────────────────────────────
SESSION_SECRET = os.getenv("SESSION_SECRET", secrets.token_hex(32))
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    https_only=False,
    same_site="none"
)

# ── CORS ────────────────────────────────────────────────────
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL, "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Google OAuth ─────────────────────────────────────────────
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

# ── Auth Routes ──────────────────────────────────────────────

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
    email = user_info.get("email", "")

    if not email.lower().endswith(f"@{ALLOWED_DOMAIN}"):
        return RedirectResponse(url=f"{FRONTEND_URL}/?error=unauthorized_domain")

    request.session["user"] = {
        "email": email,
        "name": user_info.get("name", email.split("@")[0]),
        "picture": user_info.get("picture", "")
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
        return {"authenticated": False}
    return {"authenticated": True, "user": user}

# ── Calculation Logic ────────────────────────────────────────

ADIT_RATE_CP, ADIT_AUTH_CP = 0.0225, 0.20
ADIT_RATE_ON, ADIT_AUTH_ON = 0.0290, 0.30

def calc_cp(amount, count):
    tf, af = amount * ADIT_RATE_CP, count * ADIT_AUTH_CP
    return {"type": "Card Present", "total_fee": round(tf + af, 2)}

def calc_online(amount, count):
    tf, af = amount * ADIT_RATE_ON, count * ADIT_AUTH_ON
    return {"type": "Online", "total_fee": round(tf + af, 2)}

# ── File Extraction ──────────────────────────────────────────

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

# ── API Routes ───────────────────────────────────────────────

@app.post("/api/upload")
async def upload(file: UploadFile = File(...), user=Depends(get_current_user)):
    data = await file.read()
    fname = file.filename.lower()

    if fname.endswith(".pdf"):
        text = extract_pdf(data)
    else:
        text = extract_image(data)

    return {"message": "File processed", "preview": text[:500]}

@app.get("/health")
def health():
    return {"status": "ok"}

# ── Serve Frontend (CRITICAL FIX) ────────────────────────────

app.mount("/assets", StaticFiles(directory="frontend/dist/assets"), name="assets")

@app.get("/")
async def serve_frontend():
    return FileResponse("frontend/dist/index.html")

# ── Run App ─────────────────────────────────────────────────

import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
