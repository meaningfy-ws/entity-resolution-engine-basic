"""End-to-end smoke test: app.py main() invoked directly."""

import json
import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from ere.adapters.utils import get_response_from_message
from ere.entrypoints.app import main
from ere.entrypoints.queue_worker import RedisQueueWorker

REQUEST_QUEUE = "test-app-requests"
RESPONSE_QUEUE = "test-app-responses"


@pytest.fixture
def app_queues(redis_client):
    """Provide test queue names and clean them before/after test."""
    redis_client.delete(REQUEST_QUEUE, RESPONSE_QUEUE)
    yield REQUEST_QUEUE, RESPONSE_QUEUE
    redis_client.delete(REQUEST_QUEUE, RESPONSE_QUEUE)


@pytest.mark.integration
def test_app_main_processes_single_request(
    redis_client, app_queues, resolver_config_path, rdf_mapping_path, monkeypatch
):
    """
    E2E smoke: main() resolves one queued request and writes response.

    Flow:
    1. Set env vars so main() connects to the test Redis with correct config
    2. Push one EntityMentionResolutionRequest
    3. Call main() — patched to exit after processing the first message
    4. Assert response structure
    """
    req_queue, resp_queue = app_queues

    # 1. Wire main() to test Redis + configs via env vars
    monkeypatch.setenv("REDIS_HOST", os.environ.get("REDIS_HOST", "localhost"))
    monkeypatch.setenv("REDIS_PORT", os.environ.get("REDIS_PORT", "6379"))
    monkeypatch.setenv("REDIS_DB", os.environ.get("REDIS_DB", "0"))
    if redis_password := os.environ.get("REDIS_PASSWORD"):
        monkeypatch.setenv("REDIS_PASSWORD", redis_password)
    monkeypatch.setenv("ERSYS_REQUEST_QUEUE", req_queue)
    monkeypatch.setenv("ERSYS_RESPONSE_QUEUE", resp_queue)
    monkeypatch.setenv("RESOLVER_CONFIG_PATH", str(resolver_config_path))
    monkeypatch.setenv("RDF_MAPPING_PATH", str(rdf_mapping_path))

    # 2. Push request before starting main()
    payload = {
        "type": "EntityMentionResolutionRequest",
        "entity_mention": {
            "identifiedBy": {
                "request_id": "app-smoke-001",
                "source_id": "TEST",
                "entity_type": "ORGANISATION",
            },
            "content": (
                "@prefix org: <http://www.w3.org/ns/org#> .\n"
                "@prefix cccev: <http://data.europa.eu/m8g/> .\n"
                "@prefix epo: <http://data.europa.eu/a4g/ontology#> .\n"
                "@prefix epd: <http://data.europa.eu/a4g/resource/> .\n"
                'epd:ent001 a org:Organization ;\n'
                '    epo:hasLegalName "Acme Corp" ;\n'
                '    cccev:registeredAddress [ epo:hasCountryCode "US" ] .\n'
            ),
            "content_type": "text/turtle",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ere_request_id": "app-smoke-001:01",
    }
    redis_client.rpush(req_queue, json.dumps(payload).encode())

    # 3. Call main() — stop loop after first message via KeyboardInterrupt
    _real = RedisQueueWorker.process_single_message
    calls = []

    def _stop_after_first(self):
        result = _real(self)
        calls.append(result)
        raise KeyboardInterrupt  # caught by main()'s except block → clean exit

    with patch.object(RedisQueueWorker, "process_single_message", _stop_after_first):
        with patch("sys.argv", ["ere.entrypoints.app"]):
            main()

    assert calls, "process_single_message was never called"

    # 4. Assert response
    result = redis_client.brpop(resp_queue, timeout=5)
    assert result is not None, "No response in output queue"
    _, raw = result

    response = get_response_from_message(raw)
    assert response.type == "EntityMentionResolutionResponse"
    assert response.entity_mention_id.request_id == "app-smoke-001"
    assert response.candidates is not None
