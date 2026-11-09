"""제조 소기업 MES 서버. 표준 라이브러리만 쓴다.
실행: PYTHONUTF8=1 py server.py [포트]   ->  http://PC주소:8780
데이터는 data/ (mes.db, files/, backup/, config.json). 폴더째 복사하면 이전된다.
"""
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import smtplib
import sqlite3
import sys
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

BASE = Path(__file__).parent
DATA = Path(os.environ.get("MES_DATA", BASE / "data"))
NOW = None                      # 테스트가 오늘 날짜를 고정할 때만 쓴다
LOCK_SECONDS = 60
MAX_UPLOAD = 30 * 1024 * 1024
BLOCKED_EXT = {"exe", "bat", "cmd", "com", "msi", "scr", "vbs", "ps1", "js", "jar", "dll"}
ROLES = {"admin", "office", "field"}
OFFICE = {"admin", "office"}
SESSIONS = {}                   # 토큰 -> (사용자, 만료)
FAILS = {}                      # 이름 -> (실패 횟수, 잠금 해제 시각)
WRITE_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, salt TEXT NOT NULL, pin TEXT NOT NULL, role TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS customers(id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT DEFAULT '', phone TEXT DEFAULT '', biz_doc INTEGER DEFAULT 0, memo TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, code TEXT DEFAULT '', name TEXT NOT NULL, unit TEXT DEFAULT 'EA', price INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS quotes(id INTEGER PRIMARY KEY, no TEXT UNIQUE NOT NULL, customer_id INTEGER NOT NULL REFERENCES customers(id), date TEXT, status TEXT DEFAULT 'draft', note TEXT DEFAULT '', lines TEXT NOT NULL, supply INTEGER, vat INTEGER, total INTEGER, sent_at TEXT);
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, no TEXT UNIQUE NOT NULL, quote_id INTEGER REFERENCES quotes(id), customer_id INTEGER NOT NULL REFERENCES customers(id), lines TEXT NOT NULL, supply INTEGER, vat INTEGER, total INTEGER, start TEXT, due TEXT NOT NULL, ship_addr TEXT NOT NULL, paid INTEGER DEFAULT 0, memo TEXT DEFAULT '', stage TEXT NOT NULL, hours REAL DEFAULT 8, created TEXT, done_at TEXT);
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id), name TEXT, path TEXT, size INTEGER, who TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY, title TEXT NOT NULL, due TEXT DEFAULT '', category TEXT DEFAULT '', owner TEXT NOT NULL, shared INTEGER DEFAULT 0, done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS feedback(id INTEGER PRIMARY KEY, text TEXT NOT NULL, who TEXT, at TEXT, status TEXT DEFAULT '접수');
CREATE TABLE IF NOT EXISTS log(id INTEGER PRIMARY KEY, at TEXT, who TEXT, what TEXT);
"""

FIELDS = {
    "customers": [("name", "s", 1), ("email", "email", 0), ("phone", "s", 0), ("biz_doc", "bool", 0), ("memo", "s", 0)],
    "items": [("code", "s", 0), ("name", "s", 1), ("unit", "s", 0), ("price", "int", 0)],
}


class Err(Exception):
    def __init__(self, status, msg, code=""):
        self.status, self.msg, self.code = status, msg, code


class Resp:
    def __init__(self, body, ctype="application/json; charset=utf-8", status=200, headers=None):
        self.body, self.ctype, self.status, self.headers = body, ctype, status, headers or {}


# ---------- 공통 ----------
def today():
    return NOW or date.today()


def iso(d):
    return d.isoformat()


def db():
    con = sqlite3.connect(DATA / "mes.db", timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init():
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "files").mkdir(exist_ok=True)
    con = db()
    con.executescript(SCHEMA)
    con.commit()
    con.close()


def rows(con, sql, *a):
    return [dict(r) for r in con.execute(sql, a)]


def one(con, sql, *a):
    r = con.execute(sql, a).fetchone()
    return dict(r) if r else None


def log(con, who, what):
    con.execute("INSERT INTO log(at,who,what) VALUES(?,?,?)", (datetime.now().isoformat(timespec="seconds"), who, what))


def presets():
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted((BASE / "presets").glob("*.json"))}


def load_cfg():
    p = DATA / "config.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_cfg(cfg):
    (DATA / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")


def val(kind, v, req, name):
    if isinstance(v, (list, dict)):
        raise Err(400, f"{name}: 입력 형식이 잘못됐습니다")
    if kind == "s":
        v = str(v if v is not None else "").strip()[:500]
    elif kind == "int":
        try:
            v = int(v)
        except (TypeError, ValueError):
            raise Err(400, f"{name}: 숫자로 입력하세요")
        if v < 0:
            raise Err(400, f"{name}: 0 이상이어야 합니다")
    elif kind == "num":
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise Err(400, f"{name}: 숫자로 입력하세요")
        if v <= 0:
            raise Err(400, f"{name}: 0보다 커야 합니다")
    elif kind == "bool":
        v = 1 if v in (1, True, "1", "true") else 0
    elif kind == "date":
        v = str(v or "").strip()
        if v:
            try:
                datetime.strptime(v, "%Y-%m-%d")
            except ValueError:
                raise Err(400, f"{name}: 날짜 형식은 2026-01-31 입니다")
    elif kind == "email":
        v = str(v or "").strip()[:200]
        if v and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
            raise Err(400, f"{name}: 이메일 형식이 아닙니다")
    if req and v in ("", None):
        raise Err(400, f"{name}을(를) 입력하세요")
    return v


def norm_lines(lines, con):
    if not isinstance(lines, list) or not lines:
        raise Err(400, "품목을 한 개 이상 넣으세요")
    out = []
    for l in lines:
        if not isinstance(l, dict):
            raise Err(400, "품목 형식이 잘못됐습니다")
        it = one(con, "SELECT * FROM items WHERE id=?", val("int", l["item_id"], 1, "품목")) if l.get("item_id") else None
        name = str(l.get("name") or (it["name"] if it else "")).strip()[:200]
        if not name:
            raise Err(400, "품목명을 입력하세요")
        price = val("int", l["price"] if l.get("price") not in (None, "") else (it["price"] if it else 0), 0, "단가")
        qty = val("num", l.get("qty"), 1, "수량")
        out.append({"name": name, "unit": str(l.get("unit") or (it["unit"] if it else "EA"))[:10], "qty": qty, "price": price,
                    "amount": int(qty * price + 0.5)})
    return out


def totals(lines):
    supply = sum(l["amount"] for l in lines)
    vat = (supply + 5) // 10
    return supply, vat, supply + vat


def next_no(con, table, prefix):
    stamp = prefix + today().strftime("%Y%m%d") + "-"
    n = con.execute(f"SELECT COUNT(*) FROM {table} WHERE no LIKE ?", (stamp + "%",)).fetchone()[0] + 1
    while con.execute(f"SELECT 1 FROM {table} WHERE no=?", (f"{stamp}{n:02d}",)).fetchone():
        n += 1
    return f"{stamp}{n:02d}"


def expand(r):
    if r and "lines" in r:
        r["lines"] = json.loads(r["lines"])
    return r


def hash_pin(pin, salt):
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), 100_000).hex()


def make_user(con, name, pin, role):
    name, pin = val("s", name, 1, "이름"), str(pin or "")
    if len(pin) < 4:
        raise Err(400, "PIN은 4자리 이상이어야 합니다")
    if role not in ROLES:
        raise Err(400, "권한 값이 잘못됐습니다")
    salt = secrets.token_hex(16)
    try:
        cur = con.execute("INSERT INTO users(name,salt,pin,role) VALUES(?,?,?,?)", (name, salt, hash_pin(pin, salt), role))
    except sqlite3.IntegrityError:
        raise Err(409, "같은 이름의 사용자가 있습니다")
    return cur.lastrowid


def open_orders(con):
    return rows(con, "SELECT o.*, c.name AS customer FROM orders o JOIN customers c ON c.id=o.customer_id WHERE o.done_at IS NULL ORDER BY o.due, o.id")


# ---------- 라우팅 ----------
R = []


def route(method, pat, roles="any"):
    def deco(f):
        R.append((method, re.compile(f"^{pat}$"), f, roles))
        return f
    return deco


class Ctx:
    def __init__(self, user, body, query, con, handler):
        self.user, self.body, self.query, self.con, self.h = user, body, query, con, handler

    @property
    def me(self):
        return self.user["name"]


def need_office(c):
    if c.user["role"] not in OFFICE:
        raise Err(403, "권한이 없습니다")


@route("GET", "/api/status", None)
def status(c):
    cfg = load_cfg()
    n = c.con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    out = {"setup_needed": n == 0, "company": cfg.get("company", ""), "user": c.h.user()}
    if n == 0:
        out["presets"] = [{"key": k, "label": v["label"]} for k, v in presets().items()]
    return out


@route("POST", "/api/setup", None)
def setup(c):
    if c.con.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        raise Err(403, "이미 설정되었습니다")
    b, ps = c.body, presets()
    if b.get("preset") not in ps:
        raise Err(400, "업종을 선택하세요")
    cfg = dict(ps[b["preset"]], preset=b["preset"], company=val("s", b.get("company"), 1, "회사명"), smtp={})
    uid = make_user(c.con, b.get("name"), b.get("pin"), "admin")
    save_cfg(cfg)
    log(c.con, b.get("name"), "최초 설정")
    return login_as(c.con, uid)


def login_as(con, uid):
    u = one(con, "SELECT id,name,role FROM users WHERE id=?", uid)
    tok = secrets.token_urlsafe(32)
    SESSIONS[tok] = (u, time.time() + 12 * 3600)
    return Resp(json.dumps(u, ensure_ascii=False).encode(), headers={"Set-Cookie": f"sid={tok}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200"})


@route("POST", "/api/login", None)
def login(c):
    name, pin = str(c.body.get("name", "")), str(c.body.get("pin", ""))
    if len(FAILS) > 10_000:          # ponytail: 이름별 실패 기록 상한, 넘치면 비운다
        FAILS.clear()
    n, until = FAILS.get(name, (0, 0))
    if n >= 5 and time.time() < until:
        raise Err(429, f"로그인 시도가 너무 많습니다. {int(until - time.time()) + 1}초 뒤에 다시 하세요")
    u = one(c.con, "SELECT * FROM users WHERE name=?", name)
    if not u or not hmac.compare_digest(u["pin"], hash_pin(pin, u["salt"])):
        FAILS[name] = (n + 1 if time.time() >= until or n < 5 else 1, time.time() + LOCK_SECONDS)
        raise Err(401, "이름 또는 PIN이 맞지 않습니다")
    FAILS.pop(name, None)
    return login_as(c.con, u["id"])


@route("POST", "/api/logout")
def logout(c):
    for t, (u, _) in list(SESSIONS.items()):
        if u["id"] == c.user["id"]:
            del SESSIONS[t]
    return Resp(b'{"ok":true}', headers={"Set-Cookie": "sid=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})


@route("GET", "/api/me")
def me(c):
    return c.user


@route("GET", "/api/config")
def get_config(c):
    cfg = load_cfg()
    smtp = cfg.pop("smtp", {})
    if c.user["role"] == "admin":
        cfg["smtp"] = {**smtp, "password": "", "has_password": bool(smtp.get("password"))}
    return cfg


@route("PUT", "/api/config", {"admin"})
def put_config(c):
    cfg, b = load_cfg(), c.body
    if "company" in b:
        cfg["company"] = val("s", b["company"], 1, "회사명")
    if "daily_capacity" in b:
        cfg["daily_capacity"] = val("num", b["daily_capacity"], 1, "하루 작업 가능 시간")
    if "stages" in b:
        if not isinstance(b["stages"], list):
            raise Err(400, "공정 단계 형식이 잘못됐습니다")
        st = [str(s).strip() for s in b["stages"] if str(s).strip()]
        if len(st) < 2 or len(set(st)) != len(st) or len(st) > 12:
            raise Err(400, "공정 단계는 서로 다른 이름 2~12개여야 합니다")
        bad = {o["stage"] for o in open_orders(c.con)} - set(st)
        if bad:
            raise Err(409, f"진행 중인 주문이 쓰는 공정은 지울 수 없습니다: {', '.join(sorted(bad))}")
        cfg["stages"] = st
    if "smtp" in b:
        s = b["smtp"]
        if not isinstance(s, dict):
            raise Err(400, "메일 설정 형식이 잘못됐습니다")
        old = cfg.get("smtp", {})
        port = val("int", s.get("port") or 587, 1, "포트")
        if not 1 <= port <= 65535:
            raise Err(400, "포트는 1~65535 사이여야 합니다")
        cfg["smtp"] = {"host": str(s.get("host", "")).strip(), "port": port, "user": str(s.get("user", "")).strip(),
                       "password": s.get("password") or old.get("password", ""), "from": str(s.get("from", "")).strip()}
    save_cfg(cfg)
    log(c.con, c.me, "설정 변경")
    return {"ok": True}


# ----- 거래처·품목 -----
def crud(table):
    spec = FIELDS[table]

    @route("GET", f"/api/{table}")
    def lst(c):
        return rows(c.con, f"SELECT * FROM {table} ORDER BY name")

    @route("POST", f"/api/{table}", OFFICE)
    def add(c):
        d = {k: val(t, c.body.get(k), r, k) for k, t, r in spec}
        cur = c.con.execute(f"INSERT INTO {table}({','.join(d)}) VALUES({','.join('?' * len(d))})", list(d.values()))
        return {"id": cur.lastrowid}

    @route("PUT", f"/api/{table}/(\\d+)", OFFICE)
    def upd(c, i):
        d = {k: val(t, c.body[k], r, k) for k, t, r in spec if k in c.body}
        if d:
            c.con.execute(f"UPDATE {table} SET {','.join(k + '=?' for k in d)} WHERE id=?", [*d.values(), i])
        return {"ok": True}

    @route("DELETE", f"/api/{table}/(\\d+)", OFFICE)
    def rm(c, i):
        try:
            c.con.execute(f"DELETE FROM {table} WHERE id=?", (i,))
        except sqlite3.IntegrityError:
            raise Err(409, "거래 내역이 있어 삭제할 수 없습니다")
        return {"ok": True}


crud("customers")
crud("items")


# ----- 견적 -----
def quote_row(con, i):
    q = one(con, "SELECT q.*, c.name AS customer, c.email FROM quotes q JOIN customers c ON c.id=q.customer_id WHERE q.id=?", i)
    if not q:
        raise Err(404, "견적을 찾을 수 없습니다")
    return expand(q)


@route("GET", "/api/quotes")
def quotes(c):
    return [expand(q) for q in rows(c.con, "SELECT q.*, c.name AS customer FROM quotes q JOIN customers c ON c.id=q.customer_id ORDER BY q.id DESC")]


@route("POST", "/api/quotes", OFFICE)
def add_quote(c):
    b = c.body
    cust = val("int", b.get("customer_id"), 1, "거래처")
    if not one(c.con, "SELECT 1 AS x FROM customers WHERE id=?", cust):
        raise Err(400, "거래처를 선택하세요")
    lines = norm_lines(b.get("lines"), c.con)
    s, v, t = totals(lines)
    no = next_no(c.con, "quotes", "Q")
    cur = c.con.execute("INSERT INTO quotes(no,customer_id,date,note,lines,supply,vat,total) VALUES(?,?,?,?,?,?,?,?)",
                        (no, cust, iso(today()), val("s", b.get("note"), 0, "비고"), json.dumps(lines, ensure_ascii=False), s, v, t))
    log(c.con, c.me, f"견적 작성 {no}")
    return {"id": cur.lastrowid, "no": no, "supply": s, "vat": v, "total": t}


@route("PUT", "/api/quotes/(\\d+)", OFFICE)
def upd_quote(c, i):
    q = quote_row(c.con, i)
    if q["status"] == "ordered":
        raise Err(409, "주문으로 넘어간 견적은 고칠 수 없습니다")
    b = c.body
    cust = val("int", b.get("customer_id", q["customer_id"]), 1, "거래처")
    if not one(c.con, "SELECT 1 AS x FROM customers WHERE id=?", cust):
        raise Err(400, "거래처를 선택하세요")
    lines = norm_lines(b.get("lines", q["lines"]), c.con)
    s, v, t = totals(lines)
    c.con.execute("UPDATE quotes SET customer_id=?, note=?, lines=?, supply=?, vat=?, total=? WHERE id=?",
                  (cust, val("s", b.get("note", q["note"]), 0, "비고"), json.dumps(lines, ensure_ascii=False), s, v, t, i))
    return {"supply": s, "vat": v, "total": t}


@route("DELETE", "/api/quotes/(\\d+)", OFFICE)
def del_quote(c, i):
    if quote_row(c.con, i)["status"] != "draft":
        raise Err(409, "발송했거나 주문이 된 견적은 지울 수 없습니다")
    c.con.execute("DELETE FROM quotes WHERE id=?", (i,))
    return {"ok": True}


def smtp_send(smtp, msg):
    cls = smtplib.SMTP_SSL if int(smtp["port"]) == 465 else smtplib.SMTP
    with cls(smtp["host"], int(smtp["port"]), timeout=20) as s:
        if cls is smtplib.SMTP:
            s.starttls()
        if smtp.get("user"):
            s.login(smtp["user"], smtp["password"])
        s.send_message(msg)


@route("POST", "/api/quotes/(\\d+)/mail", OFFICE)
def mail_quote(c, i):
    q, cfg = quote_row(c.con, i), load_cfg()
    smtp = cfg.get("smtp") or {}
    if not smtp.get("host") or not smtp.get("from"):
        raise Err(409, "메일 설정이 없습니다. 설정 화면에서 보내는 메일 서버를 먼저 입력하세요", "no_smtp")
    if not q["email"]:
        raise Err(409, "거래처에 이메일이 없습니다. 거래처 정보에 이메일을 넣으세요", "no_email")
    msg = EmailMessage()
    company = re.sub(r"[\r\n]+", " ", cfg.get("company", ""))
    msg["Subject"], msg["From"], msg["To"] = f"[{company}] 견적서 {q['no']}", smtp["from"], q["email"]
    body = [f"{q['customer']} 담당자님, 안녕하세요.", f"{cfg.get('company', '')}입니다. 요청하신 견적을 보내드립니다.", ""]
    body += [f"- {l['name']}  {l['qty']:g}{l['unit']} x {l['price']:,}원 = {l['amount']:,}원" for l in q["lines"]]
    body += ["", f"공급가액 {q['supply']:,}원 / 부가세 {q['vat']:,}원 / 합계 {q['total']:,}원", q["note"], "", "감사합니다."]
    msg.set_content("\n".join(body))
    try:
        smtp_send(smtp, msg)
    except Exception as e:
        raise Err(502, f"메일 발송에 실패했습니다: {e}", "smtp_fail")
    c.con.execute("UPDATE quotes SET status='sent', sent_at=? WHERE id=? AND status='draft'", (iso(today()), i))
    c.con.execute("UPDATE quotes SET sent_at=? WHERE id=?", (iso(today()), i))
    log(c.con, c.me, f"견적 메일 발송 {q['no']} -> {q['email']}")
    return {"ok": True, "to": q["email"]}


# ----- 주문·작업지시 -----
def order_rows(con, where="", *a):
    out = []
    for o in rows(con, f"SELECT o.*, c.name AS customer FROM orders o JOIN customers c ON c.id=o.customer_id {where} ORDER BY o.due, o.id", *a):
        o = expand(o)
        o["files"] = rows(con, "SELECT id,name,size,at FROM files WHERE order_id=? ORDER BY id", o["id"])
        out.append(o)
    return out


@route("GET", "/api/orders")
def orders(c):
    return order_rows(c.con)


@route("POST", "/api/orders", OFFICE)
def add_order(c):
    b, cfg = c.body, load_cfg()
    if b.get("quote_id"):
        q = quote_row(c.con, val("int", b["quote_id"], 1, "견적"))
        if q["status"] == "ordered":
            raise Err(409, "이미 주문으로 넘어간 견적입니다")
        cust, lines = q["customer_id"], q["lines"]
    else:
        q, cust, lines = None, val("int", b.get("customer_id"), 1, "거래처"), norm_lines(b.get("lines"), c.con)
    due = val("date", b.get("due"), 1, "납기일")
    addr = val("s", b.get("ship_addr"), 1, "배송지")
    start = val("date", b.get("start"), 0, "시작일") or iso(today())
    if due < start:
        raise Err(400, "납기일이 시작일보다 빠릅니다")
    customer = one(c.con, "SELECT * FROM customers WHERE id=?", cust)
    if not customer:
        raise Err(400, "거래처를 찾을 수 없습니다")
    if not customer["biz_doc"] and not b.get("force"):
        raise Err(409, "사업자등록증 확인이 안 된 거래처입니다. 확인했다면 계속 진행하세요", "biz_doc")
    s, v, t = totals(lines)
    no = next_no(c.con, "orders", "O")
    cur = c.con.execute(
        "INSERT INTO orders(no,quote_id,customer_id,lines,supply,vat,total,start,due,ship_addr,paid,memo,stage,hours,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (no, q["id"] if q else None, cust, json.dumps(lines, ensure_ascii=False), s, v, t, start, due, addr, val("bool", b.get("paid"), 0, ""),
         val("s", b.get("memo"), 0, "특이사항"), cfg["stages"][0], val("num", b.get("hours", 8), 1, "예상 작업시간"), iso(today())))
    if q:
        c.con.execute("UPDATE quotes SET status='ordered' WHERE id=?", (q["id"],))
    log(c.con, c.me, f"주문 등록 {no}")
    return {"id": cur.lastrowid, "no": no, "total": t}


def order_or_404(con, i):
    o = one(con, "SELECT * FROM orders WHERE id=?", i)
    if not o:
        raise Err(404, "주문을 찾을 수 없습니다")
    return o


@route("PUT", "/api/orders/(\\d+)", OFFICE)
def upd_order(c, i):
    o, b, d = order_or_404(c.con, i), c.body, {}
    for k, kind, req in [("due", "date", 1), ("start", "date", 1), ("ship_addr", "s", 1), ("memo", "s", 0), ("paid", "bool", 0), ("hours", "num", 1)]:
        if k in b:
            d[k] = val(kind, b[k], req, k)
    if d.get("due", o["due"]) < d.get("start", o["start"]):
        raise Err(400, "납기일이 시작일보다 빠릅니다")
    if d:
        c.con.execute(f"UPDATE orders SET {','.join(k + '=?' for k in d)} WHERE id=?", [*d.values(), i])
        log(c.con, c.me, f"주문 수정 {o['no']} {','.join(d)}")
    return {"ok": True}


@route("PUT", "/api/orders/(\\d+)/stage")
def set_stage(c, i):
    o, st = order_or_404(c.con, i), load_cfg()["stages"]
    new = c.body.get("stage")
    if new not in st:
        raise Err(400, "없는 공정 단계입니다")
    c.con.execute("UPDATE orders SET stage=?, done_at=? WHERE id=?", (new, iso(today()) if new == st[-1] else None, i))
    log(c.con, c.me, f"공정 {o['no']}: {o['stage']} -> {new}")
    return {"ok": True, "done": new == st[-1]}


@route("DELETE", "/api/orders/(\\d+)", {"admin"})
def del_order(c, i):
    o = order_or_404(c.con, i)
    if o["stage"] != load_cfg()["stages"][0]:
        raise Err(409, "작업이 시작된 주문은 지울 수 없습니다")
    for f in rows(c.con, "SELECT path FROM files WHERE order_id=?", i):
        (DATA / "files" / f["path"]).unlink(missing_ok=True)
    c.con.execute("DELETE FROM files WHERE order_id=?", (i,))
    c.con.execute("DELETE FROM orders WHERE id=?", (i,))
    if o["quote_id"]:
        c.con.execute("UPDATE quotes SET status='draft' WHERE id=?", (o["quote_id"],))
    log(c.con, c.me, f"주문 삭제 {o['no']}")
    return {"ok": True}


# ----- 파일 -----
def safe_name(raw):
    n = os.path.basename(unquote(raw or "").replace("\\", "/"))
    n = re.sub(r"[\x00-\x1f<>:\"|?*]", "", n).strip(". ")[:120]
    return n or "file"


@route("POST", "/api/orders/(\\d+)/files", OFFICE)
def upload(c, i):
    order_or_404(c.con, i)
    name = safe_name(c.h.headers.get("X-Filename"))
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in BLOCKED_EXT:
        raise Err(400, "실행 파일은 올릴 수 없습니다")
    data = c.body
    if not data:
        raise Err(400, "빈 파일입니다")
    path = uuid.uuid4().hex + ("." + ext if re.fullmatch(r"[a-z0-9]{1,8}", ext) else "")
    (DATA / "files" / path).write_bytes(data)
    cur = c.con.execute("INSERT INTO files(order_id,name,path,size,who,at) VALUES(?,?,?,?,?,?)",
                        (i, name, path, len(data), c.me, datetime.now().isoformat(timespec="seconds")))
    return {"id": cur.lastrowid, "name": name}


@route("GET", "/api/files/(\\d+)")
def download(c, i):
    f = one(c.con, "SELECT * FROM files WHERE id=?", i)
    if not f or not (DATA / "files" / f["path"]).is_file():
        raise Err(404, "파일이 없습니다")
    return Resp((DATA / "files" / f["path"]).read_bytes(), "application/octet-stream",
                headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(f['name'])}"})


@route("DELETE", "/api/files/(\\d+)", OFFICE)
def del_file(c, i):
    f = one(c.con, "SELECT * FROM files WHERE id=?", i)
    if f:
        (DATA / "files" / f["path"]).unlink(missing_ok=True)
        c.con.execute("DELETE FROM files WHERE id=?", (i,))
    return {"ok": True}


# ----- 오늘·부하 -----
@route("GET", "/api/today")
def today_view(c):
    t = today()
    t_iso, soon_iso = iso(t), iso(t + timedelta(days=3))
    keep = lambda o: {k: o[k] for k in ("id", "no", "customer", "due", "stage", "total", "paid")}
    op = open_orders(c.con)
    unpaid = rows(c.con, "SELECT o.id,o.no,c.name AS customer,o.due,o.stage,o.total,o.paid FROM orders o JOIN customers c ON c.id=o.customer_id WHERE o.paid=0 ORDER BY o.due")
    stale = rows(c.con, "SELECT q.id,q.no,c.name AS customer,q.sent_at,q.total FROM quotes q JOIN customers c ON c.id=q.customer_id WHERE q.status='sent' AND q.sent_at<=? ORDER BY q.sent_at", iso(t - timedelta(days=7)))
    tasks = rows(c.con, "SELECT * FROM tasks WHERE done=0 AND due!='' AND due<=? AND (owner=? OR shared=1) ORDER BY due", soon_iso, c.me)
    return {"today": t_iso, "late": [keep(o) for o in op if o["due"] < t_iso], "soon": [keep(o) for o in op if t_iso <= o["due"] <= soon_iso],
            "open_count": len(op), "unpaid": unpaid, "unpaid_total": sum(u["total"] for u in unpaid), "stale_quotes": stale, "tasks": tasks}


def workdays(s, e):
    out, d = [], s
    while d <= e:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    if not out:                 # 주말만 걸친 일정은 다음 평일에 몰아 넣는다
        d = e
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out = [d]
    return out


@route("GET", "/api/load")
def load(c):
    t, cap = today(), load_cfg().get("daily_capacity", 16)
    n = max(1, min(90, val("int", c.query.get("days", ["28"])[0], 1, "일수")))
    per = {}
    ops = open_orders(c.con)
    for o in ops:
        s = max(datetime.strptime(o["start"], "%Y-%m-%d").date(), t)
        e = max(datetime.strptime(o["due"], "%Y-%m-%d").date(), t)
        ds = workdays(s, e)
        for d in ds:
            per[d] = per.get(d, 0) + o["hours"] / len(ds)
    days = []
    for k in range(n):
        d = t + timedelta(days=k)
        h = round(per.get(d, 0), 2)
        days.append({"date": iso(d), "weekend": d.weekday() >= 5, "hours": h, "cap": cap, "over": h > cap})
    return {"days": days, "capacity": cap,
            "orders": [{k: o[k] for k in ("id", "no", "customer", "start", "due", "stage", "hours")} for o in ops]}


# ----- 업무 · 개선요청 -----
@route("GET", "/api/tasks")
def tasks_list(c):
    return rows(c.con, "SELECT * FROM tasks WHERE owner=? OR shared=1 ORDER BY done, CASE WHEN due='' THEN 1 ELSE 0 END, due, id", c.me)


@route("POST", "/api/tasks")
def task_add(c):
    b = c.body
    cur = c.con.execute("INSERT INTO tasks(title,due,category,owner,shared) VALUES(?,?,?,?,?)",
                        (val("s", b.get("title"), 1, "할 일"), val("date", b.get("due"), 0, "기한"), val("s", b.get("category"), 0, "분류"), c.me, val("bool", b.get("shared"), 0, "")))
    return {"id": cur.lastrowid}


@route("PUT", "/api/tasks/(\\d+)")
def task_upd(c, i):
    t = one(c.con, "SELECT * FROM tasks WHERE id=?", i)
    if not t or not (t["owner"] == c.me or t["shared"]):
        raise Err(404, "업무를 찾을 수 없습니다")
    own = t["owner"] == c.me or c.user["role"] == "admin"
    d = {}
    for k, kind, req in [("title", "s", 1), ("due", "date", 0), ("category", "s", 0), ("shared", "bool", 0), ("done", "bool", 0)]:
        if k in c.body:
            if k != "done" and not own:
                raise Err(403, "다른 사람의 업무는 완료 표시만 할 수 있습니다")
            d[k] = val(kind, c.body[k], req, k)
    if d:
        c.con.execute(f"UPDATE tasks SET {','.join(k + '=?' for k in d)} WHERE id=?", [*d.values(), i])
    return {"ok": True}


@route("DELETE", "/api/tasks/(\\d+)")
def task_del(c, i):
    t = one(c.con, "SELECT * FROM tasks WHERE id=?", i)
    if t and not (t["owner"] == c.me or c.user["role"] == "admin"):
        raise Err(403, "내 업무만 지울 수 있습니다")
    c.con.execute("DELETE FROM tasks WHERE id=?", (i,))
    return {"ok": True}


@route("GET", "/api/feedback")
def fb_list(c):
    return rows(c.con, "SELECT * FROM feedback ORDER BY id DESC")


@route("POST", "/api/feedback")
def fb_add(c):
    cur = c.con.execute("INSERT INTO feedback(text,who,at) VALUES(?,?,?)", (val("s", c.body.get("text"), 1, "내용"), c.me, iso(today())))
    return {"id": cur.lastrowid}


@route("PUT", "/api/feedback/(\\d+)", OFFICE)
def fb_upd(c, i):
    st = c.body.get("status")
    if st not in ("접수", "진행", "완료"):
        raise Err(400, "상태 값이 잘못됐습니다")
    c.con.execute("UPDATE feedback SET status=? WHERE id=?", (st, i))
    return {"ok": True}


# ----- 사용자 · 내보내기 · 백업 -----
@route("GET", "/api/users", {"admin"})
def users(c):
    return rows(c.con, "SELECT id,name,role FROM users ORDER BY id")


@route("POST", "/api/users", {"admin"})
def user_add(c):
    return {"id": make_user(c.con, c.body.get("name"), c.body.get("pin"), c.body.get("role"))}


@route("PUT", "/api/users/(\\d+)", {"admin"})
def user_upd(c, i):
    u = one(c.con, "SELECT * FROM users WHERE id=?", i)
    if not u:
        raise Err(404, "사용자를 찾을 수 없습니다")
    b = c.body
    if "role" in b:
        if b["role"] not in ROLES:
            raise Err(400, "권한 값이 잘못됐습니다")
        if u["role"] == "admin" and b["role"] != "admin" and c.con.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0] < 2:
            raise Err(409, "관리자가 한 명뿐이라 권한을 바꿀 수 없습니다")
        c.con.execute("UPDATE users SET role=? WHERE id=?", (b["role"], i))
    if b.get("pin"):
        if len(str(b["pin"])) < 4:
            raise Err(400, "PIN은 4자리 이상이어야 합니다")
        salt = secrets.token_hex(16)
        c.con.execute("UPDATE users SET salt=?, pin=? WHERE id=?", (salt, hash_pin(str(b["pin"]), salt), i))
    for t, (su, _) in list(SESSIONS.items()):
        if su["id"] == i:
            del SESSIONS[t]
    return {"ok": True}


@route("DELETE", "/api/users/(\\d+)", {"admin"})
def user_del(c, i):
    u = one(c.con, "SELECT * FROM users WHERE id=?", i)
    if u and (u["id"] == c.user["id"]):
        raise Err(409, "자기 자신은 지울 수 없습니다")
    c.con.execute("DELETE FROM users WHERE id=?", (i,))
    for t, (su, _) in list(SESSIONS.items()):
        if su["id"] == i:
            del SESSIONS[t]
    return {"ok": True}


def cell(v):
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in "=+-@" else s       # 엑셀 수식 주입 방지


@route("GET", "/api/export/(customers|items|quotes|orders)\\.csv", OFFICE)
def export(c, name):
    if name in ("quotes", "orders"):
        data = [{**{k: v for k, v in r.items() if k not in ("lines", "files")}, "품목": "; ".join(f"{l['name']} {l['qty']:g}{l['unit']}" for l in r["lines"])}
                for r in (quotes(c) if name == "quotes" else orders(c))]
    else:
        data = rows(c.con, f"SELECT * FROM {name}")
    buf = io.StringIO()
    if data:
        w = csv.DictWriter(buf, fieldnames=list(data[0]))
        w.writeheader()
        w.writerows({k: cell(v) for k, v in r.items()} for r in data)
    return Resp(("﻿" + buf.getvalue()).encode("utf-8"), "text/csv; charset=utf-8",
                headers={"Content-Disposition": f"attachment; filename={name}.csv"})


def backup_to(path):
    src, dst = sqlite3.connect(DATA / "mes.db"), sqlite3.connect(path)
    src.backup(dst)
    src.close()
    dst.close()


def auto_backup():
    d = DATA / "backup"
    d.mkdir(exist_ok=True)
    backup_to(d / f"mes-{today():%Y%m%d}.db")
    for old in sorted(d.glob("mes-*.db"))[:-14]:
        old.unlink()


@route("GET", "/api/backup", {"admin"})
def backup(c):
    tmp = DATA / f"tmp-{uuid.uuid4().hex}.db"
    try:
        backup_to(tmp)
        return Resp(tmp.read_bytes(), "application/octet-stream", headers={"Content-Disposition": f"attachment; filename=mes-{today():%Y%m%d}.db"})
    finally:
        tmp.unlink(missing_ok=True)


# ----- 인쇄 화면 -----
PRINT_CSS = """body{font-family:'Malgun Gothic',sans-serif;margin:24px;color:#111}h1{margin:0 0 4px}table{border-collapse:collapse;width:100%;margin:12px 0}
th,td{border:1px solid #999;padding:6px 8px;text-align:left}td.n,th.n{text-align:right}.meta{margin:2px 0}.btn{margin:12px 0;padding:8px 16px;font-size:16px}
@media print{.btn{display:none}body{margin:0}}"""


def page(title, inner):
    return Resp(f"<!doctype html><meta charset=utf-8><title>{escape(title)}</title><style>{PRINT_CSS}</style>"
                f"<button class=btn onclick='print()'>인쇄</button>{inner}".encode(), "text/html; charset=utf-8")


def lines_table(lines):
    tr = "".join(f"<tr><td>{escape(l['name'])}</td><td class=n>{l['qty']:g} {escape(l['unit'])}</td><td class=n>{l['price']:,}</td><td class=n>{l['amount']:,}</td></tr>" for l in lines)
    return f"<table><tr><th>품목<th class=n>수량<th class=n>단가<th class=n>금액</tr>{tr}</table>"


@route("GET", "/print/quote/(\\d+)")
def print_quote(c, i):
    q, cfg = quote_row(c.con, i), load_cfg()
    return page(f"견적서 {q['no']}", f"<h1>견적서</h1><p class=meta>{escape(cfg.get('company', ''))} · {escape(q['no'])} · {escape(q['date'])}</p>"
                f"<p class=meta><b>{escape(q['customer'])}</b> 귀하</p>{lines_table(q['lines'])}"
                f"<p>공급가액 {q['supply']:,}원 · 부가세 {q['vat']:,}원 · <b>합계 {q['total']:,}원</b></p><p>{escape(q['note'])}</p>")


@route("GET", "/print/order/(\\d+)")
def print_order(c, i):
    o = order_rows(c.con, "WHERE o.id=?", i)
    if not o:
        raise Err(404, "주문을 찾을 수 없습니다")
    o, cfg = o[0], load_cfg()
    steps = "".join(f"<td>{escape(s)}<br>☐</td>" for s in cfg["stages"])
    files = ", ".join(escape(f["name"]) for f in o["files"]) or "없음"
    return page(f"작업지시서 {o['no']}", f"<h1>작업지시서</h1><p class=meta>{escape(cfg.get('company', ''))} · {escape(o['no'])}</p>"
                f"<p class=meta><b>{escape(o['customer'])}</b> · 납기 <b>{escape(o['due'])}</b> · 결제 {'완료' if o['paid'] else '미결제'}</p>"
                f"<p class=meta>배송지: {escape(o['ship_addr'])}</p><p class=meta>특이사항: {escape(o['memo'])}</p>{lines_table(o['lines'])}"
                f"<p class=meta>첨부 파일: {files}</p><table><tr>{steps}</tr></table>")


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "MES"

    def log_message(self, *a):
        pass

    def user(self):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "sid" and v in SESSIONS:
                u, exp = SESSIONS[v]
                if exp > time.time():
                    return u
                del SESSIONS[v]
        return None

    def send(self, r):
        self.send_response(r.status)
        self.send_header("Content-Type", r.ctype)
        self.send_header("Content-Length", str(len(r.body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:")
        for k, v in r.headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(r.body)

    def err(self, status, msg, code=""):
        self.send(Resp(json.dumps({"error": msg, "code": code}, ensure_ascii=False).encode(), status=status))

    def handle_any(self, method):
        u = urlparse(self.path)
        if method == "GET" and u.path in ("/", "/index.html"):
            return self.send(Resp((BASE / "static" / "index.html").read_bytes(), "text/html; charset=utf-8"))
        origin = self.headers.get("Origin")
        if method != "GET" and origin and urlparse(origin).netloc != self.headers.get("Host"):
            return self.err(403, "허용되지 않은 요청입니다")
        for m, pat, f, roles in R:
            mt = pat.match(u.path)
            if m != method or not mt:
                continue
            user = self.user()
            if roles is not None and not user:
                return self.err(401, "로그인이 필요합니다")
            if isinstance(roles, set) and user["role"] not in roles:
                return self.err(403, "권한이 없습니다")
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self.err(400, "요청 형식이 잘못됐습니다")
            upload = u.path.endswith("/files") and method == "POST"
            if n < 0 or n > (MAX_UPLOAD if upload else 1_000_000):
                self.close_connection = True
                return self.err(413, "파일이 너무 큽니다 (최대 30MB)")
            raw = self.rfile.read(n) if n else b""
            try:
                body = raw if upload else (json.loads(raw) if raw else {})
                if not upload and not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self.err(400, "요청 형식이 잘못됐습니다")
            try:
                with (WRITE_LOCK if method != "GET" else threading.Lock()):
                    con = db()
                    try:
                        r = f(Ctx(user, body, parse_qs(u.query), con, self), *[int(g) if g.isdigit() else g for g in mt.groups()])
                        con.commit()
                    finally:
                        con.close()
            except Err as e:
                return self.err(e.status, e.msg, e.code)
            except sqlite3.IntegrityError:
                return self.err(409, "연결된 자료가 있어 처리할 수 없습니다")
            except Exception as e:        # 응답 없이 연결이 끊기지 않게 한다
                print(f"[오류] {method} {u.path}: {e!r}")
                return self.err(500, "서버 오류가 났습니다. 입력값을 확인하고 다시 시도하세요")
            return self.send(r if isinstance(r, Resp) else Resp(json.dumps(r, ensure_ascii=False).encode()))
        self.err(404, "찾을 수 없습니다")

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_PUT(self):
        self.handle_any("PUT")

    def do_DELETE(self):
        self.handle_any("DELETE")


def make_server(port=8780, host="0.0.0.0"):
    init()
    return ThreadingHTTPServer((host, port), Handler)


def backup_loop():
    while True:
        try:
            auto_backup()
        except Exception as e:      # 백업 실패가 서버를 죽이지 않게 한다
            print("백업 실패:", e)
        time.sleep(6 * 3600)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8780
    srv = make_server(port, os.environ.get("MES_HOST", "0.0.0.0"))
    threading.Thread(target=backup_loop, daemon=True).start()
    print(f"실행 중: http://127.0.0.1:{port}  (같은 네트워크의 다른 PC는 이 PC의 IP 주소로 접속)  종료: Ctrl+C")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
