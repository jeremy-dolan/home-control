"""The shared reachability state machine every panel reports through.

Before this existed each panel decided for itself how long to stay quiet about
a device that wasn't answering, and no two agreed — Hue reported the first
failed read instantly, Roku deliberately said nothing, Sonos swallowed the
exception, Midea only spoke up when it had no units at all. These tests pin the
one rule they now share.
"""

from home_control.systems.base import (
    CONNECTING,
    FAILED,
    GRACE_ATTEMPTS,
    LIVE,
    RECONNECTING,
    UNREACHABLE,
    Reachability,
    scrub_error,
)


def test_never_reached_stays_quiet_then_says_why():
    r = Reachability()
    assert r.state == CONNECTING and r.message == "Connecting..."
    assert not r.has_values  # nothing read, so nothing to draw

    for _ in range(GRACE_ATTEMPTS - 1):
        r.failed("No route to host")
        assert r.state == CONNECTING, "still within grace"
        assert r.message == "Connecting..."

    r.failed("No route to host")
    assert r.state == FAILED
    assert r.message == "No route to host"  # grace spent: say why
    assert not r.has_values


def test_a_missed_beat_keeps_the_last_reading():
    r = Reachability()
    r.succeeded()
    assert r.state == LIVE and r.message == "" and r.has_values

    r.failed("timed out")
    assert r.state == RECONNECTING
    assert r.message == "reconnecting..."
    assert r.has_values, "last reading still stands inside grace"

    for _ in range(GRACE_ATTEMPTS):
        r.failed("timed out")
    assert r.state == UNREACHABLE
    assert r.message == "unreachable — timed out"
    assert not r.has_values, "past grace those values are no longer ours to claim"


def test_recovery_clears_the_failure():
    r = Reachability()
    for _ in range(GRACE_ATTEMPTS):
        r.failed("No route to host")
    assert r.state == FAILED
    r.succeeded()
    assert r.state == LIVE and r.message == "" and r.reason == ""


def test_having_been_reached_changes_the_wording():
    """The same failure reads differently depending on whether the values we're
    dimming are real last-known state or defaults we never fetched."""
    fresh, seen = Reachability(), Reachability()
    seen.succeeded()
    for _ in range(GRACE_ATTEMPTS):
        fresh.failed("No route to host")
        seen.failed("No route to host")
    assert fresh.message == "No route to host"
    assert seen.message == "unreachable — No route to host"


def test_grace_is_configurable_per_device():
    r = Reachability(grace=1)
    r.failed("nope")
    assert r.state == FAILED


def test_failed_without_a_reason_still_counts():
    r = Reachability(grace=1)
    r.failed()
    assert r.state == FAILED and r.message == "unreachable"


def test_scrub_error_keeps_the_reason_and_drops_the_rest():
    assert scrub_error(
        "Error -1: GET Request to http://192.168.1.99/api/sEcret/lights/ failed: "
        "[Errno 113] No route to host"
    ) == "No route to host"
    # A URL collapses to its host wherever it appears, so no token rides along.
    assert scrub_error("POST http://10.0.0.4/api/t0k3n/x refused") == "POST 10.0.0.4 refused"
    assert scrub_error("Error 1: GET Request to http://10.0.0.4/api/k/ failed: unauthorized user") == (
        "unauthorized user"
    )
    # urllib's wrapper, as Roku's ECP calls raise it.
    assert scrub_error("<urlopen error [Errno 113] No route to host>") == "No route to host"
    # ...and urllib3's, several layers deep, as a Sonos SOAP call raises it.
    assert scrub_error(
        "HTTPConnectionPool(host='192.168.1.60', port=1400): Max retries exceeded with url: "
        "/MediaRenderer/AVTransport/Control (Caused by NewConnectionError("
        "'<urllib3.connection.HTTPConnection object at 0x7f>: Failed to establish a new "
        "connection: [Errno 113] No route to host'))"
    ) == "No route to host"
    # Nothing to strip: left alone.
    assert scrub_error("Press the bridge link button, then wait...") == (
        "Press the bridge link button, then wait..."
    )


def test_in_parens_leaves_an_acronym_alone():
    from home_control.systems.base import in_parens

    assert in_parens("Connecting...") == "connecting..."
    assert in_parens("No route to host") == "no route to host"
    assert in_parens("HTTPConnectionPool(...)") == "HTTPConnectionPool(...)"
    assert in_parens("") == ""


def test_reason_survives_a_later_reasonless_failure():
    """Panels that can't always name the failure shouldn't erase one that did."""
    r = Reachability(grace=1)
    r.failed("No route to host")
    r.failed()
    assert r.message == "No route to host"
