from app.core.security import RateLimiter, hash_otp, hash_token, safe_equal
from tests.conftest import API


def test_rate_limiter_window():
    rl = RateLimiter()
    assert all(rl.check("k", 3, 60) for _ in range(3))
    assert not rl.check("k", 3, 60)
    assert rl.check("other", 3, 60)


def test_hashes_are_keyed_and_salted():
    assert hash_otp("123456", "a", "secret") != hash_otp("123456", "b", "secret")
    assert hash_otp("123456", "a", "secret") != hash_otp("123456", "a", "other")
    assert safe_equal(hash_token("x"), hash_token("x")) and not safe_equal(hash_token("x"), hash_token("y"))


async def test_otp_endpoint_is_rate_limited_per_ip(portal):
    codes = [
        (await portal.client.post(f"{API}/auth/otp/request", json={"email": f"u{i}@example.com"})).status_code
        for i in range(22)
    ]
    assert codes[:20] == [202] * 20 and codes[20:] == [429, 429]


async def test_module_without_router_is_skipped_not_fatal(portal):
    # patient_registry etc. are listed in modules.yaml but not built: the app still starts.
    assert portal.app.state.mounted_modules == {"auth", "access"}
