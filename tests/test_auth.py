"""Email OTP + session rules from PRD section 7."""

from datetime import timedelta

from app.core.db import utcnow
from tests.conftest import API

A = f"{API}/auth"


async def request(portal, email):
    return await portal.client.post(f"{A}/otp/request", json={"email": email})


async def verify(portal, email, code):
    return await portal.client.post(f"{A}/otp/verify", json={"email": email, "code": code})


async def test_login_happy_path_and_me(portal):
    await portal.add_user("sam@example.com", "staff")
    token = await portal.login("sam@example.com")
    me = await portal.client.get(f"{A}/me", headers=portal.auth(token))
    assert me.status_code == 200 and me.json()["email"] == "sam@example.com" and me.json()["role"] == "staff"


async def test_otp_email_has_branded_html_and_stores_code_hashed(portal):
    await portal.add_user("sam@example.com")
    await request(portal, "sam@example.com")
    mail = portal.mailer.sent[0]
    code = portal.mailer.last_code("sam@example.com")
    assert len(code) == 6 and "TrustUs Health" in mail["subject"]
    assert f">{code}</span>" in mail["html"]
    assert "It expires in 5 minutes" in mail["html"]
    stored = await portal.db["otp_challenges"].find_one({"email": "sam@example.com"})
    assert code not in str(stored) and stored["code_hash"] != code


async def test_response_identical_for_unknown_email_and_no_mail_sent(portal):
    await portal.add_user("sam@example.com")
    known = await request(portal, "sam@example.com")
    unknown = await request(portal, "ghost@example.com")
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    assert [m["to"] for m in portal.mailer.sent] == ["sam@example.com"]


async def test_only_invited_active_users_can_log_in(portal):
    await portal.add_user("off@example.com", active=False)
    await portal.add_user("cust@example.com", role="customer")
    await request(portal, "off@example.com")
    await request(portal, "cust@example.com")
    assert portal.mailer.sent == []


async def test_wrong_code_then_lockout_after_five_attempts(portal):
    await portal.add_user("sam@example.com")
    await request(portal, "sam@example.com")
    good = portal.mailer.last_code("sam@example.com")
    bad = "000000" if good != "000000" else "111111"
    for _ in range(5):
        assert (await verify(portal, "sam@example.com", bad)).status_code == 401
    # Even the correct code is dead after 5 failures.
    assert (await verify(portal, "sam@example.com", good)).status_code == 401


async def test_code_expires_after_five_minutes(portal):
    await portal.add_user("sam@example.com")
    await request(portal, "sam@example.com")
    code = portal.mailer.last_code("sam@example.com")
    await portal.age_challenges("sam@example.com", 5 * 60 + 1)
    assert (await verify(portal, "sam@example.com", code)).status_code == 401


async def test_code_is_single_use(portal):
    await portal.add_user("sam@example.com")
    await request(portal, "sam@example.com")
    code = portal.mailer.last_code("sam@example.com")
    assert (await verify(portal, "sam@example.com", code)).status_code == 200
    assert (await verify(portal, "sam@example.com", code)).status_code == 401


async def test_new_code_invalidates_the_previous_one(portal):
    await portal.add_user("sam@example.com")
    await request(portal, "sam@example.com")
    first = portal.mailer.last_code("sam@example.com")
    await request(portal, "sam@example.com")
    second = portal.mailer.last_code("sam@example.com")
    if first != second:
        assert (await verify(portal, "sam@example.com", first)).status_code == 401
    assert (await verify(portal, "sam@example.com", second)).status_code == 200


async def test_otp_resend_cooldown_60_seconds(portal):
    portal.set_cooldown(60)
    await portal.add_user("sam@example.com")
    await request(portal, "sam@example.com")
    r = await request(portal, "sam@example.com")
    assert r.status_code == 202 and len(portal.mailer.sent) == 1  # same response, no second mail
    await portal.age_challenges("sam@example.com", 61)
    await request(portal, "sam@example.com")
    assert len(portal.mailer.sent) == 2


async def test_max_five_requests_per_email_per_hour(portal):
    await portal.add_user("sam@example.com")
    for _ in range(8):
        assert (await request(portal, "sam@example.com")).status_code == 202
    assert len(portal.mailer.sent) == 5


async def test_refresh_rotates_and_old_token_is_dead(portal):
    await portal.add_user("sam@example.com")
    await portal.login("sam@example.com")
    old_cookie = portal.client.cookies.get("tu_refresh")
    r = await portal.client.post(f"{A}/refresh")
    assert r.status_code == 200 and r.json()["access_token"]
    assert portal.client.cookies.get("tu_refresh") != old_cookie


async def test_refresh_token_reuse_revokes_whole_session(portal):
    await portal.add_user("sam@example.com")
    token = await portal.login("sam@example.com")
    stolen = portal.client.cookies.get("tu_refresh")
    assert (await portal.client.post(f"{A}/refresh")).status_code == 200  # legit rotation
    # age the used token past the grace window, then an attacker replays it
    await portal.db["refresh_tokens"].update_many(
        {"used": True}, {"$set": {"used_at": utcnow() - timedelta(minutes=1)}}
    )
    portal.client.cookies.clear()
    portal.client.cookies.set("tu_refresh", stolen, path="/api/v1/auth")
    assert (await portal.client.post(f"{A}/refresh")).status_code == 401
    # the legit user's newest refresh token and access token are now dead too
    assert (await portal.client.get(f"{A}/me", headers=portal.auth(token))).status_code == 401
    assert await portal.db["audit_logs"].find_one({"action": "auth.refresh_reuse"})


async def test_logout_revokes_immediately(portal):
    await portal.add_user("sam@example.com")
    token = await portal.login("sam@example.com")
    assert (await portal.client.post(f"{A}/logout")).status_code == 204
    assert (await portal.client.get(f"{A}/me", headers=portal.auth(token))).status_code == 401
    assert (await portal.client.post(f"{A}/refresh")).status_code == 401


async def test_session_hard_limit_12_hours(portal):
    await portal.add_user("sam@example.com")
    await portal.login("sam@example.com")
    doc = await portal.db["refresh_tokens"].find_one({})
    assert (doc["expires_at"] - doc["session_started_at"]) == timedelta(hours=12)
    # Rotation never extends the cap.
    await portal.client.post(f"{A}/refresh")
    for d in await portal.db["refresh_tokens"].find({}).to_list(None):
        assert (d["expires_at"] - d["session_started_at"]) == timedelta(hours=12)


async def test_refresh_cookie_flags(portal):
    await portal.add_user("sam@example.com")
    await request(portal, "sam@example.com")
    r = await verify(portal, "sam@example.com", portal.mailer.last_code("sam@example.com"))
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/api/v1/auth" in cookie


async def test_deactivated_user_is_locked_out_at_once(portal):
    uid = await portal.add_user("sam@example.com")
    token = await portal.login("sam@example.com")
    from bson import ObjectId

    await portal.db["users"].update_one({"_id": ObjectId(uid)}, {"$set": {"active": False}})
    assert (await portal.client.get(f"{A}/me", headers=portal.auth(token))).status_code == 401
    assert (await portal.client.post(f"{A}/refresh")).status_code == 401


async def test_bad_or_missing_tokens_rejected(portal):
    assert (await portal.client.get(f"{A}/me")).status_code == 401
    assert (await portal.client.get(f"{A}/me", headers=portal.auth("garbage"))).status_code == 401
