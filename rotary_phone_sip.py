#!/usr/bin/env python3
"""
Rotary Phone SIP Bridge  (baresip edition)
==========================================
Turns a classic rotary phone into a working SIP extension.

  GPIO (Raspberry Pi)  ←→  this script  ←→  baresip (SIP engine)

Install
-------
  sudo apt install -y baresip sox
  pip3 install python-dotenv --break-system-packages

Hardware pins (BCM numbering)
------------------------------
  HOOK_PIN   = 3   — Hook switch, pulled HIGH; LOW when handset is lifted
  DIAL_PIN   = 2   — Rotary pulse pin, FALLING edge = one pulse
  BELL_PIN_A = 15  — H-bridge IN1 (bell coil A)
  BELL_PIN_B = 14  — H-bridge IN2 (bell coil B)

SIP credentials
---------------
  Export these before running, or edit the constants below:
    export SIP_SERVER=pbx.example.com
    export SIP_USER=1001
    export SIP_PASSWORD=secret
    python3 rotary_phone_sip.py
"""

import json
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
import shutil

# ── .env file ─────────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    print("[WARN] python-dotenv not installed — .env file will not be loaded.")
    print("       Run: pip3 install python-dotenv --break-system-packages\n")

# ── GPIO ─────────────────────────────────────────────────────────────────────
try:
    import RPi.GPIO as GPIO
    SIMULATION = False
except (ImportError, RuntimeError):
    print("[WARN] RPi.GPIO not available — running in SIMULATION mode.\n")
    SIMULATION = True

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

SIP_SERVER   = os.getenv("SIP_SERVER",   "sip.example.com")
SIP_USER     = os.getenv("SIP_USER",     "1000")
SIP_PASSWORD = os.getenv("SIP_PASSWORD", "secret")
SIP_PORT     = int(os.getenv("SIP_PORT", "5060"))

AUDIO_DEVICE = os.getenv("AUDIO_DEVICE", "default")

# GPIO pin numbers (BCM)
HOOK_PIN   = 3
DIAL_PIN   = 18
BELL_PIN_A = 15  # H-bridge IN1
BELL_PIN_B = 14  # H-bridge IN2

# Dialling
DIGIT_TIMEOUT   = 3.0    # s of silence after last digit before dialling
PULSE_DEBOUNCE  = 0.030  # s — ignore pulses shorter than this
INTER_DIGIT_GAP = 1.0    # s of silence after last pulse → commit digit

# Bell
BELL_HALF_PERIOD  = 0.025  # s per H-bridge half-cycle
BELL_STROKES      = 20     # clapper strokes per ring burst
BELL_RING_TIMEOUT = 0.5    # s pause between ring bursts

# ── Audio clips ───────────────────────────────────────────────────────────────
# Set USE_CUSTOM_AUDIO = True to use your own recordings instead of
# espeak-generated speech.
#
# Drop wav files into CUSTOM_AUDIO_DIR named:
#   0.wav  1.wav  2.wav ... 9.wav
#   call_failed.wav  number_busy.wav  number_not_found.wav  not_allowed.wav
#
# Any missing files fall back to espeak automatically.
USE_CUSTOM_AUDIO = True
CUSTOM_AUDIO_DIR = Path(__file__).parent / "audio"

# baresip control socket
BARESIP_CTRL_HOST = "127.0.0.1"
BARESIP_CTRL_PORT = 4444
RESPONSE_TIMEOUT  = 5.0

# baresip config directory
BARESIP_CFG_DIR = Path.home() / ".baresip"


# ══════════════════════════════════════════════════════════════════════════════
# TONE PLAYER  +  SPEECH
# ══════════════════════════════════════════════════════════════════════════════

# Uses:
#   sox  (play)    — dial tone, error tone   sudo apt install sox
#   espeak-ng      — spoken digit / message  sudo apt install espeak-ng
#   espeak         — fallback if ng absent   sudo apt install espeak

_SOX_AVAILABLE    = shutil.which("play")       is not None
_ESPEAK_BIN       = shutil.which("espeak-ng") or shutil.which("espeak")
_ESPEAK_AVAILABLE = _ESPEAK_BIN is not None

# SIP reason codes that mean "something went wrong on the remote side"
_FAILURE_REASONS = {
    "404", "403", "486", "487", "488", "500", "503", "600", "603",
}


def _sox_play_async(synth_args: list[str]) -> subprocess.Popen | None:
    """Spawn `play -q <synth_args>` and return the Popen (caller may .wait() or .terminate())."""
    if not _SOX_AVAILABLE:
        return None
    try:
        return subprocess.Popen(
            ["play", "-q"] + synth_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        print(f"[TONE] play failed: {e}")
        return None


def _espeak(text: str, rate: int = 140, pitch: int = 40):
    """
    Speak `text` synchronously via espeak-ng / espeak.
    rate  — words per minute (default 140 — a bit slower than espeak's default)
    pitch — 0-99 (default 40 — lower = more telephony-like)
    """
    if not _ESPEAK_AVAILABLE:
        return
    try:
        subprocess.run(
            [_ESPEAK_BIN, "-s", str(rate), "-p", str(pitch), text],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        print(f"[SPEECH] espeak failed: {e}")


class TonePlayer:
    """
    Audio feedback for the rotary phone:

    1. Dial tone       — 400 Hz continuous (AU PSTN) while waiting for digits
    2. Digit announce  — espeak speaks the digit name ("one", "two" …)
    3. Error tone      — two descending 0.3 s tones + "call failed" if call
                         ends with a SIP error reason
    """

    # Words to pre-generate
    _DIGIT_WORDS = {
        "0": "zero", "1": "one",   "2": "two",   "3": "three",
        "4": "four",  "5": "five",  "6": "six",   "7": "seven",
        "8": "eight", "9": "nine",
    }
    _PHRASE_WORDS = {
        "call_failed":       "call failed",
        "number_busy":       "number busy",
        "number_not_found":  "number not found",
        "not_allowed":       "call not allowed",
    }
    _CACHE_DIR = Path("/tmp/rotary-pi-audio")

    def __init__(self):
        self._dial_proc:  subprocess.Popen | None = None
        self._play_lock   = threading.Lock()   # one aplay at a time
        self._wav_cache:  dict[str, Path] = {}
        self._prewarm()

    # ── Pre-generate wav files at startup ─────────────────────────────────────

    def _prewarm(self):
        """
        Build the wav cache at startup so all playback is instant.

        If USE_CUSTOM_AUDIO is True, looks for wav files in CUSTOM_AUDIO_DIR
        first.  Any missing custom files fall back to espeak generation.
        If USE_CUSTOM_AUDIO is False, all clips are espeak-generated and
        cached in /tmp.
        """
        self._CACHE_DIR.mkdir(parents=True, exist_ok=True)
        all_words = {**self._DIGIT_WORDS, **self._PHRASE_WORDS}
        custom_count  = 0
        espeak_count  = 0
        missing_count = 0

        for key, text in all_words.items():

            # 1. Check for a custom recording
            if USE_CUSTOM_AUDIO:
                custom_wav = CUSTOM_AUDIO_DIR / f"{key}.wav"
                if custom_wav.exists():
                    self._wav_cache[key] = custom_wav
                    custom_count += 1
                    continue
                else:
                    print(f"[TONE] Custom audio missing: {custom_wav.name} — falling back to espeak")

            # 2. Use espeak-generated clip (cached in /tmp)
            if not _ESPEAK_AVAILABLE:
                missing_count += 1
                continue

            cached_wav = self._CACHE_DIR / f"{key}.wav"
            if cached_wav.exists():
                self._wav_cache[key] = cached_wav
                espeak_count += 1
                continue

            try:
                raw = self._CACHE_DIR / f"{key}_raw.wav"
                subprocess.run(
                    [_ESPEAK_BIN, "-s", "130", "-p", "40", "-w", str(raw), text],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
                )
                if _SOX_AVAILABLE:
                    subprocess.run(
                        ["sox", str(raw), "-r", "16000", "-c", "1", str(cached_wav)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
                    )
                    raw.unlink(missing_ok=True)
                else:
                    raw.rename(cached_wav)
                self._wav_cache[key] = cached_wav
                espeak_count += 1
            except Exception as e:
                print(f"[TONE] Failed to generate '{key}': {e}")
                missing_count += 1

        mode = "custom" if USE_CUSTOM_AUDIO else "espeak"
        print(f"[TONE] Audio ready — {custom_count} custom, {espeak_count} espeak"
              + (f", {missing_count} missing" if missing_count else "") + f"  (mode={mode})")

    # ── Play a cached wav ──────────────────────────────────────────────────────

    def _play_wav(self, key: str):
        """Play a pre-cached wav file via aplay (non-blocking, runs in thread)."""
        wav = self._wav_cache.get(key)
        if not wav:
            return
        def _run():
            with self._play_lock:
                try:
                    subprocess.run(
                        ["aplay", "-q", str(wav)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                except Exception as e:
                    print(f"[TONE] aplay failed: {e}")
        threading.Thread(target=_run, daemon=True).start()

    # ── Dial tone ──────────────────────────────────────────────────────────────

    def start_dial_tone(self):
        self.stop_dial_tone()
        if not _SOX_AVAILABLE:
            print("[TONE] sox not found — no dial tone  (sudo apt install sox)")
            return
        self._dial_proc = _sox_play_async(
            ["-n", "synth", "1", "sine", "400", "repeat", "-"]
        )
        print("[TONE] Dial tone started.")

    def stop_dial_tone(self):
        if self._dial_proc and self._dial_proc.poll() is None:
            self._dial_proc.terminate()
            try:
                self._dial_proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._dial_proc.kill()
            self._dial_proc = None
            print("[TONE] Dial tone stopped.")

    # ── Digit announcement ─────────────────────────────────────────────────────

    def announce_digit(self, digit: str):
        """Play the pre-cached wav for this digit — near-instant, no chop."""
        if digit in self._wav_cache:
            self._play_wav(digit)
        elif _SOX_AVAILABLE:
            # Fallback beep if wav generation failed
            _DTMF = {
                "1": 697, "2": 770, "3": 852, "4": 941,
                "5": 1040,"6": 1209,"7": 1336,"8": 1477,
                "9": 1633,"0": 941,
            }
            freq = _DTMF.get(digit, 800)
            threading.Thread(target=self._beep_sync, args=(freq,), daemon=True).start()

    def _beep_sync(self, freq: int):
        with self._play_lock:
            proc = _sox_play_async(
                ["-n", "synth", "0.08", "sine", str(freq),
                 "fade", "t", "0", "0.08", "0.02"]
            )
            if proc:
                proc.wait()

    # ── Call-failed feedback ───────────────────────────────────────────────────

    def announce_call_failed(self, reason: str = ""):
        """Play error tones then the appropriate failure phrase."""
        threading.Thread(
            target=self._fail_sequence, args=(reason,), daemon=True
        ).start()

    def _fail_sequence(self, reason: str):
        with self._play_lock:
            if _SOX_AVAILABLE:
                for freq in (480, 350):
                    proc = _sox_play_async(
                        ["-n", "synth", "0.3", "sine", str(freq),
                         "fade", "t", "0.01", "0.3", "0.05"]
                    )
                    if proc:
                        proc.wait()
                    time.sleep(0.05)

            if "busy" in reason.lower() or "486" in reason:
                key = "number_busy"
            elif "404" in reason or "not found" in reason.lower():
                key = "number_not_found"
            elif "403" in reason:
                key = "not_allowed"
            else:
                key = "call_failed"

            wav = self._wav_cache.get(key)
            if wav:
                subprocess.run(
                    ["aplay", "-q", str(wav)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            elif _ESPEAK_AVAILABLE:
                _espeak(self._PHRASE_WORDS.get(key, "call failed"), rate=120)
            print(f"[TONE] Failure announced: '{key}'  (reason={reason or '–'})")


# Module-level singleton used by Bridge
tone_player = TonePlayer()


# ══════════════════════════════════════════════════════════════════════════════
# NETSTRING HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def encode_netstring(data: str) -> bytes:
    """Encode a string as a netstring: b'<len>:<data>,'"""
    enc = data.encode()
    return f"{len(enc)}:".encode() + enc + b","


def decode_netstrings(buf: bytes) -> tuple[list[bytes], bytes]:
    """
    Parse as many complete netstrings from buf as possible.
    Returns (list_of_payloads, remaining_buf).
    """
    results = []
    while buf:
        colon = buf.find(b":")
        if colon == -1:
            break
        try:
            length = int(buf[:colon])
        except ValueError:
            break
        end = colon + 1 + length
        if len(buf) < end + 1:
            break                       # incomplete, wait for more data
        if buf[end:end + 1] != b",":
            buf = buf[1:]               # framing error — resync
            continue
        results.append(buf[colon + 1:end])
        buf = buf[end + 1:]
    return results, buf


# ══════════════════════════════════════════════════════════════════════════════
# BARESIP CONFIG
# ══════════════════════════════════════════════════════════════════════════════

_PATCHED_MARKER = "# patched by rotary_phone_sip"


def _find_module_path() -> Path:
    import glob
    candidates = ["/usr/lib/baresip/modules", "/usr/local/lib/baresip/modules"]
    candidates += glob.glob("/usr/lib/*/baresip/modules")
    for path in candidates:
        p = Path(path)
        if p.is_dir() and any(p.iterdir()):
            return p
    raise RuntimeError(
        "Could not find baresip modules directory.\n"
        "  Run: find / -name '*.so' 2>/dev/null | grep baresip"
    )


def _needs_patch() -> bool:
    cfg = BARESIP_CFG_DIR / "config"
    return not cfg.exists() or _PATCHED_MARKER not in cfg.read_text()


def _run_pass1():
    print("[CFG] Pass 1 — generating baresip templates …")
    BARESIP_CFG_DIR.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        ["baresip", "-f", str(BARESIP_CFG_DIR)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(3.5)
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
    print("[CFG] Templates written.")


def _patch_config():
    module_path = _find_module_path()
    cfg_file    = BARESIP_CFG_DIR / "config"
    acc_file    = BARESIP_CFG_DIR / "accounts"

    patched = cfg_file.read_text() if cfg_file.exists() else ""

    patched = re.sub(
        r"^#?\s*module_path\s+.*$",
        f"module_path\t\t\t{module_path}",
        patched, flags=re.MULTILINE,
    )

    if re.search(r"^#?\s*module_app\s+ctrl_tcp\.so", patched, re.MULTILINE):
        patched = re.sub(
            r"^#?\s*module_app\s+ctrl_tcp\.so.*$",
            "module_app\t\t\tctrl_tcp.so",
            patched, flags=re.MULTILINE,
        )
    elif re.search(r"^#?\s*module\s+ctrl_tcp\.so", patched, re.MULTILINE):
        patched = re.sub(
            r"^#?\s*module\s+ctrl_tcp\.so.*$",
            "module\t\t\t\tctrl_tcp.so",
            patched, flags=re.MULTILINE,
        )
    else:
        patched += "\nmodule_app\t\t\tctrl_tcp.so\n"

    if re.search(r"^#?\s*ctrl_tcp_listen\s+", patched, re.MULTILINE):
        patched = re.sub(
            r"^#?\s*ctrl_tcp_listen\s+.*$",
            f"ctrl_tcp_listen\t\t\t0.0.0.0:{BARESIP_CTRL_PORT}",
            patched, flags=re.MULTILINE,
        )
    else:
        patched += f"\nctrl_tcp_listen\t\t\t0.0.0.0:{BARESIP_CTRL_PORT}\n"

    for key in ("audio_player", "audio_source", "audio_alert"):
        if re.search(rf"^#?\s*{key}\s+", patched, re.MULTILINE):
            patched = re.sub(
                rf"^#?\s*{key}\s+.*$",
                f"{key:<24}{AUDIO_DEVICE}",
                patched, flags=re.MULTILINE,
            )
        else:
            patched += f"\n{key:<24}{AUDIO_DEVICE}\n"

    if _PATCHED_MARKER not in patched:
        patched += f"\n{_PATCHED_MARKER}\n"

    cfg_file.write_text(patched)
    print(f"[CFG] Patched {cfg_file}")

    acc_file.write_text(
        f'<sip:{SIP_USER}@{SIP_SERVER}:{SIP_PORT}>;auth_pass={SIP_PASSWORD}\n'
    )
    print(f"[CFG] Wrote account: sip:{SIP_USER}@{SIP_SERVER}:{SIP_PORT}")


def ensure_baresip_config():
    if _needs_patch():
        _run_pass1()
        _patch_config()
    else:
        print("[CFG] Config already patched — skipping Pass 1.")


# ══════════════════════════════════════════════════════════════════════════════
# BARESIP ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class BaresipEngine:
    """
    Manages the baresip subprocess and communicates with it exclusively over
    the ctrl_tcp socket using netstring-framed JSON.

    Commands are sent as netstring JSON over TCP (NOT via stdin).
    Events arrive as netstring JSON over the same socket.
    """

    def __init__(self, bridge: "Bridge"):
        self.bridge = bridge
        self._proc:           subprocess.Popen | None = None
        self._sock:           socket.socket    | None = None
        self._reader:         threading.Thread | None = None
        self._send_lock       = threading.Lock()
        self._response_queue: queue.Queue = queue.Queue()
        self._running         = False
        self._buf             = b""

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        ensure_baresip_config()
        print("[SIP] Starting baresip …")
        self._proc = subprocess.Popen(
            ["baresip", "-f", str(BARESIP_CFG_DIR)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        def _watch_stderr():
            for raw in self._proc.stderr:
                line = raw.decode(errors="replace").strip()
                if line:
                    print(f"[baresip] {line}")

        threading.Thread(target=_watch_stderr, daemon=True).start()

        self._connect_socket()
        self._running = True
        self._reader  = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        print(f"[SIP] Connected to baresip on :{BARESIP_CTRL_PORT}")
        print("[SIP] Waiting for registration in the background …")

    def stop(self):
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        print("[SIP] baresip stopped.")

    # ── Socket connection ──────────────────────────────────────────────────────

    def _connect_socket(self, retries: int = 10):
        for attempt in range(retries):
            try:
                s = socket.create_connection(
                    (BARESIP_CTRL_HOST, BARESIP_CTRL_PORT), timeout=5
                )
                s.settimeout(None)
                self._sock = s
                return
            except ConnectionRefusedError:
                print(f"[SIP] Waiting for baresip socket … ({attempt + 1}/{retries})")
                time.sleep(1.5)
        raise RuntimeError(
            f"Could not connect to baresip ctrl_tcp on port {BARESIP_CTRL_PORT}.\n"
            "Check that ctrl_tcp.so is listed in ~/.baresip/config."
        )

    # ── Send a command (netstring JSON over TCP) ───────────────────────────────

    def _send(self, command: str, params: str | None = None) -> dict:
        payload: dict = {"command": command}
        if params is not None:
            payload["params"] = params
        raw = encode_netstring(json.dumps(payload))

        with self._send_lock:
            if not self._sock:
                raise RuntimeError("Not connected")
            self._sock.sendall(raw)

        try:
            return self._response_queue.get(timeout=RESPONSE_TIMEOUT)
        except queue.Empty:
            return {"error": f"timeout waiting for response to '{command}'"}

    # ── Read loop ─────────────────────────────────────────────────────────────

    def _read_loop(self):
        assert self._sock
        try:
            while self._running:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                self._buf += chunk
                messages, self._buf = decode_netstrings(self._buf)
                for payload in messages:
                    try:
                        msg = json.loads(payload)
                    except json.JSONDecodeError:
                        msg = {"raw": payload.decode(errors="replace")}

                    if msg.get("response"):
                        self._response_queue.put(msg)
                    else:
                        self._dispatch(msg)
        except OSError:
            pass

    # ── Event dispatch ────────────────────────────────────────────────────────

    def _dispatch(self, evt: dict):
        type_val = evt.get("type") or evt.get("class") or ""
        etype    = type_val.upper() if isinstance(type_val, str) else ""
        if not etype:
            return

        print(f"[SIP-EVT] {etype}")

        if etype in ("REGISTER_OK", "REGISTER_SUCCESS"):
            print("[SIP] Registered successfully — ready to make and receive calls.")

        elif etype == "REGISTER_FAIL":
            print("[SIP] WARN: Registration failed — check SIP credentials and network.")

        elif etype == "CALL_INCOMING":
            peer = evt.get("peeruri") or evt.get("peer") or "unknown"
            self.bridge.on_incoming_call(peer)

        elif etype == "CALL_ESTABLISHED":
            self.bridge.on_call_established()

        elif etype == "CALL_CLOSED":
            reason = evt.get("param") or evt.get("reason") or ""
            self.bridge.on_call_ended(reason)

    # ── High-level actions ────────────────────────────────────────────────────

    def dial(self, uri: str):
        if not uri.startswith("sip:"):
            uri = "sip:" + uri
        print(f"[SIP] Dialling {uri}")
        resp = self._send("dial", uri)
        print(f"[SIP] Dial response: {resp}")

    def answer(self):
        resp = self._send("accept")
        print(f"[SIP] Answer response: {resp}")

    def hangup(self):
        resp = self._send("hangup")
        print(f"[SIP] Hangup response: {resp}")


# ══════════════════════════════════════════════════════════════════════════════
# SIMULATION ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class SimEngine:
    """Fake SIP engine that mirrors BaresipEngine's interface for dev/testing."""

    def __init__(self, bridge: "Bridge"):
        self.bridge = bridge

    def start(self):
        print("[SIP-SIM] Simulated SIP engine started.")
        threading.Thread(target=self._scenario, daemon=True).start()

    def stop(self):
        print("[SIP-SIM] Stopped.")

    def dial(self, uri: str):
        print(f"[SIP-SIM] Dialling {uri} …")
        threading.Timer(2.0, self.bridge.on_call_established).start()

    def answer(self):
        print("[SIP-SIM] Answering …")
        self.bridge.on_call_established()

    def hangup(self):
        print("[SIP-SIM] Hanging up …")
        self.bridge.on_call_ended("local hangup")

    def _scenario(self):
        start = time.time()

        def wait(t):
            r = t - (time.time() - start)
            if r > 0:
                time.sleep(r)

        print("[SIM] Scripted demo starting …\n")

        wait(3);  print("\n[SIM] ── Incoming call ──")
        self.bridge.on_incoming_call("sip:alice@example.com")

        wait(5);  print("\n[SIM] ── Handset lifted (answer) ──")
        self.bridge.on_hook_change(lifted=True)

        wait(10); print("\n[SIM] ── Handset replaced (hang up) ──")
        self.bridge.on_hook_change(lifted=False)

        wait(14); print("\n[SIM] ── Handset lifted (dial out) ──")
        self.bridge.on_hook_change(lifted=True)

        wait(16); print("\n[SIM] ── Dialling 4 (4 pulses) ──")
        for _ in range(4):
            self.bridge.on_dial_pulse()
            time.sleep(0.15)

        wait(18.5); print("\n[SIM] ── Dialling 2 (2 pulses) ──")
        for _ in range(2):
            self.bridge.on_dial_pulse()
            time.sleep(0.15)

        wait(27); print("\n[SIM] ── Handset replaced (hang up) ──")
        self.bridge.on_hook_change(lifted=False)

        wait(30); print("\n[SIM] Demo complete. Press Ctrl+C to exit.\n")


# ══════════════════════════════════════════════════════════════════════════════
# BELL  — H-bridge driver (IN1 = BELL_PIN_A, IN2 = BELL_PIN_B)
# ══════════════════════════════════════════════════════════════════════════════

class Bell:
    """
    H-bridge bell driver: alternates BELL_PIN_A / BELL_PIN_B to swing the
    clapper back and forth.
    """

    def __init__(self):
        self._stop   = threading.Event()
        self._thread: threading.Thread | None = None

    def ring(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print("[BELL] Ringing …")

    def silence(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        if not SIMULATION:
            GPIO.output(BELL_PIN_A, GPIO.LOW)
            GPIO.output(BELL_PIN_B, GPIO.LOW)
        print("[BELL] Silenced.")

    def _loop(self):
        if SIMULATION:
            print("[BELL-SIM] ring … ring …")
            while not self._stop.is_set():
                self._stop.wait(BELL_RING_TIMEOUT)
            return

        while not self._stop.is_set():
            for _ in range(BELL_STROKES):
                if self._stop.is_set():
                    break
                GPIO.output(BELL_PIN_A, GPIO.HIGH)
                GPIO.output(BELL_PIN_B, GPIO.LOW)
                if self._stop.wait(BELL_HALF_PERIOD):
                    break
                GPIO.output(BELL_PIN_A, GPIO.LOW)
                GPIO.output(BELL_PIN_B, GPIO.HIGH)
                if self._stop.wait(BELL_HALF_PERIOD):
                    break
            if not self._stop.is_set():
                self._stop.wait(BELL_RING_TIMEOUT)

        GPIO.output(BELL_PIN_A, GPIO.LOW)
        GPIO.output(BELL_PIN_B, GPIO.LOW)


# ══════════════════════════════════════════════════════════════════════════════
# STATE MACHINE
# ══════════════════════════════════════════════════════════════════════════════

class State:
    IDLE        = "IDLE"
    RINGING_IN  = "RINGING_IN"
    COLLECTING  = "COLLECTING"
    CALLING_OUT = "CALLING_OUT"
    IN_CALL     = "IN_CALL"


class Bridge:
    """
    Ties GPIO events to SIP commands.
    """

    def __init__(self):
        self.state = State.IDLE
        self.bell  = Bell()
        self.sip   = SimEngine(self) if SIMULATION else BaresipEngine(self)

        self._digit_buf:       list[str] = []
        self._pulse_count:     int       = 0
        self._last_pulse_time: float     = 0.0
        self._dialling:        bool      = False
        self._digit_timer: threading.Timer | None = None
        self._lock = threading.Lock()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        _gpio_init()
        _gpio_setup_outputs()
        self.sip.start()
        _gpio_attach_inputs(self)
        print("\n[BRIDGE] Ready — waiting for a call or handset lift …\n")

    def stop(self):
        if self._digit_timer:
            self._digit_timer.cancel()
        tone_player.stop_dial_tone()
        self.bell.silence()
        self.sip.stop()
        _gpio_cleanup()
        print("[BRIDGE] Stopped.")

    # ── SIP callbacks ─────────────────────────────────────────────────────────

    def on_incoming_call(self, peer: str):
        with self._lock:
            if self.state != State.IDLE:
                print(f"[BRIDGE] Busy — rejecting call from {peer}")
                self.sip.hangup()
                return
            self.state = State.RINGING_IN
        print(f"[BRIDGE] Incoming call from {peer}")
        self.bell.ring()

    def on_call_established(self):
        with self._lock:
            self.state = State.IN_CALL
        tone_player.stop_dial_tone()
        self.bell.silence()
        print("[BRIDGE] Call established — audio up.")

    def on_call_ended(self, reason: str = ""):
        with self._lock:
            prev       = self.state
            self.state = State.IDLE
        tone_player.stop_dial_tone()
        self.bell.silence()
        if self._digit_timer:
            self._digit_timer.cancel()
        print(f"[BRIDGE] Call ended (was {prev})  reason={reason or '–'}")

        # ── Announce failure if the call never connected ──
        if prev in (State.CALLING_OUT, State.RINGING_IN) and reason:
            # "Connection reset by user" means WE hung up — not a failure
            if "user" not in reason.lower() and "cancel" not in reason.lower():
                tone_player.announce_call_failed(reason)

    # ── GPIO callbacks ────────────────────────────────────────────────────────

    def on_hook_change(self, lifted: bool):
        with self._lock:
            state = self.state
        if lifted:
            self._on_lifted(state)
        else:
            self._on_replaced(state)

    def _on_lifted(self, state: str):
        print("[HOOK] Handset lifted.")
        if state == State.RINGING_IN:
            with self._lock:
                self.state = State.IN_CALL
            self.bell.silence()
            self.bell.silence()
            # Wait for USB audio to recover after bell coil spike
            print("[HOOK] Waiting for USB audio to stabilise …")
            deadline = time.time() + 3.0
            import subprocess
            while time.time() < deadline:
                result = subprocess.run(
                    ["aplay", "-l"], capture_output=True, text=True
                )
                if "USB Audio" in result.stdout:
                    time.sleep(0.3)  # small extra buffer after detection
                    break
                time.sleep(0.1)
            self.sip.answer()
            self.sip.answer()
        elif state == State.IDLE:
            with self._lock:
                self.state        = State.COLLECTING
                self._digit_buf   = []
                self._pulse_count = 0
                self._dialling    = False
            print("[BRIDGE] Collecting digits … dial your number.")
            # ── Start dial tone so user knows we're ready ──
            tone_player.start_dial_tone()

    def _on_replaced(self, state: str):
        print("[HOOK] Handset replaced.")
        tone_player.stop_dial_tone()
        if state == State.COLLECTING:
            if self._digit_timer:
                self._digit_timer.cancel()
            with self._lock:
                self.state      = State.IDLE
                self._digit_buf = []
            print("[BRIDGE] Dialling aborted.")
        elif state in (State.IN_CALL, State.CALLING_OUT, State.RINGING_IN):
            with self._lock:
                self.state = State.IDLE
            self.bell.silence()
            self.sip.hangup()

    def on_dial_pulse(self):
        with self._lock:
            if self.state != State.COLLECTING:
                return
            now = time.time()
            if now - self._last_pulse_time < PULSE_DEBOUNCE:
                return
            self._last_pulse_time = now
            self._pulse_count    += 1
            self._dialling        = True

        # Stop dial tone as soon as user starts dialling
        tone_player.stop_dial_tone()

        if self._digit_timer:
            self._digit_timer.cancel()
        t = threading.Timer(INTER_DIGIT_GAP, self._commit_digit)
        t.daemon = True
        t.start()
        self._digit_timer = t

    def _commit_digit(self):
        with self._lock:
            if not self._dialling:
                return
            pulses            = self._pulse_count
            self._pulse_count = 0
            self._dialling    = False
            digit             = "0" if pulses == 10 else str(pulses)
            self._digit_buf.append(digit)
            buf_str           = "".join(self._digit_buf)

        print(f"[DIAL] Digit: {digit}  (buffer: {buf_str})")

        # ── Announce the digit ──
        tone_player.announce_digit(digit)

        if self._digit_timer:
            self._digit_timer.cancel()
        t = threading.Timer(DIGIT_TIMEOUT, self._initiate_call)
        t.daemon = True
        t.start()
        self._digit_timer = t

    def _initiate_call(self):
        with self._lock:
            if self.state != State.COLLECTING or not self._digit_buf:
                return
            number          = "".join(self._digit_buf)
            self._digit_buf = []
            self.state      = State.CALLING_OUT

        uri = _number_to_uri(number)
        print(f"[BRIDGE] Calling {number} → {uri}")
        self.sip.dial(uri)


# ══════════════════════════════════════════════════════════════════════════════
# GPIO helpers
# ══════════════════════════════════════════════════════════════════════════════

def _gpio_init():
    if not SIMULATION:
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)

def _gpio_setup_outputs():
    if not SIMULATION:
        GPIO.setup(BELL_PIN_A, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(BELL_PIN_B, GPIO.OUT, initial=GPIO.LOW)

def _gpio_attach_inputs(bridge: Bridge):
    if SIMULATION:
        return

    GPIO.setup(HOOK_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    time.sleep(0.1)

    def hook_cb(channel):
        time.sleep(0.05)
        bridge.on_hook_change(GPIO.input(HOOK_PIN) == GPIO.LOW)

    GPIO.add_event_detect(HOOK_PIN, GPIO.BOTH, callback=hook_cb, bouncetime=50)

    GPIO.setup(DIAL_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.add_event_detect(DIAL_PIN, GPIO.FALLING,
                          callback=lambda ch: bridge.on_dial_pulse(),
                          bouncetime=30)

def _gpio_cleanup():
    if not SIMULATION:
        GPIO.cleanup()


# ══════════════════════════════════════════════════════════════════════════════
# Utilities
# ══════════════════════════════════════════════════════════════════════════════

def _number_to_uri(number: str) -> str:
    if number.startswith("sip:"):
        return number
    return f"sip:{number}@{SIP_SERVER}"


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("╔" + "═" * 55 + "╗")
    print("║       ROTARY PHONE SIP BRIDGE  (baresip)            ║")
    print("╚" + "═" * 55 + "╝")
    mode = "SIMULATION" if SIMULATION else "LIVE GPIO + baresip"
    print(f"  Mode   : {mode}")
    print(f"  Account: sip:{SIP_USER}@{SIP_SERVER}:{SIP_PORT}")
    print(f"  Audio  : {AUDIO_DEVICE}")
    print(f"  Pins   : HOOK={HOOK_PIN}  DIAL={DIAL_PIN}  "
          f"BELL_A={BELL_PIN_A}  BELL_B={BELL_PIN_B}")
    sox_status    = "available" if _SOX_AVAILABLE    else "NOT FOUND — sudo apt install sox"
    espeak_status = _ESPEAK_BIN  if _ESPEAK_AVAILABLE else "NOT FOUND — sudo apt install espeak-ng"
    print(f"  sox    : {sox_status}")
    print(f"  espeak : {espeak_status}\n")

    bridge     = Bridge()
    stop_event = threading.Event()

    def _sig(sig, frame):
        print("\n[!] Shutting down …")
        stop_event.set()

    signal.signal(signal.SIGINT,  _sig)
    signal.signal(signal.SIGTERM, _sig)

    bridge.start()
    stop_event.wait()
    bridge.stop()
    print("Goodbye.")


if __name__ == "__main__":
    main()