#!/usr/bin/env python3
"""Verify the REST and event fulfillment loop.

Without --base-url the script runs the same FunctionGraph handlers in-process
and verifies the full NEW -> PACKAGED -> FULFILLED flow plus duplicate event
handling.  With --base-url it calls an already published APIG endpoint and
waits for the asynchronous EG consumer to reach FULFILLED.

Only the controlled fixture or --token is used.  The script never reads
credentials.csv and never runs Terraform.
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
FIXTURE = ROOT / "src" / "fixtures" / "test.jwt.json"
sys.path.insert(0, str(ROOT / "src" / "scripts"))
from make_test_jwt import encode_token  # noqa: E402


PRODUCTS = [{
    "productId": "demo-coffee-maker",
    "name": "Demo coffee maker",
    "price": 1999,
    "package": {"width": 300, "length": 240, "height": 220, "weight": 2600},
    "quantity": 2,
}]
ADDRESS = {
    "name": "Demo Customer",
    "street": "1 Verification Road",
    "city": "Beijing",
    "country": "US",
    "postCode": "100000",
}


def load_token(subject=None, roles=None):
    with open(FIXTURE, encoding="utf-8") as stream:
        fixture = json.load(stream)
    claims = dict(fixture["claims"])
    if subject:
        claims["sub"] = subject
    if roles is not None:
        claims["roles"] = roles
    return encode_token(fixture["secret"], fixture["issuer"], fixture["audience"], claims, 3600)


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def local_run():
    sys.path.insert(0, str(SRC))
    from api import index as api  # noqa: WPS433
    from consumer import index as consumer  # noqa: WPS433

    api.REPOSITORY = api.MemoryRepository()
    consumer.REPOSITORY = api.REPOSITORY
    consumer.EXPECTED_SOURCE = api.EG_SOURCE_NAME
    api.PUBLISHED_EVENTS.clear()

    class Context:
        def __init__(self):
            self.counter = 0

        def getRequestID(self):
            self.counter += 1
            return f"local-request-{self.counter}"

        def getToken(self):
            return ""

    context = Context()
    admin_token = load_token()
    other_token = load_token("customer-other-002", [])

    def invoke(method, path, token=admin_token, payload=None, expected=200):
        raw = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        event = {
            "httpMethod": method,
            "path": path,
            "headers": {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            "body": base64.b64encode(raw).decode("ascii"),
            "isBase64Encoded": True,
        }
        result = api.handler(event, context)
        body = json.loads(result["body"])
        assert_true(result["statusCode"] == expected, f"{method} {path}: expected {expected}, got {result}")
        return body

    def drain_events():
        processed = []
        while api.PUBLISHED_EVENTS:
            event = api.PUBLISHED_EVENTS.pop(0)
            processed.append((event, consumer.handler(event, context)))
        return processed

    invoke("POST", "/products/seed", payload={"products": PRODUCTS})
    validation = invoke("POST", "/products/validate", payload={"products": PRODUCTS})
    assert_true(validation["valid"] is True, f"product validation failed: {validation}")

    quote = invoke("POST", "/delivery/quote", payload={"products": PRODUCTS, "address": ADDRESS})
    delivery_amount = quote["quote"]
    total = delivery_amount + sum(item["price"] * item["quantity"] for item in PRODUCTS)

    payment_payload = {"amount": total, "idempotencyKey": "verify-payment-001", "cardNumber": "1234567890123456"}
    first_payment = invoke("POST", "/payments/simulate", payload=payment_payload)
    second_payment = invoke("POST", "/payments/simulate", payload=payment_payload)
    assert_true(first_payment["paymentToken"] == second_payment["paymentToken"], "payment token changed on retry")
    assert_true(second_payment["idempotent"] is True, "payment retry was not idempotent")

    order_body = invoke("POST", "/orders", payload={
        "products": PRODUCTS,
        "address": ADDRESS,
        "deliveryPrice": delivery_amount,
        "paymentToken": first_payment["paymentToken"],
        "customerId": "attacker-supplied-value-is-ignored",
    }, expected=201)
    order_id = order_body["order"]["orderId"]
    drain_events()
    assert_true(invoke("GET", f"/orders/{order_id}")["order"]["status"] == "NEW", "order did not start NEW")

    invoke("POST", f"/orders/{order_id}/warehouse/start")
    drain_events()
    assert_true(invoke("GET", f"/orders/{order_id}")["order"]["status"] == "NEW", "warehouse start changed order state")

    invoke("POST", f"/orders/{order_id}/warehouse/complete")
    drain_events()
    assert_true(invoke("GET", f"/orders/{order_id}")["order"]["status"] == "PACKAGED", "warehouse completion did not package order")

    invoke("POST", f"/orders/{order_id}/delivery/start")
    drain_events()
    assert_true(invoke("GET", f"/orders/{order_id}")["order"]["status"] == "PACKAGED", "delivery start changed order state")

    invoke("POST", f"/orders/{order_id}/delivery/complete")
    final_events = drain_events()
    assert_true(invoke("GET", f"/orders/{order_id}")["order"]["status"] == "FULFILLED", "delivery completion did not fulfill order")
    assert_true(final_events, "delivery completion did not publish an event")
    duplicate = consumer.handler(final_events[-1][0], context)
    assert_true(duplicate["status"] == "duplicate", f"duplicate event was not ignored: {duplicate}")

    cross_customer = invoke("GET", f"/orders/{order_id}", token=other_token, expected=404)
    assert_true(cross_customer["code"] == "not_found", "order was visible to another JWT subject")
    print(json.dumps({
        "mode": "local",
        "passed": True,
        "orderId": order_id,
        "states": ["NEW", "PACKAGED", "FULFILLED"],
        "paymentRetry": "idempotent",
        "duplicateEvent": "ignored",
        "customerIsolation": "passed",
    }, ensure_ascii=False, indent=2))


def http_json(base_url, method, path, token, payload=None):
    body = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Stage": os.environ.get("RETAIL_APIG_STAGE", "prod"),
    }
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(base_url.rstrip("/") + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status = response.status
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read().decode("utf-8")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = {"raw": raw}
    return status, value


def cloud_run(base_url, timeout):
    token = load_token()
    status, health = http_json(base_url, "GET", "/health", token)
    assert_true(status == 200 and health.get("ok") is True, f"health check failed: {status} {health}")
    for path, payload in [("/products/seed", {"products": PRODUCTS})]:
        status, result = http_json(base_url, "POST", path, token, payload)
        assert_true(status == 200, f"seed failed: {status} {result}")
    status, result = http_json(base_url, "POST", "/products/validate", token, {"products": PRODUCTS})
    assert_true(status == 200 and result.get("valid") is True, f"validation failed: {status} {result}")
    status, result = http_json(base_url, "POST", "/delivery/quote", token, {"products": PRODUCTS, "address": ADDRESS})
    assert_true(status == 200, f"quote failed: {status} {result}")
    delivery_amount = result["quote"]
    total = delivery_amount + sum(item["price"] * item["quantity"] for item in PRODUCTS)
    payment_payload = {"amount": total, "idempotencyKey": "verify-cloud-payment-001", "cardNumber": "1234567890123456"}
    status, first = http_json(base_url, "POST", "/payments/simulate", token, payment_payload)
    assert_true(status == 200, f"payment failed: {status} {first}")
    status, second = http_json(base_url, "POST", "/payments/simulate", token, payment_payload)
    assert_true(status == 200 and second["paymentToken"] == first["paymentToken"] and second["idempotent"], "cloud payment retry failed")
    status, result = http_json(base_url, "POST", "/orders", token, {
        "products": PRODUCTS, "address": ADDRESS, "deliveryPrice": delivery_amount,
        "paymentToken": first["paymentToken"],
    })
    assert_true(status == 201, f"order creation failed: {status} {result}")
    order_id = result["order"]["orderId"]
    expected = [
        ("NEW", "/orders/{0}/warehouse/start"),
        ("PACKAGED", "/orders/{0}/warehouse/complete"),
        ("PACKAGED", "/orders/{0}/delivery/start"),
        ("FULFILLED", "/orders/{0}/delivery/complete"),
    ]
    observed_states = []
    for wanted, path_template in expected:
        status, result = http_json(base_url, "POST", path_template.format(order_id), token, {})
        assert_true(status == 200, f"action failed: {status} {result}")
        deadline = time.time() + timeout
        observed = None
        while time.time() < deadline:
            status, current = http_json(base_url, "GET", f"/orders/{order_id}", token)
            if status == 200:
                observed = current["order"]["status"]
                if observed == wanted:
                    break
            time.sleep(3)
        assert_true(observed == wanted, f"unexpected state after {path_template}: {observed}, wanted {wanted}")
        observed_states.append(observed)
    assert_true(observed_states == ["NEW", "PACKAGED", "PACKAGED", "FULFILLED"], f"unexpected cloud states: {observed_states}")
    print(json.dumps({
        "mode": "cloud",
        "passed": True,
        "orderId": order_id,
        "states": ["NEW", "PACKAGED", "FULFILLED"],
        "paymentRetry": "idempotent",
        "duplicateEvent": "verify locally or publish the same CloudEvent id twice through EG",
    }, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Verify the retail REST and event fulfillment loop")
    parser.add_argument("--base-url", help="APIG debug base URL; omitted means local in-process verification")
    parser.add_argument("--timeout", type=int, default=300, help="cloud event propagation wait in seconds")
    args = parser.parse_args()
    if args.base_url:
        cloud_run(args.base_url, args.timeout)
    else:
        local_run()


if __name__ == "__main__":
    main()
