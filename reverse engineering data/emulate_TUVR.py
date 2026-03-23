import serial
import serial.tools.list_ports
import threading
import time
import msvcrt
import logging
from typing import Optional, Tuple

SOH = 0x01
STX = 0x02
ETX = 0x03
EOT = 0x04

BAUD = 9600
BOOT_PERIOD_S = 1.0
RUN_PERIOD_S = 0.2
REQUEST_GAP_S = 0.05
SECTION_COUNT = 13
HC_TIMEOUT_S = 1

LOG_LEVEL = logging.INFO

logging.basicConfig(
    level=LOG_LEVEL,
    format="[%(asctime)s.%(msecs)03d] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("hc5500")


def xor_checksum_ascii(header: str, payload: str) -> str:
    x = 0
    for ch in (header + payload):
        x ^= ord(ch)
    return f"{x:02X}"


def build_packet(header: str, payload: str) -> bytes:
    checksum = xor_checksum_ascii(header, payload)
    return (
        bytes([SOH])
        + header.encode("ascii")
        + bytes([STX])
        + payload.encode("ascii")
        + bytes([ETX])
        + checksum.encode("ascii")
        + bytes([EOT])
    )


def parse_packet(data: bytes) -> Optional[Tuple[str, str, str, str, bool]]:
    try:
        if len(data) < 8:
            return None
        if data[0] != SOH or data[-1] != EOT:
            return None

        stx_i = data.index(bytes([STX]))
        etx_i = data.index(bytes([ETX]))

        header = data[1:stx_i].decode("ascii", errors="replace")
        payload = data[stx_i + 1:etx_i].decode("ascii", errors="replace")
        checksum = data[etx_i + 1:etx_i + 3].decode("ascii", errors="replace")
        calc = xor_checksum_ascii(header, payload)
        valid = checksum.upper() == calc.upper()

        return header, payload, checksum, calc, valid
    except Exception:
        return None


class PacketStreamParser:
    def __init__(self):
        self.buf = bytearray()

    def feed(self, chunk: bytes):
        self.buf.extend(chunk)
        items = []

        while True:
            try:
                start = self.buf.index(SOH)
            except ValueError:
                if len(self.buf) > 4096:
                    self.buf.clear()
                break

            if start > 0:
                garbage = bytes(self.buf[:start])
                items.append(("garbage", garbage))
                del self.buf[:start]

            try:
                end = self.buf.index(EOT, 1)
            except ValueError:
                break

            pkt = bytes(self.buf[:end + 1])
            del self.buf[:end + 1]
            items.append(("packet", pkt))

        return items


def hex_dump(data: bytes) -> str:
    return data.hex(" ").upper()


def ascii_dump(data: bytes) -> str:
    return "".join(chr(b) if 32 <= b <= 126 else "." for b in data)


def list_ports():
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("Nincs elérhető COM port.")
        return []

    print("Elérhető COM portok:")
    for i, p in enumerate(ports):
        print(f"  [{i}] {p.device}  ({p.description})")
    return ports


def select_port() -> Optional[str]:
    ports = list_ports()
    if not ports:
        return None

    while True:
        choice = input("Port index vagy COM név: ").strip()

        if choice.isdigit():
            idx = int(choice)
            if 0 <= idx < len(ports):
                return ports[idx].device

        if choice.upper().startswith("COM"):
            return choice.upper()

        print("Érvénytelen választás.")


class HCRequester:
    def __init__(self, ser: serial.Serial):
        self.ser = ser
        self.lock = threading.Lock()
        self.running = True

        self.boot_mode = True
        self.last_valid_hc_time = 0.0

        self.dose_sequence = [200.0, 250.0, 150.0]
        self.dose_index = 0
        self.rate_lha = self.dose_sequence[self.dose_index]
        self.requested_rate_lha = self.rate_lha
        self.last_hc_rate_lha = None

        self.real_sections = 8
        self.target_sections = [0] * SECTION_COUNT
        self.last_hc_s6c = None  # 13 elemű aktuális szakaszállapot
        self.last_hc_a6b = None  # ha mégis jönne A0D 6B

    def scaled_rate(self) -> str:
        return f"{self.rate_lha / 10000.0:.5f}"

    def send_packet(self, header: str, payload: str):
        pkt = build_packet(header, payload)
        with self.lock:
            self.ser.write(pkt)
            self.ser.flush()
        logger.debug(f"TX {header} | {payload} | HEX={hex_dump(pkt)}")

    def enter_boot_mode(self, reason: str):
        if not self.boot_mode:
            logger.info(f"STATE -> BOOT ({reason})")
        self.boot_mode = True

    def enter_run_mode(self, reason: str):
        if self.boot_mode:
            logger.info(f"STATE -> RUN ({reason})")
        self.boot_mode = False

    def send_boot_request(self):
        self.send_packet("R0D", "6A")

    def send_run_cycle(self):
        sec = ",".join(str(x) for x in self.target_sections)
    

        #self.inject_rate()
        #time.sleep(REQUEST_GAP_S)
    
        # előbb a kívánt szakaszállapot
        self.send_packet("S0C", f"6C,{sec}")
        time.sleep(REQUEST_GAP_S)
    
        #self.send_packet("R0D", "6B")
        #time.sleep(REQUEST_GAP_S)
    
        #self.send_packet("R0D", "6D")

    def periodic_loop(self):
        while self.running:
            now = time.time()

            if self.last_valid_hc_time > 0 and (now - self.last_valid_hc_time) > HC_TIMEOUT_S:
                self.enter_boot_mode(f"HC timeout {now - self.last_valid_hc_time:.2f}s")

            start = time.time()

            if self.boot_mode:
                self.send_boot_request()
                period = BOOT_PERIOD_S
            else:
                self.send_run_cycle()
                period = RUN_PERIOD_S

            elapsed = time.time() - start
            sleep_left = period - elapsed
            if sleep_left > 0:
                time.sleep(sleep_left)

    def inject_rate(self):
        scaled = self.scaled_rate()
        self.send_packet("S0C", f"68,{scaled}")
        time.sleep(0.05)
        self.send_packet("R0D", "69")

    def cycle_dose_q(self):
        self.dose_index = (self.dose_index + 1) % len(self.dose_sequence)
        self.rate_lha = self.dose_sequence[self.dose_index]
        self.requested_rate_lha = self.rate_lha
        logger.info(f"Requested dose -> {self.rate_lha:.1f} l/ha (scaled {self.scaled_rate()})")
        self.inject_rate()

    def open_from_left(self):
        if not hasattr(self, "target_sections"):
            self.target_sections = [0] * SECTION_COUNT

        for i in range(self.real_sections):
            if self.target_sections[i] == 0:
                self.target_sections[i] = 1
                logger.info(f"Section trigger LEFT -> section {i+1} | target={self.target_sections}")
                self.try_inject_sections()
                return
        logger.info("Section trigger LEFT ignored, all real sections already open")


    def open_from_right(self):
        if not hasattr(self, "target_sections"):
            self.target_sections = [0] * SECTION_COUNT
    
        for i in range(self.real_sections - 1, -1, -1):
            if self.target_sections[i] == 0:
                self.target_sections[i] = 1
                logger.info(f"Section trigger RIGHT -> section {i+1} | target={self.target_sections}")
                self.try_inject_sections()
                return
        logger.info("Section trigger RIGHT ignored, all real sections already open")
    
    
    def close_all(self):
        self.target_sections = [0] * SECTION_COUNT
        logger.info(f"Section trigger CLOSE ALL | target={self.target_sections}")
        self.try_inject_sections()
    
    
    def try_inject_sections_old(self):
        sec = ",".join(str(x) for x in self.target_sections)
    
        # EZ MÉG KÍSÉRLETI
        self.send_packet("A0D", f"6B,{sec},A")
        time.sleep(0.05)
        self.send_packet("R0D", "6B")


    def try_inject_sections(self):
        time.sleep(0.01)


    def try_inject_sections_old(self):
        sec = ",".join(str(x) for x in self.target_sections)
    
        # 1) szakaszmaszk
        #self.send_packet("A0D", f"6B,{sec},A")
        self.send_packet("S0C", f"6C,{sec}")
        time.sleep(0.03)
    
        # 2) kísérleti section-control / mode flag
        self.send_packet("A0D", "6D,L,01,A")
        time.sleep(0.03)
    
        # 3) visszaolvasás
        self.send_packet("R0D", "6B")
        time.sleep(0.03)
        self.send_packet("R0D", "6D")

    def parse_section_list(self, payload: str, record_id: str):
        parts = payload.split(",")
        if not parts or parts[0] != record_id:
            return None

        values = []
        for p in parts[1:1 + SECTION_COUNT]:
            try:
                values.append(int(p))
            except ValueError:
                return None

        if len(values) != SECTION_COUNT:
            return None

        return values

    def handle_valid_hc_packet(self, header: str, payload: str):
        self.last_valid_hc_time = time.time()
        self.enter_run_mode(f"valid HC packet {header}")

        first = payload.split(",", 1)[0] if payload else ""

        if header == "A0D" and first == "6A":
            logger.debug(f"HC 6A config: {payload}")

        elif header == "A0D" and first == "69":
            try:
                scaled = float(payload.split(",")[1])
                hc_rate = scaled * 10000.0
                self.last_hc_rate_lha = hc_rate
                logger.debug(f"HC 69 target rate = {hc_rate:.1f} l/ha")

                if abs(hc_rate - self.requested_rate_lha) < 0.1:
                    logger.debug(f"DOSE accepted -> {hc_rate:.1f} l/ha")
                else:
                    logger.info(f"DOSE not accepted, HC still at {hc_rate:.1f} l/ha")
            except Exception:
                logger.info(f"HC 69 unexpected payload: {payload}")

        elif header == "S0C" and first == "68":
            try:
                scaled = float(payload.split(",")[1])
                logger.debug(f"HC S68 set/report rate = {scaled * 10000:.1f} l/ha")
            except Exception:
                logger.info(f"HC S68 unexpected payload: {payload}")

        elif header == "S0C" and first == "6C":
            values = self.parse_section_list(payload, "6C")
            if values is None:
                logger.info(f"HC S6C unexpected payload: {payload}")
            else:
                if values != self.last_hc_s6c:
                    self.last_hc_s6c = values
                    logger.info(f"HC S6C section state = {values}")
                else:
                    logger.debug(f"HC S6C section state unchanged = {values}")

        elif header == "A0D" and first == "6B":
            values = self.parse_section_list(payload[:-2] if payload.endswith(",A") else payload, "6B")
            self.last_hc_a6b = payload
            logger.debug(f"HC A6B desired sections = {payload}")

        elif header == "A0D" and first == "6D":
            logger.debug(f"HC 6D mode = {payload}")

        elif header == "V0C" and first == "68":
            try:
                scaled = float(payload.split(",")[1])
                logger.debug(f"HC V68 rate value = {scaled * 10000:.1f} l/ha")
            except Exception:
                logger.info(f"HC V68 unexpected payload: {payload}")

        elif header == "N0C" and first == "6C":
            values = self.parse_section_list(payload, "6C")
            logger.debug(f"HC N6C actual sections = {values if values is not None else payload}")

        else:
            logger.info(f"HC OTHER {header} | {payload}")


def keyboard_loop(req: HCRequester):
    logger.info("Keyboard: Q=dózis 200/250/150, A=nyit balról, S=zár mindent, D=nyit jobbról, X=kilépés")
    while req.running:
        if msvcrt.kbhit():
            key = msvcrt.getch()

            if key in (b"q", b"Q"):
                req.cycle_dose_q()
            elif key in (b"a", b"A"):
                req.open_from_left()
            elif key in (b"s", b"S"):
                req.close_all()
            elif key in (b"d", b"D"):
                req.open_from_right()
            elif key in (b"x", b"X"):
                req.running = False
                logger.info("Exit requested")
                break

        time.sleep(0.02)


def receiver_loop(ser: serial.Serial, parser: PacketStreamParser, req: HCRequester):
    while req.running:
        try:
            data = ser.read(256)
            if not data:
                continue

            logger.debug(f"RAW HEX   {hex_dump(data)}")
            logger.debug(f"RAW ASCII {ascii_dump(data)}")

            for kind, blob in parser.feed(data):
                if kind == "garbage":
                    if blob:
                        logger.debug(f"GARBAGE HEX   {hex_dump(blob)}")
                        logger.debug(f"GARBAGE ASCII {ascii_dump(blob)}")
                    continue

                parsed = parse_packet(blob)
                if parsed is None:
                    logger.info(f"BADFRAME HEX   {hex_dump(blob)}")
                    logger.info(f"BADFRAME ASCII {ascii_dump(blob)}")
                    continue

                header, payload, checksum, calc, valid = parsed
                logger.debug(f"PKT valid={valid} cs={checksum} calc={calc} {header} | {payload}")

                if valid:
                    req.handle_valid_hc_packet(header, payload)

        except Exception as e:
            logger.info(f"RX error: {e}")
            time.sleep(0.2)


def main():
    port = select_port()
    if not port:
        return

    logger.info(f"Opening {port} @ {BAUD} baud")

    ser = serial.Serial(
        port=port,
        baudrate=BAUD,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=0.02,
    )

    parser = PacketStreamParser()
    requester = HCRequester(ser)

    rx_thread = threading.Thread(target=receiver_loop, args=(ser, parser, requester), daemon=True)
    tx_thread = threading.Thread(target=requester.periodic_loop, daemon=True)
    kb_thread = threading.Thread(target=keyboard_loop, args=(requester,), daemon=True)

    rx_thread.start()
    tx_thread.start()
    kb_thread.start()

    try:
        while requester.running:
            time.sleep(0.2)
    except KeyboardInterrupt:
        requester.running = False
        logger.info("KeyboardInterrupt")
    finally:
        requester.running = False
        time.sleep(0.2)
        ser.close()
        logger.info("Serial port closed")


if __name__ == "__main__":
    main()