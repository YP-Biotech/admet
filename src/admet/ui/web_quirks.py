"""Quirks of the web stack under admet analyze, handled in one place."""

from __future__ import annotations


class _ClientGone(Exception):
    pass


def quiet_aborted_requests() -> None:
    """A browser that drops a long-poll before the server reads it (a reload, a closed tab, a proxy
    timing out) reaches engine.io as a request with nothing in it, and engine.io raises
    KeyError('REQUEST_METHOD') with a full traceback in the log. There is no one to answer, so such a
    request is dropped. Safe to call more than once."""
    from engineio import async_server
    from engineio.async_drivers import asgi

    if getattr(async_server.AsyncServer.handle_request, "_admet_quiet", False):
        return
    translate = asgi._async["translate_request"]

    async def translate_request(*args, **kwargs):
        environ = await translate(*args, **kwargs)
        if not environ:
            raise _ClientGone
        return environ

    handle = async_server.AsyncServer.handle_request

    async def handle_request(self, *args, **kwargs):
        try:
            return await handle(self, *args, **kwargs)
        except _ClientGone:
            return None

    handle_request._admet_quiet = True
    asgi._async["translate_request"] = translate_request
    async_server.AsyncServer.handle_request = handle_request
