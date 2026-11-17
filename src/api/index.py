"""REST ingress for the retail ecommerce verification application.

The file is intentionally self-contained because Terraform uploads it as an
inline FunctionGraph source file.  The default repository is an in-process
memory implementation.  Cloud mode uses the standard DynamoDB JSON API and
SigV4, which is the data-plane contract exposed by GeminiDB.
"""

import base64
import copy
import hashlib
import hmac
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone


APP_NAMESPACE = os.environ.get("APP_NAMESPACE", "retail-ecommerce-platform")
REPOSITORY_MODE = os.environ.get("REPOSITORY_MODE", "memory")
GEMINIDB_ENDPOINT = os.environ.get("GEMINIDB_ENDPOINT", "")
GEMINIDB_PORT = int(os.environ.get("GEMINIDB_PORT", "8000"))
GEMINIDB_PROTOCOL = os.environ.get("GEMINIDB_PROTOCOL", "https")
GEMINIDB_ACCESS_KEY = os.environ.get("GEMINIDB_ACCESS_KEY", "")
GEMINIDB_SECRET_KEY = os.environ.get("GEMINIDB_SECRET_KEY", "")
GEMINIDB_REGION = os.environ.get("GEMINIDB_REGION", "cn-north-4")
TABLES = {
    "products": os.environ.get("PRODUCT_TABLE", "retail_ecommerce_products"),
    "orders": os.environ.get("ORDER_TABLE", "retail_ecommerce_orders"),
    "payments": os.environ.get("PAYMENT_TABLE", "retail_ecommerce_payments"),
    "idempotency": os.environ.get("IDEMPOTENCY_TABLE", "retail_ecommerce_idempotency"),
}
JWT_SECRET = os.environ.get("JWT_SECRET", "retail-local-only-secret-v1").encode("utf-8")
JWT_ISSUER = os.environ.get("JWT_ISSUER", "retail-ecommerce-platform-local")
JWT_AUDIENCE = os.environ.get("JWT_AUDIENCE", "retail-ecommerce-platform-api")
EG_REGION = os.environ.get("EG_REGION", "cn-north-4")
EG_PROJECT_ID = os.environ.get("EG_PROJECT_ID", "")
EG_CHANNEL_ID = os.environ.get("EG_CHANNEL_ID", "")
EG_SOURCE_NAME = os.environ.get("EG_SOURCE_NAME", f"{APP_NAMESPACE}-events-source")
EG_ENDPOINT = os.environ.get("EG_ENDPOINT", f"https://eg.{EG_REGION}.myhuaweicloud.com")

COUNTRY_SHIPPING_FEES = {
    "DK": 0, "FI": 0, "NO": 0, "SE": 0,
    "AT": 1000, "BE": 1000, "BG": 1000, "CY": 1000,
    "CZ": 1000, "DE": 1000, "EE": 1000, "ES": 1000,
    "FR": 1000, "GR": 1000, "HR": 1000, "HU": 1000,
    "IE": 1000, "IT": 1000, "LT": 1000, "LU": 1000,
    "LV": 1000, "MT": 1000, "NL": 1000, "PO": 1000,
    "PT": 1000, "RO": 1000, "SI": 1000, "SK": 1000,
    "CA": 1500, "US": 1500,
    "*": 2500,
}
BOX_VOLUME = 500 * 500 * 500
BOX_WEIGHT = 12000
PUBLISHED_EVENTS = []


class DomainError(Exception):
    """A safe, expected business error."""

    def __init__(self, message, status=400, code="bad_request"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


class RepositoryNotConfigured(DomainError):
    """The external repository adapter was selected without an endpoint."""

    def __init__(self):
        super().__init__("repository endpoint is not configured", 503, "repository_unavailable")


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def log(level, message, **fields):
    record = {
        "level": level,
        "service": APP_NAMESPACE,
        "message": message,
        "time": now_iso(),
    }
    record.update(fields)
    print(json.dumps(record, ensure_ascii=False, sort_keys=True))


def clone(value):
    return copy.deepcopy(value)


def validate_product_shape(product):
    if not isinstance(product, dict):
        raise DomainError("each product must be an object")
    required = {"productId", "name", "price", "package"}
    missing = sorted(required.difference(product))
    if missing:
        raise DomainError(f"product is missing fields: {', '.join(missing)}")
    if not isinstance(product["productId"], str) or not product["productId"]:
        raise DomainError("productId must be a non-empty string")
    if not isinstance(product["price"], int) or product["price"] < 0:
        raise DomainError(f"product {product['productId']} price must be a non-negative integer")
    package = product["package"]
    if not isinstance(package, dict):
        raise DomainError(f"product {product['productId']} package must be an object")
    for key in ("width", "length", "height", "weight"):
        if not isinstance(package.get(key), (int, float)) or package[key] <= 0:
            raise DomainError(f"product {product['productId']} package.{key} must be positive")


def count_boxes(products):
    volume = sum(
        p["package"]["width"] * p["package"]["length"] * p["package"]["height"]
        for p in products
    )
    weight = sum(p["package"]["weight"] for p in products)
    return max(1, math.ceil(volume / BOX_VOLUME), math.ceil(weight / BOX_WEIGHT))


def delivery_price(products, address):
    if not isinstance(address, dict) or not isinstance(address.get("country"), str):
        raise DomainError("address.country is required")
    return count_boxes(products) * COUNTRY_SHIPPING_FEES.get(address["country"].upper(), COUNTRY_SHIPPING_FEES["*"])


class MemoryRepository:
    """Deterministic repository used by local tests and the no-database mode."""

    def __init__(self):
        self.products = {}
        self.orders = {}
        self.payments = {}
        self.processed_events = set()
        self.processed_operations = set()

    def seed_products(self, products):
        for product in products:
            validate_product_shape(product)
            self.products[product["productId"]] = clone(product)
        return {"seeded": len(products), "productIds": sorted(p["productId"] for p in products)}

    def validate_products(self, products):
        invalid = []
        for requested in products:
            try:
                validate_product_shape(requested)
            except DomainError as exc:
                invalid.append({"product": clone(requested), "reason": exc.message})
                continue
            stored = self.products.get(requested["productId"])
            if stored is None:
                invalid.append({"product": clone(requested), "reason": "product not found"})
                continue
            for key in ("name", "price", "package"):
                if requested.get(key) != stored.get(key):
                    invalid.append({
                        "product": clone(requested),
                        "reason": f"invalid value for {key}",
                    })
                    break
        return invalid

    def simulate_payment(self, customer_id, amount, idempotency_key, card_number):
        if not isinstance(amount, int) or amount <= 0:
            raise DomainError("amount must be a positive integer")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise DomainError("idempotencyKey is required")
        if not isinstance(card_number, str) or len(card_number) != 16 or not card_number.isdigit():
            raise DomainError("cardNumber must contain 16 digits")
        key = f"{customer_id}:{idempotency_key}"
        existing = self.payments.get(key)
        if existing:
            if existing["amount"] != amount:
                raise DomainError("idempotencyKey was already used with another amount", 409, "idempotency_conflict")
            return {"paymentToken": existing["paymentToken"], "idempotent": True, "amount": amount}
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        payment = {
            "paymentToken": f"pay_{digest}",
            "customerId": customer_id,
            "amount": amount,
            "status": "AUTHORIZED",
            "idempotencyKey": idempotency_key,
            "createdAt": now_iso(),
        }
        self.payments[key] = payment
        return {"paymentToken": payment["paymentToken"], "idempotent": False, "amount": amount}

    def validate_payment(self, customer_id, payment_token, amount):
        for payment in self.payments.values():
            if payment["paymentToken"] == payment_token:
                return payment["customerId"] == customer_id and payment["amount"] == amount
        return False

    def create_order(self, customer_id, products, address, delivery_amount, payment_token):
        invalid = self.validate_products(products)
        if invalid:
            raise DomainError("products failed validation", 422, "invalid_products")
        expected_delivery = delivery_price(products, address)
        if delivery_amount != expected_delivery:
            raise DomainError("deliveryPrice does not match the quote result", 422, "invalid_delivery_price")
        total = delivery_amount + sum(p["price"] * p.get("quantity", 1) for p in products)
        if not self.validate_payment(customer_id, payment_token, total):
            raise DomainError("paymentToken is not valid for this customer and total", 422, "invalid_payment")
        order_id = str(uuid.uuid4())
        order = {
            "orderId": order_id,
            "customerId": customer_id,
            "products": clone(products),
            "address": clone(address),
            "deliveryPrice": delivery_amount,
            "total": total,
            "paymentToken": payment_token,
            "status": "NEW",
            "warehouseStatus": "READY",
            "deliveryStatus": "WAITING",
            "createdAt": now_iso(),
            "modifiedAt": now_iso(),
        }
        self.orders[order_id] = order
        return clone(order)

    def get_order(self, customer_id, order_id):
        order = self.orders.get(order_id)
        if order is None or order["customerId"] != customer_id:
            return None
        return clone(order)

    def update_order_action(self, customer_id, order_id, action):
        order = self.orders.get(order_id)
        if order is None or order["customerId"] != customer_id:
            raise DomainError("order not found", 404, "not_found")
        transitions = {
            "warehouse_start": ("NEW", "warehouseStatus", "READY", "IN_PROGRESS"),
            "warehouse_complete": ("NEW", "warehouseStatus", "IN_PROGRESS", "COMPLETED"),
            "delivery_start": ("PACKAGED", "deliveryStatus", "WAITING", "IN_PROGRESS"),
            "delivery_complete": ("PACKAGED", "deliveryStatus", "IN_PROGRESS", "COMPLETED"),
        }
        if action not in transitions:
            raise DomainError("unknown order action")
        state, field, expected, target = transitions[action]
        if order["status"] != state or order[field] != expected:
            raise DomainError(
                f"cannot apply {action} while order is {order['status']}/{order[field]}",
                409,
                "invalid_state_transition",
            )
        order[field] = target
        order["modifiedAt"] = now_iso()
        return clone(order)

    def apply_event(self, event):
        event_id = event.get("id")
        data = event.get("data") or {}
        order_id = data.get("orderId")
        event_type = event.get("type", "")
        if not event_id or not order_id:
            raise DomainError("event id and data.orderId are required")
        operation_key = f"{event_type}:{order_id}"
        if event_id in self.processed_events or operation_key in self.processed_operations:
            return {"status": "duplicate", "eventId": event_id, "operationKey": operation_key}
        order = self.orders.get(order_id)
        if order is None:
            raise DomainError("event order not found", 404, "not_found")
        if event_type == "OrderCreated":
            pass
        elif event_type == "WarehouseStarted":
            pass
        elif event_type == "WarehouseCompleted":
            if order["status"] == "NEW" and order["warehouseStatus"] == "COMPLETED":
                order["status"] = "PACKAGED"
                order["deliveryStatus"] = "WAITING"
                order["modifiedAt"] = now_iso()
            elif order["status"] != "PACKAGED":
                raise DomainError("warehouse completion cannot be applied", 409, "invalid_state_transition")
        elif event_type == "DeliveryStarted":
            pass
        elif event_type == "DeliveryCompleted":
            if order["status"] == "PACKAGED" and order["deliveryStatus"] == "COMPLETED":
                order["status"] = "FULFILLED"
                order["modifiedAt"] = now_iso()
            elif order["status"] != "FULFILLED":
                raise DomainError("delivery completion cannot be applied", 409, "invalid_state_transition")
        else:
            return {"status": "ignored", "eventId": event_id, "eventType": event_type}
        self.processed_events.add(event_id)
        self.processed_operations.add(operation_key)
        return {"status": "processed", "eventId": event_id, "operationKey": operation_key}


class DynamoDBError(Exception):
    """A DynamoDB API error without exposing request credentials."""

    def __init__(self, code, message, status=502):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class DynamoDBRepository:
    """Standard DynamoDB JSON API repository for GeminiDB compatibility.

    The adapter deliberately uses only the public DynamoDB JSON 1.0 API.  It
    signs each request with SigV4 and never sends a custom
    application HTTP envelope.  Tables use ``pk`` and ``sk`` string keys; the
    namespace is part of every partition key so shared tables cannot mix apps.
    """

    SERVICE = "dynamodb"
    CONTENT_TYPE = "application/x-amz-json-1.0"
    CONDITIONAL_ERROR = "ConditionalCheckFailedException"

    def __init__(self, endpoint, port, protocol, access_key, secret_key, region, namespace, tables):
        if not endpoint or not access_key or not secret_key:
            raise RepositoryNotConfigured()
        self.endpoint = self._build_endpoint(endpoint, int(port), protocol)
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.namespace = namespace
        self.tables = dict(tables)

    @staticmethod
    def _build_endpoint(endpoint, port, protocol):
        raw = endpoint.strip()
        if "://" not in raw:
            raw = f"{protocol}://{raw}"
        parsed = urllib.parse.urlsplit(raw)
        if not parsed.scheme or not parsed.netloc:
            raise RepositoryNotConfigured()
        host = parsed.hostname or ""
        if not host:
            raise RepositoryNotConfigured()
        try:
            current_port = parsed.port
        except ValueError as exc:
            raise RepositoryNotConfigured() from exc
        if current_port is None:
            host_part = f"[{host}]" if ":" in host else host
            netloc = f"{host_part}:{port}"
        else:
            netloc = parsed.netloc
        path = parsed.path.rstrip("/") or "/"
        return urllib.parse.urlunsplit((parsed.scheme, netloc, path, "", ""))

    @staticmethod
    def _encode(value):
        if value is None:
            return {"NULL": True}
        if isinstance(value, bool):
            return {"BOOL": value}
        if isinstance(value, str):
            return {"S": value}
        if isinstance(value, int):
            return {"N": str(value)}
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("DynamoDB does not accept non-finite numbers")
            return {"N": repr(value)}
        if isinstance(value, list):
            return {"L": [DynamoDBRepository._encode(item) for item in value]}
        if isinstance(value, dict):
            return {"M": {str(key): DynamoDBRepository._encode(item) for key, item in value.items()}}
        raise TypeError(f"unsupported DynamoDB value type: {type(value).__name__}")

    @staticmethod
    def _decode(value):
        if "NULL" in value:
            return None
        if "BOOL" in value:
            return bool(value["BOOL"])
        if "S" in value:
            return value["S"]
        if "N" in value:
            number = value["N"]
            return int(number) if number.isdigit() or (number.startswith("-") and number[1:].isdigit()) else float(number)
        if "L" in value:
            return [DynamoDBRepository._decode(item) for item in value["L"]]
        if "M" in value:
            return {key: DynamoDBRepository._decode(item) for key, item in value["M"].items()}
        if "SS" in value:
            return list(value["SS"])
        if "NS" in value:
            return [int(item) if item.isdigit() else float(item) for item in value["NS"]]
        return value

    @classmethod
    def _encode_item(cls, item):
        return {key: cls._encode(value) for key, value in item.items()}

    @classmethod
    def _decode_item(cls, item):
        return {key: cls._decode(value) for key, value in item.items()}

    def _sign(self, body, target):
        parsed = urllib.parse.urlsplit(self.endpoint)
        host = parsed.netloc
        path = urllib.parse.quote(parsed.path or "/", safe="/-_.~")
        payload_hash = hashlib.sha256(body).hexdigest()
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        short_date = timestamp[:8]
        headers = {
            "content-type": self.CONTENT_TYPE,
            "host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": timestamp,
            "x-amz-target": f"DynamoDB_20120810.{target}",
        }
        canonical_headers = "".join(f"{key}:{headers[key].strip()}\n" for key in sorted(headers))
        signed_headers = ";".join(sorted(headers))
        canonical_request = "\n".join([
            "POST", path, "", canonical_headers, signed_headers, payload_hash,
        ])
        scope = f"{short_date}/{self.region}/{self.SERVICE}/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256", timestamp, scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ])

        def derive(key, value):
            return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()

        date_key = derive(("AWS4" + self.secret_key).encode("utf-8"), short_date)
        region_key = derive(date_key, self.region)
        service_key = derive(region_key, self.SERVICE)
        signing_key = derive(service_key, "aws4_request")
        signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        authorization = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return {
            "Content-Type": self.CONTENT_TYPE,
            "Host": host,
            "X-Amz-Content-Sha256": payload_hash,
            "X-Amz-Date": timestamp,
            "X-Amz-Target": headers["x-amz-target"],
            "Authorization": authorization,
        }

    def _request(self, target, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers=self._sign(body, target),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read()
                result = json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                result = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeError, json.JSONDecodeError):
                result = {}
            error_type = str(result.get("__type", "DynamoDBError")).rsplit("#", 1)[-1]
            message = str(result.get("message", "DynamoDB request failed"))
            raise DynamoDBError(error_type, message, exc.code) from exc
        except (urllib.error.URLError, TimeoutError, ValueError, UnicodeError) as exc:
            raise DynamoDBError("RepositoryUnavailable", "DynamoDB request failed", 503) from exc
        if not isinstance(result, dict):
            raise DynamoDBError("RepositoryProtocolError", "DynamoDB returned a non-object response")
        return result

    def _item(self, key, **fields):
        item = {"pk": key["pk"], "sk": key["sk"], "namespace": self.namespace}
        item.update(fields)
        return item

    def create_table(self, table_name, key_schema, attribute_definitions, capacity_mode="PAY_PER_REQUEST"):
        payload = {
            "TableName": table_name,
            "KeySchema": key_schema,
            "AttributeDefinitions": attribute_definitions,
            "BillingMode": capacity_mode,
        }
        return self._request("CreateTable", payload)

    def describe_table(self, table_name):
        return self._request("DescribeTable", {"TableName": table_name})

    def ensure_tables(self, timeout=8):
        key_schema = [
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ]
        attribute_definitions = [
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ]
        deadline = time.monotonic() + timeout
        for table_name in self.tables.values():
            try:
                self.create_table(table_name, key_schema, attribute_definitions)
            except DynamoDBError as exc:
                if exc.code not in {"ResourceInUseException", "TableAlreadyExistsException"}:
                    raise
            while True:
                status = self.describe_table(table_name).get("Table", {}).get("TableStatus")
                if status == "ACTIVE":
                    break
                if time.monotonic() >= deadline:
                    raise DynamoDBError("RepositoryUnavailable", "GeminiDB table is not ACTIVE", 503)
                time.sleep(0.3)

    def put_item(self, table_name, item, condition_expression=None):
        payload = {"TableName": table_name, "Item": self._encode_item(item)}
        if condition_expression:
            payload["ConditionExpression"] = condition_expression
        return self._request("PutItem", payload)

    def update_item(self, table_name, key, update_expression, expression_attribute_names=None,
                    expression_attribute_values=None, condition_expression=None,
                    return_values="ALL_NEW"):
        payload = {
            "TableName": table_name,
            "Key": self._encode_item(key),
            "UpdateExpression": update_expression,
            "ReturnValues": return_values,
        }
        if expression_attribute_names:
            payload["ExpressionAttributeNames"] = expression_attribute_names
        if expression_attribute_values:
            payload["ExpressionAttributeValues"] = self._encode_item(expression_attribute_values)
        if condition_expression:
            payload["ConditionExpression"] = condition_expression
        return self._request("UpdateItem", payload)

    def get_item(self, table_name, key, consistent_read=True):
        return self._request("GetItem", {
            "TableName": table_name,
            "Key": self._encode_item(key),
            "ConsistentRead": consistent_read,
        })

    def delete_item(self, table_name, key):
        return self._request("DeleteItem", {"TableName": table_name, "Key": self._encode_item(key)})

    def query(self, table_name, key_condition_expression, expression_attribute_values,
              expression_attribute_names=None, filter_expression=None, limit=None):
        payload = {
            "TableName": table_name,
            "KeyConditionExpression": key_condition_expression,
            "ExpressionAttributeValues": self._encode_item(expression_attribute_values),
        }
        if expression_attribute_names:
            payload["ExpressionAttributeNames"] = expression_attribute_names
        if filter_expression:
            payload["FilterExpression"] = filter_expression
        if limit:
            payload["Limit"] = limit
        result = self._request("Query", payload)
        result["Items"] = [self._decode_item(item) for item in result.get("Items", [])]
        return result

    def scan(self, table_name, filter_expression=None, expression_attribute_values=None, limit=None,
             exclusive_start_key=None):
        payload = {"TableName": table_name}
        if filter_expression:
            payload["FilterExpression"] = filter_expression
        if expression_attribute_values:
            payload["ExpressionAttributeValues"] = self._encode_item(expression_attribute_values)
        if limit:
            payload["Limit"] = limit
        if exclusive_start_key:
            payload["ExclusiveStartKey"] = self._encode_item(exclusive_start_key)
        result = self._request("Scan", payload)
        result["Items"] = [self._decode_item(item) for item in result.get("Items", [])]
        if result.get("LastEvaluatedKey"):
            result["LastEvaluatedKey"] = self._decode_item(result["LastEvaluatedKey"])
        return result

    def batch_write_item(self, requests):
        encoded = []
        for request in requests:
            if "PutRequest" in request:
                encoded.append({"PutRequest": {"Item": self._encode_item(request["PutRequest"]["Item"]), "TableName": request["PutRequest"].get("TableName")}})
            elif "DeleteRequest" in request:
                encoded.append({"DeleteRequest": {"Key": self._encode_item(request["DeleteRequest"]["Key"]), "TableName": request["DeleteRequest"].get("TableName")}})
        grouped = {}
        for request in encoded:
            operation = request["PutRequest"] if "PutRequest" in request else request["DeleteRequest"]
            table_name = operation.pop("TableName")
            grouped.setdefault(table_name, []).append(request)
        result = self._request("BatchWriteItem", {"RequestItems": grouped})
        for table_name, table_requests in result.get("UnprocessedItems", {}).items():
            for table_request in table_requests:
                operation = table_request.get("PutRequest") or table_request.get("DeleteRequest")
                if operation is not None:
                    operation["TableName"] = table_name
        return result

    @staticmethod
    def _key(pk, sk):
        return {"pk": pk, "sk": sk}

    def _read(self, table_name, key):
        result = self.get_item(table_name, key)
        item = result.get("Item")
        return self._decode_item(item) if item else None

    def _claim(self, key):
        try:
            self.put_item(TABLES["idempotency"], {
                "pk": f"NS#{self.namespace}#IDEMPOTENCY",
                "sk": key,
                "namespace": self.namespace,
                "entity": "idempotency",
                "claim": key,
                "createdAt": now_iso(),
            }, "attribute_not_exists(pk)")
            return True
        except DynamoDBError as exc:
            if exc.code == self.CONDITIONAL_ERROR:
                return False
            raise

    def _delete_claim(self, key):
        self.delete_item(TABLES["idempotency"], self._key(f"NS#{self.namespace}#IDEMPOTENCY", key))

    def seed_products(self, products):
        self.ensure_tables()
        # GeminiDB's compatible endpoint does not support BatchWriteItem reliably;
        # individual PutItem calls preserve the seed contract for this small catalog.
        for product in products:
            validate_product_shape(product)
            item = self._item(self._key(
                f"NS#{self.namespace}#PRODUCTS", f"PRODUCT#{product['productId']}"
            ), entity="product", **clone(product))
            self.put_item(TABLES["products"], item)
        return {"seeded": len(products), "productIds": sorted(product["productId"] for product in products)}

    def validate_products(self, products):
        invalid = []
        for requested in products:
            try:
                validate_product_shape(requested)
            except DomainError as exc:
                invalid.append({"product": clone(requested), "reason": exc.message})
                continue
            key = self._key(f"NS#{self.namespace}#PRODUCTS", f"PRODUCT#{requested['productId']}")
            stored = self._read(TABLES["products"], key)
            if stored is None:
                invalid.append({"product": clone(requested), "reason": "product not found"})
                continue
            for field in ("name", "price", "package"):
                if requested.get(field) != stored.get(field):
                    invalid.append({"product": clone(requested), "reason": f"invalid value for {field}"})
                    break
        return invalid

    def simulate_payment(self, customer_id, amount, idempotency_key, card_number):
        if not isinstance(amount, int) or amount <= 0:
            raise DomainError("amount must be a positive integer")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise DomainError("idempotencyKey is required")
        if not isinstance(card_number, str) or len(card_number) != 16 or not card_number.isdigit():
            raise DomainError("cardNumber must contain 16 digits")
        user_pk = f"NS#{self.namespace}#USER#{customer_id}"
        existing_result = self.query(
            TABLES["payments"], "pk = :pk", {":pk": user_pk, ":idempotency": idempotency_key},
            filter_expression="idempotencyKey = :idempotency", limit=1,
        )
        existing_items = existing_result.get("Items", [])
        if existing_items:
            existing = existing_items[0]
            if existing["amount"] != amount:
                raise DomainError("idempotencyKey was already used with another amount", 409, "idempotency_conflict")
            return {"paymentToken": existing["paymentToken"], "idempotent": True, "amount": amount}
        digest = hashlib.sha256(f"{customer_id}:{idempotency_key}".encode("utf-8")).hexdigest()[:24]
        payment = self._item(self._key(user_pk, f"PAYMENT#pay_{digest}"), entity="payment",
                             paymentToken=f"pay_{digest}", customerId=customer_id, amount=amount,
                             status="AUTHORIZED", idempotencyKey=idempotency_key, createdAt=now_iso())
        try:
            self.put_item(TABLES["payments"], payment, "attribute_not_exists(pk)")
        except DynamoDBError as exc:
            if exc.code != self.CONDITIONAL_ERROR:
                raise
            existing_result = self.query(
                TABLES["payments"], "pk = :pk", {":pk": user_pk, ":idempotency": idempotency_key},
                filter_expression="idempotencyKey = :idempotency", limit=1,
            )
            existing_items = existing_result.get("Items", [])
            if not existing_items or existing_items[0]["amount"] != amount:
                raise DomainError("idempotencyKey was already used with another amount", 409, "idempotency_conflict")
            return {"paymentToken": existing_items[0]["paymentToken"], "idempotent": True, "amount": amount}
        return {"paymentToken": payment["paymentToken"], "idempotent": False, "amount": amount}

    def validate_payment(self, customer_id, payment_token, amount):
        item = self._read(TABLES["payments"], self._key(
            f"NS#{self.namespace}#USER#{customer_id}", f"PAYMENT#{payment_token}"
        ))
        return bool(item and item.get("customerId") == customer_id and item.get("amount") == amount
                    and item.get("paymentToken") == payment_token)

    def create_order(self, customer_id, products, address, delivery_amount, payment_token):
        invalid = self.validate_products(products)
        if invalid:
            raise DomainError("products failed validation", 422, "invalid_products")
        expected_delivery = delivery_price(products, address)
        if delivery_amount != expected_delivery:
            raise DomainError("deliveryPrice does not match the quote result", 422, "invalid_delivery_price")
        total = delivery_amount + sum(product["price"] * product.get("quantity", 1) for product in products)
        if not self.validate_payment(customer_id, payment_token, total):
            raise DomainError("paymentToken is not valid for this customer and total", 422, "invalid_payment")
        order_id = str(uuid.uuid4())
        order = {
            "orderId": order_id, "customerId": customer_id, "products": clone(products),
            "address": clone(address), "deliveryPrice": delivery_amount, "total": total,
            "paymentToken": payment_token, "status": "NEW", "warehouseStatus": "READY",
            "deliveryStatus": "WAITING", "createdAt": now_iso(), "modifiedAt": now_iso(),
        }
        item = self._item(self._key(f"NS#{self.namespace}#USER#{customer_id}", f"ORDER#{order_id}"),
                          entity="order", **order)
        try:
            self.put_item(TABLES["orders"], item, "attribute_not_exists(pk)")
        except DynamoDBError as exc:
            raise DomainError("order could not be created", 503, "repository_unavailable") from exc
        return clone(order)

    def get_order(self, customer_id, order_id):
        item = self._read(TABLES["orders"], self._key(
            f"NS#{self.namespace}#USER#{customer_id}", f"ORDER#{order_id}"
        ))
        if item is None or item.get("customerId") != customer_id:
            return None
        return {key: clone(value) for key, value in item.items() if key not in {"pk", "sk", "namespace", "entity"}}

    def update_order_action(self, customer_id, order_id, action):
        transitions = {
            "warehouse_start": ("NEW", "warehouseStatus", "READY", "IN_PROGRESS"),
            "warehouse_complete": ("NEW", "warehouseStatus", "IN_PROGRESS", "COMPLETED"),
            "delivery_start": ("PACKAGED", "deliveryStatus", "WAITING", "IN_PROGRESS"),
            "delivery_complete": ("PACKAGED", "deliveryStatus", "IN_PROGRESS", "COMPLETED"),
        }
        if action not in transitions:
            raise DomainError("unknown order action")
        state, field, expected, target = transitions[action]
        key = self._key(f"NS#{self.namespace}#USER#{customer_id}", f"ORDER#{order_id}")
        try:
            result = self.update_item(
                TABLES["orders"], key, "SET #field = :target, modifiedAt = :modified",
                {"#field": field, "#status": "status"},
                {":target": target, ":modified": now_iso(), ":customer": customer_id,
                 ":state": state, ":expected": expected},
                "customerId = :customer AND #status = :state AND #field = :expected",
            )
        except DynamoDBError as exc:
            current = self.get_order(customer_id, order_id)
            if current is None:
                raise DomainError("order not found", 404, "not_found") from exc
            raise DomainError(
                f"cannot apply {action} while order is {current['status']}/{current[field]}",
                409, "invalid_state_transition",
            ) from exc
        updated = result.get("Attributes")
        if updated:
            decoded = self._decode_item(updated)
            return {key: clone(value) for key, value in decoded.items() if key not in {"pk", "sk", "namespace", "entity"}}
        return self.get_order(customer_id, order_id)

    def apply_event(self, event):
        event_id = event.get("id")
        data = event.get("data") or {}
        order_id = data.get("orderId")
        event_type = event.get("type", "")
        customer_id = data.get("customerId")
        if not event_id or not order_id or not customer_id:
            raise DomainError("event id, data.orderId and data.customerId are required")
        operation_key = f"{event_type}:{order_id}"
        order = self.get_order(customer_id, order_id)
        if order is None:
            raise DomainError("event order not found", 404, "not_found")
        event_claim = f"EVENT#{event_id}"
        operation_claim = f"OPERATION#{operation_key}"
        if not self._claim(event_claim):
            return {"status": "duplicate", "eventId": event_id, "operationKey": operation_key}
        if not self._claim(operation_claim):
            self._delete_claim(event_claim)
            return {"status": "duplicate", "eventId": event_id, "operationKey": operation_key}
        try:
            if event_type == "WarehouseCompleted":
                if order["status"] == "NEW" and order["warehouseStatus"] == "COMPLETED":
                    self._update_event_order(
                        customer_id, order_id, "PACKAGED", "WAITING", "NEW", "warehouseStatus", "COMPLETED"
                    )
                elif order["status"] != "PACKAGED":
                    raise DomainError("warehouse completion cannot be applied", 409, "invalid_state_transition")
            elif event_type == "DeliveryCompleted":
                if order["status"] == "PACKAGED" and order["deliveryStatus"] == "COMPLETED":
                    self._update_event_order(
                        customer_id, order_id, "FULFILLED", None, "PACKAGED", "deliveryStatus", "COMPLETED"
                    )
                elif order["status"] != "FULFILLED":
                    raise DomainError("delivery completion cannot be applied", 409, "invalid_state_transition")
            elif event_type not in {"OrderCreated", "WarehouseStarted", "DeliveryStarted"}:
                self._delete_claim(event_claim)
                self._delete_claim(operation_claim)
                return {"status": "ignored", "eventId": event_id, "eventType": event_type}
        except Exception:
            self._delete_claim(event_claim)
            self._delete_claim(operation_claim)
            raise
        return {"status": "processed", "eventId": event_id, "operationKey": operation_key}

    def _update_event_order(self, customer_id, order_id, status, delivery_status, expected_status,
                            expected_field, expected_value):
        key = self._key(f"NS#{self.namespace}#USER#{customer_id}", f"ORDER#{order_id}")
        names = {"#status": "status", "#expected": expected_field}
        values = {":status": status, ":modified": now_iso(), ":expectedStatus": expected_status,
                  ":expectedValue": expected_value}
        expression = "SET #status = :status, modifiedAt = :modified"
        if delivery_status is not None:
            expression += ", deliveryStatus = :delivery"
            values[":delivery"] = delivery_status
        try:
            self.update_item(TABLES["orders"], key, expression, names, values,
                             "#status = :expectedStatus AND #expected = :expectedValue")
        except DynamoDBError as exc:
            raise DomainError("event state transition failed", 409, "invalid_state_transition") from exc


def repository_from_env():
    if REPOSITORY_MODE == "dynamodb":
        return DynamoDBRepository(
            GEMINIDB_ENDPOINT, GEMINIDB_PORT, GEMINIDB_PROTOCOL,
            GEMINIDB_ACCESS_KEY, GEMINIDB_SECRET_KEY, GEMINIDB_REGION,
            APP_NAMESPACE, TABLES,
        )
    if REPOSITORY_MODE == "memory":
        return MemoryRepository()
    raise RepositoryNotConfigured()


REPOSITORY = repository_from_env()


def b64url_decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def verify_jwt(token):
    try:
        header_raw, payload_raw, signature = token.split(".")
        header = json.loads(b64url_decode(header_raw).decode("utf-8"))
        payload = json.loads(b64url_decode(payload_raw).decode("utf-8"))
        if header.get("alg") != "HS256" or header.get("typ") != "JWT":
            raise ValueError("unsupported JWT header")
        signing_input = f"{header_raw}.{payload_raw}".encode("ascii")
        expected = hmac.new(JWT_SECRET, signing_input, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, b64url_decode(signature)):
            raise ValueError("JWT signature mismatch")
        if payload.get("iss") != JWT_ISSUER or payload.get("aud") != JWT_AUDIENCE:
            raise ValueError("JWT issuer or audience mismatch")
        if not isinstance(payload.get("sub"), str) or not payload["sub"]:
            raise ValueError("JWT sub is required")
        if payload.get("exp") is not None and int(payload["exp"]) <= int(time.time()):
            raise ValueError("JWT has expired")
        return payload
    except (ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeError) as exc:
        raise DomainError("invalid bearer token", 401, "unauthorized") from exc


def header(event, name):
    headers = event.get("headers") or {}
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def decode_body(event):
    raw = event.get("body")
    if raw in (None, ""):
        return {}
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except (ValueError, UnicodeError) as exc:
            raise DomainError("request body is not valid base64 JSON") from exc
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise DomainError("request body must be valid JSON") from exc
    if not isinstance(value, dict):
        raise DomainError("request body must be a JSON object")
    return value


def response(payload, status=200, request_id=None):
    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    headers = {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}
    if request_id:
        headers["X-Request-Id"] = request_id
    return {"statusCode": status, "headers": headers, "isBase64Encoded": False, "body": body}


def get_request_id(context):
    if context is not None:
        getter = getattr(context, "getRequestID", None)
        if callable(getter):
            return getter()
        getter = getattr(context, "get_request_id", None)
        if callable(getter):
            return getter()
    return str(uuid.uuid4())


def publish_event(event_type, order, context):
    event = {
        "specversion": "1.0",
        "id": str(uuid.uuid4()),
        "source": EG_SOURCE_NAME,
        "type": event_type,
        "subject": order["orderId"],
        "time": now_iso(),
        "datacontenttype": "application/json",
        "data": {
            "orderId": order["orderId"],
            "customerId": order["customerId"],
            "status": order["status"],
            "warehouseStatus": order["warehouseStatus"],
            "deliveryStatus": order["deliveryStatus"],
        },
    }
    if not EG_CHANNEL_ID or not EG_PROJECT_ID:
        PUBLISHED_EVENTS.append(clone(event))
        log("INFO", "event queued for local consumer", eventId=event["id"], eventType=event_type)
        return event
    getter = getattr(context, "getToken", None) if context is not None else None
    token = getter() if callable(getter) else ""
    if not token:
        raise DomainError("FunctionGraph token is unavailable for EG publishing", 503, "event_publisher_unavailable")
    url = f"{EG_ENDPOINT.rstrip('/')}/v1/{EG_PROJECT_ID}/channels/{EG_CHANNEL_ID}/events"
    body = json.dumps({"events": [event]}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "X-Auth-Token": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as result:
            raw_reply = result.read()
            reply = json.loads(raw_reply.decode("utf-8")) if raw_reply else {}
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        log("ERROR", "EG publish failed", eventId=event["id"], eventType=event_type)
        raise DomainError("event publication failed", 503, "event_publisher_unavailable") from exc
    if not isinstance(reply, dict):
        raise DomainError("EG returned an invalid response", 502, "event_publisher_protocol_error")
    failed = [item for item in reply.get("events", []) if item.get("error_code") or item.get("errorCode")]
    if failed:
        raise DomainError("EG rejected the event", 503, "event_publisher_rejected")
    log("INFO", "event published", eventId=event["id"], eventType=event_type)
    return event


def claims_from_event(event):
    raw = header(event, "Authorization") or ""
    token = raw[7:].strip() if raw.lower().startswith("bearer ") else raw.strip()
    if not token:
        raise DomainError("Authorization bearer token is required", 401, "unauthorized")
    return verify_jwt(token)


def route(path):
    clean = path.split("?", 1)[0].rstrip("/") or "/"
    return [part for part in clean.split("/") if part]


def require_admin(claims):
    roles = claims.get("roles", [])
    if isinstance(roles, str):
        roles = [roles]
    if "admin" not in roles:
        raise DomainError("admin role is required", 403, "forbidden")


def handle(event, context):
    request_id = get_request_id(context)
    method = (event.get("httpMethod") or event.get("requestContext", {}).get("http", {}).get("method") or "GET").upper()
    path = event.get("path") or event.get("rawPath") or "/"
    if method == "OPTIONS":
        return response({"ok": True}, 200, request_id)
    if route(path) == ["health"]:
        return response({"ok": True, "service": APP_NAMESPACE, "repositoryMode": REPOSITORY_MODE}, 200, request_id)
    claims = claims_from_event(event)
    customer_id = claims["sub"]
    parts = route(path)
    body = decode_body(event)

    if method == "POST" and parts == ["products", "seed"]:
        require_admin(claims)
        products = body.get("products")
        if not isinstance(products, list) or not products:
            raise DomainError("products must be a non-empty array")
        result = REPOSITORY.seed_products(products)
        return response({"ok": True, "result": result}, 200, request_id)

    if method == "POST" and parts == ["products", "validate"]:
        products = body.get("products")
        if not isinstance(products, list):
            raise DomainError("products must be an array")
        invalid = REPOSITORY.validate_products(products)
        return response({"valid": not invalid, "invalid": invalid}, 200, request_id)

    if method == "POST" and parts == ["delivery", "quote"]:
        products = body.get("products")
        if not isinstance(products, list) or not products:
            raise DomainError("products must be a non-empty array")
        invalid = REPOSITORY.validate_products(products)
        if invalid:
            raise DomainError("products failed validation", 422, "invalid_products")
        price = delivery_price(products, body.get("address"))
        return response({"quote": price, "currency": "CNY-cent"}, 200, request_id)

    if method == "POST" and parts == ["payments", "simulate"]:
        result = REPOSITORY.simulate_payment(
            customer_id,
            body.get("amount"),
            body.get("idempotencyKey"),
            body.get("cardNumber"),
        )
        return response(result, 200, request_id)

    if len(parts) == 1 and parts[0] == "orders" and method == "POST":
        products = body.get("products")
        address = body.get("address")
        delivery_amount = body.get("deliveryPrice")
        payment_token = body.get("paymentToken")
        if not isinstance(products, list) or not products or not isinstance(address, dict):
            raise DomainError("products and address are required")
        if not isinstance(delivery_amount, int) or not isinstance(payment_token, str):
            raise DomainError("deliveryPrice and paymentToken are required")
        order = REPOSITORY.create_order(customer_id, products, address, delivery_amount, payment_token)
        publish_event("OrderCreated", order, context)
        return response({"ok": True, "order": order}, 201, request_id)

    if len(parts) == 2 and parts[0] == "orders" and method == "GET":
        order = REPOSITORY.get_order(customer_id, parts[1])
        if order is None:
            raise DomainError("order not found", 404, "not_found")
        return response({"order": order}, 200, request_id)

    actions = {
        ("warehouse", "start"): ("warehouse_start", "WarehouseStarted"),
        ("warehouse", "complete"): ("warehouse_complete", "WarehouseCompleted"),
        ("delivery", "start"): ("delivery_start", "DeliveryStarted"),
        ("delivery", "complete"): ("delivery_complete", "DeliveryCompleted"),
    }
    if len(parts) == 4 and parts[0] == "orders" and method == "POST" and (parts[2], parts[3]) in actions:
        action, event_type = actions[(parts[2], parts[3])]
        order = REPOSITORY.update_order_action(customer_id, parts[1], action)
        publish_event(event_type, order, context)
        return response({"ok": True, "order": order, "eventType": event_type}, 200, request_id)

    raise DomainError("route not found", 404, "not_found")


def handler(event, context):
    """FunctionGraph handler using the APIG proxy response contract."""
    request_id = get_request_id(context)
    try:
        return handle(event or {}, context)
    except DomainError as exc:
        log("WARNING", exc.message, code=exc.code, requestId=request_id)
        return response({"ok": False, "code": exc.code, "message": exc.message}, exc.status, request_id)
    except Exception as exc:  # platform logs retain the request correlation, response stays safe
        log("ERROR", "unexpected request failure", errorType=type(exc).__name__, requestId=request_id)
        return response({"ok": False, "code": "internal_error", "message": "internal server error"}, 500, request_id)
