"""EventGrid CloudEvents consumer for the fulfillment state machine.

EG delivers the original CloudEvents JSON to this function.  The consumer
keeps event-id and operation-key idempotency in the repository adapter, so a
redelivery or a second event with the same business operation is harmless.
Cloud mode uses the standard DynamoDB JSON API with SigV4.
"""

import copy
import hashlib
import hmac
import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
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
EXPECTED_SOURCE = os.environ.get("EG_SOURCE_NAME", "")


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def log(level, message, **fields):
    record = {"level": level, "service": APP_NAMESPACE, "message": message, "time": now_iso()}
    record.update(fields)
    print(json.dumps(record, ensure_ascii=False, sort_keys=True))


class ConsumerMemoryRepository:
    """Small local repository fallback; local verification injects the API repo."""

    def __init__(self):
        self.orders = {}
        self.processed_events = set()
        self.processed_operations = set()

    def apply_event(self, event):
        event_id = event["id"]
        data = event.get("data") or {}
        order_id = data["orderId"]
        event_type = event.get("type", "")
        operation_key = f"{event_type}:{order_id}"
        if event_id in self.processed_events or operation_key in self.processed_operations:
            return {"status": "duplicate", "eventId": event_id, "operationKey": operation_key}
        order = self.orders.get(order_id)
        if order is None:
            raise ValueError("event order not found")
        if event_type == "WarehouseCompleted":
            if order["status"] == "NEW" and order["warehouseStatus"] == "COMPLETED":
                order["status"] = "PACKAGED"
                order["modifiedAt"] = now_iso()
        elif event_type == "DeliveryCompleted":
            if order["status"] == "PACKAGED" and order["deliveryStatus"] == "COMPLETED":
                order["status"] = "FULFILLED"
                order["modifiedAt"] = now_iso()
        elif event_type not in {"OrderCreated", "WarehouseStarted", "DeliveryStarted"}:
            return {"status": "ignored", "eventId": event_id, "eventType": event_type}
        self.processed_events.add(event_id)
        self.processed_operations.add(operation_key)
        return {"status": "processed", "eventId": event_id, "operationKey": operation_key}


class DynamoDBError(Exception):
    """A DynamoDB API error without exposing request credentials."""

    def __init__(self, code, message, status=502):
        super().__init__(message)
        self.code = code
        self.status = status


class DynamoDBRepository:
    """Self-contained standard DynamoDB JSON + SigV4 adapter.

    FunctionGraph receives only this file as inline code, so the adapter is
    intentionally self-contained.  It supports the complete table/bootstrap
    action set and uses conditional writes for application idempotency.  No
    DynamoDB transaction API is used.
    """

    SERVICE = "dynamodb"
    CONTENT_TYPE = "application/x-amz-json-1.0"
    CONDITIONAL_ERROR = "ConditionalCheckFailedException"

    def __init__(self, endpoint, port, protocol, access_key, secret_key, region, namespace, tables):
        if not endpoint or not access_key or not secret_key:
            raise ValueError("GeminiDB DynamoDB configuration is incomplete")
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
        if not parsed.scheme or not parsed.netloc or not parsed.hostname:
            raise ValueError("GeminiDB endpoint is invalid")
        try:
            current_port = parsed.port
        except ValueError as exc:
            raise ValueError("GeminiDB endpoint port is invalid") from exc
        if current_port is None:
            host = parsed.hostname
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
        return {
            "Content-Type": self.CONTENT_TYPE,
            "Host": host,
            "X-Amz-Content-Sha256": payload_hash,
            "X-Amz-Date": timestamp,
            "X-Amz-Target": headers["x-amz-target"],
            "Authorization": (
                f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
                f"SignedHeaders={signed_headers}, Signature={signature}"
            ),
        }

    def _request(self, target, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(self.endpoint, data=body, headers=self._sign(body, target), method="POST")
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
            code = str(result.get("__type", "DynamoDBError")).rsplit("#", 1)[-1]
            raise DynamoDBError(code, str(result.get("message", "DynamoDB request failed")), exc.code) from exc
        except (urllib.error.URLError, TimeoutError, ValueError, UnicodeError) as exc:
            raise DynamoDBError("RepositoryUnavailable", "DynamoDB request failed", 503) from exc
        if not isinstance(result, dict):
            raise DynamoDBError("RepositoryProtocolError", "DynamoDB returned a non-object response")
        return result

    def create_table(self, table_name, key_schema, attribute_definitions, capacity_mode="PAY_PER_REQUEST"):
        return self._request("CreateTable", {
            "TableName": table_name, "KeySchema": key_schema,
            "AttributeDefinitions": attribute_definitions, "BillingMode": capacity_mode,
        })

    def describe_table(self, table_name):
        return self._request("DescribeTable", {"TableName": table_name})

    def put_item(self, table_name, item, condition_expression=None):
        payload = {"TableName": table_name, "Item": self._encode_item(item)}
        if condition_expression:
            payload["ConditionExpression"] = condition_expression
        return self._request("PutItem", payload)

    def update_item(self, table_name, key, update_expression, expression_attribute_names=None,
                    expression_attribute_values=None, condition_expression=None,
                    return_values="ALL_NEW"):
        payload = {"TableName": table_name, "Key": self._encode_item(key),
                   "UpdateExpression": update_expression, "ReturnValues": return_values}
        if expression_attribute_names:
            payload["ExpressionAttributeNames"] = expression_attribute_names
        if expression_attribute_values:
            payload["ExpressionAttributeValues"] = self._encode_item(expression_attribute_values)
        if condition_expression:
            payload["ConditionExpression"] = condition_expression
        return self._request("UpdateItem", payload)

    def get_item(self, table_name, key, consistent_read=True):
        return self._request("GetItem", {"TableName": table_name, "Key": self._encode_item(key),
                                          "ConsistentRead": consistent_read})

    def delete_item(self, table_name, key):
        return self._request("DeleteItem", {"TableName": table_name, "Key": self._encode_item(key)})

    def query(self, table_name, key_condition_expression, expression_attribute_values,
              expression_attribute_names=None, filter_expression=None, limit=None):
        payload = {"TableName": table_name, "KeyConditionExpression": key_condition_expression,
                   "ExpressionAttributeValues": self._encode_item(expression_attribute_values)}
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
        grouped = {}
        for request in requests:
            if "PutRequest" in request:
                value = request["PutRequest"]
                encoded = {"PutRequest": {"Item": self._encode_item(value["Item"])} }
                table_name = value["TableName"]
            else:
                value = request["DeleteRequest"]
                encoded = {"DeleteRequest": {"Key": self._encode_item(value["Key"])} }
                table_name = value["TableName"]
            grouped.setdefault(table_name, []).append(encoded)
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

    def _order(self, customer_id, order_id):
        result = self.get_item(TABLES["orders"], self._key(
            f"NS#{self.namespace}#USER#{customer_id}", f"ORDER#{order_id}"
        ))
        item = result.get("Item")
        return self._decode_item(item) if item else None

    def _claim(self, claim):
        try:
            self.put_item(TABLES["idempotency"], {
                "pk": f"NS#{self.namespace}#IDEMPOTENCY", "sk": claim,
                "namespace": self.namespace, "entity": "idempotency", "claim": claim,
                "createdAt": now_iso(),
            }, "attribute_not_exists(pk)")
            return True
        except DynamoDBError as exc:
            if exc.code == self.CONDITIONAL_ERROR:
                return False
            raise

    def _release(self, claim):
        self.delete_item(TABLES["idempotency"], self._key(f"NS#{self.namespace}#IDEMPOTENCY", claim))

    def apply_event(self, event):
        event_id = event.get("id")
        data = event.get("data") or {}
        order_id = data.get("orderId")
        customer_id = data.get("customerId")
        event_type = event.get("type", "")
        if not event_id or not order_id or not customer_id:
            raise ValueError("event id, data.orderId and data.customerId are required")
        order = self._order(customer_id, order_id)
        if order is None:
            raise ValueError("event order not found")
        operation_key = f"{event_type}:{order_id}"
        event_claim = f"EVENT#{event_id}"
        operation_claim = f"OPERATION#{operation_key}"
        if not self._claim(event_claim):
            return {"status": "duplicate", "eventId": event_id, "operationKey": operation_key}
        if not self._claim(operation_claim):
            self._release(event_claim)
            return {"status": "duplicate", "eventId": event_id, "operationKey": operation_key}
        key = self._key(f"NS#{self.namespace}#USER#{customer_id}", f"ORDER#{order_id}")
        try:
            if event_type == "WarehouseCompleted" and order["status"] == "NEW" and order["warehouseStatus"] == "COMPLETED":
                self.update_item(
                    TABLES["orders"], key, "SET #status = :status, deliveryStatus = :delivery, modifiedAt = :modified",
                    {"#status": "status"},
                    {":status": "PACKAGED", ":delivery": "WAITING", ":modified": now_iso(),
                     ":expected": "NEW", ":warehouse": "COMPLETED"},
                    "#status = :expected AND warehouseStatus = :warehouse",
                )
            elif event_type == "DeliveryCompleted" and order["status"] == "PACKAGED" and order["deliveryStatus"] == "COMPLETED":
                self.update_item(
                    TABLES["orders"], key, "SET #status = :status, modifiedAt = :modified",
                    {"#status": "status"},
                    {":status": "FULFILLED", ":modified": now_iso(),
                     ":expected": "PACKAGED", ":delivery": "COMPLETED"},
                    "#status = :expected AND deliveryStatus = :delivery",
                )
            elif event_type == "WarehouseCompleted" and order["status"] != "PACKAGED":
                raise ValueError("warehouse completion cannot be applied")
            elif event_type == "DeliveryCompleted" and order["status"] != "FULFILLED":
                raise ValueError("delivery completion cannot be applied")
            elif event_type not in {"OrderCreated", "WarehouseStarted", "DeliveryStarted", "WarehouseCompleted", "DeliveryCompleted"}:
                self._release(event_claim)
                self._release(operation_claim)
                return {"status": "ignored", "eventId": event_id, "eventType": event_type}
        except Exception:
            self._release(event_claim)
            self._release(operation_claim)
            raise
        return {"status": "processed", "eventId": event_id, "operationKey": operation_key}


def repository_from_env():
    if REPOSITORY_MODE == "dynamodb":
        return DynamoDBRepository(
            GEMINIDB_ENDPOINT, GEMINIDB_PORT, GEMINIDB_PROTOCOL,
            GEMINIDB_ACCESS_KEY, GEMINIDB_SECRET_KEY, GEMINIDB_REGION,
            APP_NAMESPACE, TABLES,
        )
    if REPOSITORY_MODE == "memory":
        return ConsumerMemoryRepository()
    raise ValueError("REPOSITORY_MODE must be memory or dynamodb")


REPOSITORY = repository_from_env()


def unwrap_event(event):
    if isinstance(event, dict) and isinstance(event.get("data"), dict) and event.get("specversion") == "1.0":
        return event
    if isinstance(event, dict) and isinstance(event.get("event"), dict):
        return event["event"]
    # A few EG integrations wrap the original event in a record list.  Keep
    # this defensive branch independent of any service-specific SDK.
    records = event.get("record") if isinstance(event, dict) else None
    if isinstance(records, list) and records and isinstance(records[0], dict):
        candidate = records[0].get("event") or records[0].get("data")
        if isinstance(candidate, str):
            return json.loads(candidate)
        if isinstance(candidate, dict):
            return candidate
    return event


def handler(event, context):
    """Consume one CloudEvent and return an application-level result."""
    incoming = unwrap_event(event or {})
    if not isinstance(incoming, dict):
        raise ValueError("consumer event must be a JSON object")
    if EXPECTED_SOURCE and incoming.get("source") != EXPECTED_SOURCE:
        log("INFO", "event source ignored", source=incoming.get("source"), eventId=incoming.get("id"))
        return {"status": "ignored", "reason": "source_mismatch", "eventId": incoming.get("id")}
    result = REPOSITORY.apply_event(incoming)
    log(
        "INFO",
        "fulfillment event handled",
        eventId=incoming.get("id"),
        eventType=incoming.get("type"),
        orderId=(incoming.get("data") or {}).get("orderId"),
        result=result,
    )
    return result
