"""합격 기준 AC1~AC13 검증. 실행: PYTHONUTF8=1 py test_mes.py  (가짜 데이터·임시 폴더만 쓴다)"""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import server

TMP = tempfile.mkdtemp()
server.DATA = Path(TMP)
server.NOW = date(2026, 10, 26)          # 월요일
server.LOCK_SECONDS = 1
SRV = server.make_server(0, "127.0.0.1")
PORT = SRV.server_address[1]
threading.Thread(target=SRV.serve_forever, daemon=True).start()


class Client:
    def __init__(self):
        self.cookie = ""

    def call(self, method, path, body=None, raw=None, headers=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data, method=method)
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            r = urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:
            r = e
        sc = r.headers.get("Set-Cookie")
        if sc:
            self.cookie = sc.split(";")[0]
        b = r.read()
        try:
            return r.status, json.loads(b)
        except ValueError:
            return r.status, b

    def login(self, name, pin):
        return self.call("POST", "/api/login", {"name": name, "pin": pin})


admin, office, field = Client(), Client(), Client()


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # AC1
        assert admin.call("GET", "/api/status")[1]["setup_needed"]
        s, _ = admin.call("POST", "/api/setup", {"company": "테스트공장", "preset": "metal", "name": "사장", "pin": "1234"})
        assert s == 200
        assert admin.call("GET", "/api/me")[1]["role"] == "admin"
        admin.call("POST", "/api/users", {"name": "사무", "pin": "1111", "role": "office"})
        admin.call("POST", "/api/users", {"name": "현장", "pin": "2222", "role": "field"})
        office.login("사무", "1111")
        field.login("현장", "2222")
        cls.cust = office.call("POST", "/api/customers", {"name": "동서물산", "email": "a@b.co", "biz_doc": 1})[1]["id"]
        cls.cust2 = office.call("POST", "/api/customers", {"name": "미확인상사", "biz_doc": 0})[1]["id"]
        cls.item = office.call("POST", "/api/items", {"name": "브라켓", "unit": "EA", "price": 1050})[1]["id"]

    def mkquote(self, cust=None, qty=3):
        return office.call("POST", "/api/quotes", {"customer_id": cust or self.cust, "lines": [{"item_id": self.item, "qty": qty}]})[1]

    def mkorder(self, due="2026-10-30", hours=8, paid=0, cust=None, **kw):
        q = self.mkquote(cust)
        return office.call("POST", "/api/orders", {"quote_id": q["id"], "due": due, "ship_addr": "부산", "hours": hours, "paid": paid, **kw})

    def test_ac1_setup_once(self):
        self.assertEqual(Client().call("POST", "/api/setup", {"company": "x", "preset": "metal", "name": "a", "pin": "9999"})[0], 403)
        self.assertIn("가공", admin.call("GET", "/api/config")[1]["stages"])

    def test_ac2_auth(self):
        c = Client()
        for p in ("/api/orders", "/api/me", "/api/today", "/api/backup", "/print/quote/1"):
            self.assertEqual(c.call("GET", p)[0], 401, p)
        self.assertEqual(c.login("사장", "0000")[0], 401)
        self.assertEqual(c.login("없는사람", "1234")[0], 401)
        for _ in range(5):
            c.login("현장", "x")
        self.assertEqual(c.login("현장", "2222")[0], 429)
        import time
        time.sleep(1.2)
        self.assertEqual(c.login("현장", "2222")[0], 200)

    def test_ac3_roles(self):
        self.assertEqual(field.call("POST", "/api/quotes", {"customer_id": self.cust, "lines": [{"name": "x", "qty": 1, "price": 1}]})[0], 403)
        self.assertEqual(field.call("POST", "/api/customers", {"name": "x"})[0], 403)
        self.assertEqual(field.call("GET", "/api/users")[0], 403)
        self.assertEqual(office.call("GET", "/api/users")[0], 403)
        self.assertEqual(field.call("GET", "/api/export/orders.csv")[0], 403)
        o = self.mkorder()[1]
        self.assertEqual(field.call("PUT", f"/api/orders/{o['id']}/stage", {"stage": "절단"})[0], 200)
        self.assertEqual(field.call("PUT", f"/api/orders/{o['id']}", {"due": "2026-11-01"})[0], 403)

    def test_ac4_server_totals(self):
        s, q = office.call("POST", "/api/quotes", {"customer_id": self.cust, "total": 1, "supply": 1,
                                                   "lines": [{"item_id": self.item, "qty": 3}, {"name": "가공비", "qty": 1, "price": 10000, "amount": 5}]})
        self.assertEqual((q["supply"], q["vat"], q["total"]), (13150, 1315, 14465))
        self.assertEqual(office.call("PUT", f"/api/quotes/{q['id']}", {"lines": [{"name": "a", "qty": 1, "price": 15}]})[1]["vat"], 2)  # 1.5 반올림
        self.assertEqual(office.call("POST", "/api/quotes", {"customer_id": self.cust, "lines": [{"name": "a", "qty": 0, "price": 1}]})[0], 400)
        self.assertEqual(office.call("POST", "/api/quotes", {"customer_id": self.cust, "lines": [{"name": "a", "qty": 1, "price": -5}]})[0], 400)

    def test_ac5_order_rules(self):
        q = self.mkquote()
        self.assertEqual(office.call("POST", "/api/orders", {"quote_id": q["id"], "ship_addr": "부산"})[0], 400)       # 납기 없음
        self.assertEqual(office.call("POST", "/api/orders", {"quote_id": q["id"], "due": "2026-11-01"})[0], 400)      # 배송지 없음
        q2 = self.mkquote(self.cust2)
        s, r = office.call("POST", "/api/orders", {"quote_id": q2["id"], "due": "2026-11-01", "ship_addr": "서울"})
        self.assertEqual((s, r["code"]), (409, "biz_doc"))
        self.assertEqual(office.call("POST", "/api/orders", {"quote_id": q2["id"], "due": "2026-11-01", "ship_addr": "서울", "force": True})[0], 200)
        self.assertEqual(office.call("POST", "/api/orders", {"quote_id": q2["id"], "due": "2026-11-01", "ship_addr": "서울", "force": True})[0], 409)  # 이중 주문
        self.assertEqual(office.call("DELETE", f"/api/quotes/{q2['id']}")[0], 409)
        self.assertEqual(office.call("DELETE", f"/api/customers/{self.cust2}")[0], 409)

    def test_ac6_stage(self):
        o = self.mkorder()[1]
        self.assertEqual(field.call("PUT", f"/api/orders/{o['id']}/stage", {"stage": "없는단계"})[0], 400)
        last = admin.call("GET", "/api/config")[1]["stages"][-1]
        self.assertTrue(field.call("PUT", f"/api/orders/{o['id']}/stage", {"stage": last})[1]["done"])
        self.assertNotIn(o["id"], [x["id"] for x in office.call("GET", "/api/today")[1]["soon"]])
        self.assertEqual(office.call("PUT", f"/api/orders/{o['id']}/stage", {"stage": "가공"})[0], 200)  # 되돌리기 허용, 다시 진행으로
        s, _ = admin.call("PUT", "/api/config", {"stages": ["접수", "끝"]})
        self.assertEqual(s, 409)          # 진행 중 주문이 쓰는 공정 삭제 불가
        self.assertEqual(admin.call("DELETE", f"/api/orders/{o['id']}")[0], 409)

    def test_ac7_today(self):
        late = self.mkorder(due="2026-10-26", start="2026-10-20")[1]       # 오늘 납기: 지연 아님
        past = self.mkorder(due="2026-10-24", start="2026-10-20", paid=1)[1]
        far = self.mkorder(due="2026-12-01", paid=1)[1]
        d = office.call("GET", "/api/today")[1]
        self.assertIn(past["id"], [x["id"] for x in d["late"]])
        self.assertNotIn(late["id"], [x["id"] for x in d["late"]])
        self.assertIn(late["id"], [x["id"] for x in d["soon"]])
        self.assertNotIn(far["id"], [x["id"] for x in d["soon"]])
        self.assertEqual(d["unpaid_total"], sum(u["total"] for u in d["unpaid"]))
        self.assertNotIn(past["id"], [u["id"] for u in d["unpaid"]])
        q = self.mkquote()
        con = server.db()
        con.execute("UPDATE quotes SET status='sent', sent_at='2026-10-19' WHERE id=?", (q["id"],))
        con.commit()
        con.close()
        self.assertIn(q["id"], [x["id"] for x in office.call("GET", "/api/today")[1]["stale_quotes"]])
        t = office.call("POST", "/api/tasks", {"title": "자재 주문", "due": "2026-10-27", "shared": 1})[1]["id"]
        self.assertTrue(any(x["id"] == t for x in field.call("GET", "/api/today")[1]["tasks"]))

    def test_ac8_load(self):
        con = server.db()
        base = con.execute("SELECT COALESCE(SUM(hours),0) FROM orders WHERE done_at IS NULL").fetchone()[0]
        con.close()
        # 금요일 시작~다음 월요일 납기 20시간 -> 금·월 이틀에 10시간씩
        o = self.mkorder(due="2026-11-02", start="2026-10-30", hours=20)
        self.assertEqual(o[0], 200)
        r = office.call("GET", "/api/load?days=60")[1]
        self.assertAlmostEqual(sum(d["hours"] for d in r["days"]), base + 20, delta=0.5)  # 하루 단위 반올림 오차
        by = {d["date"]: d for d in r["days"]}
        self.assertTrue(by["2026-10-31"]["weekend"])
        self.assertGreaterEqual(by["2026-10-30"]["hours"], 10)
        # 용량 초과 표시
        for _ in range(3):
            self.mkorder(due="2026-10-26", start="2026-10-26", hours=20)
        r = office.call("GET", "/api/load?days=7")[1]
        self.assertTrue(r["days"][0]["over"])

    def test_ac9_files(self):
        o = self.mkorder()[1]
        s, f = office.call("POST", f"/api/orders/{o['id']}/files", raw=b"%PDF-data", headers={"X-Filename": "..%2F..%2Fevil.pdf"})
        self.assertEqual(s, 200)
        self.assertEqual(f["name"], "evil.pdf")
        s, b = field.call("GET", f"/api/files/{f['id']}")
        self.assertEqual((s, b), (200, b"%PDF-data"))
        self.assertEqual(office.call("POST", f"/api/orders/{o['id']}/files", raw=b"x", headers={"X-Filename": "a.exe"})[0], 400)
        self.assertEqual(field.call("POST", f"/api/orders/{o['id']}/files", raw=b"x", headers={"X-Filename": "a.pdf"})[0], 403)
        self.assertEqual(len(list((Path(TMP) / "files").glob("*"))) >= 1, True)
        self.assertTrue(all(".." not in p.name for p in (Path(TMP) / "files").iterdir()))
        old = server.MAX_UPLOAD
        server.MAX_UPLOAD = 10
        try:
            self.assertEqual(office.call("POST", f"/api/orders/{o['id']}/files", raw=b"x" * 50, headers={"X-Filename": "big.bin"})[0], 413)
        finally:
            server.MAX_UPLOAD = old

    def test_ac10_mail(self):
        q = self.mkquote()
        s, r = office.call("POST", f"/api/quotes/{q['id']}/mail")
        self.assertEqual((s, r["code"]), (409, "no_smtp"))
        admin.call("PUT", "/api/config", {"smtp": {"host": "smtp.test", "port": 587, "user": "u", "password": "p", "from": "f@test.kr"}})
        self.assertNotIn("p", json.dumps(admin.call("GET", "/api/config")[1]["smtp"].get("password", "")))
        self.assertNotIn("smtp", field.call("GET", "/api/config")[1])
        sent = []
        server.smtp_send = lambda smtp, msg: sent.append(msg)
        s, r = office.call("POST", f"/api/quotes/{q['id']}/mail")
        self.assertEqual((s, r["to"]), (200, "a@b.co"))
        self.assertIn(q["no"], sent[0]["Subject"])
        row = [x for x in office.call("GET", "/api/quotes")[1] if x["id"] == q["id"]][0]
        self.assertEqual((row["status"], row["sent_at"]), ("sent", "2026-10-26"))
        q2 = self.mkquote(self.cust2)
        self.assertEqual(office.call("POST", f"/api/quotes/{q2['id']}/mail")[1]["code"], "no_email")

        def boom(smtp, msg):
            raise OSError("연결 거부")
        server.smtp_send = boom
        q3 = self.mkquote()
        s, r = office.call("POST", f"/api/quotes/{q3['id']}/mail")
        self.assertEqual((s, r["code"]), (502, "smtp_fail"))
        row = [x for x in office.call("GET", "/api/quotes")[1] if x["id"] == q3["id"]][0]
        self.assertEqual(row["status"], "draft")

    def test_ac11_csv(self):
        office.call("POST", "/api/customers", {"name": "=HYPERLINK(1)"})
        s, b = office.call("GET", "/api/export/customers.csv")
        self.assertEqual(s, 200)
        self.assertTrue(b.startswith("﻿".encode()))
        self.assertIn("동서물산".encode(), b)
        self.assertIn(b"'=HYPERLINK", b)
        self.assertEqual(office.call("GET", "/api/export/orders.csv")[0], 200)

    def test_ac12_backup(self):
        s, b = admin.call("GET", "/api/backup")
        self.assertTrue(s == 200 and b.startswith(b"SQLite format 3"))
        server.auto_backup()
        self.assertTrue(list((Path(TMP) / "backup").glob("mes-20261026.db")))

    def test_ac13_xss(self):
        cid = office.call("POST", "/api/customers", {"name": "<script>alert(1)</script>", "email": "x@y.kr", "biz_doc": 1})[1]["id"]
        q = office.call("POST", "/api/quotes", {"customer_id": cid, "lines": [{"name": "<img onerror=x>", "qty": 1, "price": 1}]})[1]
        o = office.call("POST", "/api/orders", {"quote_id": q["id"], "due": "2026-11-05", "ship_addr": "<b>주소</b>", "memo": "<i>메모</i>"})[1]
        for p in (f"/print/quote/{q['id']}", f"/print/order/{o['id']}"):
            s, b = office.call("GET", p)
            self.assertEqual(s, 200)
            self.assertNotIn(b"<script>alert", b)
            self.assertNotIn(b"<img onerror", b)
            self.assertIn(b"&lt;", b)

    def test_misc_validation(self):
        self.assertEqual(office.call("POST", "/api/customers", {"name": ""})[0], 400)
        self.assertEqual(office.call("POST", "/api/customers", {"name": "a", "email": "nope"})[0], 400)
        self.assertEqual(office.call("POST", "/api/orders", {"customer_id": self.cust, "lines": [{"name": "a", "qty": 1, "price": 1}], "due": "2026-13-45", "ship_addr": "x"})[0], 400)
        self.assertEqual(office.call("POST", "/api/orders", {"customer_id": self.cust, "lines": [{"name": "a", "qty": 1, "price": 1}], "due": "2026-10-01", "ship_addr": "x", "start": "2026-10-20"})[0], 400)
        s, _ = office.call("POST", "/api/feedback", {"text": "버튼이 작아요"})
        self.assertEqual(s, 200)
        self.assertEqual(field.call("PUT", "/api/feedback/1", {"status": "완료"})[0], 403)
        self.assertEqual(office.call("PUT", "/api/feedback/1", {"status": "완료"})[0], 200)
        # 남의 개인 업무는 안 보이고 건드릴 수 없다
        tid = office.call("POST", "/api/tasks", {"title": "비공개"})[1]["id"]
        self.assertNotIn(tid, [t["id"] for t in field.call("GET", "/api/tasks")[1]])
        self.assertEqual(field.call("PUT", f"/api/tasks/{tid}", {"done": 1})[0], 404)
        sid = office.call("POST", "/api/tasks", {"title": "공유", "shared": 1})[1]["id"]
        self.assertEqual(field.call("PUT", f"/api/tasks/{sid}", {"done": 1})[0], 200)
        self.assertEqual(field.call("PUT", f"/api/tasks/{sid}", {"title": "바꿈"})[0], 403)
        # 마지막 관리자 보호, 자기 삭제 금지
        me_id = admin.call("GET", "/api/me")[1]["id"]
        self.assertEqual(admin.call("PUT", f"/api/users/{me_id}", {"role": "field"})[0], 409)
        self.assertEqual(admin.call("DELETE", f"/api/users/{me_id}")[0], 409)
        # 다른 출처의 쓰기 요청 거절
        self.assertEqual(office.call("POST", "/api/feedback", {"text": "x"}, headers={"Origin": "http://evil.example"})[0], 403)

    def test_adversarial(self):
        """깨진 입력에도 서버가 응답 없이 끊기지 않고 4xx/5xx JSON을 준다."""
        bad = [("PUT", "/api/config", {"smtp": {"port": "abc"}}), ("PUT", "/api/config", {"smtp": "x"}), ("PUT", "/api/config", {"stages": "접수"}),
               ("PUT", "/api/config", {"smtp": {"port": 99999}}), ("GET", "/api/load?days=abc", None), ("GET", "/api/load?days=-5", None),
               ("POST", "/api/quotes", {"customer_id": self.cust, "lines": [{"item_id": {"a": 1}, "qty": 1}]}),
               ("POST", "/api/quotes", {"customer_id": self.cust, "lines": [{"item_id": "zzz", "qty": 1}]}),
               ("POST", "/api/quotes", {"customer_id": self.cust, "lines": "x"}), ("POST", "/api/quotes", {"customer_id": self.cust, "lines": [5]}),
               ("POST", "/api/quotes", {"customer_id": "x", "lines": []}), ("POST", "/api/customers", {"name": ["a"]}),
               ("POST", "/api/orders", {"customer_id": self.cust, "lines": [{"name": "a", "qty": 1, "price": 1}], "due": "2026-11-01", "ship_addr": "x", "hours": "많이"}),
               ("POST", "/api/users", {"name": "n", "pin": 1234, "role": "god"}), ("POST", "/api/tasks", {"title": "t", "due": "어제"}),
               ("PUT", "/api/orders/1", {"hours": 0}), ("PUT", "/api/orders/999999/stage", {"stage": "가공"})]
        for m, p, b in bad:
            s, r = admin.call(m, p, b)
            self.assertTrue(400 <= s < 500, (m, p, s, r))
            self.assertIn("error", r)
        s, r = admin.call("POST", "/api/feedback", raw=b"not json", headers={"Content-Type": "application/json"})
        self.assertEqual(s, 400)
        s, r = admin.call("POST", "/api/feedback", raw=b"[1,2]")
        self.assertEqual(s, 400)
        s, r = admin.call("POST", "/api/feedback", raw=b"", headers={"Content-Length": "abc"})
        self.assertEqual(s, 400)
        q = office.call("PUT", f"/api/quotes/{self.mkquote()['id']}", {"customer_id": 999999})
        self.assertEqual(q[0], 400)
        self.assertEqual(admin.call("PUT", "/api/config", {"company": "x\r\nBcc: a@b.c"})[0], 200)
        sent = []
        server.smtp_send = lambda smtp, msg: sent.append(msg)
        admin.call("PUT", "/api/config", {"smtp": {"host": "h", "port": 587, "from": "f@t.kr"}})
        office.call("POST", f"/api/quotes/{self.mkquote()['id']}/mail")
        self.assertEqual(len(str(sent[-1]).split("Bcc")), 2)   # 제목 안의 글자로만 남고 별도 헤더가 되지 않는다
        self.assertNotIn("\n", str(sent[-1]["Subject"]))
        admin.call("PUT", "/api/config", {"company": "테스트공장"})


if __name__ == "__main__":
    unittest.main(verbosity=1)
