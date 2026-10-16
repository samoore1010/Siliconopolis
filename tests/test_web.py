def test_health_and_status_routes():
    from fastapi.testclient import TestClient

    from pipeline.web.app import app

    client = TestClient(app)
    assert client.get("/health").json()["ok"] is True
    r = client.get("/status", headers={"accept": "application/json"})
    assert r.status_code == 200 and "products_by_state" in r.json()
    r = client.get("/status", headers={"accept": "text/html"})
    assert "Products by state" in r.text
