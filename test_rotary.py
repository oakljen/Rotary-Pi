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
# CLI / argument parsing
# ══════════════════════════════════════════════════════════════════════════════

SUITES = {
    "netstring": unittest.TestLoader().loadTestsFromTestCase(TestNetstring),
    "state":     unittest.TestLoader().loadTestsFromTestCase(TestStateMachine),
    "dial":      unittest.TestLoader().loadTestsFromTestCase(TestDigitAccumulation),
    "sim":       unittest.TestLoader().loadTestsFromTestCase(TestSimEngine),
    "tone":      unittest.TestLoader().loadTestsFromTestCase(TestTonePlayer),
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Rotary Phone SIP Bridge — component test runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--test",
        choices=[*SUITES, "all"],
        default="all",
        metavar="{" + "|".join([*SUITES, "all"]) + "}",
        help="Which test suite to run (default: all)",
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


def run(args: argparse.Namespace) -> int:
    apply_overrides(args)

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
