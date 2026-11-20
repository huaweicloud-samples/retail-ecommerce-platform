#!/usr/bin/env python3
"""Create a controlled local HS256 token; this script never reads credentials.csv."""

import argparse
import base64
import json
import time
from hashlib import sha256
import hmac
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "src" / "fixtures" / "test.jwt.json"


def b64(value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def encode_token(secret, issuer, audience, claims, expires_in):
    header = {"alg": "HS256", "typ": "JWT"}
    payload = dict(claims)
    payload.update({"iss": issuer, "aud": audience, "iat": int(time.time()), "exp": int(time.time()) + expires_in})
    signing_input = f"{b64(json.dumps(header, separators=(',', ':')))}.{b64(json.dumps(payload, separators=(',', ':')))}"
    signature = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), sha256).digest()
    return f"{signing_input}.{b64(signature)}"


def main():
    parser = argparse.ArgumentParser(description="Generate the controlled retail verification JWT")
    parser.add_argument("--fixture", default=str(FIXTURE))
    parser.add_argument("--sub", help="override the fixture subject")
    parser.add_argument("--roles", default=None, help="comma-separated roles")
    parser.add_argument("--expires-in", type=int, default=3600)
    args = parser.parse_args()
    with open(args.fixture, encoding="utf-8") as stream:
        fixture = json.load(stream)
    claims = dict(fixture["claims"])
    if args.sub:
        claims["sub"] = args.sub
    if args.roles is not None:
        claims["roles"] = [role for role in args.roles.split(",") if role]
    print(encode_token(fixture["secret"], fixture["issuer"], fixture["audience"], claims, args.expires_in))


if __name__ == "__main__":
    main()
