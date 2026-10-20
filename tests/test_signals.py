"""Signal conversion and cleanup masking restore process state in every context."""

import signal
import threading

import pytest

from djinn_in_a_box.core.signals import signals_as_interrupt, signals_ignored


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGHUP])
def test_interrupt_and_restore(number):
    previous = signal.getsignal(number)

    def sentinel(signum, frame):
        raise AssertionError("previous handler called")

    signal.signal(number, sentinel)
    try:
        with pytest.raises(KeyboardInterrupt), signals_as_interrupt():
            handler = signal.getsignal(number)
            assert callable(handler)
            handler(number, None)
        assert signal.getsignal(number) is sentinel
    finally:
        signal.signal(number, previous)


@pytest.mark.parametrize("number", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_cleanup_ignores_second_signal_and_restores(number):
    previous = signal.getsignal(number)
    with signals_as_interrupt():
        handler = signal.getsignal(number)
        with signals_ignored():
            assert signal.getsignal(number) is signal.SIG_IGN
            # SIG_IGN is observed without signalling the pytest process.
            assert signal.getsignal(number) is signal.SIG_IGN
        assert signal.getsignal(number) is handler
    assert signal.getsignal(number) is previous


def test_non_main_thread_is_noop():
    before = {number: signal.getsignal(number)
              for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    results = []

    def worker():
        try:
            with signals_as_interrupt(), signals_ignored():
                results.append("inside")
        except BaseException as exc:
            results.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive() and results == ["inside"]
    assert {number: signal.getsignal(number) for number in before} == before


@pytest.mark.parametrize("context", [signals_as_interrupt, signals_ignored])
def test_none_previous_handler_restores_default(context, monkeypatch):
    recorded = []

    def install(number, handler):
        recorded.append((number, handler))
        return None

    monkeypatch.setattr(signal, "signal", install)
    with context():
        pass
    expected = {signal.SIGTERM, signal.SIGHUP}
    if context is signals_ignored:
        expected.add(signal.SIGINT)
    assert {number for number, handler in recorded if handler is signal.SIG_DFL} == expected
