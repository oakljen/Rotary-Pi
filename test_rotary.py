#!/usr/bin/env python3
"""
test_rotary.py — component tests for rotary_phone_sip.py

Usage
-----
  python3 test_rotary.py                  # run all tests
  python3 test_rotary.py --test netstring # run only netstring tests
  python3 test_rotary.py --test sim       # run only simulation engine
  python3 test_rotary.py --test state     # run only state-machine tests
  python3 test_rotary.py --test tone      # run only tone-player (audio)
  python3 test_rotary.py --test dial      # run only digit-accumulation
  python3 test_rotary.py --verbose        # verbose pass/fail detail
  python3 test_rotary.py --sim-duration 8 # shorten the SIM scenario wait

SIP overrides (also respected by the real bridge via env):
  python3 test_rotary.py --server sip.local --user 1001 --password s3cr3t
"""

import argparse
import os
import sys
import time
import threading
import unittest
from io import StringIO
from unittest.mock import patch, MagicMock

# ── Make sure we can import the bridge module ──────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))

# Stub out RPi.GPIO before importing so tests run on any platform
sys.modules.setdefault("RPi", MagicMock())
sys.modules.setdefault("RPi.GPIO", MagicMock())

import rotary_phone_sip as bridge_mod


# ══════════════════════════════════════════════════════════════════════════════
# NETSTRING tests
# ══════════════════════════════════════════════════════════════════════════════

class TestNetstring(unittest.TestCase):

    def test_encode_basic(self):
        result = bridge_mod.encode_netstring("hello")
        self.assertEqual(result, b"5:hello,")

    def test_encode_empty(self):
        result = bridge_mod.encode_netstring("")
        self.assertEqual(result, b"0:,")

    def test_decode_single(self):
        msgs, rem = bridge_mod.decode_netstrings(b"5:hello,")
        self.assertEqual(msgs, [b"hello"])
        self.assertEqual(rem, b"")

    def test_decode_multiple(self):
        data = b"3:foo,3:bar,"
        msgs, rem = bridge_mod.decode_netstrings(data)
        self.assertEqual(msgs, [b"foo", b"bar"])
        self.assertEqual(rem, b"")

    def test_decode_partial(self):
        msgs, rem = bridge_mod.decode_netstrings(b"5:hel")
        self.assertEqual(msgs, [])
        self.assertEqual(rem, b"5:hel")

    def test_roundtrip_json(self):
        import json
        payload = json.dumps({"command": "dial", "params": "sip:100@test.local"})
        encoded = bridge_mod.encode_netstring(payload)
        msgs, _ = bridge_mod.decode_netstrings(encoded)
        self.assertEqual(len(msgs), 1)
        decoded = json.loads(msgs[0])
        self.assertEqual(decoded["command"], "dial")

    def test_decode_framing_error_no_crash(self):
        # Unparseable length prefix — decoder should return empty without raising
        data = b"\xffXgarbage3:ok!,"
        msgs, _ = bridge_mod.decode_netstrings(data)
        self.assertIsInstance(msgs, list)


# ══════════════════════════════════════════════════════════════════════════════
# STATE MACHINE tests
# ══════════════════════════════════════════════════════════════════════════════

class TestStateMachine(unittest.TestCase):

    def _make_bridge(self):
        """Return a Bridge with a stubbed SIP engine and tone player."""
        b = bridge_mod.Bridge.__new__(bridge_mod.Bridge)
        b.state         = bridge_mod.State.IDLE
        b.bell          = MagicMock()
        b.sip           = MagicMock()
        b._digit_buf    = []
        b._pulse_count  = 0
        b._last_pulse_time = 0.0
        b._dialling     = False
        b._digit_timer  = None
        b._lock         = threading.Lock()

        # Silence the global tone_player
        bridge_mod.tone_player.start_dial_tone = MagicMock()
        bridge_mod.tone_player.stop_dial_tone  = MagicMock()
        bridge_mod.tone_player.announce_digit  = MagicMock()
        bridge_mod.tone_player.announce_call_failed = MagicMock()
        return b

    def test_idle_lift_enters_collecting(self):
        b = self._make_bridge()
        b._on_lifted(bridge_mod.State.IDLE)
        self.assertEqual(b.state, bridge_mod.State.COLLECTING)

    def test_collecting_replace_aborts(self):
        b = self._make_bridge()
        b.state = bridge_mod.State.COLLECTING
        b._on_replaced(bridge_mod.State.COLLECTING)
        self.assertEqual(b.state, bridge_mod.State.IDLE)
        self.assertEqual(b._digit_buf, [])

    def test_ringing_lift_answers(self):
        b = self._make_bridge()
        b.state = bridge_mod.State.RINGING_IN
        mock_result = MagicMock()
        mock_result.stdout = "USB Audio"
        with patch("subprocess.run", return_value=mock_result):
            b._on_lifted(bridge_mod.State.RINGING_IN)
        self.assertEqual(b.state, bridge_mod.State.IN_CALL)
        b.sip.answer.assert_called()

    def test_in_call_replace_hangs_up(self):
        b = self._make_bridge()
        b.state = bridge_mod.State.IN_CALL
        b._on_replaced(bridge_mod.State.IN_CALL)
        self.assertEqual(b.state, bridge_mod.State.IDLE)
        b.sip.hangup.assert_called_once()

    def test_incoming_when_busy_rejected(self):
        b = self._make_bridge()
        b.state = bridge_mod.State.IN_CALL
        b.on_incoming_call("sip:bob@test.local")
        # State unchanged; hangup called to reject
        self.assertEqual(b.state, bridge_mod.State.IN_CALL)
        b.sip.hangup.assert_called_once()

    def test_call_ended_resets_to_idle(self):
        b = self._make_bridge()
        b.state = bridge_mod.State.IN_CALL
        b.on_call_ended("normal clearing")
        self.assertEqual(b.state, bridge_mod.State.IDLE)

    def test_call_ended_failure_announces(self):
        b = self._make_bridge()
        b.state = bridge_mod.State.CALLING_OUT
        b.on_call_ended("486 Busy Here")
        bridge_mod.tone_player.announce_call_failed.assert_called_once()

    def test_user_hangup_does_not_announce_failure(self):
        b = self._make_bridge()
        b.state = bridge_mod.State.CALLING_OUT
        b.on_call_ended("Connection reset by user")
        bridge_mod.tone_player.announce_call_failed.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# DIGIT ACCUMULATION tests
# ══════════════════════════════════════════════════════════════════════════════

class TestDigitAccumulation(unittest.TestCase):

    def _make_bridge(self):
        b = bridge_mod.Bridge.__new__(bridge_mod.Bridge)
        b.state            = bridge_mod.State.COLLECTING
        b.bell             = MagicMock()
        b.sip              = MagicMock()
        b._digit_buf       = []
        b._pulse_count     = 0
        b._last_pulse_time = 0.0
        b._dialling        = False
        b._digit_timer     = None
        b._lock            = threading.Lock()
        bridge_mod.tone_player.stop_dial_tone  = MagicMock()
        bridge_mod.tone_player.announce_digit  = MagicMock()
        return b

    def _pulse_n(self, b, n, gap=0.05):
        for _ in range(n):
            b.on_dial_pulse()
            time.sleep(gap)

    def test_single_digit_committed(self):
        b = self._make_bridge()
        self._pulse_n(b, 3)
        time.sleep(bridge_mod.INTER_DIGIT_GAP + 0.3)
        self.assertEqual(b._digit_buf, ["3"])

    def test_ten_pulses_is_zero(self):
        b = self._make_bridge()
        self._pulse_n(b, 10)
        time.sleep(bridge_mod.INTER_DIGIT_GAP + 0.3)
        self.assertEqual(b._digit_buf, ["0"])

    def test_multi_digit_sequence(self):
        b = self._make_bridge()
        self._pulse_n(b, 4)
        time.sleep(bridge_mod.INTER_DIGIT_GAP + 0.2)
        self._pulse_n(b, 2)
        time.sleep(bridge_mod.INTER_DIGIT_GAP + 0.3)
        self.assertEqual(b._digit_buf, ["4", "2"])

    def test_number_to_uri_plain(self):
        uri = bridge_mod._number_to_uri("1234")
        self.assertIn("sip:1234@", uri)

    def test_number_to_uri_passthrough(self):
        full = "sip:99@pbx.local"
        self.assertEqual(bridge_mod._number_to_uri(full), full)


# ══════════════════════════════════════════════════════════════════════════════
# SIMULATION ENGINE smoke test
# ══════════════════════════════════════════════════════════════════════════════

class TestSimEngine(unittest.TestCase):

    def test_sim_dial_triggers_established(self):
        b = MagicMock()
        engine = bridge_mod.SimEngine(b)
        engine.dial("sip:100@test.local")
        time.sleep(2.5)
        b.on_call_established.assert_called_once()

    def test_sim_hangup_triggers_ended(self):
        b = MagicMock()
        engine = bridge_mod.SimEngine(b)
        engine.hangup()
        b.on_call_ended.assert_called_once()

    def test_sim_answer_triggers_established(self):
        b = MagicMock()
        engine = bridge_mod.SimEngine(b)
        engine.answer()
        b.on_call_established.assert_called_once()


# ══════════════════════════════════════════════════════════════════════════════
# TONE PLAYER smoke test  (no real audio device required)
# ══════════════════════════════════════════════════════════════════════════════

class TestTonePlayer(unittest.TestCase):

    def test_wav_cache_populated(self):
        """At least some keys should be present after prewarm."""
        tp = bridge_mod.TonePlayer()
        # May be 0 if neither custom audio nor espeak is available,
        # but the cache dict itself must exist.
        self.assertIsInstance(tp._wav_cache, dict)

    def test_stop_dial_tone_idempotent(self):
        """Stopping when nothing is playing must not raise."""
        tp = bridge_mod.TonePlayer()
        tp.stop_dial_tone()
        tp.stop_dial_tone()

    def test_announce_digit_unknown_key_no_crash(self):
        tp = bridge_mod.TonePlayer()
        tp.announce_digit("X")  # should silently do nothing


# ══════════════════════════════════════════════════════════════════════════════
# LIVE DIAL  — interactive dial simulator for tuning timing constants
# ══════════════════════════════════════════════════════════════════════════════

def live_dial(args: argparse.Namespace):
    """
    Interactive dial-pulse simulator.

    Type a pulse count (1-10) and press Enter to simulate one rotary digit.
    The digit is committed after INTER_DIGIT_GAP seconds, then a call is
    placed after DIGIT_TIMEOUT more seconds — just like the real hardware.

    Commands
    --------
      1-10       simulate N pulses (10 = digit 0)
      0          same as 10 pulses  -> digit 0
      r  reset   clear digit buffer without dialling
      t  timing  print current timing values
      q  quit    exit
      ?  help    show this list
    """
    # Apply timing overrides from args
    if args.inter_digit_gap is not None:
        bridge_mod.INTER_DIGIT_GAP = args.inter_digit_gap
    if args.digit_timeout is not None:
        bridge_mod.DIGIT_TIMEOUT = args.digit_timeout
    if args.pulse_debounce is not None:
        bridge_mod.PULSE_DEBOUNCE = args.pulse_debounce

    # Silence audio so the loop is fast and clean
    bridge_mod.tone_player.start_dial_tone    = lambda: None
    bridge_mod.tone_player.stop_dial_tone     = lambda: None
    bridge_mod.tone_player.announce_digit     = lambda d: None
    bridge_mod.tone_player.announce_call_failed = lambda r="": None

    # Build a minimal Bridge with a spy SIP engine
    b = bridge_mod.Bridge.__new__(bridge_mod.Bridge)
    b.state            = bridge_mod.State.COLLECTING
    b.bell             = MagicMock()
    b._digit_buf       = []
    b._pulse_count     = 0
    b._last_pulse_time = 0.0
    b._dialling        = False
    b._digit_timer     = None
    b._lock            = threading.Lock()

    dialled_numbers: list[str] = []

    class SpySIP:
        def dial(self, uri):
            dialled_numbers.append(uri)
            print(f"\n  >>> WOULD DIAL: {uri}")
            print(f"  >>> buffer after dial: {b._digit_buf}")
            # Reset so user can dial again immediately
            with b._lock:
                b.state      = bridge_mod.State.COLLECTING
                b._digit_buf = []
        def hangup(self): pass
        def answer(self): pass

    b.sip = SpySIP()

    def _print_timing():
        print(f"  INTER_DIGIT_GAP : {bridge_mod.INTER_DIGIT_GAP:.3f} s  "
              f"(silence after last pulse -> commit digit)")
        print(f"  DIGIT_TIMEOUT   : {bridge_mod.DIGIT_TIMEOUT:.3f} s  "
              f"(silence after last digit -> dial)")
        print(f"  PULSE_DEBOUNCE  : {bridge_mod.PULSE_DEBOUNCE:.3f} s  "
              f"(min pulse width)")

    def _print_help():
        print("  1-10 / 0  simulate N pulses   r  reset buffer")
        print("  t         show timing          q  quit")

    print()
    print("=" * 52)
    print("  LIVE DIAL SIMULATOR  (Ctrl+C or q to quit)")
    print("=" * 52)
    _print_timing()
    print()
    _print_help()
    print()

    try:
        while True:
            buf_display = "".join(b._digit_buf) or "(empty)"
            raw = input(f"  buffer={buf_display}  pulses> ").strip().lower()

            if not raw:
                continue

            if raw in ("q", "quit", "exit"):
                break

            if raw in ("?", "help"):
                _print_help()
                continue

            if raw in ("t", "timing"):
                _print_timing()
                continue

            if raw in ("r", "reset"):
                if b._digit_timer:
                    b._digit_timer.cancel()
                with b._lock:
                    b._digit_buf   = []
                    b._pulse_count = 0
                    b._dialling    = False
                    b.state        = bridge_mod.State.COLLECTING
                print("  buffer cleared.")
                continue

            # Parse pulse count
            try:
                n = int(raw)
            except ValueError:
                print(f"  Unknown command '{raw}'. Type ? for help.")
                continue

            if n == 0:
                n = 10  # 0 on rotary = 10 pulses
            if not (1 <= n <= 10):
                print("  Enter 1-10 (or 0 for digit 0).")
                continue

            # Simulate N pulses with realistic inter-pulse gap
            gap = max(bridge_mod.PULSE_DEBOUNCE + 0.01, 0.08)
            print(f"  Sending {n} pulse(s) ... ", end="", flush=True)
            for i in range(n):
                b.on_dial_pulse()
                if i < n - 1:
                    time.sleep(gap)
            digit = "0" if n == 10 else str(n)
            print(f"digit will be '{digit}' in {bridge_mod.INTER_DIGIT_GAP:.2f}s, "
                  f"dial in {bridge_mod.INTER_DIGIT_GAP + bridge_mod.DIGIT_TIMEOUT:.2f}s")

    except (KeyboardInterrupt, EOFError):
        pass

    if b._digit_timer:
        b._digit_timer.cancel()

    print()
    print(f"  Session summary: {len(dialled_numbers)} call(s) placed")
    for u in dialled_numbers:
        print(f"    {u}")
    print()


# ══════════════════════════════════════════════════════════════════════════════
# GPIO DIAL CALIBRATION  — runs on the real Pi with the physical rotary dial
# ══════════════════════════════════════════════════════════════════════════════

def gpio_dial(args: argparse.Namespace):
    """
    Real-hardware dial calibration.  Run this ON THE PI.

    Step 1 — run with no extra flags.  The script prints the current pin
    state (HIGH/LOW) and then shows EVERY edge (rising and falling) so
    you can confirm the wiring is alive before worrying about counts.

    Step 2 — once you see activity, use --edge falling or --edge rising
    to lock in the correct edge and start counting real digits.

    Tuning tips
    -----------
    Too many pulses  -> raise --inter-digit-gap  or  lower --pulse-debounce
    Too few pulses   -> lower  --inter-digit-gap  or  raise --pulse-debounce
    """
    try:
        import RPi.GPIO as GPIO
    except (ImportError, RuntimeError):
        print("ERROR: RPi.GPIO not available — run this on the Pi.")
        return 1

    # Apply timing overrides
    if args.inter_digit_gap is not None:
        bridge_mod.INTER_DIGIT_GAP = args.inter_digit_gap
    if args.digit_timeout is not None:
        bridge_mod.DIGIT_TIMEOUT = args.digit_timeout
    if args.pulse_debounce is not None:
        bridge_mod.PULSE_DEBOUNCE = args.pulse_debounce

    dial_pin = args.dial_pin
    edge_arg = args.edge.lower()      # "both", "rising", or "falling"

    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(dial_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    # Read pin state immediately — tells us if wiring is alive
    state_now = GPIO.input(dial_pin)
    state_str = "HIGH (3.3 V — idle/open)" if state_now else "LOW  (GND  — shorted/closed)"

    print()
    print("=" * 56)
    print("  GPIO DIAL CALIBRATION")
    print("=" * 56)
    print(f"  DIAL_PIN   : BCM {dial_pin}   current state: {state_str}")
    print(f"  edge mode  : {edge_arg}  (change with --edge falling|rising|both)")
    print(f"  INTER_DIGIT_GAP : {bridge_mod.INTER_DIGIT_GAP:.3f} s")
    print(f"  PULSE_DEBOUNCE  : {bridge_mod.PULSE_DEBOUNCE:.3f} s")
    print()

    if edge_arg == "both":
        print("  RAW MODE — showing every edge (rising + falling).")
        print("  Spin the dial and look for activity.")
        print("  When you see pulses, re-run with --edge falling or --edge rising.")
    else:
        print(f"  COUNTING MODE — counting {edge_arg} edges as pulses.")
        print("  Spin the dial.  Ctrl+C to quit and see summary.")
    print()

    lock         = threading.Lock()
    pulse_count  = [0]
    last_t       = [0.0]
    commit_timer: list[threading.Timer] = [None]
    session_log:  list[tuple[int, str]] = []
    digit_index   = [0]

    def _commit():
        with lock:
            n            = pulse_count[0]
            pulse_count[0] = 0
            last_t[0]    = 0.0
        digit = "0" if n == 10 else str(n)
        ok    = "OK" if 1 <= n <= 10 else "ERR"
        idx   = digit_index[0]
        digit_index[0] += 1
        session_log.append((n, digit))
        print(f"\n  [{ok}] digit #{idx+1} => '{digit}'  ({n} pulses)\n", flush=True)

    def _count_cb(channel):
        now = time.time()
        with lock:
            gap = now - last_t[0]
            # Skip if within debounce window (but always count the very first pulse)
            if last_t[0] and gap < bridge_mod.PULSE_DEBOUNCE:
                print(f"  [debounce skip] gap={gap*1000:.1f}ms", flush=True)
                return
            last_t[0]      = now
            pulse_count[0] += 1
            n               = pulse_count[0]
        print(f"  pulse #{n:2d}   gap={gap*1000:6.1f}ms", flush=True)

        if commit_timer[0]:
            commit_timer[0].cancel()
        t = threading.Timer(bridge_mod.INTER_DIGIT_GAP, _commit)
        t.daemon = True
        t.start()
        commit_timer[0] = t

    def _raw_cb(channel):
        now      = time.time()
        state    = GPIO.input(dial_pin)
        edge_str = "RISING ^" if state else "FALLING v"
        gap      = now - last_t[0]
        last_t[0] = now
        print(f"  {edge_str}   gap={gap*1000:7.1f}ms   pin={'HIGH' if state else 'LOW '}", flush=True)

    # Register the appropriate callback
    if edge_arg == "both":
        GPIO.add_event_detect(dial_pin, GPIO.BOTH, callback=_raw_cb, bouncetime=5)
    elif edge_arg == "rising":
        GPIO.add_event_detect(dial_pin, GPIO.RISING,  callback=_count_cb, bouncetime=5)
    else:
        GPIO.add_event_detect(dial_pin, GPIO.FALLING, callback=_count_cb, bouncetime=5)

    print(f"  Listening on BCM {dial_pin} ...  (Ctrl+C to stop)")
    print()

    try:
        while True:
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        if commit_timer[0]:
            commit_timer[0].cancel()
        GPIO.cleanup()

    print()
    print("=" * 52)
    print("  CALIBRATION SUMMARY")
    print("=" * 52)
    if not session_log:
        print("  No digits recorded.")
    else:
        errors = 0
        for i, (pulses, digit) in enumerate(session_log):
            expected_pulses = 10 if digit == "0" else int(digit)
            flag = ""
            if pulses != expected_pulses:
                flag = f"  <-- MISMATCH (expected {expected_pulses})"
                errors += 1
            print(f"  dial #{i+1:2d}: {pulses:2d} pulses -> '{digit}'{flag}")
        print()
        print(f"  {len(session_log)} digit(s) read, {errors} mismatch(es)")
        if errors:
            print()
            print("  To fix mismatches:")
            print("    Too many pulses -> --pulse-debounce 0.05  (raise debounce)")
            print("    Too few pulses  -> --pulse-debounce 0.02  (lower debounce)")
            print("    Commits early   -> --inter-digit-gap 1.5")
            print("    Commits late    -> --inter-digit-gap 0.7")
    print()
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# CLI / argument parsing
# ══════════════════════════════════════════════════════════════════════════════

SUITES = {
    "netstring": unittest.TestLoader().loadTestsFromTestCase(TestNetstring),
    "state":     unittest.TestLoader().loadTestsFromTestCase(TestStateMachine),
    "dial":      unittest.TestLoader().loadTestsFromTestCase(TestDigitAccumulation),
    "sim":       unittest.TestLoader().loadTestsFromTestCase(TestSimEngine),
    "tone":      unittest.TestLoader().loadTestsFromTestCase(TestTonePlayer),
}

_ALL_TESTS = [*SUITES, "all", "live", "gpio"]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Rotary Phone SIP Bridge -- component test runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--test",
        choices=_ALL_TESTS,
        default="all",
        metavar="{" + "|".join(_ALL_TESTS) + "}",
        help="'gpio' = real dial calibration on Pi; 'live' = keyboard simulator; 'all' = unit tests (default: all)",
    )
    p.add_argument(
        "--dial-pin",
        type=int,
        default=bridge_mod.DIAL_PIN,
        metavar="BCM",
        help=f"GPIO BCM pin for the dial pulse wire (default: {bridge_mod.DIAL_PIN})",
    )
    p.add_argument(
        "--edge",
        choices=["both", "falling", "rising"],
        default="both",
        help="Edge to count as a pulse. Start with 'both' to see raw activity, "
             "then switch to 'falling' or 'rising' once confirmed (default: both)",
    )
    p.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show individual test names and pass/fail",
    )
    p.add_argument(
        "--server",
        default=None,
        metavar="HOST",
        help="Override SIP_SERVER for this run",
    )
    p.add_argument(
        "--user",
        default=None,
        metavar="EXT",
        help="Override SIP_USER for this run",
    )
    p.add_argument(
        "--password",
        default=None,
        metavar="PASS",
        help="Override SIP_PASSWORD for this run",
    )
    p.add_argument(
        "--sim-duration",
        type=float,
        default=None,
        metavar="SECS",
        help="Max seconds to let the SimEngine scenario run (default: let it finish)",
    )
    # Timing knobs (live mode + unit tests)
    p.add_argument(
        "--inter-digit-gap",
        type=float,
        default=None,
        metavar="SECS",
        help=f"Override INTER_DIGIT_GAP (default {bridge_mod.INTER_DIGIT_GAP}s): "
             "silence after last pulse before committing a digit",
    )
    p.add_argument(
        "--digit-timeout",
        type=float,
        default=None,
        metavar="SECS",
        help=f"Override DIGIT_TIMEOUT (default {bridge_mod.DIGIT_TIMEOUT}s): "
             "silence after last digit before dialling",
    )
    p.add_argument(
        "--pulse-debounce",
        type=float,
        default=None,
        metavar="SECS",
        help=f"Override PULSE_DEBOUNCE (default {bridge_mod.PULSE_DEBOUNCE}s): "
             "minimum pulse width to count",
    )
    return p


def apply_overrides(args: argparse.Namespace):
    if args.server:
        bridge_mod.SIP_SERVER = args.server
        os.environ["SIP_SERVER"] = args.server
    if args.user:
        bridge_mod.SIP_USER = args.user
        os.environ["SIP_USER"] = args.user
    if args.password:
        bridge_mod.SIP_PASSWORD = args.password
        os.environ["SIP_PASSWORD"] = args.password
    if args.inter_digit_gap is not None:
        bridge_mod.INTER_DIGIT_GAP = args.inter_digit_gap
    if args.digit_timeout is not None:
        bridge_mod.DIGIT_TIMEOUT = args.digit_timeout
    if args.pulse_debounce is not None:
        bridge_mod.PULSE_DEBOUNCE = args.pulse_debounce


def run(args: argparse.Namespace) -> int:
    apply_overrides(args)

    if args.test == "live":
        live_dial(args)
        return 0

    if args.test == "gpio":
        return gpio_dial(args)

    verbosity = 2 if args.verbose else 1

    if args.test == "all":
        suite = unittest.TestSuite(SUITES.values())
    else:
        suite = SUITES[args.test]

    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


def main():
    parser = build_parser()
    args   = parser.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
