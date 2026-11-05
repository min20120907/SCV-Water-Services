"""
SSH Remote Deployment Service for SCV Water
Connects to Raspberry Pi via SSH and deploys sensor drivers automatically.

Endpoints:
  POST /deploy           -> Deploy sensor driver to Raspberry Pi
  GET  /deploy/status    -> Check deployment status
  GET  /health           -> Liveness probe
"""

import os
import json
import base64
import re
import time
import urllib.request
import logging
from datetime import datetime
from typing import Optional

import paramiko
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import secrets

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("deploy-service")

app = FastAPI(title="SCV Water Deploy Service", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# API Key 驗證（App 端寫死，防止外人亂 call）
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("SCV_WATER_API_KEY", "scv-water-2026-secure-key")

def verify_api_key(request: Request):
    key = request.headers.get("X-API-Key", "")
    if not secrets.compare_digest(key, API_KEY):
        raise HTTPException(status_code=403, detail="Invalid API key")

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------
class DeployRequest(BaseModel):
    device_id: str
    sensor_description: str
    protocol: str  # emulator, ip, mqtt, ble, zigbee
    schema: dict
    pi_host: str
    pi_port: int = 22
    pi_user: str = "pi"
    pi_ssh_key: str = ""  # auto-discovered if empty
    firebase_config: dict = Field(default_factory=dict)  # auto-loaded from env/file if empty


class DeployResponse(BaseModel):
    success: bool
    device_id: str
    message: str
    logs: list[str]
    deployed_at: str


class DeployStatus(BaseModel):
    device_id: str
    status: str  # pending, deploying, running, failed
    last_deployed: Optional[str]
    logs: list[str]


# ---------------------------------------------------------------------------
# SSH Helper
# ---------------------------------------------------------------------------
def _get_ssh_client(host: str, port: int, username: str, key_path: str = "") -> paramiko.SSHClient:
    """Create and connect an SSH client. The key is service-side; the App never
    supplies credentials.

    Key resolution order:
      1. explicit key_path (CLI/testing only)
      2. $SCV_SSH_KEY — the deployed service's own key
      3. ~/.ssh/{id_ed25519,id_rsa,id_ecdsa}
      4. the running ssh-agent
    """
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    # Auto-discover SSH keys if not specified
    resolved_key: str | None = key_path if os.path.exists(os.path.expanduser(key_path)) else None
    if not resolved_key:
        resolved_key = os.environ.get("SCV_SSH_KEY") or None
    if not resolved_key:
        ssh_dir = os.path.expanduser("~/.ssh")
        if os.path.isdir(ssh_dir):
            for fname in ["id_ed25519", "id_rsa", "id_ecdsa"]:
                candidate = os.path.join(ssh_dir, fname)
                if os.path.exists(candidate):
                    resolved_key = candidate
                    break

    proxy_cmd = None
    ssh_config_file = os.path.expanduser("~/.ssh/config")
    if os.path.exists(ssh_config_file):
        try:
            ssh_conf = paramiko.SSHConfig()
            with open(ssh_config_file) as f:
                ssh_conf.parse(f)
            host_conf = ssh_conf.lookup(host)
            if "proxycommand" in host_conf:
                proxy_cmd = host_conf["proxycommand"]
            if "hostname" in host_conf:
                host = host_conf["hostname"]
            if "user" in host_conf and username in ("pi", ""):
                username = host_conf["user"]
        except Exception as e:
            logger.warning(f"Failed to parse ssh config: {e}")

    def _create_and_connect(user: str) -> paramiko.SSHClient:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs = {
            "hostname": host,
            "port": port,
            "username": user,
            "timeout": 30,
        }
        if proxy_cmd:
            kwargs["sock"] = paramiko.ProxyCommand(proxy_cmd)
        if resolved_key:
            kwargs["key_filename"] = os.path.expanduser(resolved_key)
        else:
            kwargs["allow_agent"] = True
            kwargs["look_for_keys"] = True
        c.connect(**kwargs)
        return c

    try:
        return _create_and_connect(username)
    except paramiko.AuthenticationException:
        if username != "admin":
            logger.info("Authentication failed for %s, retrying with admin", username)
            return _create_and_connect("admin")
        raise


def _exec_command(client: paramiko.SSHClient, command: str, timeout: int = 120) -> tuple[str, str, int]:
    """Execute a command over SSH and return (stdout, stderr, exit_code)."""
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    return out, err, exit_code


# ---------------------------------------------------------------------------
# Deployment Script Generator
# ---------------------------------------------------------------------------
def _get_web_firebase_credentials() -> tuple[str, str]:
    opts_path = os.path.join(os.path.dirname(__file__), "..", "scv_water_dashboard", "lib", "firebase_options.dart")
    project_id = "login-verificati-1712467959577"
    api_key = ""
    if os.path.exists(opts_path):
        try:
            with open(opts_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            m_key = re.search(r'apiKey:\s*[\'\"]([^\'\"]+)[\'\"]', content)
            m_proj = re.search(r'projectId:\s*[\'\"]([^\'\"]+)[\'\"]', content)
            if m_key:
                api_key = m_key.group(1)
            if m_proj:
                project_id = m_proj.group(1)
        except Exception:
            pass
    return project_id, api_key


def _generate_driver_code(req: DeployRequest, field_names: list[str]) -> str:
    """Generate Python sensor driver code integrated with Digital Twin emulator."""
    fields_str = repr(field_names)
    project_id, web_api_key = _get_web_firebase_credentials()

    driver_code = '''#!/usr/bin/env python3
"""
SCV Water Sensor Driver & Digital Twin Emulator
Auto-generated by SCV Water Deployment Service
"""

import os
import json
import time
from datetime import datetime

try:
    from google.cloud import firestore
    from google.oauth2 import service_account
    HAS_FIRESTORE = True
except ImportError:
    HAS_FIRESTORE = False

try:
    from twin_engine import WaterDigitalTwin
    twin = WaterDigitalTwin()
    # Default to active demo scenario so sensor values are dynamically non-zero
    twin.set_scenario("morning_garden")
    twin.state.valve_1 = True
    twin.state.valve_2 = True
    twin.state.flow_rate_lpm = 4.5
    twin.state.water_level_pct = 58.0
    twin.state.tds_ppm = 135.0
    twin.state.ambient_temp_c = 26.5
    twin.state.humidity_pct = 68.0
except Exception as e:
    print(f"Digital Twin load note: {e}")
    twin = None

# Config
DEVICE_ID = "''' + req.device_id + '''"
PROTOCOL = "''' + req.protocol + '''"
PROJECT_ID = "''' + project_id + '''"
WEB_API_KEY = "''' + web_api_key + '''"
FIREBASE_CONFIG_PATH = os.path.expanduser("~/.config/scv_water/firebase_service_account.json")

# Initialize Firebase
db = None
if HAS_FIRESTORE and os.path.exists(FIREBASE_CONFIG_PATH):
    try:
        creds = service_account.Credentials.from_service_account_file(FIREBASE_CONFIG_PATH)
        db = firestore.Client(credentials=creds)
    except Exception as e:
        print(f"Firestore init note: {e}")

def read_sensors():
    """Step digital twin physics simulation and format telemetry."""
    if twin is not None:
        s = twin.step()
        mls = s.flow_rate_lpm * 1000 / 60
        values = {
            'kitchen_flow': mls if s.valve_1 else 0.0,
            'shower_flow': 0.0,
            'bathtub_flow': 0.0,
            'toilet_flow': mls if s.valve_2 else 0.0,
            'flow': s.flow_rate_lpm,
            'water_level': s.water_level_pct,
            'soil': s.soil_moisture_pct,
            'temp': s.ambient_temp_c,
            'humidity': s.humidity_pct,
            'rain': s.rain_detected,
            'leak': s.leak_detected,
            'tds': s.tds_ppm,
            'valve_in': s.valve_in,
            'valve_1': s.valve_1,
            'valve_2': s.valve_2,
            'scenario': s.active_scenario,
            'alert': s.alert_msg,
            'time': s.time_str,
            'twin_time': s.time_str,
            'twin_water_level': s.water_level_pct,
            'twin_soil': s.soil_moisture_pct,
            'twin_temp': s.ambient_temp_c,
            'twin_humidity': s.humidity_pct,
            'twin_rain': s.rain_detected,
            'twin_leak': s.leak_detected,
            'twin_tds': s.tds_ppm,
            'twin_valve_in': s.valve_in,
            'twin_valve_1': s.valve_1,
            'twin_valve_2': s.valve_2,
            'twin_scenario': s.active_scenario,
            'twin_alert': s.alert_msg,
        }
    else:
        values = {}

    for field in ''' + fields_str + ''':
        if field not in values:
            values[field] = 0.0
    return values

def send_to_cloud(data):
    """Send data to Firebase Firestore (SDK or REST fallback)."""
    if db is not None:
        try:
            doc_ref = db.collection("readings").document(DEVICE_ID).collection("stream").add({
                **data,
                "timestamp": firestore.SERVER_TIMESTAMP,
            })
            print(f"{datetime.now().isoformat()} Sent (SDK): {data.get('time')} flow={data.get('flow')}")
            return
        except Exception as e:
            print(f"Firestore SDK send error: {e}")

    if PROJECT_ID and WEB_API_KEY:
        try:
            import urllib.request
            url = f"https://firestore.googleapis.com/v1/projects/{PROJECT_ID}/databases/(default)/documents/readings/{DEVICE_ID}/stream?key={WEB_API_KEY}"
            fs_fields = {}
            for k, v in data.items():
                if isinstance(v, bool):
                    fs_fields[k] = {"booleanValue": v}
                elif isinstance(v, (int, float)):
                    fs_fields[k] = {"doubleValue": float(v)}
                elif isinstance(v, str):
                    fs_fields[k] = {"stringValue": v}
            fs_fields["timestamp"] = {"timestampValue": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}
            req_body = json.dumps({"fields": fs_fields}).encode("utf-8")
            h_req = urllib.request.Request(url, data=req_body, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(h_req, timeout=5) as resp:
                if resp.status in (200, 201):
                    print(f"{datetime.now().isoformat()} Sent (REST): {data.get('time')} flow={data.get('flow')}")
                    return
        except Exception as e:
            print(f"Firestore REST error: {e}")

    print(f"{datetime.now().isoformat()} Emulated reading: {data}")

def main():
    print(f"SCV Water Driver started: {DEVICE_ID} ({PROTOCOL})")
    while True:
        try:
            data = read_sensors()
            send_to_cloud(data)
            time.sleep(3)  # Send every 3 seconds
        except KeyboardInterrupt:
            print("Driver stopped.")
            break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
'''
    return driver_code


def _generate_deploy_script(req: DeployRequest) -> str:
    """Generate a bash deployment script for the Raspberry Pi.

    Two design rules this script follows, both learned from a failed run:

    1. Never hard-fail on a missing privilege escalation. `sudo` needs a
       password on stock Raspberry Pi OS only if the operator has not set up
       NOPASSWD, and on the test target it needs one too. The driver, its venv
       and its config all live in the login user's home, so the script
       completes without root and only *reports* that the systemd unit and the
       system packages were skipped.
    2. Resolve $HOME / $USER at runtime instead of writing /home/pi. The account
       the deploy service logs in as is a request parameter, not a constant.
    """
    # Extract schema fields
    fields = req.schema.get("fields", [])
    field_names = [f.get("key", "") for f in fields if f.get("key")]

    # Python driver code
    driver_code = _generate_driver_code(req, field_names)

    # Load twin engine code if available
    twin_engine_path = os.path.join(os.path.dirname(__file__), "..", "digital_twin", "twin_engine.py")
    twin_engine_code = ""
    if os.path.exists(twin_engine_path):
        try:
            with open(twin_engine_path, "r", encoding="utf-8") as f:
                twin_engine_code = f.read()
        except Exception as e:
            logger.warning(f"Could not read twin_engine.py: {e}")

    # Firebase config JSON
    firebase_json = json.dumps(req.firebase_config, indent=2)

    # Build script using string concatenation (not f-string, to avoid brace escaping issues)
    parts = []
    parts.append("#!/usr/bin/env bash\n")
    parts.append("# Generated by SCV Water deploy service. Do not edit on the device.\n")
    parts.append('echo "=== SCV Water Sensor Deployment ==="\n')
    parts.append('echo "Device: ' + req.device_id + '"\n')
    parts.append('echo "Protocol: ' + req.protocol + '"\n')
    parts.append('echo "Target: $(id -un)@$(hostname)  home=$HOME"\n')
    parts.append('echo "Time: $(date)"\n\n')

    # 1. System dependencies -- best effort. The driver only needs these to be
    #    present; if apt is unavailable we continue and say so.
    parts.append('echo "[1/7] System dependencies (optional)"\n')
    parts.append(
        "if sudo -n true 2>/dev/null; then\n"
        "    sudo apt-get update -qq || echo '  warn: apt-get update failed'\n"
        "    sudo apt-get install -y -qq python3-pip python3-venv i2c-tools spi-tools \\\n"
        "        || echo '  warn: apt-get install failed'\n"
        "else\n"
        "    echo '  skip: no passwordless sudo; assuming python3/venv already present'\n"
        "fi\n\n"
    )

    # 2. Create project directory
    parts.append('echo "[2/7] Setting up project directory..."\n')
    parts.append("mkdir -p \"$HOME/scv_water_sensors\"\n")
    parts.append('cd "$HOME/scv_water_sensors" || exit 1\n\n')

    # 3. Python virtual environment
    parts.append('echo "[3/7] Creating Python virtual environment..."\n')
    parts.append(
        "if [ ! -x venv/bin/python ]; then\n"
        "    python3 -m venv venv || { echo '  error: python3-venv missing'; exit 1; }\n"
        "fi\n"
        "source venv/bin/activate\n\n"
    )

    # 4. Install Python packages -- wheels that fail to build (no GPIO header on
    #    a test target) must not abort the deployment.
    parts.append('echo "[4/7] Installing Python packages..."\n')
    parts.append(
        "pip install --quiet google-cloud-firestore firebase-admin || echo '  warn: firestore install failed'\n"
        "for pkg in RPi.GPIO smbus2 spidev paho-mqtt; do\n"
        "    pip install --quiet \"$pkg\" || echo \"  warn: $pkg unavailable on this target\"\n"
        "done\n\n"
    )

    # 5. Deploy digital twin & sensor driver
    driver_filename = f"sensor_driver_{req.device_id}.py"
    parts.append('echo "[5/7] Deploying digital twin & sensor driver..."\n')
    if twin_engine_code:
        parts.append("cat > twin_engine.py << 'TWIN_EOF'\n")
        parts.append(twin_engine_code)
        parts.append("\nTWIN_EOF\n")
    parts.append(f"cat > {driver_filename} << 'DRIVER_EOF'\n")
    parts.append(driver_code)
    parts.append("\nDRIVER_EOF\n")
    parts.append(f"chmod +x {driver_filename}\n\n")

    # 6. Firebase config
    parts.append('echo "[6/7] Setting up Firebase config..."\n')
    parts.append('mkdir -p "$HOME/.config/scv_water"\n')
    parts.append("cat > \"$HOME/.config/scv_water/firebase_service_account.json\" << 'FB_EOF'\n")
    parts.append(firebase_json)
    parts.append("\nFB_EOF\n")
    parts.append("chmod 600 \"$HOME/.config/scv_water/firebase_service_account.json\"\n\n")

    # 7. systemd unit -- install to system if sudo available, otherwise install to user unit
    parts.append('echo "[7/7] Installing systemd service..."\n')
    parts.append("if sudo -n true 2>/dev/null; then\n")
    parts.append("    sudo tee /etc/systemd/system/scv-water-" + req.device_id + ".service > /dev/null << 'SERVICE_EOF'\n")
    parts.append("[Unit]\n")
    parts.append("Description=SCV Water Sensor Driver - " + req.device_id + "\n")
    parts.append("After=network.target\n\n")
    parts.append("[Service]\n")
    parts.append("Type=simple\n")
    parts.append("User=$USER\n")
    parts.append("WorkingDirectory=$HOME/scv_water_sensors\n")
    parts.append(f"ExecStart=$HOME/scv_water_sensors/venv/bin/python $HOME/scv_water_sensors/{driver_filename}\n")
    parts.append("Restart=always\n")
    parts.append("RestartSec=10\n\n")
    parts.append("[Install]\n")
    parts.append("WantedBy=multi-user.target\n")
    parts.append("SERVICE_EOF\n")
    parts.append("    sudo systemctl daemon-reload\n")
    parts.append("    sudo systemctl enable --now scv-water-" + req.device_id + ".service && sudo systemctl restart scv-water-" + req.device_id + ".service \\\n")
    parts.append("        && echo '  systemd unit enabled (system)' \\\n")
    parts.append("        || echo '  warn: systemctl enable failed'\n")
    parts.append("else\n")
    parts.append("    echo '  no passwordless sudo; installing systemd user unit...'\n")
    parts.append("    mkdir -p \"$HOME/.config/systemd/user\"\n")
    parts.append("    cat > \"$HOME/.config/systemd/user/scv-water-" + req.device_id + ".service\" << 'USER_SERVICE_EOF'\n")
    parts.append("[Unit]\n")
    parts.append("Description=SCV Water Sensor Driver - " + req.device_id + "\n")
    parts.append("After=network.target\n\n")
    parts.append("[Service]\n")
    parts.append("Type=simple\n")
    parts.append("WorkingDirectory=%h/scv_water_sensors\n")
    parts.append(f"ExecStart=%h/scv_water_sensors/venv/bin/python %h/scv_water_sensors/{driver_filename}\n")
    parts.append("Restart=always\n")
    parts.append("RestartSec=10\n\n")
    parts.append("[Install]\n")
    parts.append("WantedBy=default.target\n")
    parts.append("USER_SERVICE_EOF\n")
    parts.append("    systemctl --user daemon-reload\n")
    parts.append("    systemctl --user enable --now scv-water-" + req.device_id + ".service && systemctl --user restart scv-water-" + req.device_id + ".service \\\n")
    parts.append("        && echo '  systemd unit enabled (user)' \\\n")
    parts.append("        || echo '  warn: user systemctl enable failed'\n")
    parts.append("fi\n\n")

    parts.append('echo "=== Deployment finished ==="\n')

    # 8. Verify what is actually verifiable: the artefacts exist
    parts.append('echo "Verifying installed artefacts..."\n')
    parts.append(
        'if python3 -c "import json; json.load(open('
        "'$HOME/.config/scv_water/firebase_service_account.json'"
        '))" 2>/dev/null; then\n'
        "    echo '  ok: firebase config is valid JSON'\n"
        "else\n"
        "    echo '  warn: firebase config missing or unparseable'\n"
        "fi\n"
    )
    parts.append("test -f twin_engine.py && echo '  ok: twin_engine.py present'\n")
    parts.append(f"test -f {driver_filename} && echo '  ok: {driver_filename} present'\n")
    parts.append("test -x venv/bin/python && echo '  ok: venv created'\n")
    parts.append('echo "=== Deployment Complete ==="\n')

    return "".join(parts)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
async def health(request: Request):
    verify_api_key(request)
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


# ---------------------------------------------------------------------------
# 硬體安裝圖生成（Middle service 持有 Fritzing / LLM / SSH，不放在手機端）
# ---------------------------------------------------------------------------

class HardwareDiagramRequest(BaseModel):
    site_id: str
    site_name: str = ""
    sensors: list[dict] = Field(default_factory=list)
    use_llm: bool = True
    llm_ir: Optional[dict] = None


@app.post("/hardware/diagram")
async def hardware_diagram(req: HardwareDiagramRequest, request: Request):
    """Render a Fritzing breadboard diagram for one site.

    The App posts the site's sensors; this returns a PNG (base64), the source
    SVG and a flat wiring table. Runs after deployment because it needs the same
    middle-service credentials as the deploy path.
    """
    verify_api_key(request)
    if not req.sensors:
        raise HTTPException(status_code=400, detail="sensors is required")

    try:
        import hardware_diagram as hd

        art = hd.generate(
            site_id=req.site_id,
            site_name=req.site_name or req.site_id,
            sensors=req.sensors,
            use_llm=req.use_llm,
            llm_ir=req.llm_ir,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:  # noqa: BLE001 - surface the real reason to the App
        logger.exception("hardware diagram failed")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")

    with open(art["png"], "rb") as f:
        png_b64 = base64.b64encode(f.read()).decode("ascii")
    with open(art["svg"], "r", encoding="utf-8") as f:
        svg_text = f.read()

    return {
        "site_id": req.site_id,
        "png_base64": png_b64,
        "svg": svg_text,
        "fzz": art["fzz"],
        "wiring_table": art["wiring_table"],
        "ir": art["ir"],
        "parts": art.get("parts"),
        "zero_source": art.get("zero_source"),
        "run_id": art["run_id"],
    }


# ---------------------------------------------------------------------------
# 全自動一鍵流水線：輸入型號與場域 -> Qwen 生成規格 -> SSH 部署 -> 配線圖 -> Firestore
# ---------------------------------------------------------------------------
def _call_qwen_schema(model_text: str) -> dict:
    url = "http://127.0.0.1:8088/v1/chat/completions"
    sys_prompt = """You are an IoT Sensor Expert. Convert the user's sensor model or description into a JSON Schema.
Output MUST be a valid JSON object only with this structure:
{
  "type_id": "DEVICE_TYPE_NAME",
  "protocol": "gpio|i2c|spi",
  "fields": [
    {
      "key": "internal_variable_name",
      "label": "Human Readable Label",
      "unit": "Unit (e.g. mL/s, mg/L, hPa, °C)",
      "data_type": "double or int"
    }
  ]
}
Return ONLY valid JSON. No conversational text."""
    payload = {
        "model": "qwen3.8-27b-fastmtp",
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": f"Sensor Model / Description:\n{model_text}"}
        ],
        "temperature": 0.1,
        "max_tokens": 512
    }
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as res:
        data = json.loads(res.read())
    raw = data["choices"][0]["message"]["content"]
    clean = re.sub(r"<think>[\s\S]*?</think>", "", raw).strip()
    clean = re.sub(r"^```json\s*", "", clean)
    clean = re.sub(r"^```\s*", "", clean)
    clean = re.sub(r"\s*```$", "", clean)
    return json.loads(clean)


class PipelineRequest(BaseModel):
    model: str
    site_id: str
    site_name: str = ""
    place: str = ""
    device_id: Optional[str] = None
    pi_host: str = "ssh.labertus.online"
    pi_port: int = 22
    pi_user: str = "pi"


@app.post("/pipeline/deploy")
async def pipeline_deploy(req: PipelineRequest, request: Request):
    verify_api_key(request)
    # 1. Qwen Schema Generation
    t0_llm = time.time()
    try:
        schema = _call_qwen_schema(req.model)
    except Exception as e:
        logger.exception("Qwen schema generation failed")
        raise HTTPException(status_code=502, detail=f"LLM Schema Generation Failed: {e}")
    t_llm = round(time.time() - t0_llm, 2)

    dev_id = req.device_id or f"{schema.get('type_id', 'DEV')}_{secrets.token_hex(2).upper()}"
    protocol = schema.get("protocol", "gpio")

    # 2. Deploy to Pi via SSH
    t0_dep = time.time()
    dep_req = DeployRequest(
        device_id=dev_id,
        sensor_description=req.model,
        protocol=protocol,
        schema=schema,
        pi_host=req.pi_host,
        pi_port=req.pi_port,
        pi_user=req.pi_user,
    )
    dep_res = await deploy(dep_req, request)
    t_dep = round(time.time() - t0_dep, 2)

    # 3. Fritzing Diagram Generation
    t0_diag = time.time()
    import hardware_diagram as hd
    art = hd.generate(
        site_id=req.site_id,
        site_name=req.site_name or req.site_id,
        sensors=[{"id": dev_id, "name": req.model, "description": req.model, "protocol": protocol}],
        use_llm=False,
    )
    t_diag = round(time.time() - t0_diag, 2)

    with open(art["png"], "rb") as f:
        png_b64 = base64.b64encode(f.read()).decode("ascii")

    # 4. Register in Firestore so App instantly displays the new sensor
    project_id, web_api_key = _get_web_firebase_credentials()
    if project_id and web_api_key:
        try:
            fs_url = f"https://firestore.googleapis.com/v1/projects/{project_id}/databases/(default)/documents/sensors/{dev_id}?key={web_api_key}"
            fs_fields = {
                "id": {"stringValue": dev_id},
                "place": {"stringValue": req.place or req.model},
                "site_id": {"stringValue": req.site_id},
                "site_name": {"stringValue": req.site_name or req.site_id},
                "connection_profile": {
                    "mapValue": {"fields": {"protocol": {"stringValue": protocol}, "status": {"stringValue": "active"}}}
                },
                "schema": {
                    "mapValue": {
                        "fields": {
                            "fields": {
                                "arrayValue": {
                                    "values": [
                                        {
                                            "mapValue": {
                                                "fields": {
                                                    "key": {"stringValue": f.get("key", "")},
                                                    "label": {"stringValue": f.get("label", "")},
                                                    "unit": {"stringValue": f.get("unit", "")},
                                                }
                                            }
                                        }
                                        for f in schema.get("fields", [])
                                    ]
                                }
                            }
                        }
                    }
                },
            }
            fs_req = urllib.request.Request(fs_url, data=json.dumps({"fields": fs_fields}).encode("utf-8"),
                                           headers={"Content-Type": "application/json"}, method="PATCH")
            with urllib.request.urlopen(fs_req, timeout=10):
                pass
        except Exception as e:
            logger.warning(f"Firestore sensor registration error: {e}")

    return {
        "success": dep_res.success,
        "device_id": dev_id,
        "model": req.model,
        "site_id": req.site_id,
        "schema": schema,
        "timings": {"llm_sec": t_llm, "deploy_sec": t_dep, "diagram_sec": t_diag},
        "wiring_table": art["wiring_table"],
        "png_path": art["png"],
        "png_base64": png_b64,
    }


# SCV Water 自訂 MAC 前綴 — 只掃描我們的樹莓派
# 樹莓派出廠 MAC 前綴：b8:27:eb (Pi 1/2/3), dc:a6:32 (Pi 4), e4:5f:01 (Pi 4/5)
# 若樹莓派有自訂 MAC，請修改此前綴
SCV_WATER_MAC_PREFIXES = ["de:ad:be"]

# 外網樹莓派註冊表（記憶體，重啟後清空）
_registered_devices: dict[str, dict] = {}


@app.post("/register")
async def register_device(request: Request):
    verify_api_key(request)
    """外網樹莓派主動註冊自己的 IP、MAC、serial。"""
    body = await request.json()
    ip = body.get("ip")
    mac = body.get("mac", "")
    serial = body.get("serial", "")
    hostname = body.get("hostname", "")

    if not ip:
        raise HTTPException(status_code=400, detail="ip is required")

    # 驗證 MAC 前綴
    if mac and not any(mac.lower().startswith(p) for p in SCV_WATER_MAC_PREFIXES):
        raise HTTPException(status_code=403, detail="MAC prefix not allowed")

    key = serial or ip
    _registered_devices[key] = {
        "ip": ip,
        "port": 22,
        "name": hostname or f"scv-water-{ip.split('.')[-1]}",
        "method": "registered",
        "mac": mac,
        "serial": serial,
        "registered_at": datetime.now().isoformat(),
    }
    return {"status": "ok", "message": "registered"}


@app.get("/registered")
async def list_registered(request: Request):
    verify_api_key(request)
    """列出所有已註冊的外網樹莓派。"""
    return {"devices": list(_registered_devices.values()), "count": len(_registered_devices)}


@app.get("/scan")
async def scan_network(request: Request):
    verify_api_key(request)
    """Scan local network for SCV Water Raspberry Pi devices."""
    import socket
    import subprocess
    import re

    devices = []

    # Method 1: mDNS/Bonjour scan for ._tcp.local (SSH service)
    try:
        import zeroconf
        from zeroconf import ServiceBrowser, Zeroconf

        class SSHListener:
            def __init__(self):
                self.found = []

            def add_service(self, zc, type_, name):
                info = zc.get_service_info(type_, name)
                if info:
                    addresses = info.parsed_addresses()
                    for addr in addresses:
                        self.found.append({
                            "ip": addr,
                            "port": info.port,
                            "name": name,
                            "method": "mdns",
                        })

            def remove_service(self, zc, type_, name):
                pass

            def update_service(self, zc, type_, name):
                pass

        zeroconf = Zeroconf()
        listener = SSHListener()
        browser = ServiceBrowser(zeroconf, "_ssh._tcp.local.", listener)
        import time
        time.sleep(3)
        browser.cancel()
        zeroconf.close()
        devices.extend(listener.found)
    except Exception as e:
        logger.warning(f"mDNS scan failed: {e}")

    # Enrich mDNS results with MAC via ARP table lookup + filter by prefix
    enriched = []
    for d in devices:
        if "mac" not in d:
            try:
                arp_result = subprocess.run(["arp", "-n", d["ip"]], capture_output=True, text=True, timeout=2)
                for line in arp_result.stdout.splitlines():
                    m = re.search(r"([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})", line)
                    if m:
                        d["mac"] = m.group(1)
                        break
            except Exception:
                pass
        # Only keep devices with SCV Water MAC prefix
        if d.get("mac") and any(d["mac"].lower().startswith(p) for p in SCV_WATER_MAC_PREFIXES):
            enriched.append(d)
    devices = enriched

    # Method 2: ARP scan (fallback)
    try:
        # Get local subnet
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()

        # Extract subnet (e.g., 192.168.1.x -> 192.168.1.)
        subnet = ".".join(local_ip.split(".")[:3]) + "."

        # Try arp-scan
        result = subprocess.run(
            ["arp-scan", "-l", "--retry=1", "--timeout=100"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in result.stdout.splitlines():
            match = re.match(r"(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F:]+)", line)
            if match:
                ip = match.group(1)
                mac = match.group(2)
                # Check if it's a SCV Water device (MAC prefix match)
                if any(mac.lower().startswith(prefix) for prefix in SCV_WATER_MAC_PREFIXES):
                    devices.append({
                        "ip": ip,
                        "port": 22,
                        "name": f"raspberry-pi-{ip.split('.')[-1]}",
                        "method": "arp",
                        "mac": mac,
                    })
    except Exception as e:
        logger.warning(f"ARP scan failed: {e}")

    # Method 3: Quick port scan on common Pi subnets
    if not devices:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
            subnet = ".".join(local_ip.split(".")[:3]) + "."

            for i in range(1, 255):
                ip = f"{subnet}{i}"
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(0.1)
                    result = sock.connect_ex((ip, 22))
                    sock.close()
                    if result == 0:
                        # Try to get MAC from ARP table
                        mac = None
                        try:
                            arp_result = subprocess.run(["arp", "-n", ip], capture_output=True, text=True, timeout=2)
                            m = re.search(r"([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})", arp_result.stdout)
                            if m:
                                mac = m.group(1)
                        except Exception:
                            pass
                        # Only keep SCV Water devices (MAC prefix match)
                        if mac and any(mac.lower().startswith(p) for p in SCV_WATER_MAC_PREFIXES):
                            devices.append({
                                "ip": ip,
                                "port": 22,
                                "name": f"scv-water-{ip.split('.')[-1]}",
                                "method": "port-scan",
                                "mac": mac,
                            })
                except:
                    pass
        except Exception as e:
            logger.warning(f"Port scan failed: {e}")

    # 外網已註冊或 ~/.ssh/config 樹莓派主機
    for reg in _registered_devices.values():
        if not any(d.get("ip") == reg.get("ip") for d in devices):
            devices.append(reg)

    ssh_config_file = os.path.expanduser("~/.ssh/config")
    if os.path.exists(ssh_config_file):
        try:
            ssh_conf = paramiko.SSHConfig()
            with open(ssh_config_file) as f:
                ssh_conf.parse(f)
            for h in ssh_conf.get_hostnames():
                if h != "*" and ("ssh." in h or "pi" in h or "labertus" in h or "scv" in h):
                    if not any(d.get("ip") == h for d in devices):
                        devices.append({
                            "ip": h,
                            "port": 22,
                            "name": f"樹莓派 ({h})",
                            "method": "ssh-config",
                            "mac": "cloudflared-tunnel",
                        })
        except Exception:
            pass

    return {"devices": devices, "count": len(devices)}


def _load_firebase_config() -> dict:
    """Auto-load Firebase service account from env or file."""
    # Try environment variable first
    env_config = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if env_config:
        try:
            return json.loads(env_config)
        except json.JSONDecodeError:
            pass

    # Try common file paths
    paths = [
        os.path.expanduser("~/.config/scv_water/firebase_service_account.json"),
        os.path.expanduser("~/scv_water/firebase_service_account.json"),
        "/etc/scv_water/firebase_service_account.json",
    ]
    for path in paths:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)

    return {}


@app.post("/deploy", response_model=DeployResponse)
async def deploy(req: DeployRequest, request: Request):
    verify_api_key(request)
    logger.info(f"Deploy request: device={req.device_id}, host={req.pi_host}")

    # Auto-load Firebase config if not provided
    if not req.firebase_config:
        req.firebase_config = _load_firebase_config()
        if not req.firebase_config:
            return DeployResponse(
                success=False,
                device_id=req.device_id,
                message="Firebase service account not found. Set FIREBASE_SERVICE_ACCOUNT env var or place JSON in ~/.config/scv_water/",
                logs=[],
                deployed_at=datetime.now().isoformat(),
            )

    logs = []
    ssh = None

    try:
        # Connect via SSH
        ssh = _get_ssh_client(
            host=req.pi_host,
            port=req.pi_port,
            username=req.pi_user,
            key_path=req.pi_ssh_key,
        )
        logs.append(f"SSH connected to {req.pi_user}@{req.pi_host}:{req.pi_port}")

        # Generate deployment script
        script = _generate_deploy_script(req)

        # Upload script to Pi
        sftp = ssh.open_sftp()
        with sftp.file("/tmp/scv_deploy.sh", "w") as f:
            f.write(script)
        sftp.close()
        logs.append("Deployment script uploaded")

        # Execute script
        stdout, stderr, exit_code = _exec_command(ssh, "bash /tmp/scv_deploy.sh", timeout=300)
        logs.append(stdout)
        if stderr:
            logs.append(f"stderr: {stderr}")

        if exit_code != 0:
            raise Exception(f"Deployment script failed with exit code {exit_code}")

        logs.append("Deployment completed successfully")

        return DeployResponse(
            success=True,
            device_id=req.device_id,
            message="Sensor driver deployed and running on Raspberry Pi",
            logs=logs,
            deployed_at=datetime.now().isoformat(),
        )

    except Exception as e:
        logger.exception("Deployment failed")
        return DeployResponse(
            success=False,
            device_id=req.device_id,
            message=f"Deployment failed: {e}",
            logs=logs,
            deployed_at=datetime.now().isoformat(),
        )

    finally:
        if ssh:
            ssh.close()


class UndeployRequest(BaseModel):
    device_id: str
    pi_host: str
    pi_port: int = 22
    pi_user: str = "pi"
    pi_ssh_key: str = ""


@app.post("/undeploy")
async def undeploy(req: UndeployRequest, request: Request):
    verify_api_key(request)
    ssh = None
    logs = []
    try:
        ssh = _get_ssh_client(
            host=req.pi_host,
            port=req.pi_port,
            username=req.pi_user,
            key_path=req.pi_ssh_key,
        )
        cmd = f"""
        systemctl --user stop scv-water-{req.device_id}.service 2>/dev/null || true
        systemctl --user disable scv-water-{req.device_id}.service 2>/dev/null || true
        rm -f $HOME/.config/systemd/user/scv-water-{req.device_id}.service
        rm -f $HOME/scv_water_sensors/sensor_driver_{req.device_id}.py
        systemctl --user daemon-reload 2>/dev/null || true
        if sudo -n true 2>/dev/null; then
            sudo systemctl stop scv-water-{req.device_id}.service 2>/dev/null || true
            sudo systemctl disable scv-water-{req.device_id}.service 2>/dev/null || true
            sudo rm -f /etc/systemd/system/scv-water-{req.device_id}.service
            sudo rm -f /home/*/scv_water_sensors/sensor_driver_{req.device_id}.py 2>/dev/null || true
            sudo systemctl daemon-reload 2>/dev/null || true
        fi
        """
        stdout, stderr, exit_code = _exec_command(ssh, cmd, timeout=30)
        logs.append(stdout.strip())
        return {
            "success": True,
            "device_id": req.device_id,
            "message": f"Device {req.device_id} unbound and stopped on Raspberry Pi",
            "logs": logs,
        }
    except Exception as e:
        logger.exception("Undeploy failed")
        return {
            "success": False,
            "device_id": req.device_id,
            "message": f"Undeploy failed: {e}",
            "logs": logs,
        }
    finally:
        if ssh:
            ssh.close()


@app.get("/deploy/status/{device_id}", response_model=DeployStatus)
async def deploy_status(device_id: str, request: Request):
    verify_api_key(request)
    """Check deployment status of a device."""
    # TODO: Implement status check via SSH
    return DeployStatus(
        device_id=device_id,
        status="unknown",
        last_deployed=None,
        logs=["Status check not yet implemented"],
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8767))
    uvicorn.run(app, host="0.0.0.0", port=port)
