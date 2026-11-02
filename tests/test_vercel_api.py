import asyncio
import tempfile
import unittest
from datetime import date
from pathlib import Path

try:
    import httpx
    from app import app
    from server import TxokoStore
except ImportError:
    httpx = None
    app = None
    TxokoStore = None


@unittest.skipIf(httpx is None, "Install requirements-test.txt to run Vercel API integration tests.")
class VercelApiTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = TxokoStore(Path(self.temp_dir.name))
        app.state.txoko_store = self.store

    def tearDown(self):
        app.state.txoko_store = None
        self.temp_dir.cleanup()

    def test_fastapi_routes_preserve_login_inventory_payments_and_pot(self):
        async def workflow():
            transport = httpx.ASGITransport(app=app)
            async with (
                httpx.AsyncClient(transport=transport, base_url="http://txoko.test") as president,
                httpx.AsyncClient(transport=transport, base_url="http://txoko.test") as member,
                httpx.AsyncClient(transport=transport, base_url="http://txoko.test") as guest,
            ):
                self.assertEqual((await president.get("/")).status_code, 200)
                self.assertEqual((await president.get("/app.js")).status_code, 200)
                self.assertEqual((await president.get("/styles.css")).status_code, 200)
                foreign_origin = await president.post(
                    "/api/logout",
                    headers={"Origin": "https://attacker.test"},
                    json={},
                )
                self.assertEqual(foreign_origin.status_code, 403)
                status = await president.get("/api/status")
                self.assertTrue(status.json()["needsPresident"])

                registered = await president.post(
                    "/api/register",
                    json={
                        "firstName": "Pablo",
                        "lastName": "Presidente",
                        "username": "presidente",
                        "pin": "123456",
                        "setupToken": self.store.setup_token_path.read_text(encoding="utf-8").strip(),
                    },
                )
                self.assertEqual(registered.status_code, 201, registered.text)
                product = await president.post(
                    "/api/products",
                    json={"name": "Coca-Cola", "stock": 1, "priceCents": 150},
                )
                self.assertEqual(product.status_code, 201, product.text)

                joined = await member.post(
                    "/api/register",
                    json={
                        "firstName": "Ana",
                        "lastName": "Socia",
                        "username": "ana",
                        "pin": "654321",
                    },
                )
                self.assertEqual(joined.status_code, 201, joined.text)
                year = date.today().year
                cleaning_group = await member.post(
                    "/api/cleaning-groups",
                    json={
                        "year": year,
                        "name": "Turno Verde",
                        "memberIds": [
                            registered.json()["user"]["id"],
                            joined.json()["user"]["id"],
                        ],
                    },
                )
                self.assertEqual(cleaning_group.status_code, 201, cleaning_group.text)
                self.assertEqual(
                    (
                        await member.post(
                            "/api/confirm-cleaning-groups",
                            json={"year": year},
                        )
                    ).status_code,
                    403,
                )
                confirmation = await president.post(
                    "/api/confirm-cleaning-groups",
                    json={"year": year},
                )
                self.assertEqual(confirmation.status_code, 200, confirmation.text)
                cleaning = await member.get(f"/api/cleaning?year={year}")
                self.assertTrue(cleaning.json()["cleaning"]["confirmed"])
                self.assertGreaterEqual(len(cleaning.json()["cleaning"]["schedule"]), 52)

                proposal = await member.post(
                    "/api/rules",
                    json={"text": "Dejar limpio el espacio después de usarlo."},
                )
                self.assertEqual(proposal.status_code, 201, proposal.text)
                self.assertEqual(
                    (
                        await member.post(
                            "/api/review-rule",
                            json={"ruleId": proposal.json()["rule"]["id"], "approved": True},
                        )
                    ).status_code,
                    403,
                )
                reviewed_rule = await president.post(
                    "/api/review-rule",
                    json={"ruleId": proposal.json()["rule"]["id"], "approved": True},
                )
                self.assertEqual(reviewed_rule.status_code, 200, reviewed_rule.text)
                visible_rules = await member.get("/api/rules")
                self.assertEqual(visible_rules.json()["rules"][0]["status"], "approved")

                member_product = await member.post(
                    "/api/products",
                    json={"name": "Agua", "stock": 6, "priceCents": 100},
                )
                self.assertEqual(member_product.status_code, 201, member_product.text)
                events = await member.get("/api/inventory-events")
                self.assertEqual(events.status_code, 200, events.text)
                member_event = next(
                    event
                    for event in events.json()["events"]
                    if event["productId"] == member_product.json()["product"]["id"]
                )
                self.assertEqual(member_event["actorName"], "Ana Socia")
                self.assertEqual(member_event["quantity"], 6)
                self.assertEqual(
                    (await guest.get("/api/inventory-events")).status_code,
                    401,
                )
                consumed = await member.post(
                    "/api/consume",
                    json={"productId": product.json()["product"]["id"]},
                )
                self.assertEqual(consumed.status_code, 201, consumed.text)
                transaction_id = consumed.json()["transactionId"]
                self.assertEqual(
                    (await member.post("/api/request-payment", json={"transactionId": transaction_id})).status_code,
                    200,
                )
                self.assertEqual(
                    (await president.post("/api/confirm-payment", json={"transactionId": transaction_id})).status_code,
                    200,
                )

                expense = await member.post(
                    "/api/expenses",
                    json={"description": "Bolsas de basura", "amountCents": 100},
                )
                self.assertEqual(expense.status_code, 201, expense.text)
                self.assertEqual(
                    (
                        await member.post(
                            "/api/approve-expense",
                            json={"expenseId": expense.json()["expense"]["id"]},
                        )
                    ).status_code,
                    403,
                )
                approved = await president.post(
                    "/api/approve-expense",
                    json={"expenseId": expense.json()["expense"]["id"]},
                )
                self.assertEqual(approved.status_code, 200, approved.text)
                self.assertEqual((await president.get("/api/ledger")).json()["potBalanceCents"], 50)

        asyncio.run(workflow())


if __name__ == "__main__":
    unittest.main()
