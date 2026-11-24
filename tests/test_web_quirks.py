"""A browser gone before its request was read is dropped, not logged as a crash."""

import asyncio
import unittest


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
        self.assertEqual(sent, [])                                 # nothing to answer, nothing raised

        async def request():
            return {"type": "http.request", "body": b"", "more_body": False}

        asyncio.run(server.handle_request(scope, request, send))
        self.assertEqual(sent[0]["type"], "http.response.start")   # a live client is answered as before


if __name__ == "__main__":
    unittest.main()
