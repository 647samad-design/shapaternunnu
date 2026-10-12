"""Run slow work (sending email) after the page has been sent to the browser.

The web worker finishes the response first, then runs these jobs, so the
person clicking "Send" or "Place order" never waits for the mail server.
Outside a web request (management commands, shell) the job runs at once.
"""
import logging
import threading

log = logging.getLogger(__name__)
_state = threading.local()


def after_response(fn, *args, **kwargs):
    jobs = getattr(_state, "jobs", None)
    if jobs is None:
        _run(fn, args, kwargs)
    else:
        jobs.append((fn, args, kwargs))


def _run(fn, args, kwargs):
    try:
        fn(*args, **kwargs)
    except Exception:  # a failed background job must never break anything
        log.exception("Background job %s failed", getattr(fn, "__name__", fn))


class AfterResponseMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        _state.jobs = []
        try:
            response = self.get_response(request)
        except Exception:
            jobs, _state.jobs = _state.jobs, None
            for fn, args, kwargs in jobs:
                _run(fn, args, kwargs)
            raise
        jobs, _state.jobs = _state.jobs, None
        if jobs:
            original_close = response.close

            def close():
                try:
                    original_close()
                finally:
                    for fn, args, kwargs in jobs:
                        _run(fn, args, kwargs)
            response.close = close
        return response
