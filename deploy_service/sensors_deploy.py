"""
RESEARCH phase for SCV Water deploy service.

Flow: normalise sensor model -> local catalog (known sensors) or web research
(unknown sensors) -> LLM codegen for custom drivers -> pin allocation (shared
registry so the Fritzing diagram and the deployed driver agree) -> test on the
real Pi via pi_driver/driver.py --selftest -> persist to research_registry.json.

Kept deliberately small: stdlib + urllib only.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
RESEARCH_REGISTRY_PATH = BASE_DIR / "research_registry.json"
PIN_REGISTRY_PATH = BASE_DIR / "pin_registry.json"

# Same pool hardware_diagram.build_ir uses -- diagram and driver must agree.
GPIO_POOL = [17, 27, 22, 23, 24, 25, 5, 6, 12, 13, 16, 19, 20, 21, 26]

FIRESTORE_PROJECT = "login-verificati-1717467959577"
# Fallback project the web API key (from scv_water_dashboard/lib/firebase_options.dart)
# is confirmed to have access to.
FIRESTORE_PROJECT_FALLBACK = "login-verificati-1712467959577"

WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


# ---------------------------------------------------------------------------
# JSON registry helpers
# ---------------------------------------------------------------------------
def load_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Local sensor catalog (real datasheet facts; skips web research + LLM codegen)
# ---------------------------------------------------------------------------
def _f(key: str, label: str, unit: str, vmin: float, vmax: float) -> dict:
    return {"key": key, "label": label, "unit": unit, "min": vmin, "max": vmax}


# Hand-written driver modules for catalog entries that need code (the local
# codegen path is too slow/flaky for known sensors). These run in the Pi
# driver's custom-code harness: they must define read(config) -> dict and may
# define verify(config) -> dict; HWError/smbus2/time are pre-injected.
_MPU6050_CODE = '''"""MPU-6050 6-axis IMU driver (custom code for the SCV Water Pi driver harness).

Registers per the InvenSense MPU-6050 datasheet (Rev 3.4, 2012):
  WHO_AM_I 0x75 (0x6F); PWR_MGMT_1 0x6B (bit0 SLEEP); ACCEL_CONFIG 0x1C (FS +/-2g)
  GYRO_CONFIG 0x1B (FS +/-2000dps); ACCEL_XOUT 0x3B (6 bytes), TEMP_OUT 0x41
  (2 bytes), GYRO_XOUT 0x43 (6 bytes). Accel LSB 16.4, gyro LSB 0.07, temp
  3.4 degC/LSB + 36.536 baseline.
"""

HW_NO_DEVICE = "no_device"
HW_BUS_ERROR = "bus_error"


def _i2c_read_block(bus_no, addr, reg, n):
    b = smbus2.SMBus(int(bus_no))
    try:
        return bytes(b.read_i2c_block_data(addr, reg, n))
    finally:
        b.close()


def _i2c_write_byte(bus_no, addr, reg, value):
    b = smbus2.SMBus(int(bus_no))
    try:
        b.write_byte_data(addr, reg, int(value) & 0xFF)
    finally:
        b.close()


def _init(cfg):
    bus_no = cfg.get("bus", 1)
    addr = int(cfg.get("address", 0x68))
    _i2c_write_byte(bus_no, addr, 0x6B, 0x00)  # wake: SLEEP=0, X-axis ref
    _i2c_write_byte(bus_no, addr, 0x1C, 0x00)  # accel FS +/-2g
    _i2c_write_byte(bus_no, addr, 0x1B, 0x03)  # gyro FS +/-2000 dps


def read(cfg):
    bus_no = cfg.get("bus", 1)
    addr = int(cfg.get("address", 0x68))
    try:
        _init(cfg)
        a_raw = _i2c_read_block(bus_no, addr, 0x3B, 6)
        t_raw = _i2c_read_block(bus_no, addr, 0x41, 2)
        g_raw = _i2c_read_block(bus_no, addr, 0x43, 6)
    except HWError:
        raise
    except Exception as e:
        raise HWError(HW_NO_DEVICE,
                      "MPU-6050 I2C read failed on bus %s at 0x%02x: %s"
                      % (bus_no, addr, e))
    s16 = lambda b, i: int.from_bytes(b[i:i + 2], "big", signed=True)
    ax = s16(a_raw, 0) / 16384.0
    ay = s16(a_raw, 2) / 16384.0
    az = s16(a_raw, 4) / 16384.0
    temp = s16(t_raw, 0) / 333.875 + 36.536
    gx = s16(g_raw, 0) / 65.5
    gy = s16(g_raw, 2) / 65.5
    gz = s16(g_raw, 4) / 65.5
    return {"accel_x": ax, "accel_y": ay, "accel_z": az,
            "gyro_x": gx, "gyro_y": gy, "gyro_z": gz, "temp": temp}


def verify(cfg):
    bus_no = cfg.get("bus", 1)
    addr = int(cfg.get("address", 0x68))
    try:
        who = smbus2.SMBus(int(bus_no)).read_byte_data(addr, 0x75)
    except Exception as e:
        raise HWError(HW_NO_DEVICE,
                      "MPU-6050 WHO_AM_I read failed on bus %s at 0x%02x: %s"
                      % (bus_no, addr, e))
    if who not in (0x68, 0x70, 0x71, 0x73, 0x6F):
        raise HWError(HW_NO_DEVICE,
                      "MPU-6050 WHO_AM_I=0x%02x, expected 0x68/0x6F/0x70-0x73" % who)
    return {"who_am_i": who}
'''


SENSOR_CATALOG: dict[str, dict] = {
    "bh1750": {
        "type": "bh1750", "protocol": "i2c", "address": 0x23, "bus": 1,
        "pin_names": [],
        "fields": [_f("lux", "Light Level", "lx", 0, 65535)],
        "datasheet": "http://www.rossemann.biz/downloads/bh1750fvi.pdf",
        "datasheet_title": "BH1750FVI Datasheet ( Rohm )",
    },
    "bme280": {
        "type": "bme280", "protocol": "i2c", "address": 0x76, "bus": 1,
        "pin_names": [],
        "fields": [
            _f("temp", "Temperature", "°C", -40, 85),
            _f("humidity", "Humidity", "%", 0, 100),
            _f("pressure", "Pressure", "hPa", 300, 1100),
        ],
        "datasheet": "https://www.bosch-sensortec.com/media/boschsensortec/downloads/data_sheets_bst_bme280-ds126.pdf",
        "datasheet_title": "BME280 Datasheet ( Bosch Sensortec )",
    },
    "dht22": {
        "type": "dht22", "protocol": "gpio",
        "pin_names": ["data"],
        "fields": [
            _f("temp", "Temperature", "°C", -40, 80),
            _f("humidity", "Humidity", "%", 0, 100),
        ],
        "datasheet": "https://www.ams.com/products/sensors/temperature-and-humidity-sensors/dht22",
        "datasheet_title": "DHT22 Datasheet ( ams )",
    },
    "hcsr04": {
        "type": "hcsr04", "protocol": "gpio",
        "pin_names": ["trig", "echo"],
        "fields": [_f("distance", "Distance", "cm", 0.3, 400)],
        "datasheet": "https://cdn.sparkfun.com/datasheets/Components/HCSR04.pdf",
        "datasheet_title": "HC-SR04 Ultrasonic Sensor Datasheet ( SparkFun )",
    },
    "yf_s201": {
        "type": "yf_s201", "protocol": "gpio",
        "pin_names": ["signal"], "k": 7.5,
        "fields": [_f("flow_rate", "Flow Rate", "L/min", 0, 30)],
        "datasheet": "https://www.seeedstudio.com/water-flow-meter-sensor-yf-s201-p-3165.html",
        "datasheet_title": "YF-S201 Water Flow Meter ( Seeed Studio )",
    },
    "mpu6050": {
        "type": "mpu6050", "protocol": "i2c", "address": 0x68, "bus": 1,
        "pin_names": [],
        "fields": [
            _f("accel_x", "Accel X", "g", -4, 4),
            _f("accel_y", "Accel Y", "g", -4, 4),
            _f("accel_z", "Accel Z", "g", -4, 4),
            _f("gyro_x", "Gyro X", "deg/s", -2000, 2000),
            _f("gyro_y", "Gyro Y", "deg/s", -2000, 2000),
            _f("gyro_z", "Gyro Z", "deg/s", -2000, 2000),
            _f("temp", "Temperature", "C", -40, 105),
        ],
        "datasheet": "https://www.invensense.tdk.com/products/motion-solutions/6-axis-icd/mpu-6050/",
        "datasheet_title": "MPU-6050 6-Axis Motion Tracking Sensor ( InvenSense/TDK )",
        "code": _MPU6050_CODE,
    },
    "ds18b20": {
        "type": "ds18b20", "protocol": "gpio",
        "pin_names": ["data"],
        "fields": [_f("temp", "Temperature", "°C", -55, 125)],
        "datasheet": "https://cache.maximintegrated.com/en/design/ds/DS18B20.pdf",
        "datasheet_title": "DS18B20 Datasheet ( Maxim )",
    },
}


def normalise_model(model: str) -> tuple[dict | None, str]:
    """Match a free-text model string against the local catalog.

    Returns (catalog_entry_or_None, normalised_type). 'BH1750 光線感測器' ->
    catalog bh1750; unknown text -> (None, 'custom').
    """
    key = (model or "").strip().lower()
    for name, entry in SENSOR_CATALOG.items():
        if name in key:
            return entry, name
    return None, "custom"


# ---------------------------------------------------------------------------
# Pin allocation registry (shared with hardware_diagram.build_ir)
# ---------------------------------------------------------------------------
def get_site_pins(site_id: str, sensor_type: str) -> dict | None:
    """Already-allocated GPIO pins for (site, type), or None."""
    reg = load_json(PIN_REGISTRY_PATH, {})
    entry = reg.get(site_id, {}).get(sensor_type)
    if entry and entry.get("pins"):
        return entry["pins"]
    return None


def allocate_pins(site_id: str, sensor_type: str, pin_names: list[str]) -> dict[str, int]:
    """Assign free BCM pins from the shared pool for the given sensor type.

    Idempotent per (site, type): re-requests return the same pins.
    ponytail: whole-file read/write on every call; fine at this scale,
    swap for per-process lock + atomic merge if concurrent writes appear.
    """
    if not pin_names:
        return {}
    reg = load_json(PIN_REGISTRY_PATH, {})
    site = reg.setdefault(site_id, {})
    pins = dict(site.get(sensor_type, {}).get("pins", {}))
    used = set()
    for entry in site.values():
        used.update(p for p in (entry.get("pins") or {}).values() if isinstance(p, int))
    for name in pin_names:
        if name not in pins:
            for p in GPIO_POOL:
                if p not in used and p not in pins.values():
                    pins[name] = p
                    used.add(p)
                    break
    site[sensor_type] = {"pins": pins}
    save_json(PIN_REGISTRY_PATH, reg)
    return {n: pins[n] for n in pin_names}


# ---------------------------------------------------------------------------
# Web research (DuckDuckGo HTML, Bing fallback) -- no API key, stdlib only
# ---------------------------------------------------------------------------
class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "template"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())

    def text(self) -> str:
        return " ".join(self.parts)


def _fetch(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": WEB_UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(600000).decode("utf-8", errors="replace")


def _strip_tags(html_fragment: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_fragment)).strip()


def _html_to_text(html: str) -> str:
    p = _TextExtractor()
    try:
        p.feed(html)
    except Exception:
        pass
    return p.text()


_SPEC_KEYWORDS = (
    "i2c", "i2c address", "sda", "scl", "register", "0x", "address", "gpio",
    "pin", "pins", "resolution", "supply", "vcc", "vdd", "gnd", "protocol",
    "baud", "pulse", "frequency", "range", "accuracy", "output", "logic level",
    "interface", "datasheet", "measurement", "sensor", "threshold", "period",
)


def _spec_text(text: str, limit: int = 4000) -> str:
    """Keep the ~4000 chars most dense in spec-like content."""
    paras = [b.strip() for b in re.split(r"\n\s*\n|\.{2,}|[;|] ", text) if len(b.strip()) > 40]
    scored = sorted(
        paras,
        key=lambda b: sum(b.lower().count(k) for k in _SPEC_KEYWORDS),
        reverse=True,
    )
    out = []
    size = 0
    for b in scored:
        if size >= limit:
            break
        out.append(b)
        size += len(b) + 1
    return "\n".join(out)[:limit]


def _ddg_search(query: str) -> list[dict]:
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query)
    html = _fetch(url)
    results = []
    for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
        href, title = m.group(1), _strip_tags(m.group(2))
        if href.startswith("//"):
            href = "https:" + href
        if "duckduckgo.com/l/" in href:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            if qs.get("uddg"):
                href = qs["uddg"][0]
        if re.match(r"^https?://", href) and not href.lower().endswith(".pdf"):
            results.append({"title": title, "url": href})
    return results[:5]


def _bing_search(query: str) -> list[dict]:
    url = "https://www.bing.com/search?q=" + urllib.parse.quote_plus(query)
    html = _fetch(url)
    results = []
    for m in re.finditer(r'<li class="b_algo".*?<h2><a href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
        href, title = m.group(1), _strip_tags(m.group(2))
        if re.match(r"^https?://", href) and not href.lower().endswith(".pdf"):
            results.append({"title": title, "url": href})
    return results[:5]


def web_research(model: str) -> tuple[str, list[dict]]:
    """Search the datasheet and return (spec_text, sources_used).

    ponytail: regex HTML scraping of DDG/Bing -- good enough for a dev
    pipeline, swap for a real search API if they rate-limit us.
    """
    query = f"{model} sensor datasheet"
    try:
        results = _ddg_search(query)
        search_engine = "duckduckgo"
    except Exception as e:
        results = _bing_search(query)
        search_engine = f"bing (ddg failed: {e})"

    sources: list[dict] = []
    chunks: list[str] = []
    for r in results[:5]:
        try:
            text = _spec_text(_html_to_text(_fetch(r["url"])), limit=4000)
        except Exception:
            continue
        if len(text) < 200:
            continue
        sources.append({"title": r["title"] or r["url"], "url": r["url"]})
        chunks.append(f"### {r['title'] or r['url']}\n{text}")
        if len("\n".join(chunks)) >= 9000:
            break
    return "\n\n".join(chunks)[:9000], sources


# ---------------------------------------------------------------------------
# Local LLM (FastMTP Qwen3.8 27B, single-slot -> serial calls, retry on 500)
# ---------------------------------------------------------------------------
LLM_URL = "http://127.0.0.1:8088/v1/chat/completions"
LLM_MODEL = "qwen3.8-27b-fastmtp"


def _strip_thinking(raw: str) -> str:
    clean = re.sub(r"(?is)<\s*think(?:ing)?\s*>.*?<\s*/\s*think(?:ing)?\s*>", "", raw)
    # DSML / other tag artifacts some serving stacks leave behind
    clean = re.sub(r"(?is)<\s*(DSML_[A-Z]+|/DSML_[A-Z]+)\s*[^>]*/?\s*>", "", clean)
    return clean.strip()


def _extract_json(raw: str):
    """Parse a JSON object out of an LLM reply (fences / thinking / prose)."""
    clean = _strip_thinking(raw)
    clean = re.sub(r"(?is)```(?:json)?\s*", " ", clean)
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        pass
    start = clean.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(clean)):
            ch = clean[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(clean[start:i + 1])
                        except json.JSONDecodeError:
                            break
        start = clean.find("{", start + 1)
    raise ValueError("no JSON object found in LLM reply")


def _extract_code(raw: str) -> str:
    """Pull the python module out of a fenced reply (the model sometimes emits a
    code fence instead of the JSON object). Returns the code or ''."""
    m = re.search(r"(?is)```(?:python|py)?\s*(.*?)```", raw)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return ""


def _field_keys_from_code(code: str) -> list[str]:
    """Best-effort: keys the driver's read() returns, from its dict literals."""
    keys: list[str] = []
    for m in re.finditer(r"return\s*\{([^}]*)\}", code, re.S):
        for k in re.findall(r'["\']([a-z0-9_]+)["\']\s*:', m.group(1)):
            if k not in keys:
                keys.append(k)
    return keys


def qwen_call(messages: list[dict], max_tokens: int = 3000,
              temperature: float = 0.1, timeout: int = 180,
              max_attempts: int = 3, enable_thinking: bool = False) -> str:
    """One chat completion; retry after 15s (server is single-slot and may be
    busy with the main Hermes session).

    enable_thinking=False via chat_template_kwargs: the local thinking model
    can burn the whole token budget on reasoning and return empty content.
    """
    last: Exception | None = None
    for attempt in range(max_attempts):
        try:
            payload = {
                "model": LLM_MODEL,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if enable_thinking is False:
                # Top-level chat_template_kwargs reaches vLLM/FastMTP directly;
                # extra_body is a vLLM-python-client wrapper the raw HTTP API
                # ignores (thinking kept running and ate the whole budget).
                payload["chat_template_kwargs"] = {"enable_thinking": False}
            req = urllib.request.Request(
                LLM_URL, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as res:
                data = json.loads(res.read())
            msg = data["choices"][0]["message"]
            content = msg.get("content") or ""
            # Do NOT fall back to reasoning_content: that is model *thinking*,
            # not the final answer, and feeding it to the JSON parser makes
            # truncated replies look like "invalid JSON".
            if not content.strip() and data["choices"][0].get("finish_reason") == "length":
                raise RuntimeError("LLM reply truncated at max_tokens (all tokens spent on reasoning)")
            return content
        except Exception as e:  # noqa: BLE001 - retry any transport/HTTP error
            last = e
            if attempt < max_attempts - 1:
                time.sleep(15)
    raise RuntimeError(f"LLM endpoint failed after {max_attempts} attempts: {last}")


CODEGEN_SYSTEM = (
    "You are an embedded IoT Python expert writing drivers for a Raspberry Pi. "
    "You output exactly one valid JSON object on its own: no prose before or "
    "after, no markdown fences, no comments. Keep the code under 90 lines."
)


def codegen_prompt(model: str, datasheet_text: str, protocol: str,
                   pins: dict[str, int], bus: int | None,
                   address: int | None) -> str:
    pin_hint = ", ".join(f"{k}={v}" for k, v in pins.items()) or "none (I2C)"
    return (
        f"Sensor model: {model}\n"
        f"Protocol: {protocol}\n"
        f"Allocated GPIO pins (BCM): {pin_hint}\n"
        f"I2C bus: {bus}\nI2C address: {address}\n\n"
        f"Datasheet text extracted from the web:\n{datasheet_text[:9000]}\n\n"
        "Write a compact (under 90 lines) Python driver module for a Raspberry Pi. "
        "Requirements:\n"
        "- define read(config: dict) -> dict returning REAL measured values keyed "
        "by the field keys below; raise HWError(\"no_device\", msg) if the hardware "
        "is absent or unreadable. HWError is pre-injected (signature "
        "HWError(code, detail, evidence=None)). NEVER fake, simulate, or default values.\n"
        "- optionally define verify(config: dict) -> dict of evidence.\n"
        "- for I2C use smbus2 (pre-injected; bus number and address from config); "
        "for GPIO use RPi.GPIO with the allocated BCM pins from config['pins']. "
        "Handle import errors of those libraries gracefully with a clear message.\n"
        "- stdlib + smbus2/lgpio/RPi.GPIO only.\n\n"
        'Return JSON with exactly these keys, nothing else:\n'
        '{"code": "<full python module source>", '
        '"fields": [{"key","label","unit","min","max"}], '
        '"config": {"type":"custom","pins":{...},"bus":int|null,"address":int|null,'
        '"interval_s":3}}'
    )


def fix_prompt(model: str, prev_code: str, selftest_result: dict,
               datasheet_text: str, protocol: str, pins: dict) -> str:
    return (
        f"Sensor model: {model} (protocol {protocol}, pins {pins})\n"
        f"Previous driver code:\n{prev_code}\n\n"
        f"The on-Pi selftest failed with this JSON:\n{json.dumps(selftest_result, ensure_ascii=False)}\n\n"
        f"Datasheet text:\n{datasheet_text[:6000]}\n\n"
        "Fix the driver so it reads REAL values and its selftest passes, or fails "
        "honestly with a precise error code when the hardware is truly absent. "
        "Same contract: read(config) -> dict, raise HWError(\"no_device\", msg) when "
        "absent (HWError pre-injected), never fake values, stdlib+smbus2/RPi.GPIO, "
        "under 90 lines.\n"
        'Return JSON: {"code": "<fixed python module source>"}\n/no_think'
    )


# ---------------------------------------------------------------------------
# Research pipeline
# ---------------------------------------------------------------------------
def research(model: str, site_id: str, force_research: bool = False) -> tuple[dict, str, list[dict], str]:
    """Normalise + (catalog | web research + codegen) + pin allocation.

    force_research=True bypasses the local catalog so even a known sensor runs
    the full web-research + LLM-codegen path (used to exercise that path).

    Returns (config_without_device_id, code_or_empty, sources, protocol).
    """
    entry, norm_type = normalise_model(model)

    if entry is not None and not force_research:
        cfg: dict = {
            "type": norm_type,
            "fields": entry["fields"],
            "pins": allocate_pins(site_id, norm_type, entry["pin_names"]) if entry["pin_names"] else {},
            "interval_s": 3,
        }
        if entry["protocol"] == "i2c":
            cfg["bus"] = entry.get("bus", 1)
            cfg["address"] = entry.get("address")
        if entry.get("k") is not None:
            cfg["k"] = entry["k"]
        sources = [{"title": entry["datasheet_title"], "url": entry["datasheet"]}]
        # Known sensor with a hand-written module (e.g. mpu6050): run it through
        # the same custom-code path as generated drivers instead of the builtin
        # dispatch, so catalog and research behave identically on the Pi.
        code = entry.get("code", "")
        if code:
            cfg["type"] = "custom"
        return cfg, code, sources, entry["protocol"]

    # --- unknown model: web research + LLM codegen ---
    datasheet_text, sources = web_research(model)
    if not datasheet_text:
        raise RuntimeError("web research returned no usable datasheet text")

    protocol_hint = "gpio"
    if re.search(r"\bi2c\b|sda|scl", datasheet_text, re.I):
        protocol_hint = "i2c"

    messages = [
        {"role": "system", "content": CODEGEN_SYSTEM},
        {"role": "user",
         "content": codegen_prompt(model, datasheet_text, protocol_hint, {}, None, None)},
    ]
    code = fields = None
    cfg = {"type": "custom", "pins": {}, "interval_s": 3}
    gen_cfg: dict = {}
    for attempt in range(2):  # one retry if nothing usable came back
        raw = qwen_call(messages, max_tokens=8000, timeout=420)
        gen = None
        try:
            gen = _extract_json(raw)
        except ValueError:
            pass
        if isinstance(gen, dict) and (gen.get("code") or "").strip():
            code = gen["code"]
            fields = gen.get("fields") or []
            gen_cfg = gen.get("config") or {}
            break
        # the model emitted a code fence (or prose) instead of the JSON object
        fenced = _extract_code(raw)
        if fenced:
            code = fenced
            break
        if attempt == 0:
            messages[1]["content"] += ("\n\nYour previous reply had no driver code. "
                                       "Reply with ONLY the JSON object.")
            continue
        raise RuntimeError("LLM codegen returned no driver code")

    if not code:
        raise RuntimeError("LLM codegen returned no code")
    if gen_cfg.get("protocol"):
        protocol = gen_cfg["protocol"]
    else:
        protocol = protocol_hint
    if gen_cfg.get("bus") is not None:
        cfg["bus"] = gen_cfg["bus"]
    elif protocol_hint == "i2c":
        cfg["bus"] = 1
    if gen_cfg.get("address") is not None:
        cfg["address"] = gen_cfg["address"]
    if protocol == "gpio":
        names = [p for p in (gen_cfg.get("pins") or {}) if isinstance((gen_cfg.get("pins") or {}).get(p), int)] or ["signal"]
        cfg["pins"] = allocate_pins(site_id, norm_type, names)
    if not fields:
        # model omitted fields: derive from the code's returned dict so the on-Pi
        # selftest validates the REAL keys the driver emits
        keys = _field_keys_from_code(code) or ["value"]
        fields = [{"key": k, "label": k.replace("_", " ").title(), "unit": "",
                   "min": None, "max": None} for k in keys]
    cfg["fields"] = fields
    return cfg, code, sources, protocol


# ---------------------------------------------------------------------------
# Self-check (offline): python3 sensors_deploy.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    e, t = normalise_model("BH1750 光線感測器")
    assert e is not None and t == "bh1750", (e, t)
    assert normalise_model("INA219 current monitor")[1] == "custom"
    m, mt = normalise_model("MPU6050 6軸感測器")
    assert m is not None and mt == "mpu6050" and m.get("code"), (mt, bool(m and m.get("code")))
    compile(m["code"], "<mpu>", "exec")  # hand-written driver must be valid python

    html = "<html><head><style>x{}</style></head><body><script>bad()</script>" \
           "<p>i2c address 0x23 SDA SCL register 0x10 resolution 0.01 lux</p>" \
           "<p>filler text that should score low and get dropped by the scorer</p>" \
           "<p>supply voltage VCC 3.3V to 5V, logic level, pulse output</p></body></html>"
    text = _spec_text(_html_to_text(html), limit=2000)
    assert "0x23" in text and "bad()" not in text

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        PIN_REGISTRY_PATH = Path(td) / "pins.json"
        p1 = allocate_pins("ZZ", "hcsr04", ["trig", "echo"])
        p2 = allocate_pins("ZZ", "hcsr04", ["trig", "echo"])
        assert p1 == p2 and len(set(p1.values())) == 2
        p3 = allocate_pins("ZZ", "dht22", ["data"])
        assert p3["data"] not in p1.values()
        assert get_site_pins("ZZ", "hcsr04") == p1

    raw = 'Sure! Here you go:\n```json\n{"code":"print(1)","fields":[]}\n```\nDone.'
    assert _extract_json(raw)["code"] == "print(1)"

    print("sensors_deploy self-check: OK")
