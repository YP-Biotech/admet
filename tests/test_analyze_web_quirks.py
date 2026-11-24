"""A browser gone before its request was read is dropped, not logged as a crash."""

import asyncio
import unittest
from importlib.util import find_spec


@unittest.skipUnless(find_spec("engineio"), "the analyze extra is not installed")
class AbortedRequestTests(unittest.TestCase):
    def test_a_request_with_nothing_in_it_is_dropped_and_a_real_one_still_answered(self):
        import engineio

        from admet.ui.web_quirks import quiet_aborted_requests

        quiet_aborted_requests()
        quiet_aborted_requests()                                   # twice: wrapped once
        server = engineio.AsyncServer(async_mode="asgi")
        sent = []

        async def send(message):
            sent.append(message)

        async def gone():
            return {"type": "http.disconnect"}

        scope = {"type": "http", "method": "GET", "path": "/engine.io/", "query_string": b"transport=polling&EIO=4",
                 "headers": [], "server": ("x", 80), "client": ("y", 1)}
        self.assertIsNone(asyncio.run(server.handle_request(scope, gone, send)))
        self.assertEqual([(m["type"], m.get("status")) for m in sent],
                         [("http.response.start", 400), ("http.response.body", None)])   # an empty reply, nothing raised
        sent.clear()

        async def request():
            return {"type": "http.request", "body": b"", "more_body": False}

        asyncio.run(server.handle_request(scope, request, send))
        self.assertEqual(sent[0]["type"], "http.response.start")   # a live client is answered as before
        self.assertNotEqual(sent[0]["status"], 400)

    def test_behind_a_starlette_middleware_as_nicegui_runs_it_a_gone_browser_raises_nothing(self):
        import engineio
        from starlette.middleware.base import BaseHTTPMiddleware

        from admet.ui.web_quirks import quiet_aborted_requests

        quiet_aborted_requests()

        class Through(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                return await call_next(request)

        app = Through(engineio.ASGIApp(engineio.AsyncServer(async_mode="asgi")))
        scope = {"type": "http", "method": "GET", "path": "/engine.io/", "query_string": b"transport=polling&EIO=4",
                 "headers": [], "server": ("x", 80), "client": ("y", 1), "scheme": "http", "root_path": "",
                 "http_version": "1.1"}

        async def gone():
            return {"type": "http.disconnect"}

        async def send(_message):
            pass

        asyncio.run(app(scope, gone, send))                        # no "No response returned"


if __name__ == "__main__":
    unittest.main()
