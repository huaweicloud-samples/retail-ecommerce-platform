#!/usr/bin/env python3
"""Create and seed the application's GeminiDB DynamoDB-compatible tables.

Credentials are read only from GEMINIDB_ACCESS_KEY and GEMINIDB_SECRET_KEY.
The script never prints credential values and never deletes an instance.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
FIXTURE = ROOT / "src" / "fixtures" / "geminidb_seed.json"
sys.path.insert(0, str(SRC))

from api import index as api  # noqa: E402


TABLE_ENV = {
    "products": "PRODUCT_TABLE",
    "orders": "ORDER_TABLE",
    "payments": "PAYMENT_TABLE",
    "idempotency": "IDEMPOTENCY_TABLE",
}
TABLE_DEFAULTS = {
    "products": "retail_ecommerce_products",
    "orders": "retail_ecommerce_orders",
    "payments": "retail_ecommerce_payments",
    "idempotency": "retail_ecommerce_idempotency",
}
TABLE_SPECS = {
    name: {
        "KeySchema": [
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        "AttributeDefinitions": [
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
    }
    for name in TABLE_ENV
}


def env_required(name):
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


def table_names():
    return {key: os.environ.get(env_name, TABLE_DEFAULTS[key]) for key, env_name in TABLE_ENV.items()}


def repository(args, tables):
    access_key = env_required("GEMINIDB_ACCESS_KEY")
    secret_key = env_required("GEMINIDB_SECRET_KEY")
    endpoint = args.endpoint or env_required("GEMINIDB_ENDPOINT")
    port = args.port if args.port is not None else int(os.environ.get("GEMINIDB_PORT", "8000"))
    protocol = (args.protocol or os.environ.get("GEMINIDB_PROTOCOL", "https")).lower()
    region = args.region or os.environ.get("GEMINIDB_REGION", "cn-north-4")
    # The repository module is also used by the inline function.  Point its
    # table map at this script's explicit table names without persisting state.
    api.TABLES = tables
    return api.DynamoDBRepository(
        endpoint, port, protocol, access_key, secret_key, region, args.namespace, tables
    )


def schema_signature(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def ensure_table(repo, logical_name, table_name, timeout):
    spec = TABLE_SPECS[logical_name]
    try:
        repo.create_table(table_name, spec["KeySchema"], spec["AttributeDefinitions"])
        created = True
    except api.DynamoDBError as exc:
        if exc.code != "ResourceInUseException":
            raise
        created = False

    deadline = time.monotonic() + timeout
    while True:
        result = repo.describe_table(table_name)
        table = result.get("Table", {})
        status = table.get("TableStatus")
        if status == "ACTIVE":
            actual_schema = sorted(table.get("KeySchema", []), key=lambda item: item["AttributeName"])
            expected_schema = sorted(spec["KeySchema"], key=lambda item: item["AttributeName"])
            if schema_signature(actual_schema) != schema_signature(expected_schema):
                raise RuntimeError(f"table {table_name} has an incompatible key schema")
            return {"table": table_name, "created": created, "status": status}
        if time.monotonic() >= deadline:
            raise TimeoutError(f"table {table_name} did not become ACTIVE")
        time.sleep(2)


def delete_namespace(repo, table_name, namespace):
    prefix = f"NS#{namespace}"
    deleted = 0
    exclusive_start_key = None
    while True:
        result = repo.scan(
            table_name,
            filter_expression="begins_with(pk, :prefix)",
            expression_attribute_values={":prefix": prefix},
            limit=100,
            exclusive_start_key=exclusive_start_key,
        )
        items = result.get("Items", [])
        pending = [{"DeleteRequest": {"TableName": table_name, "Key": {"pk": item["pk"], "sk": item["sk"]}}} for item in items]
        while pending:
            batch = pending[:25]
            pending = pending[25:]
            response = repo.batch_write_item(batch)
            for table_requests in response.get("UnprocessedItems", {}).values():
                pending.extend(table_requests)
        deleted += len(items)
        exclusive_start_key = result.get("LastEvaluatedKey")
        if not exclusive_start_key:
            return deleted


def seed_products(repo, fixture, table_name):
    products = fixture.get("products", [])
    requests = []
    for product in products:
        api.validate_product_shape(product)
        item = repo._item(repo._key(
            f"NS#{repo.namespace}#PRODUCTS", f"PRODUCT#{product['productId']}"
        ), entity="product", **product)
        requests.append({"PutRequest": {"TableName": table_name, "Item": item}})
    pending = requests
    written = 0
    while pending:
        batch = pending[:25]
        pending = pending[25:]
        response = repo.batch_write_item(batch)
        written += len(batch) - sum(len(items) for items in response.get("UnprocessedItems", {}).values())
        for table_requests in response.get("UnprocessedItems", {}).values():
            pending.extend(table_requests)
    return written


def main():
    parser = argparse.ArgumentParser(description="Create and seed GeminiDB DynamoDB-compatible application tables")
    parser.add_argument("--endpoint", help="private host/IP; otherwise GEMINIDB_ENDPOINT")
    parser.add_argument("--port", type=int, help="data-plane port; otherwise GEMINIDB_PORT or 8000")
    parser.add_argument("--protocol", choices=("http", "https"), help="data-plane protocol")
    parser.add_argument("--region", help="SigV4 region; otherwise GEMINIDB_REGION")
    parser.add_argument("--namespace", default=os.environ.get("APP_NAMESPACE", "retail-ecommerce-platform"))
    parser.add_argument("--fixture", default=str(FIXTURE))
    parser.add_argument("--timeout", type=int, default=300, help="table ACTIVE wait timeout in seconds")
    parser.add_argument("--reset-namespace", action="store_true", help="delete only this application's namespaced items")
    args = parser.parse_args()

    if not args.namespace:
        raise SystemExit("namespace must not be empty")
    tables = table_names()
    repo = repository(args, tables)
    results = [ensure_table(repo, logical_name, table_name, args.timeout) for logical_name, table_name in tables.items()]

    reset = {}
    if args.reset_namespace:
        reset = {logical_name: delete_namespace(repo, table_name, args.namespace) for logical_name, table_name in tables.items()}

    with open(args.fixture, encoding="utf-8") as stream:
        fixture = json.load(stream)
    seeded = seed_products(repo, fixture, tables["products"])
    print(json.dumps({
        "namespace": args.namespace,
        "tables": results,
        "resetNamespaceItems": reset,
        "seededProducts": seeded,
        "transactions": "not-used",
    }, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
