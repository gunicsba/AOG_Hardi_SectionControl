import serial
import serial.tools.list_ports
import socket
import threading
import time
import msvcrt
import logging
import os
import sys
from configparser import ConfigParser
from enum import Enum, auto
from typing import Optional

# ---------------------------------------------------------------------------
#  PAVPAGT protocol constants
# ---------------------------------------------------------------------------
BAUD = 115200
DEFAULT_SECTION_COUNT = 7

# ---------------------------------------------------------------------------
#  Timing / protocol constants
# ---------------------------------------------------------------------------
WDT_PERIOD_S = 1.0          # WDT probe interval (DISCONNECTED/CONNECTED only)
DEFAULT_SCT_HZ = 1          # Default section command rate
DEFAULT_SPD_HZ = 2          # Default speed command rate
MACHINE_TIMEOUT_S = 3.0     # No valid response -> DISCONNECTED
VER_TIMEOUT_S = 3.0         # Time to wait for VER response
REQUEST_GAP_S = 0.05        # 50ms gap between consecutive serial commands

UDP_PORT = 8888
UDP_TIMEOUT_S = 3
AOG_PORT = 9999              # AgIO listens on this port for replies

# AgOpenGPS Machine Module identity
AOG_MACHINE_SRC = 0x7B       # 123 = machine module

TICK_S = 0.05                # Main periodic loop tick (50 ms)


# ---------------------------------------------------------------------------
#  State machine
# ---------------------------------------------------------------------------
class MachineState(Enum):
    DISCONNECTED = auto()
    CONNECTED = auto()
    READY = auto()
    RUNNING = auto()


# ---------------------------------------------------------------------------
#  Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = logging.INFO

logging.basicConfig(
    level=LOG_LEVEL,
    format="[%(asctime)s.%(msecs)03d] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("pavpagt")

# ---------------------------------------------------------------------------
#  Config
# ---------------------------------------------------------------------------
def get_app_directory() -> str:
    """Get the application directory (works for both script and frozen exe)."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    else:
        return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(get_app_directory(), "config_pavpagt.ini")


def load_config() -> ConfigParser:
    config = ConfigParser()
    if not os.path.exists(CONFIG_PATH):
        config["main"] = {
            "com": "0",
            "comms_lost_zero": "1",
            "sections": str(DEFAULT_SECTION_COUNT),
            "sct_hz": str(DEFAULT_SCT_HZ),
            "spd_hz": str(DEFAULT_SPD_HZ),
            "subnet": "255.255.255.255",
        }
        with open(CONFIG_PATH, "w") as f:
            config.write(f)
    else:
        config.read(CONFIG_PATH)
    return config


def save_config(config: ConfigParser):
    with open(CONFIG_PATH, "w") as f:
        config.write(f)


# ===================================================================
#  PAVPAGT checksum / message building / parsing
# ===================================================================

def pavpagt_checksum(body: str) -> str:
    """XOR all ASCII chars in *body*, return 2-char uppercase hex."""
    x = 0
    for ch in body:
        x ^= ord(ch)
    return f"{x:02X}"


def build_pavpagt(cmd: str, *args: str) -> bytes:
    """Build a complete PAVPAGT sentence as bytes.

    Examples:
        build_pavpagt("WDT")       -> b"$PAVPAGT,WDT*2E\\r\\n"
        build_pavpagt("SPD", "0")  -> b"$PAVPAGT,SPD,0*32\\r\\n"
    """
    if args:
        body = f"PAVPAGT,{cmd},{','.join(args)}"
    else:
        body = f"PAVPAGT,{cmd}"
    cs = pavpagt_checksum(body)
    return f"${body}*{cs}\r\n".encode("ascii")


def parse_pavpagt_line(line: str):
    """Parse a stripped PAVPAGT line (no leading $, no trailing \\r\\n).

    Returns (is_valid, body, fields) or (False, line, []) on failure.
    *body* is the part between $ and *.
    *fields* is body split by comma, e.g. ["PAVPAGT", "SWT", "A", "0", ...].
    """
    star = line.rfind("*")
    if star < 0:
        return False, line, []

    body = line[:star]
    received_cs = line[star + 1:]
    expected_cs = pavpagt_checksum(body)

    if received_cs.upper() != expected_cs.upper():
        return False, body, []

    fields = body.split(",")
    return True, body, fields


# ===================================================================
#  Line-based stream parser (replaces PacketStreamParser)
# ===================================================================

class LineStreamParser:
    """Buffers serial bytes and yields complete PAVPAGT sentences.

    Handles both \\r\\n-terminated lines and back-to-back sentences
    where the machine sends e.g. $PAVPAGT,SWT,...*CS$PAVPAGT,ACK,...*CS
    without line breaks in between.
    """

    def __init__(self):
        self.buf = bytearray()

    def _parse_sentence(self, sentence: str, items: list):
        """Parse a single $PAVPAGT sentence and append result to items."""
        if sentence.startswith("$PAVPAGT,"):
            stripped = sentence[1:]  # remove leading $
            valid, body, fields = parse_pavpagt_line(stripped)
            if valid:
                items.append(("valid", (body, fields)))
            else:
                items.append(("bad_checksum", sentence))
        elif sentence.startswith("$"):
            items.append(("unknown", sentence))
        elif sentence.strip():
            items.append(("unknown", sentence))

    def feed(self, chunk: bytes):
        """Feed raw bytes, return list of (kind, data) tuples.

        kind is one of:
          "valid"        -> data = (body, fields)   -- checksum OK
          "bad_checksum" -> data = raw_line_str
          "unknown"      -> data = raw_line_str     -- not a $PAVPAGT line
        """
        self.buf.extend(chunk)
        items = []

        # Process complete lines (terminated by \n)
        while b"\n" in self.buf:
            idx = self.buf.index(b"\n")
            raw = bytes(self.buf[:idx + 1])
            del self.buf[:idx + 1]

            line = raw.decode("ascii", errors="replace").strip()
            if not line:
                continue

            # Split on '$' to handle concatenated sentences
            # e.g. "$PAVPAGT,SWT,...*CS$PAVPAGT,ACK,SPD*4B"
            self._process_line(line, items)

        # Also check for complete sentences in buffer without \n
        # (machine may send $...*CS$...*CS without line endings)
        buf_str = self.buf.decode("ascii", errors="replace")
        while buf_str.count("$PAVPAGT,") >= 2:
            # There are at least 2 sentence starts -- the first is complete
            first = buf_str.index("$PAVPAGT,")
            second = buf_str.index("$PAVPAGT,", first + 1)
            sentence = buf_str[first:second].strip()
            buf_str = buf_str[second:]
            self.buf = bytearray(buf_str.encode("ascii", errors="replace"))
            if sentence:
                self._parse_sentence(sentence, items)

        # Safety valve: clear if buffer grows without usable data
        if len(self.buf) > 4096:
            logger.warning(f"Line buffer overflow ({len(self.buf)} bytes), clearing")
            self.buf.clear()

        return items

    def _process_line(self, line: str, items: list):
        """Split a line on '$' boundaries and parse each sentence."""
        # Find all '$PAVPAGT,' positions
        parts = []
        i = 0
        while i < len(line):
            next_dollar = line.find("$", i + 1) if i < len(line) - 1 else -1
            if next_dollar == -1:
                parts.append(line[i:])
                break
            else:
                parts.append(line[i:next_dollar])
                i = next_dollar

        for part in parts:
            part = part.strip()
            if not part:
                continue
            # Skip leading garbage before first '$'
            dollar = part.find("$")
            if dollar > 0:
                logger.debug(f"Stripped {dollar} garbage byte(s) before $")
                part = part[dollar:]
            elif dollar < 0:
                # No '$' at all -- garbage
                items.append(("unknown", part))
                continue

            self._parse_sentence(part, items)


# ===================================================================
#  AgOpenGPS message helpers  (verbatim from TUVR bridge)
# ===================================================================

def aog_checksum(msg: bytes) -> int:
    """Sum bytes 2..n-1 (everything between preamble and CRC slot)."""
    return sum(msg[2:]) & 0xFF


def build_hello_reply(relay_lo: int, relay_hi: int) -> bytes:
    """Build the Hello reply that makes the machine icon go green in AOG."""
    msg = bytearray([
        0x80, 0x81,
        AOG_MACHINE_SRC,   # src  = 123
        AOG_MACHINE_SRC,   # pgn  = 123
        5,                 # len  = 5 data bytes
        relay_lo & 0xFF,
        relay_hi & 0xFF,
        0, 0, 0,
    ])
    msg.append(aog_checksum(msg))
    return bytes(msg)


def build_from_machine(relay_lo: int, relay_hi: int) -> bytes:
    """Build the 'From Machine' PGN 0xED."""
    msg = bytearray([
        0x80, 0x81,
        AOG_MACHINE_SRC,   # src = 123
        0xED,              # pgn = 237 (From Machine)
        8,                 # len = 8 data bytes
        relay_lo & 0xFF,   # byte 5: relayLo
        relay_hi & 0xFF,   # byte 6: relayHi
        0, 0,              # bytes 7-8: reserved
        0, 0, 0, 0,        # bytes 9-12: reserved
    ])
    msg.append(aog_checksum(msg))
    return bytes(msg)


def build_section_data(main_sw_bits: int, relay_lo: int, relay_hi: int,
                       off_lo: int = 0, off_hi: int = 0) -> bytes:
    """Build PGN 0xEA (234) -- Section Control Data to AOG.

    This is the PGN that the ESP32 section control module sends
    and that Rate Controller / AgOpenGPS listens for.

    Byte 0:  0x80        Header Hi
    Byte 1:  0x81        Header Lo
    Byte 2:  0x7B        Source (machine module 123)
    Byte 3:  0xEA        PGN 234 (Section Control Data)
    Byte 4:  0x08        Length = 8
    Byte 5:  main_sw     bit0=MasterOn, bit1=MasterOff
    Byte 6-8: reserved
    Byte 9:  relay ON 1-8
    Byte 10: relay OFF 1-8
    Byte 11: relay ON 9-16
    Byte 12: relay OFF 9-16
    Byte 13: CRC
    """
    msg = bytearray([
        0x80, 0x81,
        AOG_MACHINE_SRC,       # src = 123
        0xEA,                  # pgn = 234 (Section Control Data)
        8,                     # len = 8 data bytes
        main_sw_bits & 0xFF,   # byte 5: main switch / rate bits
        0,                     # byte 6: reserved
        0,                     # byte 7: reserved
        0,                     # byte 8: reserved
        relay_lo & 0xFF,       # byte 9:  sections ON  1-8
        off_lo & 0xFF,         # byte 10: sections OFF 1-8
        relay_hi & 0xFF,       # byte 11: sections ON  9-16
        off_hi & 0xFF,         # byte 12: sections OFF 9-16
    ])
    msg.append(aog_checksum(msg))
    return bytes(msg)


def _crc8(data: bytes, length: int) -> int:
    """Simple sum-of-bytes CRC used by PGN32618."""
    return sum(data[:length]) & 0xFF


def build_switch_pgn(auto_on: bool, master_on: bool,
                     sw_lo: int, sw_hi: int) -> bytes:
    """Build PGN32618 -- switch box message to Rate Controller.

    Byte 0: 106  (HeaderLo)
    Byte 1: 127  (HeaderHi)
    Byte 2: flags  bit0=Auto, bit1=MasterOn, bit2=MasterOff
    Byte 3: section switches 0-7
    Byte 4: section switches 8-15
    Byte 5: CRC
    """
    flags = 0
    if auto_on:
        flags |= 0x01   # bit 0 = Auto
    if master_on:
        flags |= 0x02   # bit 1 = MasterOn
    else:
        flags |= 0x04   # bit 2 = MasterOff

    msg = bytearray([
        106,               # HeaderLo
        127,               # HeaderHi
        flags,
        sw_lo & 0xFF,
        sw_hi & 0xFF,
        0,                 # CRC placeholder
    ])
    msg[5] = _crc8(msg, 5)
    return bytes(msg)


# ===================================================================
#  COM port selection  (verbatim from TUVR bridge)
# ===================================================================

def list_ports():
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("No COM ports available.")
        return []

    print("Available COM ports:")
    for i, p in enumerate(ports):
        print(f"  [{i}] {p.device}  ({p.description})")
    return ports


def select_port() -> Optional[str]:
    ports = list_ports()
    if not ports:
        return None

    while True:
        choice = input("Select port index or COM name: ").strip()

        if choice.isdigit():
            idx = int(choice)
            if 0 <= idx < len(ports):
                return ports[idx].device

        if choice.upper().startswith("COM"):
            return choice.upper()

        print("Invalid choice.")


# ===================================================================
#  PAVPAGTRequester  -- manages AvMap AgTronic serial communication
# ===================================================================

class PAVPAGTRequester:
    def __init__(self, ser: serial.Serial, section_count: int,
                 sct_hz: int, spd_hz: int, config: ConfigParser):
        self.ser = ser
        self.config = config
        self.lock = threading.Lock()            # serial write lock
        self.sections_lock = threading.Lock()    # protects target_sections & speed
        self.running = True

        # Machine connection state
        self.state = MachineState.DISCONNECTED
        self.last_valid_machine_time = 0.0
        self.ver_sent_time = 0.0
        self.firmware_version: Optional[str] = None

        # Section configuration
        self.section_count = section_count
        self.section_widths_cm: Optional[list] = None

        # Section state (written by UDP thread, read by TX thread)
        self.target_sections = [0] * self.section_count
        self.machine_sections: Optional[list] = None
        self.machine_mode: Optional[str] = None

        # Speed (written by UDP thread, read by TX thread)
        self.current_speed_kmh = 0.0

        # AgIO connection flag
        self.agio_connected = False

        # Current relay bytes (for AOG Hello reply / From Machine PGN)
        self.relay_lo = 0
        self.relay_hi = 0
        self.off_lo = 0          # sections forced OFF 1-8 (PGN 0xEA byte 10)
        self.off_hi = 0          # sections forced OFF 9-16 (PGN 0xEA byte 12)
        self.main_sw_bits = 0    # byte 5 of PGN 0xEA: bit0=MasterOn, bit1=MasterOff
        self.is_auto_mode = True # tracks mode for PGN 0xEA relay byte behavior

        # Switch PGN to send to AgIO (set by _handle_swt, sent by UDP thread)
        self.switch_pgn_pending: Optional[bytes] = None

        # Configurable rates
        self.sct_hz = max(1, sct_hz)
        self.spd_hz = max(1, spd_hz)

        # Timer tracking
        self.last_wdt_time = 0.0
        self.last_sct_time = 0.0
        self.last_spd_time = 0.0

    # ---- serial helpers ----

    def send_line(self, cmd: str, *args: str):
        pkt = build_pavpagt(cmd, *args)
        with self.lock:
            self.ser.write(pkt)
            self.ser.flush()
        logger.info(f"TX >> {pkt.strip().decode()}")

    # ---- state transitions ----

    def enter_disconnected(self, reason: str):
        if self.state != MachineState.DISCONNECTED:
            logger.info(f"STATE -> DISCONNECTED ({reason})")
        self.state = MachineState.DISCONNECTED
        self.firmware_version = None
        self.machine_sections = None
        self.machine_mode = None

    def enter_connected(self, reason: str):
        if self.state != MachineState.CONNECTED:
            logger.info(f"STATE -> CONNECTED ({reason})")
        self.state = MachineState.CONNECTED
        # Send VER query once
        self.send_line("VER")
        self.ver_sent_time = time.time()

    def enter_ready(self, reason: str):
        if self.state != MachineState.READY:
            logger.info(f"STATE -> READY ({reason})")
            if self.firmware_version:
                logger.info(f"Machine firmware: {self.firmware_version}")
        self.state = MachineState.READY

    def enter_running(self, reason: str):
        if self.state != MachineState.RUNNING:
            logger.info(f"STATE -> RUNNING ({reason})")
        self.state = MachineState.RUNNING
        # Reset SCT/SPD timers so they fire immediately
        self.last_sct_time = 0.0
        self.last_spd_time = 0.0

    # ---- periodic TX loop ----

    def periodic_loop(self):
        while self.running:
            now = time.time()

            # --- timeout checks ---

            # Machine timeout (any state except DISCONNECTED)
            if self.state != MachineState.DISCONNECTED:
                if self.last_valid_machine_time > 0 and \
                   (now - self.last_valid_machine_time) > MACHINE_TIMEOUT_S:
                    self.enter_disconnected(
                        f"machine timeout {now - self.last_valid_machine_time:.1f}s")

            # VER timeout (CONNECTED state only)
            if self.state == MachineState.CONNECTED:
                if (now - self.ver_sent_time) > VER_TIMEOUT_S:
                    self.enter_disconnected("VER timeout")

            # READY -> RUNNING when AgIO connects
            if self.state == MachineState.READY and self.agio_connected:
                self.enter_running("AgIO connected")

            # --- send messages ---

            # WDT: 1 Hz, only DISCONNECTED and CONNECTED (probe/handshake)
            if self.state in (MachineState.DISCONNECTED, MachineState.CONNECTED):
                if (now - self.last_wdt_time) >= WDT_PERIOD_S:
                    self.send_line("WDT")
                    self.last_wdt_time = now

            # SCT: configurable Hz, RUNNING only
            if self.state == MachineState.RUNNING:
                if (now - self.last_sct_time) >= (1.0 / self.sct_hz):
                    with self.sections_lock:
                        sec_args = [str(s) for s in self.target_sections]
                    self.send_line("SCT", *sec_args)
                    self.last_sct_time = now

            # SPD: configurable Hz, RUNNING only
            if self.state == MachineState.RUNNING:
                if (now - self.last_spd_time) >= (1.0 / self.spd_hz):
                    with self.sections_lock:
                        spd = self.current_speed_kmh
                    self.send_line("SPD", str(int(round(spd * 10))))
                    self.last_spd_time = now

            time.sleep(TICK_S)

    # ---- section / speed update from AgOpenGPS ----

    def update_sections_from_aog(self, section_bits: int):
        """Map AgOpenGPS 8-bit section mask to N-element array."""
        new_sections = [0] * self.section_count
        for i in range(min(8, self.section_count)):
            new_sections[i] = 1 if (section_bits >> i) & 1 else 0

        changed = False
        with self.sections_lock:
            if new_sections != self.target_sections:
                changed = True
            self.target_sections = new_sections
            # Note: relay_lo/hi are set from machine's SWT response only,
            # so AgOpenGPS always sees the actual machine state.

        # Send immediately on change so the machine reacts without waiting
        if changed and self.state == MachineState.RUNNING:
            self.send_line("SCT", *[str(s) for s in new_sections])

    def update_speed_from_aog(self, speed_kmh: float):
        with self.sections_lock:
            self.current_speed_kmh = speed_kmh

    # ---- machine response parsing ----

    def handle_machine_response(self, body: str, fields: list):
        """Process a validated PAVPAGT response from the machine."""
        self.last_valid_machine_time = time.time()

        # Transition from DISCONNECTED -> CONNECTED on first valid response
        if self.state == MachineState.DISCONNECTED:
            self.enter_connected("valid machine response")

        if len(fields) < 2:
            logger.info(f"SHORT response: {body}")
            return

        cmd = fields[1]

        if cmd == "SWT":
            self._handle_swt(fields)
        elif cmd == "VER":
            self._handle_ver(fields)
        elif cmd == "ACK":
            self._handle_ack(fields)
        elif cmd == "WDT":
            self._handle_wdt(fields)
        else:
            logger.info(f"UNKNOWN response: {body}")

    def _handle_swt(self, fields: list):
        """Handle SWT (switch status) response.

        Format: [PAVPAGT, SWT, mode, main_switch, s1, s2, ..., sN]
        - fields[2]   = mode letter (A=auto, M=manual)
        - fields[3]   = main switch (0=off, 1=on)
        - fields[4..] = individual section states
        """
        if len(fields) < 4:
            logger.debug(f"SWT keepalive (no data)")
            return

        mode = fields[2]

        # Main switch at index 3
        try:
            main_sw = int(fields[3])
        except ValueError:
            main_sw = 0

        # Extract section values (starting at index 4)
        section_values = []
        for i in range(4, 4 + self.section_count):
            if i < len(fields):
                try:
                    section_values.append(int(fields[i]))
                except ValueError:
                    section_values.append(0)
            else:
                section_values.append(0)

        # Log extra values beyond configured sections for protocol discovery
        extra_start = 4 + self.section_count
        if extra_start < len(fields):
            extras = fields[extra_start:]
            logger.debug(f"SWT extra values beyond {self.section_count} sections: {extras}")

        # Log mode / main switch changes
        if mode != self.machine_mode:
            logger.info(f"SWT mode: {mode} (was {self.machine_mode})")
            self.machine_mode = mode

        if main_sw != getattr(self, '_last_main_sw', None):
            logger.info(f"SWT main switch: {'ON' if main_sw else 'OFF'}")
            self._last_main_sw = main_sw

        # Log section state changes
        if section_values != self.machine_sections:
            self.machine_sections = section_values
            logger.info(f"SWT sections = {section_values}")
        else:
            logger.debug(f"SWT sections unchanged = {section_values}")

        # Update relay bitmask from machine-reported sections
        relay = 0
        off = 0
        for i, v in enumerate(section_values):
            if v:
                relay |= (1 << i)
            else:
                off |= (1 << i)
        self.relay_lo = relay & 0xFF
        self.relay_hi = (relay >> 8) & 0xFF
        self.off_lo = off & 0xFF
        self.off_hi = (off >> 8) & 0xFF

        # Track mode for PGN 0xEA relay byte behavior
        auto_on = (mode == "A")
        master_on = bool(main_sw)

        # Main switch bits for PGN 0xEA byte 5 -- MOMENTARY only.
        # ESP32 only sets these during switch transition (debounce window),
        # then clears to 0.  We pulse them once on change, then clear.
        prev_mode = getattr(self, '_ea_prev_mode', None)
        prev_master = getattr(self, '_ea_prev_master', None)
        msb = 0
        if (auto_on != prev_mode) or (master_on != prev_master):
            # State just changed -- set appropriate bits for this cycle
            if master_on and auto_on:
                msb |= 0x01   # bit 0 = MasterOn (when auto)
            if not master_on:
                msb |= 0x02   # bit 1 = MasterOff
            self._ea_prev_mode = auto_on
            self._ea_prev_master = master_on
        self.main_sw_bits = msb

        # Store current mode so UDP sender knows whether to include relay bytes
        self.is_auto_mode = auto_on

        # Build switch PGN (32618) only when something changed
        # (mode, main switch, or section states)
        new_sw_pgn = build_switch_pgn(
            auto_on, master_on, self.relay_lo, self.relay_hi)
        if new_sw_pgn != getattr(self, '_last_sw_pgn', None):
            self._last_sw_pgn = new_sw_pgn
            self.switch_pgn_pending = new_sw_pgn
            logger.info(f"Switch PGN queued: auto={auto_on} master={master_on} "
                        f"relay=0x{self.relay_lo:02X}")

    def _handle_ver(self, fields: list):
        """Handle VER (version) response."""
        if len(fields) >= 3:
            self.firmware_version = fields[2]
        else:
            self.firmware_version = "unknown"

        logger.info(f"Machine firmware: {self.firmware_version}")

        if self.state == MachineState.CONNECTED:
            self.enter_ready("VER received")

    def _handle_ack(self, fields: list):
        """Handle ACK response."""
        acked = fields[2] if len(fields) >= 3 else "?"
        logger.debug(f"ACK received for {acked}")

    def _handle_wdt(self, fields: list):
        """Handle WDT (watchdog) response.

        The machine echoes WDT with section widths in cm, e.g.:
        PAVPAGT,WDT,0250,0250,0250,0300,0250,0250,0250
        """
        raw_values = fields[2:]
        widths = []
        for v in raw_values:
            try:
                widths.append(int(v))
            except ValueError:
                widths.append(0)

        if widths != self.section_widths_cm:
            self.section_widths_cm = widths
            desc = ", ".join(f"{w}cm" for w in widths)
            logger.info(f"Section widths: [{desc}]")

            # Save to config
            self.config.set("main", "section_widths",
                            ",".join(str(w) for w in widths))
            save_config(self.config)
        else:
            logger.debug(f"WDT section widths unchanged")


# ===================================================================
#  Thread functions
# ===================================================================

def receiver_loop(ser: serial.Serial, parser: LineStreamParser,
                  req: PAVPAGTRequester):
    """Thread: reads serial data from AvMap AgTronic, parses lines."""
    while req.running:
        try:
            data = ser.read(256)
            if not data:
                continue

            logger.info(f"RX << {data!r}")

            for kind, payload in parser.feed(data):
                if kind == "valid":
                    body, fields = payload
                    logger.info(f"RX OK: {body}")
                    req.handle_machine_response(body, fields)
                elif kind == "bad_checksum":
                    logger.warning(f"RX bad checksum: {payload}")
                else:  # "unknown"
                    logger.info(f"RX unknown: {payload}")

        except Exception as e:
            logger.info(f"RX error: {e}")
            time.sleep(0.2)


def udp_listener_loop(req: PAVPAGTRequester, comms_lost_zero: bool,
                      subnet: str):
    """Thread: receives AgOpenGPS PGNs via UDP, updates shared state,
    and sends Hello reply + From Machine PGN back to AgIO."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("", UDP_PORT))
    sock.settimeout(UDP_TIMEOUT_S)
    logger.info(f"UDP listening on port {UDP_PORT}")

    broadcast = (subnet, AOG_PORT)
    logger.info(f"UDP broadcast -> {broadcast}")

    while req.running:
        try:
            data, addr = sock.recvfrom(1024)
        except socket.timeout:
            if req.agio_connected:
                logger.info("AgIO timeout -- connection lost")
                req.agio_connected = False
                if comms_lost_zero:
                    req.update_sections_from_aog(0x00)
                    req.update_speed_from_aog(0.0)
                # Drop from RUNNING to READY
                if req.state == MachineState.RUNNING:
                    req.enter_ready("AgIO timeout")
            continue
        except OSError:
            if not req.running:
                break
            raise

        if len(data) < 5:
            continue

        if data[0] != 0x80 or data[1] != 0x81:
            continue

        pgn = data[3]

        if pgn == 0xC8:  # AgIO Hello
            if not req.agio_connected:
                version = data[5] if len(data) > 5 else 0
                logger.info(f"AgIO connected (version {version / 10:.1f})")
            req.agio_connected = True

            # Reply when machine is at least READY
            if req.state in (MachineState.READY, MachineState.RUNNING):
                reply = build_hello_reply(req.relay_lo, req.relay_hi)
                sock.sendto(reply, broadcast)
                logger.info(f"TX Hello reply -> {broadcast} [{reply.hex()}]")
            else:
                logger.debug("AgIO Hello ignored -- machine not connected")

        elif pgn == 0xEF:  # Machine Data -- section bits
            if len(data) > 12:
                section_bits = data[11]
                req.update_sections_from_aog(section_bits)
                logger.debug(
                    f"AgIO sections byte=0x{section_bits:02X} "
                    f"-> {req.target_sections}")

                # Send section data when in RUNNING state
                if req.state == MachineState.RUNNING:
                    # PGN 0xEA (234): Section Control Data
                    # Auto mode:  relay ON = 0 (AGO controls), OFF = disabled sections
                    # Manual mode: relay ON = active sections, OFF = inactive sections
                    if req.is_auto_mode:
                        ea_relay_lo, ea_relay_hi = 0, 0
                    else:
                        ea_relay_lo = req.relay_lo
                        ea_relay_hi = req.relay_hi
                    # OFF bytes always sent -- tells AGO which sections are
                    # disabled/forced-off regardless of mode
                    ea_off_lo = req.off_lo
                    ea_off_hi = req.off_hi
                    sect_data = build_section_data(
                        req.main_sw_bits,
                        ea_relay_lo, ea_relay_hi,
                        ea_off_lo, ea_off_hi)
                    sock.sendto(sect_data, broadcast)
                    logger.info(
                        f"TX SectData 0xEA -> {broadcast} "
                        f"main=0x{req.main_sw_bits:02X} "
                        f"relay=0x{ea_relay_lo:02X} "
                        f"auto={req.is_auto_mode}")

                    # Clear momentary main_sw_bits after sending
                    req.main_sw_bits = 0

                    # PGN 0xED (237): From Machine -- kept for compatibility
                    from_machine = build_from_machine(req.relay_lo, req.relay_hi)
                    sock.sendto(from_machine, broadcast)

                    # Send switch PGN (32618) if pending (mode/section feedback)
                    sw_pgn = req.switch_pgn_pending
                    if sw_pgn is not None:
                        sock.sendto(sw_pgn, broadcast)
                        req.switch_pgn_pending = None
                        logger.info(f"TX SwitchPGN -> {broadcast} [{sw_pgn.hex()}]")

        elif pgn == 0xFE:  # Steer Data -- speed + section bits
            if len(data) > 6:
                spd = int.from_bytes(data[5:7], "little", signed=False) * 0.1
                req.update_speed_from_aog(spd)
                logger.debug(f"AgIO speed={spd:.1f} km/h")
            # ESP32 also reads section bits from 0xFE at bytes 11-12
            if len(data) > 12:
                section_bits = data[11]
                req.update_sections_from_aog(section_bits)


def keyboard_loop(req: PAVPAGTRequester):
    """Thread: keyboard input. X = exit."""
    logger.info("Keyboard: X = exit")
    while req.running:
        if msvcrt.kbhit():
            key = msvcrt.getch()
            if key in (b"x", b"X"):
                req.running = False
                logger.info("Exit requested")
                break
        time.sleep(0.05)


# ===================================================================
#  Main
# ===================================================================

def main():
    print("AOG-PAVPAGT Bridge  (AgOpenGPS -> AvMap AgTronic section control)")
    print()

    # --- config ---
    config = load_config()
    saved_com = config.get("main", "com", fallback="0")
    comms_lost_zero = config.getboolean("main", "comms_lost_zero", fallback=True)
    section_count = config.getint("main", "sections", fallback=DEFAULT_SECTION_COUNT)
    sct_hz = config.getint("main", "sct_hz", fallback=DEFAULT_SCT_HZ)
    spd_hz = config.getint("main", "spd_hz", fallback=DEFAULT_SPD_HZ)
    subnet = config.get("main", "subnet", fallback="192.168.1.255")

    print(f"Config: sections={section_count}  SCT={sct_hz}Hz  SPD={spd_hz}Hz  "
          f"comms_lost_zero={comms_lost_zero}  subnet={subnet}")
    print()

    # --- COM port selection ---
    available = {p.device for p in serial.tools.list_ports.comports()}
    if saved_com != "0" and saved_com in available:
        print(f"Using saved COM port: {saved_com}")
        port = saved_com
    else:
        if saved_com != "0":
            print(f"Saved port {saved_com} not found.")
        port = select_port()
        if not port:
            return
        config.set("main", "com", port)
        save_config(config)

    logger.info(f"Opening {port} @ {BAUD} baud")

    ser = serial.Serial(
        port=port,
        baudrate=BAUD,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=0.05,
    )

    parser = LineStreamParser()
    requester = PAVPAGTRequester(ser, section_count, sct_hz, spd_hz, config)

    # --- start threads ---
    threads = [
        threading.Thread(target=udp_listener_loop,
                         args=(requester, comms_lost_zero, subnet), daemon=True),
        threading.Thread(target=receiver_loop,
                         args=(ser, parser, requester), daemon=True),
        threading.Thread(target=requester.periodic_loop, daemon=True),
        threading.Thread(target=keyboard_loop,
                         args=(requester,), daemon=True),
    ]
    for t in threads:
        t.start()

    try:
        while requester.running:
            time.sleep(0.2)
    except KeyboardInterrupt:
        requester.running = False
        logger.info("KeyboardInterrupt")
    finally:
        requester.running = False
        time.sleep(0.3)
        ser.close()
        logger.info("Serial port closed")


if __name__ == "__main__":
    main()
