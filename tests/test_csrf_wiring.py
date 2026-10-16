"""CSRF must be wired end to end for the server-rendered interface.

Regression guard for a production-only failure: ``JWT_COOKIE_CSRF_PROTECT`` is
True in production, but nothing supplied the ``csrf_token`` template variable.
The ``X-CSRF-TOKEN`` header therefore rendered empty, every write failed CSRF
verification, and the user was redirected to sign-in — which presents as "the
system logs me out on every action".

Two independent paths must both work, because the interface uses both:

* HTMX requests send ``X-CSRF-TOKEN`` (from ``hx-headers`` on ``<body>``).
* Ordinary ``method="post"`` forms cannot set headers, so they carry the token
  as a hidden field, which requires ``JWT_CSRF_CHECK_FORM``.
"""

from __future__ import annotations

import re

import pytest

from app.features.auth.jwt_callbacks import current_csrf_token

ADMIN_EMAIL = "admin@ems.local"
ADMIN_PASSWORD = "ChangeMe123!"
# A cookie-authenticated write that exists for every role.
WRITE_PATH = "/app/notifications/read_all"


@pytest.fixture()
def prod(app, admin_user):
    """The app reconfigured with production CSRF settings over HTTPS.

    Reuses the session database (and therefore the seeded admin) but flips the
    CSRF-related config to its production values, which is the only state where
    this bug appears.
    """
    previous = {
        key: app.config.get(key)
        for key in (
            "JWT_COOKIE_CSRF_PROTECT",
            "JWT_CSRF_CHECK_FORM",
            "JWT_CSRF_IN_COOKIES",
            "JWT_COOKIE_SECURE",
            "JWT_ACCESS_CSRF_HEADER_NAME",
            "JWT_ACCESS_CSRF_FIELD_NAME",
        )
    }
    app.config.update(
        JWT_COOKIE_CSRF_PROTECT=True,
        JWT_CSRF_CHECK_FORM=True,
        JWT_CSRF_IN_COOKIES=True,
        JWT_COOKIE_SECURE=True,
        JWT_ACCESS_CSRF_HEADER_NAME="X-CSRF-TOKEN",
        JWT_ACCESS_CSRF_FIELD_NAME="csrf_token",
        PREFERRED_URL_SCHEME="https",
    )
    try:
        yield app
    finally:
        app.config.update(previous)


def _sign_in(client):
    return client.post(
        "/app/login",
        data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )


def _rendered_token(client) -> str:
    """The token the layout emits into hx-headers."""
    page = client.get("/app/employees").get_data(as_text=True)
    match = re.search(r'X-CSRF-TOKEN":\s*"([^"]*)"', page)
    assert match is not None, "the hx-headers CSRF attribute is missing from the page"
    return match.group(1)


class TestCsrfWiring:
    def test_protection_is_on(self, prod):
        assert prod.config["JWT_COOKIE_CSRF_PROTECT"] is True

    def test_form_field_check_is_enabled(self, prod):
        # Without this, hidden CSRF inputs are ignored and plain forms fail.
        assert prod.config["JWT_CSRF_CHECK_FORM"] is True

    def test_sign_in_still_works_with_csrf_on(self, prod, client):
        assert _sign_in(client).status_code == 302

    def test_template_renders_a_non_empty_token(self, prod, client):
        assert _sign_in(client).status_code == 302
        token = _rendered_token(client)
        assert token, "the CSRF token rendered empty; every write would fail"

    def test_header_submission_is_accepted(self, prod, client):
        """The HTMX path."""
        assert _sign_in(client).status_code == 302
        token = _rendered_token(client)

        response = client.post(
            WRITE_PATH, headers={"X-CSRF-TOKEN": token}, follow_redirects=False
        )
        location = response.headers.get("Location", "")
        assert "login" not in location, (
            f"the header path was rejected ({response.status_code} -> {location}); "
            "the user would appear logged out"
        )

    def test_form_field_submission_is_accepted(self, prod, client):
        """The plain-HTML-form path (token posted as a hidden field)."""
        assert _sign_in(client).status_code == 302
        token = _rendered_token(client)

        response = client.post(
            WRITE_PATH, data={"csrf_token": token}, follow_redirects=False
        )
        location = response.headers.get("Location", "")
        assert "login" not in location, (
            f"the form-field path was rejected ({response.status_code} -> {location}); "
            "plain forms would appear to log the user out"
        )

    def test_a_write_without_any_token_is_still_blocked(self, prod, client):
        """The protection must still bite, or the fix disabled CSRF entirely."""
        assert _sign_in(client).status_code == 302

        response = client.post(WRITE_PATH, follow_redirects=False)
        location = response.headers.get("Location", "")
        assert response.status_code == 401 or "login" in location, (
            "a cookie-authenticated write with no CSRF token was allowed through"
        )

    def test_current_csrf_token_never_raises(self, prod):
        """It runs while rendering every page, including anonymous ones."""
        with prod.test_request_context("/app/login"):
            assert current_csrf_token() == ""

    def test_login_page_renders_without_a_token(self, prod, client):
        assert client.get("/app/login").status_code == 200

    def test_every_layout_includes_the_form_stamping_script(self, prod, client):
        anonymous = client.get("/app/login").get_data(as_text=True)
        assert "csrf_token" in anonymous, (
            "the sign-in page does not include the CSRF form stamping script"
        )
        assert "stampForms" in anonymous, (
            "the sign-in page is missing the script that injects the hidden field"
        )

        assert _sign_in(client).status_code == 302
        authenticated = client.get("/app/employees").get_data(as_text=True)
        assert "stampForms" in authenticated, (
            "the authenticated layout is missing the CSRF form stamping script"
        )
