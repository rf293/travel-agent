#!/usr/bin/env python3
"""Smoke-test Duffel API credentials from repo .env."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.env import load_repo_env

load_repo_env()
API = "https://api.duffel.com"


def duffel_headers() -> dict[str, str]:
    key = os.getenv("DUFFEL_API_KEY", "").strip()
    if not key:
        raise RuntimeError("DUFFEL_API_KEY is not set in .env")
    return {
        "Authorization": f"Bearer {key}",
        "Duffel-Version": "v2",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Accept-Encoding": "gzip",
    }


def main() -> int:
    headers = duffel_headers()

    airlines = requests.get(f"{API}/air/airlines?limit=1", headers=headers, timeout=30)
    print(f"GET /air/airlines -> {airlines.status_code}")
    if airlines.status_code != 200:
        print(airlines.text)
        return 1

    payload = {
        "data": {
            "cabin_class": "economy",
            "slices": [
                {
                    "departure_date": "2026-09-10",
                    "destination": "JFK",
                    "origin": "LHR",
                }
            ],
            "passengers": [{"type": "adult"}],
        }
    }
    offers = requests.post(
        f"{API}/air/offer_requests", headers=headers, json=payload, timeout=60
    )
    print(f"POST /air/offer_requests -> {offers.status_code}")
    if offers.status_code not in (200, 201):
        print(offers.text)
        return 1

    data = offers.json().get("data") or {}
    count = len(data.get("offers") or [])
    sample = (data.get("offers") or [{}])[0]
    print(f"offer_request: {data.get('id')}")
    print(f"offers: {count}")
    if sample:
        print(
            f"sample: {sample.get('total_amount')} {sample.get('total_currency')} "
            f"({(sample.get('owner') or {}).get('name')})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
