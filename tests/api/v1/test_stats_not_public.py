"""The portfolio volume must not be readable without a session."""

import pytest


@pytest.mark.parametrize("path", ["/api/v1/stats/public-overview", "/api/v1/stats/public"])
def test_no_public_stats_endpoint_answers_without_a_session(client, path):
    """The login marquee used to read the firm's semáforo counts with no auth.
    Anonymous requests get no counts: the route is gone (404) or guarded (401/403)."""
    r = client.get(path)
    assert r.status_code in (401, 403, 404)
    assert "rojo" not in r.text and "verde" not in r.text


@pytest.mark.parametrize("path", ["/api/v1/stats/firm", "/api/v1/stats/me"])
def test_the_authenticated_stats_stay_guarded(client, path):
    assert client.get(path).status_code in (401, 403)
