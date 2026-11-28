"""Synthetic sign-in site served inside Chromium after the real browser policy."""
from harness.integrations.browser import BrowserService

FIXTURE_URL = "https://93.184.215.14/"
FIXTURE_VALUE = "fixture-only-value"


class SignInRoute:
    def __init__(self, route):
        self.route = route
        self.request = route.request
        self.abort = route.abort

    async def continue_(self):
        headers = await self.request.all_headers()
        signed_in = "fixture=approved" in headers.get("cookie", "")
        response_headers = {}
        if self.request.method == "POST":
            signed_in = self.request.post_data == f"proof={FIXTURE_VALUE}"
            if signed_in:
                response_headers["Set-Cookie"] = "fixture=approved; Secure; Max-Age=3600; Path=/"
        if signed_in:
            body = "<h1>Fixture signed in</h1>"
        else:
            body = ('<form method="post" style="position:absolute;left:100px;top:100px;">'
                    '<input type="password" name="proof"><button>Sign in</button></form>')
        await self.route.fulfill(status=200, body=body, content_type="text/html", headers=response_headers)


class SignInFixture(BrowserService):
    async def _route_request(self, route):
        if route.request.url == FIXTURE_URL:
            await super()._route_request(SignInRoute(route))
        else:
            await route.abort()
