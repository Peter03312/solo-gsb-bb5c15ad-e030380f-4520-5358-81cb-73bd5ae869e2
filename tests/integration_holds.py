"""付款预留指令（预留/取消/确认）与可用额：端到端集成测试
（pgserver 便携 PostgreSQL，无需 Docker）。

运行: PYTHONPATH=. python3 tests/integration_holds.py
"""
import os
import pathlib
import tempfile
import threading

import pgserver

_pgdata = pathlib.Path(tempfile.mkdtemp(prefix="royalty_hold_")) / "pgdata"
server = pgserver.get_server(_pgdata, cleanup_mode="delete")
try:
    os.environ["DATABASE_URL"] = server.get_uri()
    # 配置必须在设置环境变量后导入
    from fastapi.testclient import TestClient
    import psycopg
    from app import db
    from app.main import app

    failures = []

    def check(name, cond, extra=""):
        print(("PASS" if cond else "FAIL"), name, extra)
        if not cond:
            failures.append(name)

    with TestClient(app) as c:
        check("health", c.get("/health").json()["status"] == "ok")

        # ---- 建档：作品 W + 权利人 A(60)/B(40)；W2 + C(100)；W3 + D(100) ----
        c.post("/works", json={"code": "W", "title": "作品"})
        c.post("/works", json={"code": "W2", "title": "作品二"})
        c.post("/works", json={"code": "W3", "title": "作品三"})
        for code, name in [("A", "甲"), ("B", "乙"), ("C", "丙"), ("D", "丁")]:
            c.post("/rightsholders", json={"code": code, "name": name})
        c.post("/agreements", json={
            "work_code": "W",
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "A", "percent": "60"},
                       {"holder_code": "B", "percent": "40"}]})
        c.post("/agreements", json={
            "work_code": "W2",
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "C", "percent": "100"}]})
        c.post("/agreements", json={
            "work_code": "W3",
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "D", "percent": "100"}]})

        period = {"start": "2026-02-01T00:00:00+08:00",
                  "end": "2026-03-01T00:00:00+08:00"}
        period2 = {"start": "2026-05-01T00:00:00+08:00",
                   "end": "2026-06-01T00:00:00+08:00"}
        period3 = {"start": "2026-07-01T00:00:00+08:00",
                   "end": "2026-08-01T00:00:00+08:00"}

        # 出具 100 分单据：A=60，B=40
        c.post("/plays/import", json={
            "import_id": "P1", "work_code": "W",
            "occurred_at": "2026-02-10T20:00:00+08:00", "amount": 100})
        c.post("/statements/issue", json=period)

        # ---- 入参校验 ----
        r = c.post("/payment-holds",
                   json={"holder_code": "A", "hold_ref": "HX", "amount": 0})
        check("amount zero 422", r.status_code == 422, r.text)
        r = c.post("/payment-holds",
                   json={"holder_code": "A", "hold_ref": "HX", "amount": -5})
        check("amount negative 422", r.status_code == 422, r.text)
        r = c.post("/payment-holds",
                   json={"holder_code": "A", "hold_ref": "HX", "amount": 1.5})
        check("amount non-int 422", r.status_code == 422, r.text)
        r = c.post("/payment-holds",
                   json={"holder_code": "A", "hold_ref": "HX", "amount": True})
        check("amount bool 422", r.status_code == 422, r.text)
        r = c.post("/payment-holds",
                   json={"holder_code": "A", "hold_ref": "", "amount": 1})
        check("empty hold_ref 422", r.status_code == 422, r.text)
        r = c.post("/payment-holds",
                   json={"holder_code": "NOBODY", "hold_ref": "HX", "amount": 1})
        check("holder missing 404", r.status_code == 404
              and r.json()["error"] == "holder_not_found", r.text)
        check("rejected hold leaves no trace",
              c.get("/payment-holds/HX").status_code == 404)

        # ---- 预留：可用额 = 余额 - 全部待确认预留 ----
        r = c.post("/payment-holds",
                   json={"holder_code": "A", "hold_ref": "H1", "amount": 25})
        body = r.json()
        check("reserve 201", r.status_code == 201
              and body["status"] == "created"
              and body["hold_status"] == "pending"
              and body["available_after"] == 35, r.text)
        h1_id = body["hold_id"]

        # 同号同内容重试 => 200 返回原指令
        r = c.post("/payment-holds",
                   json={"holder_code": "A", "hold_ref": "H1", "amount": 25})
        body2 = r.json()
        check("reserve idempotent retry", r.status_code == 200
              and body2["status"] == "duplicate"
              and body2["hold_id"] == h1_id
              and body2["available_after"] == 35, r.text)

        # 同号异内容（金额/权利人）=> 409 hold_ref_conflict
        r = c.post("/payment-holds",
                   json={"holder_code": "A", "hold_ref": "H1", "amount": 30})
        check("hold_ref conflict amount", r.status_code == 409
              and r.json()["error"] == "hold_ref_conflict"
              and r.json()["detail"]["existing_amount"] == 25, r.text)
        r = c.post("/payment-holds",
                   json={"holder_code": "B", "hold_ref": "H1", "amount": 25})
        check("hold_ref conflict holder", r.status_code == 409
              and r.json()["error"] == "hold_ref_conflict"
              and r.json()["detail"]["existing_holder_code"] == "A", r.text)

        # 超出可用额：60 - 25 = 35，预 36 拒绝且不留痕
        r = c.post("/payment-holds",
                   json={"holder_code": "A", "hold_ref": "H2", "amount": 36})
        check("reserve exceeds available", r.status_code == 409
              and r.json()["error"] == "hold_exceeds_available"
              and r.json()["detail"]["balance"] == 60
              and r.json()["detail"]["pending_holds"] == 25
              and r.json()["detail"]["available"] == 35, r.text)
        check("rejected reserve leaves no trace",
              c.get("/payment-holds/H2").status_code == 404)

        # 恰好占满可用额
        r = c.post("/payment-holds",
                   json={"holder_code": "A", "hold_ref": "H2", "amount": 35})
        check("reserve exact available", r.status_code == 201
              and r.json()["available_after"] == 0, r.text)
        r = c.post("/payment-holds",
                   json={"holder_code": "A", "hold_ref": "H3", "amount": 1})
        check("no available blocks reserve", r.status_code == 409
              and r.json()["error"] == "hold_exceeds_available", r.text)

        # ---- 新付款须避开待确认预留 ----
        r = c.post("/payments",
                   json={"holder_code": "A", "external_ref": "DIR0", "amount": 1})
        check("payment avoids pending holds", r.status_code == 409
              and r.json()["error"] == "payment_exceeds_balance"
              and r.json()["detail"]["balance"] == 60
              and r.json()["detail"]["pending_holds"] == 60
              and r.json()["detail"]["available"] == 0, r.text)

        # ---- 对账单：待确认预留、可用额、指令状态 ----
        led = c.get("/rightsholders/A/ledger").json()
        check("ledger shows holds", led["balance"] == 60
              and led["pending_holds_total"] == 60
              and led["available"] == 0
              and [(h["hold_ref"], h["status"]) for h in led["holds"]]
              == [("H1", "pending"), ("H2", "pending")], str(led))

        # ---- 取消：释放额度，重复取消幂等 ----
        r = c.post("/payment-holds/H2/cancel")
        check("cancel releases", r.status_code == 200
              and r.json()["status"] == "cancelled"
              and r.json()["hold_status"] == "cancelled"
              and r.json()["available_after"] == 35, r.text)
        r = c.post("/payment-holds/H2/cancel")
        check("cancel retry idempotent", r.status_code == 200
              and r.json()["status"] == "already_cancelled"
              and r.json()["available_after"] == 35, r.text)
        r = c.post("/payment-holds/H-NOPE/cancel")
        check("cancel missing 404", r.status_code == 404
              and r.json()["error"] == "hold_not_found", r.text)
        r = c.post("/payment-holds/H2/confirm",
                   json={"external_ref": "BANK-H2"})
        check("confirm cancelled 409", r.status_code == 409
              and r.json()["error"] == "hold_already_cancelled", r.text)
        led = c.get("/rightsholders/A/ledger").json()
        check("ledger after cancel", led["pending_holds_total"] == 25
              and led["available"] == 35, str(led))

        # ---- 可用额内的直接付款仍按原约定入账 ----
        r = c.post("/payments",
                   json={"holder_code": "A", "external_ref": "DIR1", "amount": 36})
        check("payment over available rejected", r.status_code == 409
              and r.json()["error"] == "payment_exceeds_balance"
              and r.json()["detail"]["available"] == 35, r.text)
        r = c.post("/payments",
                   json={"holder_code": "A", "external_ref": "DIR1", "amount": 35})
        check("payment within available", r.status_code == 201
              and r.json()["balance_after"] == 25, r.text)
        r = c.post("/payments",
                   json={"holder_code": "A", "external_ref": "DIR2", "amount": 1})
        check("available exhausted", r.status_code == 409
              and r.json()["detail"]["balance"] == 25
              and r.json()["detail"]["available"] == 0, r.text)

        # ---- 确认：原子登记付款并结束预留 ----
        r = c.post("/payment-holds/H1/confirm",
                   json={"external_ref": "BANK-H1"})
        body = r.json()
        check("confirm 201", r.status_code == 201
              and body["status"] == "created"
              and body["hold_status"] == "confirmed"
              and body["amount"] == 25
              and body["balance_after"] == 0
              and body["available_after"] == 0
              and body["payment_id"] >= 1, r.text)
        h1_payment_id = body["payment_id"]

        # 同流水号重试 => 200 幂等返回原付款记录，不重复入账
        r = c.post("/payment-holds/H1/confirm",
                   json={"external_ref": "BANK-H1"})
        check("confirm retry idempotent", r.status_code == 200
              and r.json()["status"] == "duplicate"
              and r.json()["payment_id"] == h1_payment_id
              and r.json()["balance_after"] == 0, r.text)
        led = c.get("/rightsholders/A/ledger").json()
        check("confirm registered once", led["paid_total"] == 60
              and led["balance"] == 0
              and led["pending_holds_total"] == 0
              and led["available"] == 0
              and [(h["hold_ref"], h["status"]) for h in led["holds"]]
              == [("H1", "confirmed"), ("H2", "cancelled")]
              and led["holds"][0]["external_ref"] == "BANK-H1"
              and led["holds"][0]["payment_id"] == h1_payment_id, str(led))

        # 异流水号 / 取消已确认指令 => 409
        r = c.post("/payment-holds/H1/confirm",
                   json={"external_ref": "BANK-OTHER"})
        check("confirm different ref 409", r.status_code == 409
              and r.json()["error"] == "hold_already_confirmed"
              and r.json()["detail"]["existing_external_ref"] == "BANK-H1",
              r.text)
        r = c.post("/payment-holds/H1/cancel")
        check("cancel confirmed 409", r.status_code == 409
              and r.json()["error"] == "hold_already_confirmed", r.text)

        # 指令不可变（触发器）：内容不可改、终态不可逆、不可删除
        with db.pool.connection() as db_conn:
            try:
                db_conn.execute(
                    "UPDATE payment_holds SET amount = 1 WHERE hold_ref = 'H1'")
                blocked_content = False
            except psycopg.Error:
                blocked_content = True
            try:
                db_conn.execute(
                    "UPDATE payment_holds SET status = 'cancelled', "
                    "resolved_at = now() WHERE hold_ref = 'H1'")
                blocked_terminal = False
            except psycopg.Error:
                blocked_terminal = True
            try:
                db_conn.execute("DELETE FROM payment_holds WHERE hold_ref = 'H1'")
                blocked_delete = False
            except psycopg.Error:
                blocked_delete = True
        check("hold frozen by trigger",
              blocked_content and blocked_terminal and blocked_delete)
        got = c.get("/payment-holds/H1").json()
        check("hold intact after attempts", got["hold_status"] == "confirmed"
              and got["amount"] == 25 and got["external_ref"] == "BANK-H1",
              str(got))
        check("confirm leaves payment frozen",
              c.get("/rightsholders/A/ledger").json()["paid_total"] == 60)

        # ---- 退回继续加回余额（可用额同步回升） ----
        r = c.post("/refunds", json={
            "payment_ref": "DIR1", "refund_ref": "RET1", "amount": 15})
        check("refund adds back", r.status_code == 201
              and r.json()["balance_after"] == 15, r.text)
        r = c.post("/payment-holds",
                   json={"holder_code": "A", "hold_ref": "H7", "amount": 16})
        check("reserve over refunded available", r.status_code == 409
              and r.json()["detail"]["available"] == 15, r.text)
        r = c.post("/payment-holds",
                   json={"holder_code": "A", "hold_ref": "H7", "amount": 15})
        check("reserve within refunded available", r.status_code == 201
              and r.json()["available_after"] == 0, r.text)
        r = c.post("/payment-holds/H7/cancel")
        check("cancel restores", r.status_code == 200
              and r.json()["available_after"] == 15, r.text)

        # ---- 确认时流水号被其他付款占用 => 409，指令保持待确认 ----
        r = c.post("/payment-holds",
                   json={"holder_code": "B", "hold_ref": "H4", "amount": 10})
        check("B reserve", r.status_code == 201
              and r.json()["available_after"] == 30, r.text)
        r = c.post("/payment-holds/H4/confirm",
                   json={"external_ref": "DIR1"})
        check("confirm with occupied ref", r.status_code == 409
              and r.json()["error"] == "payment_ref_conflict"
              and r.json()["detail"]["existing_holder_code"] == "A"
              and r.json()["detail"]["existing_amount"] == 35, r.text)
        got = c.get("/payment-holds/H4").json()
        check("hold still pending after occupied ref",
              got["hold_status"] == "pending", str(got))
        r = c.post("/payment-holds/H4/confirm",
                   json={"external_ref": "BANK-H4"})
        check("confirm with free ref", r.status_code == 201
              and r.json()["balance_after"] == 30, r.text)
        led = c.get("/rightsholders/B/ledger").json()
        check("B ledger", led["billed_total"] == 40
              and led["paid_total"] == 10 and led["balance"] == 30
              and led["available"] == 30, str(led))

        # ---- 负额追补冲减余额：确认须留足更早待确认指令及本指令金额 ----
        c.post("/plays/import", json={
            "import_id": "PC1", "work_code": "W2",
            "occurred_at": "2026-05-10T20:00:00+08:00", "amount": 100})
        c.post("/statements/issue", json=period2)  # C 余额 100
        r = c.post("/payment-holds",
                   json={"holder_code": "C", "hold_ref": "H5", "amount": 60})
        check("C reserve H5", r.status_code == 201
              and r.json()["available_after"] == 40, r.text)
        r = c.post("/payment-holds",
                   json={"holder_code": "C", "hold_ref": "H6", "amount": 40})
        check("C reserve H6", r.status_code == 201
              and r.json()["available_after"] == 0, r.text)

        # 负额追补 -70：C 余额 100 -> 30
        c.post("/plays/import", json={
            "import_id": "PC1R1", "work_code": "W2",
            "occurred_at": "2026-05-10T20:00:00+08:00", "amount": 30,
            "corrects_id": "PC1"})
        r = c.post("/statements/issue", json=period2)
        check("negative supplement", r.json()["kind"] == "supplement"
              and {x["holder_code"]: x["delta"] for x in r.json()["payable"]}
              == {"C": -70}, r.text)
        led = c.get("/rightsholders/C/ledger").json()
        check("C balance shrunk", led["balance"] == 30
              and led["pending_holds_total"] == 100
              and led["available"] == -70, str(led))

        # 确认更晚的 H6：须留足更早的 H5(60) + 本指令 40，余额 30 不够 => 拒绝且指令不变
        r = c.post("/payment-holds/H6/confirm",
                   json={"external_ref": "BANK-H6"})
        check("later confirm needs earlier holds", r.status_code == 409
              and r.json()["error"] == "hold_exceeds_available"
              and r.json()["detail"]["balance"] == 30
              and r.json()["detail"]["earlier_pending_holds"] == 60
              and r.json()["detail"]["requested_amount"] == 40, r.text)
        check("H6 still pending",
              c.get("/payment-holds/H6").json()["hold_status"] == "pending")
        # 确认更早的 H5：余额 30 < 60 同样拒绝
        r = c.post("/payment-holds/H5/confirm",
                   json={"external_ref": "BANK-H5"})
        check("earlier confirm needs own amount", r.status_code == 409
              and r.json()["detail"]["earlier_pending_holds"] == 0, r.text)
        check("no payment registered on rejection",
              c.get("/rightsholders/C/ledger").json()["paid_total"] == 0)

        # 取消更晚的 H6 释放额度；H5 仍因余额不足不可确认
        r = c.post("/payment-holds/H6/cancel")
        check("cancel later hold", r.status_code == 200
              and r.json()["available_after"] == -30, r.text)
        r = c.post("/payment-holds/H5/confirm",
                   json={"external_ref": "BANK-H5"})
        check("still insufficient", r.status_code == 409
              and r.json()["error"] == "hold_exceeds_available", r.text)

        # 正额追补 +170：C 余额 200，H5 可确认
        c.post("/plays/import", json={
            "import_id": "PC1R2", "work_code": "W2",
            "occurred_at": "2026-05-10T20:00:00+08:00", "amount": 200,
            "corrects_id": "PC1R1"})
        c.post("/statements/issue", json=period2)
        r = c.post("/payment-holds/H5/confirm",
                   json={"external_ref": "BANK-H5"})
        check("confirm after recovery", r.status_code == 201
              and r.json()["balance_after"] == 140
              and r.json()["available_after"] == 140, r.text)
        led = c.get("/rightsholders/C/ledger").json()
        check("C ledger final", led["billed_total"] == 200
              and led["paid_total"] == 60 and led["balance"] == 140
              and led["pending_holds_total"] == 0
              and led["available"] == 140
              and [(h["hold_ref"], h["status"]) for h in led["holds"]]
              == [("H5", "confirmed"), ("H6", "cancelled")], str(led))

        # ---- 并发：同指令号并发预留，恰一条创建，其余幂等 ----
        c.post("/plays/import", json={
            "import_id": "PD1", "work_code": "W3",
            "occurred_at": "2026-07-10T20:00:00+08:00", "amount": 1000})
        c.post("/statements/issue", json=period3)  # D 余额 1000

        same_results = []

        def fire_same_reserve():
            rr = c.post("/payment-holds",
                        json={"holder_code": "D", "hold_ref": "HC", "amount": 50})
            same_results.append((rr.status_code, rr.json()))

        ts = [threading.Thread(target=fire_same_reserve) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        ids = {b["hold_id"] for _, b in same_results}
        created = sum(1 for _, b in same_results if b["status"] == "created")
        dup = sum(1 for _, b in same_results if b["status"] == "duplicate")
        check("same-ref concurrent reserve serialized",
              len(ids) == 1 and created == 1 and dup == 7,
              f"{[(s, b['status']) for s, b in same_results]}")

        # 再备一条待确认指令，供"异流水号并发确认"使用
        r = c.post("/payment-holds",
                   json={"holder_code": "D", "hold_ref": "H9", "amount": 50})
        check("D reserve H9", r.status_code == 201, r.text)

        # ---- 并发：预留与直接付款并发抢额，已付 + 待确认预留绝不超单据 ----
        outcomes = {"reserve_created": 0, "reserve_rejected": 0,
                    "pay_created": 0, "pay_rejected": 0, "errors": []}

        def fire_reserve(i):
            try:
                rr = c.post("/payment-holds",
                            json={"holder_code": "D",
                                  "hold_ref": f"HR-{i}", "amount": 100})
                if rr.status_code == 201:
                    outcomes["reserve_created"] += 1
                elif (rr.status_code == 409
                      and rr.json()["error"] == "hold_exceeds_available"):
                    outcomes["reserve_rejected"] += 1
                else:
                    outcomes["errors"].append((rr.status_code, rr.text))
            except Exception as e:  # noqa
                outcomes["errors"].append(repr(e))

        def fire_payment(i):
            try:
                rr = c.post("/payments",
                            json={"holder_code": "D",
                                  "external_ref": f"DP-{i}", "amount": 100})
                if rr.status_code == 201:
                    outcomes["pay_created"] += 1
                elif (rr.status_code == 409
                      and rr.json()["error"] == "payment_exceeds_balance"):
                    outcomes["pay_rejected"] += 1
                else:
                    outcomes["errors"].append((rr.status_code, rr.text))
            except Exception as e:  # noqa
                outcomes["errors"].append(repr(e))

        ts = ([threading.Thread(target=fire_reserve, args=(i,)) for i in range(10)]
              + [threading.Thread(target=fire_payment, args=(i,)) for i in range(10)])
        [t.start() for t in ts]
        [t.join() for t in ts]
        led = c.get("/rightsholders/D/ledger").json()
        # 不变量：已付 + 待确认预留 <= 单据累计；可用额非负
        check("reserve/pay storm no overspend",
              not outcomes["errors"]
              and outcomes["reserve_created"] + outcomes["reserve_rejected"] == 10
              and outcomes["pay_created"] + outcomes["pay_rejected"] == 10
              and led["paid_total"] + led["pending_holds_total"]
              <= led["billed_total"]
              and led["available"] >= 0
              and led["available"]
              == led["balance"] - led["pending_holds_total"],
              f"{outcomes} {led}")

        # ---- 并发：同指令号同流水号并发确认，恰一笔付款，其余幂等 ----
        confirm_same = []

        def fire_same_confirm():
            rr = c.post("/payment-holds/HC/confirm",
                        json={"external_ref": "BANK-HC"})
            confirm_same.append((rr.status_code, rr.json()))

        ts = [threading.Thread(target=fire_same_confirm) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        pids = {b.get("payment_id") for _, b in confirm_same}
        created = sum(1 for _, b in confirm_same if b.get("status") == "created")
        dup = sum(1 for _, b in confirm_same if b.get("status") == "duplicate")
        check("same-ref concurrent confirm serialized",
              len(pids) == 1 and created == 1 and dup == 7,
              f"{[(s, b.get('status')) for s, b in confirm_same]}")

        # ---- 并发：同指令号异流水号并发确认，恰一笔成功，其余拒绝 ----
        confirm_distinct = {"created": 0, "rejected": 0, "errors": []}

        def fire_distinct_confirm(i):
            try:
                rr = c.post("/payment-holds/H9/confirm",
                            json={"external_ref": f"BANK-H9-{i}"})
                if rr.status_code == 201:
                    confirm_distinct["created"] += 1
                elif rr.status_code == 409 and rr.json()["error"] in (
                        "hold_already_confirmed", "payment_ref_conflict"):
                    confirm_distinct["rejected"] += 1
                else:
                    confirm_distinct["errors"].append((rr.status_code, rr.text))
            except Exception as e:  # noqa
                confirm_distinct["errors"].append(repr(e))

        ts = [threading.Thread(target=fire_distinct_confirm, args=(i,))
              for i in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        got = c.get("/payment-holds/H9").json()
        check("distinct-ref concurrent confirm: exactly one wins",
              not confirm_distinct["errors"]
              and confirm_distinct["created"] == 1
              and confirm_distinct["rejected"] == 7
              and got["hold_status"] == "confirmed",
              f"{confirm_distinct} {got}")
        # H9 只登记了一笔付款
        led = c.get("/rightsholders/D/ledger").json()
        h9_payments = [p for p in led["payments"]
                       if p["payment_id"] == got["payment_id"]]
        check("H9 single payment", len(h9_payments) == 1
              and h9_payments[0]["amount"] == 50
              and h9_payments[0]["external_ref"] == got["external_ref"],
              str(led["payments"]))
        check("D invariant holds",
              led["paid_total"] + led["pending_holds_total"]
              <= led["billed_total"] and led["available"] >= 0, str(led))

    print()
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        raise SystemExit(1)
    print("ALL PAYMENT-HOLD INTEGRATION CHECKS PASSED")
finally:
    server._cleanup()
    import shutil
    shutil.rmtree(_pgdata.parent, ignore_errors=True)
