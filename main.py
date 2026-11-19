import os
import re
import urllib.parse
import sqlite3
import hashlib
import secrets
import httpx
from fastapi import FastAPI, HTTPException, Response, Cookie
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="VITA X Production Server")

AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://deepseek.com").rstrip("/")
AI_MODEL = os.getenv("AI_MODEL", "deepseek-chat")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "vitax_admin_2026")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0") == "1"

DB_PATH = "vitax.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            plan TEXT DEFAULT 'FREE',
            credits INTEGER DEFAULT 10,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            code TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
    """)
    conn.commit()
    conn.close()

init_db()

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()

def get_current_user(session_token: str = Cookie(default=None)):
    if not session_token:
        return None
    conn = get_db()
    row = conn.execute(
        "SELECT users.* FROM users JOIN sessions ON users.id = sessions.user_id WHERE sessions.token = ?",
        (session_token,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None

class AuthPayload(BaseModel):
    email: str
    password: str

class GeneratePayload(BaseModel):
    prompt: str
    mode: str = "code"
    language: str = "Uzbek"
    existing_code: str = ""

class AdminCreditPayload(BaseModel):
    admin_token: str
    email: str
    plan: str
    credits: int

@app.get("/")
def serve_home():
    return FileResponse("index.html")

@app.get("/manifest.json")
def serve_manifest():
    return FileResponse("manifest.json")

@app.post("/api/register")
def register(payload: AuthPayload, response: Response):
    email = payload.email.strip().lower()
    if "@" not in email or len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Email notoʻgʻri yoki parol 8 belgidan kam.")
    
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, plan, credits) VALUES (?, ?, 'FREE', 10)",
            (email, hash_password(payload.password))
        )
        user_id = cur.lastrowid
        token = secrets.token_hex(32)
        conn.execute("INSERT INTO sessions (token, user_id) VALUES (?, ?)", (token, user_id))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Bu email allaqachon roʻyxatdan oʻtgan.")
    conn.close()

    response.set_cookie(key="session_token", value=token, httponly=True, secure=COOKIE_SECURE, samesite="lax", max_age=2592000)
    return {"status": "ok", "email": email, "plan": "FREE", "credits": 10}

@app.post("/api/login")
def login(payload: AuthPayload, response: Response):
    email = payload.email.strip().lower()
    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE email = ? AND password_hash = ?",
        (email, hash_password(payload.password))
    ).fetchone()
    if not user:
        conn.close()
        raise HTTPException(status_code=401, detail="Email yoki parol notoʻgʻri.")
    
    token = secrets.token_hex(32)
    conn.execute("INSERT INTO sessions (token, user_id) VALUES (?, ?)", (token, user["id"]))
    conn.commit()
    conn.close()

    response.set_cookie(key="session_token", value=token, httponly=True, secure=COOKIE_SECURE, samesite="lax", max_age=2592000)
    return {"status": "ok", "email": user["email"], "plan": user["plan"], "credits": user["credits"]}

@app.post("/api/logout")
def logout(response: Response, session_token: str = Cookie(default=None)):
    if session_token:
        conn = get_db()
        conn.execute("DELETE FROM sessions WHERE token = ?", (session_token,))
        conn.commit()
        conn.close()
    response.delete_cookie("session_token")
    return {"status": "ok"}

@app.get("/api/me")
def get_me(session_token: str = Cookie(default=None)):
    user = get_current_user(session_token)
    if not user:
        return {"authenticated": False}
    conn = get_db()
    projects = conn.execute(
        "SELECT id, title, code, created_at FROM projects WHERE user_id = ? ORDER BY id DESC LIMIT 20",
        (user["id"],)
    ).fetchall()
    conn.close()
    return {
        "authenticated": True,
        "email": user["email"],
        "plan": user["plan"],
        "credits": user["credits"],
        "projects": [dict(p) for p in projects]
    }

@app.post("/api/generate")
async def generate_ai(payload: GeneratePayload, session_token: str = Cookie(default=None)):
    user = get_current_user(session_token)
    if not user:
        raise HTTPException(status_code=401, detail="Avval tizimga kiring.")
    
    if user["plan"] == "FREE" and user["credits"] <= 0:
        raise HTTPException(status_code=402, detail="Bepul kreditlar tugadi. PRO yoki ULTRA tarifiga oʻting.")

    is_code = payload.mode == "code"
    system_prompt = (
        f"You are VITA X ({user['plan']} Engine), an elite AI software engineer. "
        f"Create a complete, responsive, bug-free single-file HTML/CSS/JS web app in {payload.language}. "
        "Return ONLY valid raw HTML starting with <!DOCTYPE html>. No markdown backticks, no thinking tags."
        if is_code else
        f"You are VITA X ({user['plan']} Engine), a smart multilingual AI assistant fluent in 20 languages. Reply clearly and helpfully in {payload.language}."
    )

    user_msg = payload.prompt
    if is_code and payload.existing_code:
        user_msg = f"Update this existing HTML code based on: '{payload.prompt}'.\n\nExisting HTML:\n{payload.existing_code}"

    output_text = ""
    try:
        if AI_API_KEY:
            model_to_use = AI_MODEL
            if user["plan"] == "ULTRA" and "openrouter" in AI_BASE_URL:
                model_to_use = "deepseek/deepseek-v4-pro"

            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    f"{AI_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"},
                    json={
                        "model": model_to_use,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_msg}
                        ]
                    }
                )
                data = resp.json()
                if "error" in data:
                    raise HTTPException(status_code=500, detail=str(data["error"]))
                output_text = data["choices"]["message"]["content"]
        else:
            async with httpx.AsyncClient(timeout=60.0) as client:
                q = urllib.parse.quote(f"{system_prompt}\n\n{user_msg}")
                resp = await client.get(f"https://pollinations.ai{q}")
                output_text = resp.text
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI server xatosi: {str(e)}")

    clean_text = re.sub(r"<think>[\s\S]*?</think>", "", output_text).strip()
    if is_code:
        clean_text = clean_text.replace("```html", "").replace("```", "").strip()

    conn = get_db()
    new_credits = user["credits"]
    if user["plan"] == "FREE":
        new_credits = max(0, user["credits"] - 1)
        conn.execute("UPDATE users SET credits = ? WHERE id = ?", (new_credits, user["id"]))

    if is_code and len(clean_text) > 50:
        title = payload.prompt[:45] + ("..." if len(payload.prompt) > 45 else "")
        conn.execute("INSERT INTO projects (user_id, title, code) VALUES (?, ?, ?)", (user["id"], title, clean_text))

    conn.commit()
    conn.close()

    return {"result": clean_text, "credits": new_credits}

@app.post("/api/admin/credits")
def admin_grant_credits(payload: AdminCreditPayload):
    if not ADMIN_TOKEN or payload.admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Ruxsat yoʻq (Admin token notoʻgʻri).")
    
    email = payload.email.strip().lower()
    plan = payload.plan.upper()
    if plan not in ["FREE", "PRO", "ULTRA"]:
        raise HTTPException(status_code=400, detail="Tarif FREE, PRO yoki ULTRA boʻlishi kerak.")

    conn = get_db()
    cur = conn.execute("UPDATE users SET plan = ?, credits = ? WHERE email = ?", (plan, payload.credits, email))
    conn.commit()
    updated = cur.rowcount
    conn.close()

    if updated == 0:
        raise HTTPException(status_code=404, detail="Bunday email topilmadi.")
    return {"status": "ok", "message": f"{email} uchun {plan} tarifi va {payload.credits} kredit berildi."}
