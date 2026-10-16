"""Friendly entry points.

The web UI lives under ``/app``, so the bare deployment URL and the obvious
guesses (``/login``, ``/logout``) previously answered 404. That is the first
thing anyone typing the address sees, and it makes a working system look broken.

These tests pin the intended behaviour: a visitor is sent to the sign-in page, a
signed-in user is sent to their dashboard, and the ``?next=`` parameter survives
the hop so a deep link still lands where it was aimed.
"""

from __future__ import annotations

import pytest

ADMIN_EMAIL = "admin@ems.local"
ADMIN_PASSWORD = "ChangeMe123!"


def _sign_in(client):
    return client.post(
        "/app/login",
        data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )


class TestAnonymousEntryPoints:
    @pytest.mark.parametrize("path", ["/", "/app", "/app/"])
    def test_bare_paths_go_to_sign_in(self, client, admin_user, path):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/app/login")

    @pytest.mark.parametrize("path", ["/login", "/login/"])
    def test_login_alias_reaches_the_sign_in_page(self, client, admin_user, path):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 302
        assert "/app/login" in response.headers["Location"]

        # Following it must actually render the form, not bounce again.
        page = client.get(path, follow_redirects=True)
        assert page.status_code == 200
        assert b"password" in page.data.lower()

    @pytest.mark.parametrize("path", ["/logout", "/logout/"])
    def test_logout_alias_does_not_404(self, client, admin_user, path):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 302

    def test_the_bare_domain_is_not_a_404(self, client, admin_user):
        """The regression this whole module exists for."""
        assert client.get("/", follow_redirects=False).status_code != 404

    def test_next_is_preserved_through_the_alias(self, client, admin_user):
        response = client.get(
            "/login?next=/app/leave", follow_redirects=False
        )
        location = response.headers["Location"]
        assert "next=" in location
        assert "leave" in location


class TestSignedInEntryPoints:
    def test_root_goes_to_the_dashboard(self, client, admin_user):
        assert _sign_in(client).status_code == 302
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/app/dashboard")

    def test_app_root_goes_to_the_dashboard(self, client, admin_user):
        assert _sign_in(client).status_code == 302
        response = client.get("/app", follow_redirects=False)
        assert response.headers["Location"].endswith("/app/dashboard")

    def test_logout_alias_actually_signs_out(self, client, admin_user):
        assert _sign_in(client).status_code == 302
        client.get("/logout", follow_redirects=True)

        # Signed out: the root must send them back to sign-in again.
        response = client.get("/", follow_redirects=False)
        assert response.headers["Location"].endswith("/app/login")


class TestHealthStillWorks:
    """The entry-point redirect must not shadow the probes."""

    @pytest.mark.parametrize("path", ["/health", "/api/v1/health"])
    def test_health_answers(self, client, path):
        assert client.get(path).status_code == 200


class TestPageAliases:
    """The UI is namespaced under /app, but people type the page name."""

    @pytest.mark.parametrize(
        "slug",
        [
            "dashboard",
            "attendance",
            "leave",
            "employees",
            "departments",
            "devices",
            "reports",
            "rules",
            "profile",
            "notifications",
        ],
    )
    def test_slug_redirects_to_its_real_view(self, client, admin_user, slug):
        response = client.get(f"/{slug}", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["Location"].endswith(f"/app/{slug}")

    def test_robotstxt_disallows_crawlers(self, client):
        """An internal HR system should not be indexed."""
        response = client.get("/robots.txt")
        assert response.status_code == 200
        assert "Disallow: /" in response.get_data(as_text=True)

    def test_favicon_does_not_404(self, client):
        assert client.get("/favicon.ico").status_code != 404
