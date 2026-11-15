"""Fixtures for the bill_generator tests."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from mongomock_motor import AsyncMongoMockClient

from app.core.config import load_config

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def bg_cfg():
    return load_config(ROOT / "config")


@pytest.fixture
def bg_db():
    return AsyncMongoMockClient()["bg_test"]


@pytest.fixture(scope="session")
def samples() -> Path:
    return ROOT / "tests" / "samples"


@pytest.fixture
def sushant_entry() -> dict:
    """Matches SUSHANT_SAWANT-bill_14-10-2026.pdf (plain layout, no discount)."""
    return {
        "_id": "e1",
        "partner_key": "spectrum",
        "patient_name": "Sushant Sawant",
        "age": 56,
        "age_unit": "Y",
        "gender": "Male",
        "lab_pid": "0200118334",
        "referred_by": "Dr. Toraskar",
        "registration_date": "2026-10-14",
        "bill_no": "556635",
        "tests": ["Sugar Fasting & Post Prandial", "Routine Examination of Urine"],
        "test_amounts": [Decimal("200"), Decimal("150")],
    }


@pytest.fixture
def ruju_entry() -> dict:
    """Matches RUJU_PRASANNAJI-bill_08-11-2024.pdf (with client, with discount)."""
    return {
        "_id": "e2",
        "partner_key": "spectrum",
        "patient_name": "Ruju Prasanna Shreeji",
        "age": 43,
        "age_unit": "Y",
        "gender": "Female",
        "lab_pid": "0200042177",
        "referred_by": "Dr. Rekhaben",
        "registration_date": str(date(2024, 11, 8)),
        "bill_no": "558936",
        "client_name": "Chandraprabh Jain Sangh",
        "tests": ["CBC", "Iron Studies", "Blood Group"],
        "test_amounts": [Decimal("250"), Decimal("800"), Decimal("150")],
        "discount_amount": Decimal("240"),
    }