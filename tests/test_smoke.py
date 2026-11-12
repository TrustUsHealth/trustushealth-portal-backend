async def test_health_and_bootstrap_admin(portal):
    assert (await portal.client.get("/healthz")).json() == {"status": "ok"}
    admin = await portal.db["users"].find_one({"email": "boss@example.com"})
    assert admin["role"] == "admin" and admin["active"]
