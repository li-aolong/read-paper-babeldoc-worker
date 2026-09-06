from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_app import pdf_bytes

import app


@pytest.fixture
def models_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(app, "_jobs", {})
    monkeypatch.setattr(app, "_idempotency_jobs", {})
    monkeypatch.setattr(app, "_job_credentials", {})
    monkeypatch.setattr(app, "WORKER_TOKEN", "")
    monkeypatch.setattr(app, "_executor", SimpleNamespace(submit=lambda *_: None))
    monkeypatch.setenv("BABELDOC_MODELS", "model-a, model-b,model-a")
    return TestClient(app.app)


def test_info_only_exposes_allowed_models(models_worker, monkeypatch):
    info = models_worker.get("/api/info").json()
    assert info["models"] == ["model-a", "model-b"]
    assert info["model"] == "model-a"
    assert not info["user_provider_overrides"]
    monkeypatch.delenv("BABELDOC_MODELS")
    assert app.info()["models"] == [app.ENGINE["model"]]


def test_model_whitelist_cannot_be_bypassed(models_worker):
    response = models_worker.post(
        "/api/jobs",
        files={"file": ("paper.pdf", pdf_bytes())},
        data={"model": "expensive-model"},
    )
    assert response.status_code == 422
    assert not app._jobs
    job = models_worker.post(
        "/api/jobs",
        files={"file": ("paper.pdf", pdf_bytes())},
        data={"model": "model-b"},
    ).json()
    assert job["model"] == job["engine"]["model"] == "model-b"


def test_overrides_require_configured_and_matching_worker_token(
    models_worker, monkeypatch
):
    kwargs = {
        "files": {"file": ("paper.pdf", pdf_bytes())},
        "data": {
            "model": "user-a",
            "api_base_url": "https://api.example/v1",
            "api_key": "private-key",
        },
    }
    assert models_worker.post("/api/jobs", **kwargs).status_code == 503
    monkeypatch.setattr(app, "WORKER_TOKEN", "worker-token")
    assert models_worker.post("/api/jobs", **kwargs).status_code == 401
    assert (
        models_worker.post(
            "/api/jobs", headers={"Authorization": "Bearer wrong"}, **kwargs
        ).status_code
        == 401
    )
    assert not app._jobs


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/v1",
        "https://localhost/v1",
        "https://127.0.0.1/v1",
        "https://user:password@api.example/v1",
    ],
)
def test_user_api_urls_follow_outbound_safety_rules(models_worker, monkeypatch, url):
    monkeypatch.setattr(app, "WORKER_TOKEN", "worker-token")
    response = models_worker.post(
        "/api/jobs",
        headers={"Authorization": "Bearer worker-token"},
        files={"file": ("paper.pdf", pdf_bytes())},
        data={"model": "user-a", "api_base_url": url, "api_key": "private-key"},
    )
    assert response.status_code == 400
    assert not app._jobs


def test_user_credentials_are_memory_only_and_model_idempotency_is_scoped(
    models_worker, monkeypatch, tmp_path
):
    monkeypatch.setattr(app, "WORKER_TOKEN", "worker-token")

    async def safe(url, **_):
        return url

    monkeypatch.setattr(app, "validate_outbound_url", safe)
    raw = pdf_bytes()

    def submit(model, key, base="https://api.example/v1"):
        return models_worker.post(
            "/api/jobs",
            headers={"Authorization": "Bearer worker-token"},
            files={"file": ("paper.pdf", raw)},
            data={
                "model": model,
                "api_base_url": base,
                "api_key": "private-key",
                "provider": "custom",
                "idempotency_key": key,
            },
        )

    first = submit("user-a", "a" * 32).json()
    assert submit("user-a", "a" * 32).json()["id"] == first["id"]
    assert submit("user-b", "a" * 32).status_code == 409
    assert submit("user-a", "a" * 32, "https://second.example/v1").status_code == 409
    second = submit("user-b", "b" * 32).json()
    assert first["id"] != second["id"]
    assert app._job_credentials[first["id"]]["api_key"] == "private-key"
    app._write_manifest(app._jobs[first["id"]], {})
    for path in tmp_path.rglob("*.json"):
        assert "private-key" not in path.read_text()
        assert "api.example" not in path.read_text()
    assert "private-key" not in json.dumps(first)


@pytest.mark.parametrize("failure", [False, True])
def test_credentials_clear_on_completion_or_failure_and_errors_do_not_leak(
    models_worker, monkeypatch, tmp_path, caplog, failure
):
    monkeypatch.setattr(app, "WORKER_TOKEN", "worker-token")
    job = app._create_job(
        pdf_bytes(),
        "paper.pdf",
        "",
        2,
        False,
        False,
        model="user-a",
        credentials={
            "provider": "custom",
            "api_base_url": "https://api.example/v1",
            "api_key": "private-key",
        },
    )

    async def translate(job_id):
        assert app._job_credentials[job_id]["api_key"] == "private-key"
        if failure:
            raise RuntimeError("private-key https://api.example/v1")
        app._patch_job(job_id, status="completed")

    monkeypatch.setattr(app, "_translate", translate)
    app._run_job(job["id"])
    assert job["id"] not in app._job_credentials
    assert app._jobs[job["id"]]["status"] == ("failed" if failure else "completed")
    assert "private-key" not in caplog.text
    assert "private-key" not in app._status_path(job["id"]).read_text()


def test_restart_loses_credentials_and_requires_explicit_retry(
    models_worker, monkeypatch
):
    monkeypatch.setattr(app, "WORKER_TOKEN", "worker-token")
    raw = pdf_bytes()
    credentials = {
        "provider": "custom",
        "api_base_url": "https://api.example/v1",
        "api_key": "private-key",
    }
    first = app._create_job(
        raw, "paper.pdf", "", 2, False, False, "a" * 32, "user-a", credentials
    )
    app._jobs.clear()
    app._job_credentials.clear()
    app._load_existing_jobs()
    assert app._jobs[first["id"]]["status"] == "failed"
    assert "凭据已清除" in app._jobs[first["id"]]["error"]
    assert json.loads(app._status_path(first["id"]).read_text())["status"] == "failed"
    second = app._create_job(
        raw, "paper.pdf", "", 2, False, False, "a" * 32, "user-a", credentials
    )
    assert second["id"] != first["id"]


def test_translator_receives_selected_model_and_secret_free_isolated_cache(
    models_worker, monkeypatch
):
    from babeldoc.translator import translator as module

    calls = []

    class Translator:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            self.parameters = {}

        def add_cache_impact_parameters(self, key, value):
            self.parameters[key] = value

    monkeypatch.setattr(module, "OpenAITranslator", Translator)
    first = app._create_job(
        pdf_bytes(), "paper.pdf", "", 2, False, False, model="model-b"
    )
    translator = app._make_translator(first, "https://api.example/v1", "private-key")
    assert calls[0]["model"] == "model-b"
    assert calls[0]["api_key"] == "private-key"
    assert calls[0]["base_url"] == "https://api.example/v1"
    assert translator.parameters["job_id"] == first["id"]
    assert (
        translator.parameters["provider_fingerprint"] == first["provider_fingerprint"]
    )
    assert "private-key" not in json.dumps(translator.parameters)
