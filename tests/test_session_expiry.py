"""Session expiry must not litter the page with sign-in pages.

Regression guard. HTMX follows redirects transparently, so when a session
expired the sign-in *page* was fetched and swapped into whichever fragment was
polling. With the board, the punch stream and the notification bell all polling,
the user collected a stack of nested sign-in pages instead of being asked to sign
in once.

The fix is ``HX-Redirect``: HTMX performs a real navigation instead of a swap, so
exactly one clean sign-in page appears and no sign-in HTML ever reaches a
fragment target.
"""

from __future__ import annotations

import pytest

#: Routes HTMX polls, and the href a real navigation would use.
FRAGMENT_ROUTES = [
    "/app/attendance/live",
    "/app/notifications/bell",
]

PAGE_ROUTES = [
    "/app/employees",
    "/app/attendance",
    "/app/dashboard",
]


class TestExpiredSessionOnHtmxPoll:
    @pytest.mark.parametrize("path", FRAGMENT_ROUTES)
    def test_answers_with_hx_redirect(self, client, path):
        response = client.get(path, headers={"HX-Request": "true"},
                              follow_redirects=False)
        assert response.status_code == 200
        assert response.headers.get("HX-Redirect", "").startswith("/app/login")

    @pytest.mark.parametrize("path", FRAGMENT_ROUTES)
    def test_sends_no_html_to_swap(self, client, path):
        """The whole bug was sign-in HTML being swapped into a fragment."""
        response = client.get(path, headers={"HX-Request": "true"},
                              follow_redirects=False)
        body = response.get_data(as_text=True)
        assert body == "", f"a fragment target received {len(body)} bytes of HTML"
        assert "password" not in body.lower()

    @pytest.mark.parametrize("path", FRAGMENT_ROUTES)
    def test_does_not_redirect_a_fragment_to_itself(self, client, path):
        """`next` must not be a fragment URL.

        Sending the user back to a polled fragment would render a bare partial
        with no layout after they signed in.
        """
        response = client.get(path, headers={"HX-Request": "true"},
                              follow_redirects=False)
        target = response.headers.get("HX-Redirect", "")
        assert "/live" not in target
        assert "/bell" not in target

    def test_a_non_htmx_request_still_gets_a_redirect(self, client):
        response = client.get("/app/employees", follow_redirects=False)
        assert response.status_code == 302
        assert "/app/login" in response.headers["Location"]


class TestExpiredSessionOnNavigation:
    @pytest.mark.parametrize("path", PAGE_ROUTES)
    def test_redirects_and_remembers_where_you_were_going(self, client, path):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 302
        location = response.headers["Location"]
        assert "/app/login" in location
        # A real page IS a useful destination, so `next` is kept here.
        assert "next=" in location

    def test_no_hx_redirect_header_on_a_plain_navigation(self, client):
        response = client.get("/app/employees", follow_redirects=False)
        assert "HX-Redirect" not in response.headers


class TestTheFixDoesNotBreakSignIn:
    def test_the_sign_in_page_itself_is_reachable(self, client):
        assert client.get("/app/login").status_code == 200

    def test_it_renders_a_password_field(self, client):
        body = client.get("/app/login").get_data(as_text=True)
        assert "password" in body.lower()

    def test_a_signed_in_user_is_unaffected(self, client, admin_user):
        client.post(
            "/app/login",
            data={"email": "admin@ems.local", "password": "ChangeMe123!"},
        )
        response = client.get("/app/attendance/live",
                              headers={"HX-Request": "true"})
        assert response.status_code == 200
        assert "HX-Redirect" not in response.headers
        assert "Live punch stream" in response.get_data(as_text=True)


class TestTheClientGuardExists:
    """The server header is the fix; the script is the backstop."""

    def test_app_js_guards_against_repeated_redirects(self, app):
        from pathlib import Path

        source = Path(app.static_folder, "js", "app.js").read_text(encoding="utf-8")
        assert "redirectingToSignIn" in source, "no single-navigation guard"
        assert "goToSignIn" in source

    def test_app_js_handles_401_from_the_keepalive(self, app):
        from pathlib import Path

        source = Path(app.static_folder, "js", "app.js").read_text(encoding="utf-8")
        assert "htmx:beforeSwap" in source, "no guard against swapping a sign-in page"
        assert "response.status === 401" in source, "keepalive 401 is not handled"
