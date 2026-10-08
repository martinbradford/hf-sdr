#!/usr/bin/env python3
"""Unit test for in-place VRX mode/filter changes (lsb/usb/cw tap swap, no flowgraph change).

No hardware and no GNU Radio needed (stubs are installed if it is absent). Checks the validation
and bookkeeping logic; it cannot show that the swap is *gapless* on a real flowgraph. That is
test_sideband_flowgraph.py, which needs GNU Radio.

  (1) lsb <-> usb <-> cw changes are accepted and apply the new mode's default passband,
  (2) an explicit filter is validated (numeric, ordered, inside the post-decimation band),
  (3) a change that would need another demodulator is refused with `unsupported`, and a bad
      request changes nothing,
  (4) apply_demod_change swaps taps on the running filter (set_taps) and updates the record,
  (5) update_vrx never takes the graph lock (lock()/unlock() is the multi-second gap).

Run:  python server\\python\\headless\\test_vrx_update.py   (needs numpy)
"""
from unittest import mock

from gr_stubs import install_gnuradio_stubs

install_gnuradio_stubs()
import server  # noqa: E402
from server import (DEFAULT_FILTERS, IN_PLACE_MODES, INTER_RATE, ProtoError,  # noqa: E402
                    apply_demod_change, resolve_demod_change)

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


def raises(code, fn, *a, **k):
    try:
        fn(*a, **k)
    except ProtoError as e:
        return e.code == code
    return False


# (1) sideband changes
check(resolve_demod_change("lsb", "usb") == ("usb", DEFAULT_FILTERS["usb"]),
      "lsb -> usb: accepted, takes usb's default passband")
check(resolve_demod_change("usb", "lsb") == ("lsb", DEFAULT_FILTERS["lsb"]), "usb -> lsb")
check(resolve_demod_change("usb", "cw") == ("cw", DEFAULT_FILTERS["cw"]), "usb -> cw")
check(resolve_demod_change("lsb", "lsb") == ("lsb", None), "same mode, no filter: nothing to do")
check(resolve_demod_change("lsb") == ("lsb", None), "no mode, no filter: nothing to do")

# (2) explicit filter
check(resolve_demod_change("lsb", None, {"low_hz": -2400, "high_hz": -300}) == ("lsb", (-2400, -300)),
      "filter only: same mode, new edges")
check(resolve_demod_change("lsb", "usb", {"low_hz": 300, "high_hz": 2400}) == ("usb", (300, 2400)),
      "mode + filter together: the explicit filter wins over the default")
for bad in ({"low_hz": 3000, "high_hz": 200}, {"low_hz": 200}, {"low_hz": "a", "high_hz": 5},
            {"low_hz": 0, "high_hz": INTER_RATE}, {"low_hz": -INTER_RATE, "high_hz": 0}, 5, "x"):
    check(raises("bad_request", resolve_demod_change, "usb", None, bad), f"bad filter rejected: {bad!r}")

# (3) refused changes
check(raises("unsupported", resolve_demod_change, "usb", "am"), "usb -> am (not implemented): unsupported")
check(raises("unsupported", resolve_demod_change, "usb", "bogus"), "unknown mode: unsupported")
saved = IN_PLACE_MODES
try:
    server.IN_PLACE_MODES = ("lsb", "usb")          # pretend cw needed its own demodulator
    check(raises("unsupported", resolve_demod_change, "usb", "cw"),
          "mode outside the in-place set needs remove + add")
finally:
    server.IN_PLACE_MODES = saved

# (4) taps swapped in place, record updated
rec = {"mode": "lsb", "filter": {"low_hz": -3000, "high_hz": -200}, "sb": mock.MagicMock()}
new_mode, edges = resolve_demod_change(rec["mode"], "usb")
apply_demod_change(rec, new_mode, edges)
check(rec["mode"] == "usb" and rec["filter"] == {"low_hz": 200, "high_hz": 3000}, "record updated")
check(rec["sb"].set_taps.call_count == 1, "set_taps called exactly once on the running filter")
try:
    server.firdes.complex_band_pass.assert_called_with(1.0, INTER_RATE, 200, 3000, server.SIDEBAND_TRANSITION_HZ)
    designed = True
except AssertionError:
    designed = False
check(designed, "taps designed for the new passband at the post-decimation rate")

rec["sb"].reset_mock()
apply_demod_change(rec, "usb", None)
check(rec["sb"].set_taps.call_count == 0, "no passband change: filter untouched")

# a refused request must not touch the filter
rec2 = {"mode": "usb", "filter": {"low_hz": 200, "high_hz": 3000}, "sb": mock.MagicMock()}
try:
    n, e = resolve_demod_change(rec2["mode"], "am")
    apply_demod_change(rec2, n, e)
except ProtoError:
    pass
check(rec2["mode"] == "usb" and rec2["sb"].set_taps.call_count == 0, "refused change leaves VRX and filter unchanged")

# (5) update_vrx end to end with a stand-in `self`: never locks, answers with the public record
locked = []
vrx_rec = {"vrx_id": 7, "freq": 7_150_000, "mode": "lsb", "filter": {"low_hz": -3000, "high_hz": -200},
           "volume": 0.5, "sb": mock.MagicMock(), "xlate": mock.MagicMock(), "vol": mock.MagicMock()}
fake = mock.MagicMock()
fake._vrx = {7: vrx_rec}
fake.center = 7_150_000
fake.lock.side_effect = lambda: locked.append("lock")
fake.unlock.side_effect = lambda: locked.append("unlock")
fake._vrx_public.side_effect = lambda vid: {"vrx_id": vid, "mode": vrx_rec["mode"], "freq_hz": vrx_rec["freq"]}

out = server.SdrServer.update_vrx(fake, 7, mode="usb", freq_hz=7_151_000)
check(out["mode"] == "usb" and vrx_rec["mode"] == "usb", "update_vrx(mode=usb) reports and records the new mode")
check(vrx_rec["freq"] == 7_151_000 and vrx_rec["xlate"].set_center_freq.called, "frequency still applied in the same call")
check(locked == [], "update_vrx never calls lock()/unlock() (no flowgraph restart)")
check(vrx_rec["sb"].set_taps.call_count == 1, "exactly one tap swap")

try:
    server.SdrServer.update_vrx(fake, 7, mode="am", freq_hz=7_152_000)
    refused = False
except ProtoError as e:
    refused = e.code == "unsupported"
check(refused and vrx_rec["mode"] == "usb" and vrx_rec["freq"] == 7_151_000,
      "refused mode change is atomic: neither mode nor frequency changed")
check(locked == [], "still no lock after a refused request")

print("ALL PASS" if not fails else f"{fails} FAILED")
raise SystemExit(1 if fails else 0)
