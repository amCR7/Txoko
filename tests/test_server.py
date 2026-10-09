import http.cookiejar
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

import server


class LocalApiTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = server.TxokoStore(Path(self.temp_dir.name))
        self.setup_token = self.store.setup_token_path.read_text(encoding="utf-8").strip()
        handler = partial(
            server.TxokoHandler,
            directory=str(server.ROOT),
            store=self.store,
            secure_cookie=False,
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.guest_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.temp_dir.cleanup()

    def request(self, path, method="GET", payload=None, opener=None, extra_headers=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"} if data is not None else {}
        if extra_headers:
            headers.update(extra_headers)
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers=headers,
        )
        try:
            response = (opener or self.guest_opener).open(request)
        except urllib.error.HTTPError as error:
            response = error
        return response.code, json.loads(response.read())

    def register(self, username, token=None):
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        status, result = self.request(
            "/api/register",
            "POST",
            {
                "firstName": username.title(),
                "lastName": "Txoko",
                "username": username,
                "pin": "123456",
                "setupToken": token,
            },
            opener=opener,
        )
        self.assertEqual(status, 201, result)
        return result["user"], opener

    def test_president_member_guest_inventory_and_payment_flow(self):
        self.assertEqual(self.request("/api/status")[1]["needsPresident"], True)
        president, president_opener = self.register("presidente", self.setup_token)
        self.assertEqual(president["role"], "president")
        self.assertFalse(self.store.setup_token_path.exists())

        member, member_opener = self.register("socio")
        self.assertEqual(member["role"], "member")
        self.assertEqual(self.request("/api/status")[1]["needsPresident"], False)

        status, created = self.request(
            "/api/products",
            "POST",
            {"name": "Coca-Cola", "stock": 2, "priceCents": 150},
            president_opener,
        )
        self.assertEqual(status, 201, created)
        product_id = created["product"]["id"]

        status, _ = self.request(
            "/api/products",
            "POST",
            {"name": "Agua", "stock": 5, "priceCents": 100},
            member_opener,
        )
        self.assertEqual(status, 403)

        status, consumption = self.request(
            "/api/consume",
            "POST",
            {
                "productId": product_id,
                "guestName": {"firstName": "Ana", "lastName": "Invitada"},
            },
        )
        self.assertEqual(status, 201, consumption)
        guest_id = server.guest_key("Ana", "Invitada")
        self.assertEqual(self.request("/api/products")[1]["products"][0]["stock"], 1)
        self.assertEqual(self.request("/api/ledger")[1]["pendingCents"], 150)

        debts = self.request(f"/api/guest-debts?guestId={guest_id}")[1]["debts"]
        self.assertEqual(len(debts), 1)
        status, _ = self.request(
            "/api/request-payment",
            "POST",
            {"transactionId": debts[0]["id"], "guestId": guest_id},
        )
        self.assertEqual(status, 200)

        president_debts = self.request("/api/debts", opener=president_opener)[1]["debts"]
        self.assertEqual(president_debts[0]["status"], "payment_requested")
        self.assertEqual(self.request("/api/ledger")[1]["pendingCents"], 150)
        status, _ = self.request(
            "/api/confirm-payment",
            "POST",
            {"transactionId": debts[0]["id"]},
            president_opener,
        )
        self.assertEqual(status, 200)
        ledger = self.request("/api/ledger")[1]
        self.assertEqual(ledger["totalSalesCents"], 150)
        self.assertEqual(ledger["pendingCents"], 0)
        self.assertEqual(ledger["collectedCents"], 150)

    def test_first_account_requires_setup_token_and_login_session_persists(self):
        status, _ = self.request(
            "/api/register",
            "POST",
            {
                "firstName": "Primera",
                "lastName": "Persona",
                "username": "primera",
                "pin": "123456",
            },
        )
        self.assertEqual(status, 403)

        user, opener = self.register("presidente", self.setup_token)
        self.assertEqual(self.request("/api/me", opener=opener)[1]["user"]["id"], user["id"])
        self.request("/api/logout", "POST", {}, opener)
        self.assertIsNone(self.request("/api/me", opener=opener)[1]["user"])

        status, result = self.request(
            "/api/login",
            "POST",
            {"username": "presidente", "pin": "123456"},
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result["user"]["role"], "president")

    def test_mutating_requests_reject_foreign_origins(self):
        status, _ = self.request(
            "/api/register",
            "POST",
            {
                "firstName": "Intruso",
                "lastName": "Prueba",
                "username": "intruso",
                "pin": "123456",
                "setupToken": self.setup_token,
            },
            extra_headers={"Origin": "http://attacker.example"},
        )
        self.assertEqual(status, 403)
        self.assertTrue(self.store.needs_initial_president())

    def test_mutations_accept_private_lan_and_vpn_origins(self):
        _, opener = self.register("presidente", self.setup_token)
        for host in ("192.168.1.23:8080", "100.80.1.4:8080", "localhost:8080"):
            request = urllib.request.Request(
                f"http://127.0.0.1:{self.httpd.server_port}/api/logout",
                data=b"{}",
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Origin": f"http://{host}",
                    "Host": host,
                },
            )
            with opener.open(request) as response:
                self.assertEqual(response.status, 200, response.read())

    def test_deactivating_member_preserves_financial_records_and_revokes_session(self):
        president, president_opener = self.register("presidente", self.setup_token)
        member, member_opener = self.register("socio")
        product = self.request(
            "/api/products",
            "POST",
            {"name": "Agua", "stock": 2, "priceCents": 75},
            president_opener,
        )[1]["product"]
        status, _ = self.request(
            "/api/consume",
            "POST",
            {"productId": product["id"]},
            member_opener,
        )
        self.assertEqual(status, 201)
        self.assertEqual(self.request("/api/debts", opener=member_opener)[1]["debts"][0]["consumer"]["uid"], member["id"])

        status, _ = self.request(
            "/api/deactivate-member",
            "POST",
            {"userId": member["id"]},
            president_opener,
        )
        self.assertEqual(status, 200)
        self.assertIsNone(self.request("/api/me", opener=member_opener)[1]["user"])
        history = self.store.list_debts(president)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["amountCents"], 75)

    def test_pot_only_counts_confirmed_income_and_presidentially_approved_expenses(self):
        _, president_opener = self.register("presidente", self.setup_token)
        member, member_opener = self.register("socio")
        product = self.request(
            "/api/products",
            "POST",
            {"name": "Coca-Cola", "stock": 1, "priceCents": 150},
            president_opener,
        )[1]["product"]
        _, consumption = self.request(
            "/api/consume",
            "POST",
            {"productId": product["id"]},
            member_opener,
        )
        transaction_id = consumption["transactionId"]
        self.request(
            "/api/request-payment",
            "POST",
            {"transactionId": transaction_id},
            member_opener,
        )
        self.assertEqual(self.request("/api/ledger")[1]["potBalanceCents"], 0)
        self.request(
            "/api/confirm-payment",
            "POST",
            {"transactionId": transaction_id},
            president_opener,
        )
        self.assertEqual(self.request("/api/ledger")[1]["potBalanceCents"], 150)

        status, result = self.request(
            "/api/expenses",
            "POST",
            {"description": "Bolsas de basura", "amountCents": 90},
            member_opener,
        )
        self.assertEqual(status, 201, result)
        expense_id = result["expense"]["id"]
        self.assertEqual(result["expense"]["requesterId"], member["id"])
        ledger = self.request("/api/ledger")[1]
        self.assertEqual(ledger["potBalanceCents"], 150)
        self.assertEqual(ledger["pendingExpensesCents"], 90)
        expenses = self.request("/api/expenses", opener=president_opener)[1]["expenses"]
        self.assertEqual(expenses[0]["status"], "pending")

        status, _ = self.request(
            "/api/approve-expense",
            "POST",
            {"expenseId": expense_id},
            member_opener,
        )
        self.assertEqual(status, 403)
        status, _ = self.request(
            "/api/approve-expense",
            "POST",
            {"expenseId": expense_id},
            president_opener,
        )
        self.assertEqual(status, 200)
        ledger = self.request("/api/ledger")[1]
        self.assertEqual(ledger["approvedExpensesCents"], 90)
        self.assertEqual(ledger["potBalanceCents"], 60)
        approved = self.request("/api/expenses", opener=member_opener)[1]["expenses"][0]
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["reviewerName"], "Presidente Txoko")

        _, overspend = self.request(
            "/api/expenses",
            "POST",
            {"description": "Compra demasiado cara", "amountCents": 61},
            member_opener,
        )
        status, error = self.request(
            "/api/approve-expense",
            "POST",
            {"expenseId": overspend["expense"]["id"]},
            president_opener,
        )
        self.assertEqual(status, 409)
        self.assertIn("bote disponible", error["error"])
        self.assertEqual(self.request("/api/ledger")[1]["potBalanceCents"], 60)
        still_pending = next(
            expense
            for expense in self.request("/api/expenses", opener=president_opener)[1]["expenses"]
            if expense["id"] == overspend["expense"]["id"]
        )
        self.assertEqual(still_pending["status"], "pending")

    def test_expense_api_requires_active_members_and_president_review_is_single_use(self):
        _, president_opener = self.register("presidente", self.setup_token)
        member, member_opener = self.register("socio")
        status, _ = self.request("/api/expenses")
        self.assertEqual(status, 401)

        status, expense = self.request(
            "/api/expenses",
            "POST",
            {"description": "Agua para el Txoko", "amountCents": 125},
            member_opener,
        )
        self.assertEqual(status, 201)
        expense_id = expense["expense"]["id"]
        status, _ = self.request(
            "/api/reject-expense",
            "POST",
            {"expenseId": expense_id},
            president_opener,
        )
        self.assertEqual(status, 200)
        status, _ = self.request(
            "/api/reject-expense",
            "POST",
            {"expenseId": expense_id},
            president_opener,
        )
        self.assertEqual(status, 409)
        rejected = self.request("/api/expenses", opener=member_opener)[1]["expenses"][0]
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["reviewerName"], "Presidente Txoko")
        self.assertEqual(self.request("/api/ledger")[1]["potBalanceCents"], 0)


if __name__ == "__main__":
    unittest.main()
