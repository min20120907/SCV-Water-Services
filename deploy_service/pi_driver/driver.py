#!/usr/bin/env python3
"""SCV Water real-hardware sensor driver for Raspberry Pi Zero 2 W (aarch64, Py 3.13).

Honest-by-design: a read that cannot produce a REAL value RAISES HWError. No fake
data, no zero-filling, no twin_engine import. Hardware absence is reported, never
papered over. The selftest always prints exactly ONE JSON line and exits 0.

Usage:
  python3 driver.py --config config.json              # normal loop -> Firestore
  python3 driver.py --config config.json --once       # one iteration, JSON to stdout
  python3 driver.py --config config.json --selftest   # one JSON line, always exit 0

Catalogue types: bh1750, bme280, hcsr04, yf_s201, dht22, custom
"""
import argparse
import json
import math
import os
import sys
import time
from collections import deque
from datetime import datetime, timezone

# ---------------------------------------------------------------- libraries
# GPIO backend preference: lgpio (function API) > lgpio (class API) > RPi.GPIO.
# All are optional at import time; absence becomes a runtime HWError(gpio_unavailable)
# so I2C-only selftests still work on a box without a GPIO lib installed.
lgpio = None
LGPIO_API = None
try:
    import lgpio as _lgpio
    if hasattr(_lgpio, "gpiochip_open"):
        lgpio, LGPIO_API = _lgpio, "func"
    elif hasattr(_lgpio, "gpio"):
        lgpio, LGPIO_API = _lgpio, "class"
except ImportError:
    lgpio, LGPIO_API = None, None

try:
    import RPi.GPIO as RPiGPIO
except ImportError:
    RPiGPIO = None

try:
    import smbus2
except ImportError:
    smbus2 = None

OK = "ok"
NO_DEVICE = "no_device"
BUS_ERROR = "bus_error"
TIMEOUT = "timeout"
OUT_OF_RANGE = "out_of_range"
GPIO_UNAVAILABLE = "gpio_unavailable"
LIB_MISSING = "lib_missing"
BAD_CODE = "bad_code"

_FS_PATH = os.path.expanduser("~/.config/scv_water/firebase_service_account.json")


class HWError(Exception):
    """Hardware failure with a stable machine-readable code.

    Signature: HWError(code, detail, evidence=None). The selftest harness and the
    custom-code harness MUST lift (code, detail, evidence) from this exception.
    """

    def __init__(self, code, detail, evidence=None):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail
        self.evidence = evidence if isinstance(evidence, dict) else (
            {"detail": str(evidence)} if evidence is not None else {})


# ---------------------------------------------------------------- I2C
def _bus_open(bus):
    if smbus2 is None:
        raise HWError(LIB_MISSING, "smbus2 is not installed")
    try:
        return smbus2.SMBus(int(bus))
    except Exception as e:  # noqa: BLE001 - any open failure is a bus error
        raise HWError(BUS_ERROR, "cannot open I2C bus %s: %s" % (bus, e))


def i2c_scan(buses=(0, 1)):
    """Real quick-ACK probe of the standard I2C address space per bus.

    A bus node that is missing (e.g. no /dev/i2c-0) is reported as an empty
    list instead of aborting the whole scan — the evidence must stay honest
    per bus, never crash.
    """
    out = {}
    for b in buses:
        addrs = []
        try:
            bus = _bus_open(b)
        except HWError:
            out[str(b)] = []  # bus node absent on this board
            continue
        for a in range(0x03, 0x78):
            try:
                bus.read_byte_data(a, 0)
                addrs.append(a)
            except Exception:  # noqa: BLE001 - NAK means "not there"
                pass
        try:
            bus.close()
        except Exception:
            pass
        out[str(b)] = sorted(addrs)
    return out


# ---------------------------------------------------------------- BH1750
def _bh1750(cfg):
    bus_no = cfg.get("bus", 1)
    addr = int(cfg.get("address", 0x23))
    bus = _bus_open(bus_no)
    try:
        bus.write_byte_data(addr, 0x00, 0x01)   # power on
        time.sleep(0.05)
        bus.write_byte_data(addr, 0x00, 0x10)   # continuous-H, low range (1 lux)
        time.sleep(0.25)                        # wait for first measurement
        hi = bus.read_byte_data(addr, 0x00)
        lo = bus.read_byte_data(addr, 0x01)
    except Exception as e:  # noqa: BLE001
        raise HWError(NO_DEVICE,
                      "no I2C response from BH1750 at 0x%02x on bus %s: %s" % (addr, bus_no, e),
                      {"i2c_scan": i2c_scan()})
    finally:
        try:
            bus.close()
        except Exception:  # noqa: BLE001
            pass
    raw = (hi << 8) | lo
    return {"illuminance": max(0.0, (raw - 1) / 1.2)}


def _bh1750_verify(cfg):
    bus_no = cfg.get("bus", 1)
    addr = int(cfg.get("address", 0x23))
    ev = {"i2c_scan": i2c_scan()}
    if addr not in ev["i2c_scan"].get(str(bus_no), []):
        raise HWError(NO_DEVICE,
                      "BH1750 address 0x%02x not ACKed on I2C bus %s" % (addr, bus_no), ev)
    return ev


# ---------------------------------------------------------------- BME280
def _s16(v):
    return v - 65536 if v >= 0x8000 else v


def _bme280_compensate(d, adc_t, adc_p, adc_h):
    """Bosch BME280 double-precision compensation (datasheet BST-BME280-DS002 /
    BoschSensortec BME280_driver), transcribed exactly."""
    # temperature
    v1 = (adc_t / 16384.0 - d["T1"] / 1024.0) * d["T2"]
    v2 = ((adc_t / 131072.0 - d["T1"] / 8192.0) ** 2) * d["T3"]
    tfine = int(v1 + v2)          # C: (int32_t)(var1 + var2)
    t = (v1 + v2) / 5120.0
    t = max(-40.0, min(85.0, t))
    # pressure
    v1 = tfine / 2.0 - 64000.0
    v2 = v1 * v1 * d["P6"] / 32768.0
    v2 = v2 + v1 * d["P5"] * 2.0
    v2 = v2 / 4.0 + d["P4"] * 65536.0
    v3 = d["P3"] * v1 * v1 / 524288.0
    v1 = (v3 + d["P2"] * v1) / 524288.0
    v1 = (1.0 + v1 / 32768.0) * d["P1"]
    if v1 > 0.0:
        p = 1048576.0 - adc_p
        p = (p - v2 / 4096.0) * 6250.0 / v1
        v1 = d["P9"] * p * p / 2147483648.0
        v2 = p * d["P8"] / 32768.0
        p = p + (v1 + v2 + d["P7"]) / 16.0
        p = max(30000.0, min(110000.0, p))
    else:
        p = 30000.0
    # humidity
    v1 = tfine - 76800.0
    v2 = d["H4"] * 64.0 + (d["H5"] / 16384.0) * v1
    v3 = adc_h - v2
    v4 = d["H2"] / 65536.0
    v5 = 1.0 + (d["H3"] / 67108864.0) * v1
    v6 = 1.0 + (d["H6"] / 67108864.0) * v1 * v5
    v6 = v3 * v4 * (v5 * v6)
    h = v6 * (1.0 - d["H1"] * v6 / 524288.0)
    h = max(0.0, min(100.0, h))
    return {"temp": t, "pressure": p / 100.0, "humidity": h}


def _bme280_read_regs(bus, addr):
    """One block read 0x88..0x9F (26 bytes) carries T/P calib + humidity calib;
    0xF4..0xFF (12 bytes) carries the raw measurement. Parsing matches the Bosch
    driver's parse_temp_press_calib_data / parse_humidity_calib_data exactly."""
    cal = bytes(bus.read_i2c_block_data(addr, 0x88, 26))
    data = bytes(bus.read_i2c_block_data(addr, 0xF4, 12))
    d = {
        "T1": (cal[1] << 8) | cal[0],
        "T2": _s16((cal[3] << 8) | cal[2]),
        "T3": _s16((cal[5] << 8) | cal[4]),
        "P1": (cal[7] << 8) | cal[6],
        "P2": _s16((cal[9] << 8) | cal[8]),
        "P3": _s16((cal[11] << 8) | cal[10]),
        "P4": _s16((cal[13] << 8) | cal[12]),
        "P5": _s16((cal[15] << 8) | cal[14]),
        "P6": _s16((cal[17] << 8) | cal[16]),
        "P7": _s16((cal[19] << 8) | cal[18]),
        "P8": _s16((cal[21] << 8) | cal[20]),
        "P9": _s16((cal[23] << 8) | cal[22]),
        "H1": cal[25],
    }
    hum = bytes(bus.read_i2c_block_data(addr, 0xE1, 7))
    d["H2"] = _s16((hum[1] << 8) | hum[0])
    d["H3"] = hum[2]
    h4_msb = hum[3] - 256 if hum[3] >= 0x80 else hum[3]
    d["H4"] = h4_msb * 16 + (hum[4] & 0x0F)
    h5_msb = hum[5] - 256 if hum[5] >= 0x80 else hum[5]
    d["H5"] = h5_msb * 16 + (hum[4] >> 4)
    d["H6"] = _s16(hum[6])
    adc_t = (data[0] << 12) | (data[1] << 4) | (data[2] >> 4)
    adc_p = (data[3] << 12) | (data[4] << 4) | (data[5] >> 4)
    adc_h = (data[6] << 8) | data[7]
    return d, adc_t, adc_p, adc_h


def _bme280_read_cfg(cfg):
    """Shared read path; returns the measurement dict."""
    bus_no = cfg.get("bus", 1)
    addr = int(cfg.get("address", 0x76))
    bus = _bus_open(bus_no)
    try:
        chip = bus.read_byte_data(addr, 0xD0)
    except Exception as e:  # noqa: BLE001
        raise HWError(NO_DEVICE,
                      "no I2C response from BME280 at 0x%02x on bus %s: %s" % (addr, bus_no, e),
                      {"i2c_scan": i2c_scan(), "chip_id": None})
    if chip != 0x58:
        raise HWError(NO_DEVICE, "BME280 chip id 0x%02x != 0x58 at 0x%02x" % (chip, addr),
                      {"i2c_scan": i2c_scan(), "chip_id": chip})
    try:
        bus.write_byte_data(addr, 0xF4, 0x33)   # normal mode, T x2, P x16, H x1
        time.sleep(0.05)
        d, adc_t, adc_p, adc_h = _bme280_read_regs(bus, addr)
    except HWError:
        raise
    except Exception as e:  # noqa: BLE001
        raise HWError(BUS_ERROR, "BME280 measurement read failed: %s" % e)
    finally:
        try:
            bus.close()
        except Exception:  # noqa: BLE001
            pass
    out = _bme280_compensate(d, adc_t, adc_p, adc_h)
    return {"temp": out["temp"], "pressure": out["pressure"], "humidity": out["humidity"]}


def _bme280(cfg):
    return _bme280_read_cfg(cfg)


def _bme280_verify(cfg):
    bus_no = cfg.get("bus", 1)
    addr = int(cfg.get("address", 0x76))
    bus = _bus_open(bus_no)
    try:
        chip = bus.read_byte_data(addr, 0xD0)
    except Exception as e:  # noqa: BLE001
        raise HWError(NO_DEVICE, "no I2C response from BME280 at 0x%02x: %s" % (addr, e),
                      {"i2c_scan": i2c_scan(), "chip_id": None})
    finally:
        try:
            bus.close()
        except Exception:  # noqa: BLE001
            pass
    if chip != 0x58:
        raise HWError(NO_DEVICE, "BME280 chip id 0x%02x != 0x58" % chip,
                      {"i2c_scan": i2c_scan(), "chip_id": chip})
    return {"chip_id": chip}


# ---------------------------------------------------------------- GPIO shim
def _gpio_lib_state():
    if lgpio is not None and LGPIO_API == "func":
        return ("lgpio", None)
    if lgpio is not None and LGPIO_API == "class":
        return ("lgpio_class", None)
    if RPiGPIO is not None:
        return ("rpigpio", None)
    return (None, HWError(GPIO_UNAVAILABLE,
                          "no GPIO library importable (need lgpio or RPi.GPIO)"))


def _gpio_open(chip=0):
    api, err = _gpio_lib_state()
    if err:
        raise err
    try:
        if api == "lgpio":
            lgpio.exceptions = False
            h = lgpio.gpiochip_open(int(chip))
            if not isinstance(h, int) or h < 0:
                raise HWError(GPIO_UNAVAILABLE, "gpiochip_open(%s) -> %r" % (chip, h))
            return {"api": api, "h": h}
        if api == "lgpio_class":
            g = lgpio.gpio(int(chip))
            if g is None:
                raise HWError(GPIO_UNAVAILABLE, "lgpio.gpio(%s) failed" % chip)
            return {"api": api, "g": g}
        # rpigpio
        RPiGPIO.setmode(RPiGPIO.BCM)
        return {"api": api}
    except HWError:
        raise
    except Exception as e:  # noqa: BLE001
        raise HWError(GPIO_UNAVAILABLE, "GPIO open failed: %s" % e)


class _Gpio:
    """Tiny shim over the lgpio function/class API or RPi.GPIO.

    Edge timestamps come from the lgpio kernel callback (CLOCK_MONOTONIC ns); a
    wall-clock fallback is used only if a timestamp is unusable.
    """

    def __init__(self, chip=0):
        self._state = _gpio_open(chip)
        self.api = self._state["api"]
        self._claimed = {}
        self._mono0 = time.monotonic()
        self._epoch_ns = time.time_ns()

    def _close(self):
        st = self._state
        for pin in list(self._claimed):
            try:
                if self.api == "lgpio":
                    lgpio.gpio_free(st["h"], pin)
                elif self.api == "lgpio_class":
                    st["g"].unclaim(pin)
                elif self.api == "rpigpio":
                    RPiGPIO.cleanup(pin)
            except Exception:  # noqa: BLE001
                pass
        self._claimed.clear()
        if self.api == "lgpio":
            try:
                lgpio.gpiochip_close(st["h"])
            except Exception:  # noqa: BLE001
                pass

    # -- pin claims
    def input(self, pin, pull=True):
        pin = int(pin)
        st = self._state
        if self.api == "lgpio":
            flags = lgpio.SET_PULL_UP if pull else 0
            r = lgpio.gpio_claim_input(st["h"], pin, flags)
            if isinstance(r, int) and r < 0:
                raise HWError(GPIO_UNAVAILABLE, "gpio_claim_input(%d) err %d" % (pin, r))
        elif self.api == "lgpio_class":
            st["g"].set_mode(pin, "in", pull)
        elif self.api == "rpigpio":
            RPiGPIO.setup(pin, RPiGPIO.IN,
                          pull_up_down=RPiGPIO.PUD_UP if pull else RPiGPIO.PUD_OFF)
        self._claimed[pin] = "in"

    def output(self, pin, init=0):
        pin = int(pin)
        st = self._state
        if self.api == "lgpio":
            r = lgpio.gpio_claim_output(st["h"], pin, int(init))
            if isinstance(r, int) and r < 0:
                raise HWError(GPIO_UNAVAILABLE, "gpio_claim_output(%d) err %d" % (pin, r))
        elif self.api == "lgpio_class":
            st["g"].set_mode(pin, "out")
            st["g"].write(pin, int(init))
        elif self.api == "rpigpio":
            RPiGPIO.setup(pin, RPiGPIO.OUT, initial=bool(init))
        self._claimed[pin] = "out"

    def read(self, pin):
        pin = int(pin)
        st = self._state
        try:
            if self.api == "lgpio":
                v = lgpio.gpio_read(st["h"], pin)
            elif self.api == "lgpio_class":
                v = st["g"].read(pin)
            else:
                v = 1 if RPiGPIO.input(pin) else 0
        except Exception as e:  # noqa: BLE001
            raise HWError(GPIO_UNAVAILABLE, "GPIO read %d failed: %s" % (pin, e))
        if isinstance(v, int) and v < 0:
            raise HWError(GPIO_UNAVAILABLE, "GPIO read %d err %d" % (pin, v))
        return bool(v)

    def write(self, pin, level):
        pin = int(pin)
        st = self._state
        try:
            if self.api == "lgpio":
                r = lgpio.gpio_write(st["h"], pin, 1 if level else 0)
                if isinstance(r, int) and r < 0:
                    raise HWError(GPIO_UNAVAILABLE, "GPIO write %d err %d" % (pin, r))
            elif self.api == "lgpio_class":
                st["g"].write(pin, 1 if level else 0)
            else:
                RPiGPIO.output(pin, bool(level))
        except HWError:
            raise
        except Exception as e:  # noqa: BLE001
            raise HWError(GPIO_UNAVAILABLE, "GPIO write %d failed: %s" % (pin, e))

    def callback(self, pin, edge, func, debounce_us=0):
        pin = int(pin)
        st = self._state
        try:
            if self.api == "lgpio":
                if debounce_us > 0:
                    lgpio.gpio_set_debounce_micros(st["h"], pin, debounce_us)
                cb = lgpio.callback(st["h"], pin, edge, func)
                return cb
            if self.api == "lgpio_class":
                return st["g"].callback(pin, edge, func)
            if self.api == "rpigpio":
                # map our edge ints to RPi edges
                if edge == 1:
                    r_edge = RPiGPIO.RISING
                elif edge == 2:
                    r_edge = RPiGPIO.FALLING
                else:
                    r_edge = RPiGPIO.BOTH
                RPiGPIO.add_event_detect(pin, r_edge, callback=lambda _: func(pin, pin, self.read(pin), time.monotonic_ns()),
                                         bouncetime=debounce_us)
                return None
        except HWError:
            raise
        except Exception as e:  # noqa: BLE001
            raise HWError(GPIO_UNAVAILABLE, "GPIO callback %d failed: %s" % (pin, e))

    # -- timestamps
    def _edge_ts_ns(self, ts):
        """Normalize a lgpio callback timestamp to monotonic-equivalent ns."""
        try:
            ts = int(ts)
        except (TypeError, ValueError):
            return None
        if ts <= 0:
            return None
        # Kernel timestamps are monotonic ns (modern) or epoch ns (old).
        # Distinguish: if it is far above the current epoch, it is epoch-based.
        if ts > self._epoch_ns:
            # epoch-based: convert to monotonic-equivalent
            return ts - (self._epoch_ns - int(self._mono0 * 1e9))
        # monotonic-based: align to our monotonic reference
        return ts + int(self._mono0 * 1e9)


def _mono_ns():
    return time.monotonic_ns()


# ---------------------------------------------------------------- HC-SR04
def _hcsr04(cfg):
    pins = cfg.get("pins", {}) or {}
    trig, echo = pins.get("trig"), pins.get("echo")
    if trig is None or echo is None:
        raise HWError(BAD_CODE, "hcsr04 needs pins {trig, echo} in config")
    g = _Gpio(cfg.get("bus", 0))
    try:
        g.output(int(trig), 0)
        g.input(int(echo), pull=False)
        events = []

        def cb(_chip, _pin, level, ts):
            events.append((level, ts))

        g.callback(int(echo), 3, cb)          # both edges
        g.write(int(trig), 0)
        time.sleep(0.02)
        events.clear()
        g.write(int(trig), 1)
        time.sleep(0.00001)                    # 10 us trigger
        g.write(int(trig), 0)
        # wait for a rising+falling pair on ECHO
        t0 = _mono_ns()
        rising = falling = None
        while rising is None or falling is None:
            for level, ts in events:
                if level == 1 and rising is None:
                    rising = ts
                elif level == 0 and falling is None and rising is not None:
                    falling = ts
            if _mono_ns() - t0 > 300_000_000:
                raise HWError(TIMEOUT,
                              "no ECHO pulse within 300 ms (transducer absent?)",
                              {"events": len(events)})
            time.sleep(0.0002)
        dur = g._edge_ts_ns(falling)
        rising_ns = g._edge_ts_ns(rising)
        if dur is None or rising_ns is None:
            raise HWError(TIMEOUT, "ECHO edge timestamps unavailable")
        dur = dur - rising_ns
        if dur < 1000:
            raise HWError(TIMEOUT, "ECHO pulse too short (%d us)" % dur)
        if dur > 25000:
            raise HWError(TIMEOUT, "ECHO high %d us exceeds 25000 us -> no reading" % dur)
        return {"distance_cm": dur / 58.0}
    finally:
        g._close()


def _hcsr04_verify(cfg):
    pins = cfg.get("pins", {}) or {}
    trig, echo = pins.get("trig"), pins.get("echo")
    if trig is None or echo is None:
        raise HWError(BAD_CODE, "hcsr04 needs pins {trig, echo}")
    g = _Gpio(cfg.get("bus", 0))
    try:
        g.output(int(trig), 0)
        g.input(int(echo), pull=False)
        g.write(int(trig), 1)
        time.sleep(0.00001)
        g.write(int(trig), 0)
    finally:
        g._close()
    return {"trigger_ok": True, "trig": int(trig), "echo": int(echo)}


# ---------------------------------------------------------------- YF-S201
_YF_Q = {}


def _yf_s201(cfg):
    pins = cfg.get("pins", {}) or {}
    pin = pins.get("flow") or pins.get("signal") or pins.get("data")
    if pin is None:
        raise HWError(BAD_CODE, "yf_s201 needs pins {flow|signal|data}")
    k = float(cfg.get("k", 7.5))
    chip = int(cfg.get("bus", 0))
    key = (chip, int(pin))
    g = _Gpio(chip)
    if key not in _YF_Q:
        q = deque()

        def cb(_c, _p, _lvl, ts):
            q.append(ts)

        g.input(int(pin), pull=True)
        g.callback(int(pin), 3, cb)           # both edges, active-low signal
        _YF_Q[key] = (q, g)
    q, g = _YF_Q[key]
    now = _mono_ns()
    cutoff = now - 1_000_000_000
    # drop stale
    while q:
        t0 = g._edge_ts_ns(q[0])
        if t0 is None or t0 < cutoff:
            q.popleft()
        else:
            break
    pulses = len(q)
    return {"flow_lpm": pulses * k / 60.0}


def _yf_s201_verify(cfg):
    pins = cfg.get("pins", {}) or {}
    pin = pins.get("flow") or pins.get("signal") or pins.get("data")
    if pin is None:
        raise HWError(BAD_CODE, "yf_s201 needs pins {flow|signal|data}")
    g = _Gpio(int(cfg.get("bus", 0)))
    try:
        g.input(int(pin), pull=True)
    finally:
        pass
    return {"gpio": int(pin), "gpio_available": True}


# ---------------------------------------------------------------- DHT22
def _dht22(cfg):
    pins = cfg.get("pins", {}) or {}
    pin = pins.get("data") or pins.get("dht")
    if pin is None:
        raise HWError(BAD_CODE, "dht22 needs pins {data}")
    pin = int(pin)
    g = _Gpio(cfg.get("bus", 0))
    try:
        g.output(pin, 0)
        g.write(pin, 0)
        time.sleep(0.020)                     # host pull-low >= 18 ms
        events = []

        def cb(_c, _p, level, ts):
            events.append((level, ts))

        # switch to input (pull-up) and catch the sensor response + 40 bits
        g.input(pin, pull=True)
        g.callback(pin, 3, cb)
        # wait for the response low (level 0) after release
        t0 = _mono_ns()
        resp = None
        while resp is None:
            for level, ts in events:
                if level == 0:
                    resp = ts
                    break
            if _mono_ns() - t0 > 5_000_000:   # 5 ms: sensor must answer fast
                raise HWError(TIMEOUT, "DHT22: no response after host low (sensor absent?)",
                              {"events": len(events)})
            time.sleep(0.0002)
        # wait until we have enough edges: resp low + resp high + 40*(low,high) = 82
        t1 = _mono_ns()
        while len(events) < 84:
            if _mono_ns() - t1 > 25_000_000:  # 25 ms: full frame must finish
                raise HWError(TIMEOUT, "DHT22: incomplete frame (%d edges)" % len(events))
            time.sleep(0.00005)
        # find the response-low index
        i = None
        for idx, (level, _ts) in enumerate(events):
            if level == 0:
                i = idx
                break
        if i is None or len(events) < i + 82:
            raise HWError(TIMEOUT, "DHT22: bad frame shape (%d edges)" % len(events))
        # sequence from i: [0 resp][1 resp] then per bit b: [0 bit low][1 bit high]
        bits = []
        for b in range(40):
            hi_start = events[i + 2 + b * 2 + 1][1]
            hi_end = events[i + 2 + (b + 1) * 2][1]
            hs = g._edge_ts_ns(hi_start)
            he = g._edge_ts_ns(hi_end)
            if hs is None or he is None:
                raise HWError(TIMEOUT, "DHT22: bit %d timestamp unavailable" % b)
            bits.append(1 if (he - hs) > 40_000 else 0)   # ~40us threshold
        raw = 0
        for bit in bits:
            raw = (raw << 1) | bit
        b = [raw >> (8 * (4 - j)) & 0xFF for j in range(5)]
        if (b[0] + b[1] + b[2] + b[3]) & 0xFF != b[4]:
            raise HWError(TIMEOUT, "DHT22 checksum mismatch: bytes=%r" % b)
        hum = b[0] + b[1] / 256.0
        temp = b[2] + b[3] / 256.0
        if b[2] & 0x80:
            temp = -temp
        return {"temperature": temp, "humidity": hum}
    finally:
        g._close()


def _dht22_verify(cfg):
    pins = cfg.get("pins", {}) or {}
    pin = pins.get("data") or pins.get("dht")
    if pin is None:
        raise HWError(BAD_CODE, "dht22 needs pins {data}")
    g = _Gpio(int(cfg.get("bus", 0)))
    try:
        g.output(int(pin), 0)
    finally:
        g._close()
    return {"data_pin": int(pin), "gpio_available": True}


# ---------------------------------------------------------------- custom
def _run_custom_code(cfg):
    """Compile+exec custom code in a fresh namespace; returns the namespace."""
    code = cfg.get("code")
    if not isinstance(code, str) or not code.strip():
        raise HWError(BAD_CODE, "custom config missing 'code' string")
    try:
        compiled = compile(code, "<custom>", "exec")
    except SyntaxError as e:
        raise HWError(BAD_CODE, "syntax error: %s" % e)
    ns = {"HWError": HWError, "smbus2": smbus2, "time": time}
    try:
        exec(compiled, ns)  # noqa: S102 - this is the point of "custom"
    except ImportError as e:
        mod = getattr(e, "name", None) or (str(e).split()[-1] if str(e) else "module")
        raise HWError(LIB_MISSING, "custom module import failed: %s" % e, {"module": str(mod)})
    except HWError:
        raise
    except Exception as e:  # noqa: BLE001
        raise HWError(BAD_CODE, "custom module top-level exec failed: %s" % e)
    if "read" not in ns or not callable(ns.get("read")):
        raise HWError(BAD_CODE, "custom module must define read(config) -> dict")
    return ns


def _read_builtin(cfg, devtype):
    if devtype == "bh1750":
        return _bh1750(cfg)
    if devtype == "bme280":
        return _bme280_read_cfg(cfg)
    if devtype == "hcsr04":
        return _hcsr04(cfg)
    if devtype == "yf_s201":
        return _yf_s201(cfg)
    if devtype == "dht22":
        return _dht22(cfg)
    raise HWError(BAD_CODE, "unknown type %r" % devtype)


def _verify_builtin(cfg, devtype):
    if devtype == "bh1750":
        return _bh1750_verify(cfg)
    if devtype == "bme280":
        return _bme280_verify(cfg)
    if devtype == "hcsr04":
        return _hcsr04_verify(cfg)
    if devtype == "yf_s201":
        return _yf_s201_verify(cfg)
    if devtype == "dht22":
        return _dht22_verify(cfg)
    return {}


def _validate_samples(samples, fields):
    """Return a violation string, or None if all samples are finite + in range."""
    for si, s in enumerate(samples):
        for f in fields:
            key = f["key"]
            if key not in s:
                return "sample %d missing field %r" % (si, key)
            v = s[key]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                return "field %r not a finite number: %r" % (key, v)
            lo, hi = f.get("min"), f.get("max")
            if lo is not None and v < lo:
                return "field %r=%s below min %s" % (key, v, lo)
            if hi is not None and v > hi:
                return "field %r=%s above max %s" % (key, v, hi)
    return None


# ---------------------------------------------------------------- selftest
def run_selftest(cfg):
    """Return exactly the selftest contract dict. Never raises for hardware."""
    devtype = cfg.get("type", "")
    dev_id = cfg.get("device_id", "")
    result = {"ok": False, "device_id": dev_id, "type": devtype,
              "error": None, "detail": None, "samples": None, "evidence": None}
    try:
        fields = cfg.get("fields", []) or []
        if devtype == "custom":
            ns = _run_custom_code(cfg)
            evidence = {}
            verify = ns.get("verify")
            if callable(verify):
                try:
                    ev = verify(cfg)
                    if isinstance(ev, dict):
                        evidence.update(ev)
                except HWError:
                    raise
                except Exception as e:  # noqa: BLE001
                    raise HWError(BAD_CODE, "custom verify() failed: %s" % e)
            samples = [ns["read"](cfg) for _ in range(2)]
            bad = _validate_samples(samples, fields)
            if bad:
                result.update(ok=False, error=OUT_OF_RANGE, detail=bad,
                              samples=samples, evidence=evidence or None)
                return result
            result.update(ok=True, error=OK, samples=samples, evidence=evidence or None)
            return result
        elif devtype in ("bh1750", "bme280", "hcsr04", "yf_s201", "dht22"):
            evidence = _verify_builtin(cfg, devtype) or {}
            samples = [_read_builtin(cfg, devtype) for _ in range(2)]
            bad = _validate_samples(samples, fields)
            if bad:
                result.update(ok=False, error=OUT_OF_RANGE, detail=bad,
                              samples=samples, evidence=evidence or None)
                return result
            result.update(ok=True, error=OK, samples=samples, evidence=evidence or None)
            return result
        else:
            result.update(error=BAD_CODE, detail="unknown type %r" % devtype)
            return result
    except HWError as e:
        result.update(ok=False, error=e.code, detail=e.detail,
                      evidence=e.evidence if e.evidence else None)
        return result
    except Exception as e:  # noqa: BLE001 - honest generic failure, never fake
        result.update(ok=False, error=BAD_CODE,
                      detail="unexpected: %s: %s" % (type(e).__name__, e))
        return result


# ---------------------------------------------------------------- Firestore
_FS_CLIENT = None
_FS_FAIL = False


def _fs_client():
    global _FS_CLIENT, _FS_FAIL
    if _FS_CLIENT is not None or _FS_FAIL:
        return _FS_CLIENT
    try:
        import firebase_admin
        from firebase_admin import credentials
        from google.cloud import firestore
        if not os.path.exists(_FS_PATH):
            raise OSError("service account json missing at " + _FS_PATH)
        cred = credentials.Certificate(_FS_PATH)
        if not firebase_admin.get_apps():
            firebase_admin.initialize_app(cred)
        _FS_CLIENT = firestore.Client()
    except Exception:  # noqa: BLE001
        _FS_FAIL = True
        _FS_CLIENT = None
    return _FS_CLIENT


def _fs_set(cfg, doc_type, payload):
    client = _fs_client()
    if client is None:
        print("firestore unavailable; skipping write", file=sys.stderr)
        return
    site = str(cfg.get("site_id", "default"))
    dev = str(cfg.get("device_id", ""))
    coll = "water_reading" if doc_type == "reading" else "driver_status"
    # site/{site_id}/{coll}/{device_id}
    doc_ref = client.collection(site).document(coll).collection(dev).document(dev)
    doc_ref.set(payload)


def _fs_status(cfg, res):
    payload = {"ok": False, "error": res.get("error"), "detail": res.get("detail"),
               "ts": datetime.now(timezone.utc).isoformat()}
    _fs_set(cfg, "status", payload)


def _fs_reading(cfg, res):
    payload = {}
    for s in res["samples"]:
        for k, v in s.items():
            payload[k] = v
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    _fs_set(cfg, "reading", payload)


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args(argv)
    with open(args.config) as f:
        cfg = json.load(f)
    if args.selftest:
        print(json.dumps(run_selftest(cfg), ensure_ascii=False))
        return 0
    interval = float(cfg.get("interval_s", 3))
    while True:
        res = run_selftest(cfg)
        if args.once:
            print(json.dumps(res, ensure_ascii=False))
            break
        if res["ok"]:
            try:
                _fs_reading(cfg, res)
            except Exception as e:  # noqa: BLE001
                print("reading write failed:", e, file=sys.stderr)
        else:
            try:
                _fs_status(cfg, res)
            except Exception as e:  # noqa: BLE001
                print("status write failed:", e, file=sys.stderr)
        time.sleep(interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
