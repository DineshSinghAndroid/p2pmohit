import csv
import io
import time

from fastapi.testclient import TestClient

from app import main
from app.database import Database


def test_auth_run_history_and_csv_without_saving_credentials(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "runs.sqlite3"))
    monkeypatch.setattr(main, "DB", database)
    captured = []

    def mocked_browser(run_id, config, stop_event, db, screenshot_dir):
        captured.append(config)
        db.add_click({
            "run_id": run_id, "row_id": "DUMMY-1", "amount": "1145.70",
            "timestamp": "2026-11-15T12:00:00+00:00", "popup_shown": 1,
            "outcome": "OTP_REACHED", "latency_ms": 321, "error_text": None,
        })
        db.update_run(run_id, "completed", "2026-11-15T12:01:00+00:00")

    monkeypatch.setattr(main, "run_browser", mocked_browser)
    client = TestClient(main.app)
    assert client.get("/api/runs").status_code == 401
    login = client.post("/api/login", json={"username": main.ADMIN_USER, "password": main.ADMIN_PASS})
    assert login.status_code == 200
    assert "httponly" in login.headers["set-cookie"].lower()

    payload = {
        "site_url": "https://example.test", "email": "dummy@example.test", "password": "do-not-store-this",
        "min_amount": "100", "max_amount": "2000", "max_clicks": 3,
        "delay_ms": 250, "clicks_per_minute": 30,
    }
    response = client.post("/api/runs", json=payload)
    assert response.status_code == 200
    run_id = response.json()["id"]
    for _ in range(50):
        run = database.get_run(run_id)
        if run["status"] != "running":
            break
        time.sleep(0.01)
    assert captured[0]["password"] == payload["password"]
    assert database.get_run(run_id)["clicks_per_minute"] == 30
    assert "password" not in database.get_run(run_id)

    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["stats"]["otp_reached"] == 1
    assert "do-not-store-this" not in str(detail)
    export = client.get(f"/api/runs/{run_id}/export.csv")
    rows = list(csv.DictReader(io.StringIO(export.text)))
    assert rows[0]["row_id"] == "DUMMY-1"
    assert rows[0]["outcome"] == "OTP_REACHED"
    assert "do-not-store-this" not in export.text
    assert client.get("/api/runs").json()[0]["id"] == run_id
    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/runs").status_code == 401


def test_only_one_run_can_be_active(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "single-worker.sqlite3"))
    monkeypatch.setattr(main, "DB", database)
    monkeypatch.setattr(main, "run_browser", lambda *_args: None)
    client = TestClient(main.app)
    client.post("/api/login", json={"username": main.ADMIN_USER, "password": main.ADMIN_PASS})
    payload = {"site_url": "https://example.test", "email": "dummy@example.test", "password": "dummy"}
    first = client.post("/api/runs", json=payload)
    second = client.post("/api/runs", json=payload)
    assert first.status_code == 200
    assert second.status_code == 409
    assert database.has_active_run()


def test_start_rejects_bad_amount_range(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB", Database(str(tmp_path / "invalid.sqlite3")))
    client = TestClient(main.app)
    client.post("/api/login", json={"username": main.ADMIN_USER, "password": main.ADMIN_PASS})
    response = client.post("/api/runs", json={
        "site_url": "https://example.test", "email": "dummy@example.test", "password": "secret",
        "min_amount": "500", "max_amount": "100",
    })
    assert response.status_code == 422


def test_default_site_url_and_validation_errors_do_not_echo_password(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "defaults.sqlite3"))
    monkeypatch.setattr(main, "DB", database)
    monkeypatch.setattr(main, "run_browser", lambda run_id, config, stop, db, screenshots: db.update_run(run_id, "completed"))
    client = TestClient(main.app)
    client.post("/api/login", json={"username": main.ADMIN_USER, "password": main.ADMIN_PASS})
    response = client.post("/api/runs", json={"email": "dummy@example.test", "password": "hidden-target-secret"})
    assert response.status_code == 200
    detail = client.get(f"/api/runs/{response.json()['id']}").json()
    assert detail["run"]["site_url"] == "https://welcome.p2p-expert-traders.com"

    invalid = client.post("/api/runs", json={
        "email": "dummy@example.test", "password": "hidden-target-secret" * 60,
    })
    assert invalid.status_code == 422
    assert "hidden-target-secret" not in invalid.text


def test_settings_save_and_retrieve_stealth_mode(tmp_path, monkeypatch):
    test_settings_file = tmp_path / "saved_settings.json"
    monkeypatch.setattr(main, "SETTINGS_FILE", test_settings_file)
    client = TestClient(main.app)
    client.post("/api/login", json={"username": main.ADMIN_USER, "password": main.ADMIN_PASS})

    res = client.put("/api/settings", json={
        "site_url": "https://welcome.p2p-expert-traders.com",
        "email": "user@test.com",
        "password": "pass",
        "proxy": "http://zkagboxz:pass@p.webshare.io:80",
        "stealth_mode": False
    })
    assert res.status_code == 200

    saved = client.get("/api/settings").json()
    assert saved["stealth_mode"] is False
    assert saved["proxy"] == "http://zkagboxz:pass@p.webshare.io:80"

    # toggle back to True
    res2 = client.put("/api/settings", json={"stealth_mode": True})
    assert res2.status_code == 200
    saved2 = client.get("/api/settings").json()
    assert saved2["stealth_mode"] is True

