"""
Hardware installation-diagram generator (Fritzing) for SCV Water.

This module is the middle-service half of "生成硬體安裝圖": it owns the SSH
private keys, the LLM credentials and the Fritzing binary, none of which belong
on a phone. The App posts a site + its sensors, this returns a rendered PNG.

Pipeline (from wiring_gen/, user-supplied):
    sensor IR  ->  wiring_gen pipeline (LLM extract + rule validation)
               ->  make_fzz (deterministic Fritzing .fz writer)
               ->  Fritzing headless -svg  ->  rsvg-convert  ->  PNG

The design rule that matters: the LLM only ever emits semantic wiring JSON
(BCM pin + pin name + evidence). Geometry is computed by the deterministic
renderer, so a hallucinated coordinate cannot reach the picture.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

WIRING_GEN_DIR = Path(__file__).resolve().parent.parent / "wiring_gen"
CACHE_DIR = Path(os.environ.get("SCV_WIRING_CACHE", "/tmp/scv_wiring_cache"))

# Fritzing: an extracted AppImage is required (the AppImage itself is read-only,
# so parts cannot be registered into it).
_FRITZING_CANDIDATES = [
    os.environ.get("FRITZING_BIN", ""),
    # Extracted AppImage layouts, most specific first.
    str(Path.home() / "fritzing/squashfs-root/AppRun"),
    str(Path.home() / "fritzing/AppRun"),
    "/opt/fritzing/squashfs-root/AppRun",
    "/opt/fritzing/AppRun",
    "/usr/bin/fritzing",
]


def find_fritzing() -> str | None:
    for c in _FRITZING_CANDIDATES:
        if c and os.path.exists(c) and os.access(c, os.X_OK):
            return c
    return shutil.which("fritzing")


# ---------------------------------------------------------------------------
# Sensor catalogue -- maps a free-text sensor model onto a wiring IR type.
# Unknown models fall back to a generic 3-pin GPIO module so the diagram still
# renders; the LLM refines pin names when it has datasheet text to work from.
# ---------------------------------------------------------------------------
SENSOR_ALIASES = {
    "yf-s201": "yf_s201", "yfs201": "yf_s201", "yf_s201": "yf_s201",
    "flow": "yf_s201", "water flow": "yf_s201", "流量": "yf_s201",
    "bme280": "bme280", "gy-bme280": "bme280",
    "dht22": "dht22", "dht11": "dht22", "am2302": "dht22",
    "hcsr04": "hcsr04", "hc-sr04": "hcsr04", "sr04": "hcsr04",
    "ds18b20": "ds18b20", "bh1750": "bh1750", "pir": "pir",
    "mpu6050": "mpu6050", "ssd1306": "ssd1306", "ina219": "ina219",
}

GPIO_POOL = [17, 27, 22, 23, 24, 25, 5, 6, 12, 13, 16, 19, 20, 21, 26]


def normalise_sensor_type(model: str) -> str:
    key = (model or "").strip().lower()
    if key in SENSOR_ALIASES:
        return SENSOR_ALIASES[key]
    for alias, target in SENSOR_ALIASES.items():
        if alias and alias in key:
            return target
    return "gpio_3pin"


def build_ir(site_id: str, site_name: str, sensors: list[dict[str, Any]]) -> dict:
    """Deterministic wiring IR for a site's sensors.

    Each sensor gets a distinct BCM GPIO; power/ground are shared rails. This is
    the fallback path -- when the caller supplies LLM-extracted IR it is used as-is.
    """
    comps: list[dict] = []
    conns: list[dict] = []
    passives: list[dict] = []

    for i, s in enumerate(sensors):
        cid = f"S{i + 1}"
        raw_model = (s.get("model") or "").strip()
        stype = normalise_sensor_type(raw_model)
        name = (s.get("name") or raw_model or stype).strip()
        # Rail vocabulary must match wiring_gen/src/pi_hardware.py RAIL_PINS.
        vcc = "3V3" if stype in {"bme280", "bh1750", "dht22", "ds18b20", "ssd1306",
                                  "mpu6050", "ina219", "gpio_3pin"} else "5V"
        bcm = GPIO_POOL[i] if i < len(GPIO_POOL) else None

        comps.append({
            "id": cid, "type": stype, "name": name,
            "interface": "I2C" if stype in {"bme280", "bh1750", "ssd1306", "ina219"} else "GPIO",
            "i2c_address": None, "vcc": vcc, "logic_level": vcc,
            "pins": s.get("pins"),
            "evidence": s.get("evidence") or f"user-entered sensor model: {raw_model or stype}",
            "notes": s.get("notes") or "",
        })

        for pin, rail, func in (("VCC", vcc, "POWER"), ("GND", "GND", "GROUND")):
            conns.append({
                "component": cid, "component_pin": pin, "bcm": None,
                "power_rail": rail, "function": func, "direction": None,
                "evidence": f"{name} {pin} -> {rail}",
            })

        if stype in {"bme280", "bh1750", "ssd1306", "ina219"}:
            for pin, bcm_pin, fn in (("SDA", 2, "I2C_SDA"), ("SCL", 3, "I2C_SCL")):
                conns.append({
                    "component": cid, "component_pin": pin, "bcm": bcm_pin,
                    "power_rail": None, "function": fn, "direction": "bidirectional",
                    "evidence": f"I2C bus: {pin} -> GPIO{bcm_pin}",
                })
        else:
            conns.append({
                "component": cid, "component_pin": "SIG", "bcm": bcm,
                "power_rail": None, "function": "GPIO_IN", "direction": "in",
                "evidence": f"signal line -> GPIO{bcm}",
            })
            if vcc == "5V":
                # 5V sensor into a 3.3V-only GPIO: pull up to 3.3V, not 5V.
                passives.append({
                    "component": cid, "on_pin": "SIG", "kind": "resistor",
                    "value": "10kΩ",
                    "purpose": "SIG pull-up to 3.3V (Pi GPIO is 3.3V-only)",
                })

    return {
        "board": "raspberry_pi_zero_2w",
        "project_title": f"{site_name}（{site_id}）硬體安裝",
        "source_file": "app: site + sensors",
        "components": comps,
        "connections": conns,
        "passives": passives,
        "warnings_from_code": [
            w for w in [
                "5V 供電感測器的 SIG 不可直接接 GPIO，需 10kΩ 上拉至 3.3V 做電位隔離"
                if any(c["vcc"] == "5V" for c in comps) else None,
                "Pi Zero 2 W GPIO 僅耐 3.3V，勿將 5V 直接接入",
            ] if w
        ],
        "confidence": 0.85,
    }


# ---------------------------------------------------------------------------
# Fritzing render
# ---------------------------------------------------------------------------

def _run(cmd: list[str], cwd: str | None = None, env: dict | None = None,
         timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True,
                          text=True, timeout=timeout)


def render_diagram(ir: dict, out_dir: Path, stem: str = "wiring") -> dict:
    """Render one wiring IR to Fritzing SVG + PNG. Returns artifact paths."""
    exe = find_fritzing()
    if not exe:
        raise RuntimeError(
            "Fritzing not found. Set FRITZING_BIN or run wiring_gen/setup_fritzing.sh")

    sys.path.insert(0, str(WIRING_GEN_DIR / "src"))
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. deterministic .fzz writer
    from ir_to_fritzing import build_fzz  # type: ignore
    import fritzing_export as fx           # type: ignore

    fzz = out_dir / f"{stem}.fzz"
    info = build_fzz(ir, str(fzz), stem)

    # 2. Fritzing headless export (-svg takes ONE folder argument)
    workdir = out_dir / f"_fritzing_run_{stem}"
    if workdir.exists():
        shutil.rmtree(workdir)
    svgs = fx.export_svg(str(fzz), str(workdir))

    # 3. Move the artifacts out before deleting workdir -- svg_to_png reads them
    #    in place, and the alpha channel must be flattened for phone screens.
    kept = []
    for s in svgs:
        dest = out_dir / os.path.basename(s)
        shutil.copyfile(s, dest)
        kept.append(str(dest))
    shutil.rmtree(workdir, ignore_errors=True)

    bb = next((s for s in kept if s.endswith("_breadboard.svg")), kept[0])
    png = out_dir / f"{stem}_breadboard.png"
    r = _run(["rsvg-convert", "-w", "1600", "--background-color=white", bb,
              "-o", str(png)])
    if r.returncode != 0:
        raise RuntimeError(f"PNG conversion failed: {r.stderr[-400:]}")

    return {
        "fzz": str(fzz),
        "svg": bb,
        "png": str(png),
        "parts": info.get("parts"),
        "zero_source": info.get("zero_source"),
    }


# ---------------------------------------------------------------------------
# Public entry point used by the FastAPI endpoint
# ---------------------------------------------------------------------------

def generate(site_id: str, site_name: str, sensors: list[dict[str, Any]],
             use_llm: bool = True, llm_ir: dict | None = None) -> dict:
    """Produce the full artifact set for one site."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex[:10]
    out_dir = CACHE_DIR / run_id

    ir = llm_ir if (use_llm and llm_ir) else build_ir(site_id, site_name, sensors)
    art = render_diagram(ir, out_dir, stem=f"{_safe(site_id)}_{run_id}")

    table = _wiring_table(ir)
    art["ir"] = ir
    art["wiring_table"] = table
    art["run_id"] = run_id
    with open(out_dir / f"{_safe(site_id)}_{run_id}.wiring.json", "w",
              encoding="utf-8") as f:
        json.dump(ir, f, ensure_ascii=False, indent=2)
    return art


def _safe(s: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in s)[:40] or "site"


def _wiring_table(ir: dict) -> list[dict]:
    """Flatten the IR into rows the App can render directly."""
    comps = {c["id"]: c for c in ir["components"]}
    rows = []
    for c in ir["connections"]:
        comp = comps.get(c["component"], {})
        rows.append({
            "sensor": comp.get("name", c["component"]),
            "sensor_pin": c["component_pin"],
            "pi_pin": c.get("power_rail") or (f"GPIO{c['bcm']}" if c.get("bcm") else "-"),
            "function": c.get("function", ""),
        })
    return rows
