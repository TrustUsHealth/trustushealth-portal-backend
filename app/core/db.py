"""MongoDB (PyMongo Async). The client is created once at startup and kept on app.state.

Money is exact: Python Decimal is stored as BSON Decimal128 and read back as Decimal.
"""

from datetime import UTC, datetime
from decimal import Decimal

from bson.codec_options import TypeCodec, TypeRegistry
from bson.decimal128 import Decimal128
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from app.core.config import Settings


class DecimalCodec(TypeCodec):
    python_type = Decimal
    bson_type = Decimal128

    def transform_python(self, value: Decimal) -> Decimal128:
        return Decimal128(value)

    def transform_bson(self, value: Decimal128) -> Decimal:
        return value.to_decimal()


def create_client(settings: Settings) -> AsyncMongoClient:
    return AsyncMongoClient(
        settings.mongodb_uri,
        tz_aware=True,
        serverSelectionTimeoutMS=8000,
        type_registry=TypeRegistry([DecimalCodec()]),
    )


def get_database(client: AsyncMongoClient, settings: Settings) -> AsyncDatabase:
    return client[settings.mongodb_db]


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(dt: datetime) -> datetime:
    """Normalise datetimes read back from MongoDB (naive = UTC)."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)