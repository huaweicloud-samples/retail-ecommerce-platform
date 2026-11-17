import base64
import json
import sys
import unittest
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC_ROOT))

from api import index as api  # noqa: E402
from consumer import index as consumer  # noqa: E402


PRODUCT = {
    "productId": "p-1",
    "name": "Test product",
    "price": 1000,
    "package": {"width": 100, "length": 100, "height": 100, "weight": 1000},
    "quantity": 1,
}
ADDRESS = {"country": "US", "street": "test"}


class Context:
    def getRequestID(self):
        return "unit-request"

    def getToken(self):
        return ""


class DomainTests(unittest.TestCase):
    def setUp(self):
        api.REPOSITORY = api.MemoryRepository()
        consumer.REPOSITORY = api.REPOSITORY
        consumer.EXPECTED_SOURCE = api.EG_SOURCE_NAME
        api.PUBLISHED_EVENTS.clear()
        self.token = self._token("customer-unit", ["admin"])

    @staticmethod
    def _token(subject, roles):
        secret = "retail-local-only-secret-v1"
        header = api.base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=").decode()
        payload = {"sub": subject, "roles": roles, "iss": api.JWT_ISSUER, "aud": api.JWT_AUDIENCE}
        payload_text = json.dumps(payload, separators=(",", ":")).encode()
        payload_part = api.base64.urlsafe_b64encode(payload_text).rstrip(b"=").decode()
        signing = f"{header}.{payload_part}"
        signature = api.hmac.new(secret.encode(), signing.encode(), api.hashlib.sha256).digest()
        signature_part = api.base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
        return f"{signing}.{signature_part}"

    def invoke(self, method, path, payload=None, token=None, expected=200):
        body = json.dumps(payload or {}).encode()
        event = {
            "httpMethod": method,
            "path": path,
            "headers": {"Authorization": f"Bearer {token or self.token}"},
            "body": base64.b64encode(body).decode(),
            "isBase64Encoded": True,
        }
        result = api.handler(event, Context())
        self.assertEqual(result["statusCode"], expected, result)
        return json.loads(result["body"])

    def test_product_validation_and_delivery_quote(self):
        self.invoke("POST", "/products/seed", {"products": [PRODUCT]})
        result = self.invoke("POST", "/products/validate", {"products": [PRODUCT]})
        self.assertTrue(result["valid"])
        quote = self.invoke("POST", "/delivery/quote", {"products": [PRODUCT], "address": ADDRESS})
        self.assertEqual(quote["quote"], 1500)

    def test_payment_idempotency(self):
        first = self.invoke("POST", "/payments/simulate", {
            "amount": 2500, "idempotencyKey": "same-key", "cardNumber": "1234567890123456",
        })
        second = self.invoke("POST", "/payments/simulate", {
            "amount": 2500, "idempotencyKey": "same-key", "cardNumber": "1234567890123456",
        })
        self.assertEqual(first["paymentToken"], second["paymentToken"])
        self.assertTrue(second["idempotent"])

    def test_lifecycle_and_duplicate_event(self):
        self.invoke("POST", "/products/seed", {"products": [PRODUCT]})
        payment = self.invoke("POST", "/payments/simulate", {
            "amount": 2500, "idempotencyKey": "order-key", "cardNumber": "1234567890123456",
        })
        order = self.invoke("POST", "/orders", {
            "products": [PRODUCT], "address": ADDRESS, "deliveryPrice": 1500,
            "paymentToken": payment["paymentToken"],
        }, expected=201)["order"]
        self._consume_all()
        self.assertEqual(self._get(order["orderId"])["status"], "NEW")
        self.invoke("POST", f"/orders/{order['orderId']}/warehouse/start")
        self._consume_all()
        self.invoke("POST", f"/orders/{order['orderId']}/warehouse/complete")
        self._consume_all()
        self.assertEqual(self._get(order["orderId"])["status"], "PACKAGED")
        self.invoke("POST", f"/orders/{order['orderId']}/delivery/start")
        self._consume_all()
        self.invoke("POST", f"/orders/{order['orderId']}/delivery/complete")
        events = self._consume_all()
        self.assertEqual(self._get(order["orderId"])["status"], "FULFILLED")
        duplicate = consumer.handler(events[-1], Context())
        self.assertEqual(duplicate["status"], "duplicate")

    def test_customer_isolation_and_request_customer_id_is_ignored(self):
        self.invoke("POST", "/products/seed", {"products": [PRODUCT]})
        payment = self.invoke("POST", "/payments/simulate", {
            "amount": 2500, "idempotencyKey": "isolation-key", "cardNumber": "1234567890123456",
        })
        order = self.invoke("POST", "/orders", {
            "products": [PRODUCT], "address": ADDRESS, "deliveryPrice": 1500,
            "paymentToken": payment["paymentToken"], "customerId": "not-the-token-sub",
        }, expected=201)["order"]
        other = self._token("customer-other", [])
        self.invoke("GET", f"/orders/{order['orderId']}", token=other, expected=404)
        self.assertEqual(self._get(order["orderId"])["customerId"], "customer-unit")

    def _consume_all(self):
        events = []
        while api.PUBLISHED_EVENTS:
            events.append(api.PUBLISHED_EVENTS.pop(0))
            consumer.handler(events[-1], Context())
        return events

    def _get(self, order_id):
        return self.invoke("GET", f"/orders/{order_id}")["order"]


if __name__ == "__main__":
    unittest.main()
