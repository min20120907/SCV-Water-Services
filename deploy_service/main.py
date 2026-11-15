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
    """Generate Python sensor driver code (real hardware only, no simulation)."""
    fields_str = repr(field_names)
    project_id, web_api_key = _get_web_firebase_credentials()

    driver_code = '''#!/usr/bin/env python3
"""
SCV Water Real-Hardware Sensor Driver
Auto-generated by SCV Water Deployment Service (no simulation / no zero-filling)
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

# Config
DEVICE_ID = "''' + req.device_id + '''"
PROTOCOL = "''' + req.protocol + '''"
EXPECTED_FIELDS = ''' + fields_str + '''
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
    """Read real hardware sensor values. Raises if no hardware reader is bound."""
    raise RuntimeError(
        f"No real hardware reader bound for {DEVICE_ID} ({PROTOCOL}); "
        "deploy via /pipeline/research + /pipeline/deploy-draft instead of legacy /deploy"
    )

def send_to_cloud(data):
    """Send data to Firebase Firestore (SDK or REST fallback)."""
    if db is not None:
        try:
            db.collection("readings").document(DEVICE_ID).collection("stream").add({
                **data,
                "timestamp": firestore.SERVER_TIMESTAMP,
            })
            print(f"{datetime.now().isoformat()} Sent (SDK): {data}")
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
                    print(f"{datetime.now().isoformat()} Sent (REST): {data}")
                    return
        except Exception as e:
            print(f"Firestore REST error: {e}")

    raise RuntimeError(f"Firestore upload unavailable for {DEVICE_ID}")

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

    # 5. Deploy sensor driver
    driver_filename = f"sensor_driver_{req.device_id}.py"
    parts.append('echo "[5/7] Deploying sensor driver..."\n')
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


class HardwareInspectRequest(BaseModel):
    pi_host: str = "ssh.labertus.online"
    pi_port: int = 22
    pi_user: str = "admin"


@app.get("/hardware/inspect")
@app.post("/hardware/inspect")
async def hardware_inspect(request: Request, req: Optional[HardwareInspectRequest] = None):
    verify_api_key(request)
    host = req.pi_host if req else "ssh.labertus.online"
    port = req.pi_port if req else 22
    user = req.pi_user if req else "admin"

    script = (
        "~/scv_water_sensors/venv/bin/python - <<'EOF'\n"
        "import json, subprocess, os\n"
        "res = {}\n"
        "try:\n"
        "    ps = subprocess.check_output(['pgrep', '-af', 'sensor_driver'], text=True)\n"
        "    sensors = []\n"
        "    for line in ps.strip().splitlines():\n"
        "        parts = line.split(None, 1)\n"
        "        if len(parts) == 2 and 'sensor_driver_' in parts[1]:\n"
        "            pid, cmd = parts\n"
        "            name = os.path.basename(cmd.split()[-1]).replace('sensor_driver_', '').replace('.py', '')\n"
        "            if name and name != 'EOF':\n"
        "                sensors.append({'pid': int(pid), 'name': name})\n"
        "    res['sensors'] = sensors\n"
        "except Exception:\n"
        "    res['sensors'] = []\n"
        "try:\n"
        "    usb_out = subprocess.check_output(['lsusb'], text=True)\n"
        "    res['usb'] = [l.strip() for l in usb_out.strip().splitlines() if l.strip()]\n"
        "except Exception:\n"
        "    res['usb'] = []\n"
        "i2c = {}\n"
        "try:\n"
        "    import smbus2\n"
        "    for b in [1, 2]:\n"
        "        found = []\n"
        "        try:\n"
        "            bus = smbus2.SMBus(b)\n"
        "            for a in range(0x03, 0x78):\n"
        "                try:\n"
        "                    bus.read_byte(a)\n"
        "                    found.append(f'0x{a:02X}')\n"
        "                except Exception:\n"
        "                    pass\n"
        "            bus.close()\n"
        "            i2c[f'bus_{b}'] = found\n"
        "        except Exception:\n"
        "            pass\n"
        "except Exception:\n"
        "    pass\n"
        "res['i2c'] = i2c\n"
        "gpio = []\n"
        "try:\n"
        "    p_out = subprocess.check_output(['pinctrl', 'get', '2,3,4,17,18,22,23,24,27'], text=True)\n"
        "    for line in p_out.strip().splitlines():\n"
        "        if '//' in line:\n"
        "            gpio.append(line.split('//')[-1].strip())\n"
        "        elif line.strip():\n"
        "            gpio.append(line.strip())\n"
        "except Exception:\n"
        "    pass\n"
        "res['gpio'] = gpio\n"
        "print(json.dumps(res))\n"
        "EOF\n"
    )

    try:
        ssh = _get_ssh_client(host=host, port=port, username=user)
        out, err, rc = _exec_command(ssh, script, timeout=15)
        ssh.close()
        for line in reversed(out.splitlines()):
            if line.strip().startswith("{"):
                data = json.loads(line)
                return {"success": True, "host": host, "port": port, "user": user, **data}
        return {"success": False, "error": f"no json in output: {out} {err}"}
    except Exception as e:
        return {"success": False, "error": str(e), "host": host}


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


# ---------------------------------------------------------------------------
# RESEARCH phase: web datasheet -> LLM codegen -> on-Pi selftest -> registry
# ---------------------------------------------------------------------------
import urllib.error

import sensors_deploy as sd


def _driver_py_path() -> str:
    return str(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pi_driver", "driver.py"))


def _pi_dir(device_id: str) -> str:
    return f'$HOME/scv_water_sensors/{device_id}'


def _pi_setup_command(device_id: str) -> str:
    """Create the per-device dir, venv and deps (idempotent; deps marker)."""
    d = _pi_dir(device_id)
    return (
        'set -e; mkdir -p ' + d + '; cd ' + d + '; '
        'if [ ! -d venv ] && [ -d ~/scv_water_sensors/venv ]; then ln -s ~/scv_water_sensors/venv venv; fi; '
        'if [ ! -x venv/bin/python ]; then python3 -m venv venv; fi; '
        'if [ ! -f .deps_ok ] && [ ! -L venv ]; then '
        'venv/bin/pip install --quiet smbus2 lgpio RPi.GPIO google-cloud-firestore firebase-admin '
        '|| echo "  warn: some pip deps unavailable"; '
        'touch .deps_ok; fi; '
        'echo SETUP_OK'
    )


def _parse_selftest(stdout: str) -> dict:
    """The contract: exactly ONE JSON line from --selftest (exit 0)."""
    for line in reversed([l for l in stdout.splitlines() if l.strip().startswith("{")]):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return {"ok": False, "error": "bad_code",
            "detail": f"selftest produced no JSON line; tail: {stdout[-400:]}"}


def _pi_selftest(ssh: paramiko.SSHClient, device_id: str) -> dict:
    out, err, rc = _exec_command(
        ssh,
        'cd ' + _pi_dir(device_id) + ' && ./venv/bin/python driver.py --config config.json --selftest 2>&1',
        timeout=180,
    )
    if rc == 0:
        return _parse_selftest(out)
    return {"ok": False, "error": "bad_code",
            "detail": f"selftest exited {rc}; tail: {(out + err)[-400:]}"}


def _write_site_schema_fs(site_id: str, site_name: str, place: str,
                          model: str, device_id: str, entry: dict) -> tuple[str, bool]:
    """Register the site schema in Firestore (REST, web API key).

    Site doc: {name, location, type, sensors: {model: {id, type, fields, pins,
    bus, address, k?}}, updated_at}. Merges into an existing sensors map.
    Returns (project_id, success).
    """
    _, web_api_key = _get_web_firebase_credentials()
    if not web_api_key:
        return "", False
    sensor_doc = {
        "id": {"stringValue": device_id},
        "type": {"stringValue": entry.get("type", "custom")},
        "fields": {"arrayValue": {"values": [
            {"mapValue": {"fields": {
                "key": {"stringValue": f.get("key", "")},
                "label": {"stringValue": f.get("label", "")},
                "unit": {"stringValue": f.get("unit", "")},
                "min": {"doubleValue": float(f["min"])} if f.get("min") is not None else {"nullValue": {}},
                "max": {"doubleValue": float(f["max"])} if f.get("max") is not None else {"nullValue": {}},
            }}} for f in entry.get("fields", [])
        ]}},
    }
    for opt in ("pins", "bus", "address", "k"):
        if entry.get(opt) is not None:
            v = entry[opt]
            sensor_doc[opt] = {"mapValue": {"fields": {
                str(kk): {"intValue": vv} for kk, vv in v.items()}}} if isinstance(v, dict) \
                else {"intValue": int(v)}

    last_err = ""
    for pid in (sd.FIRESTORE_PROJECT, sd.FIRESTORE_PROJECT_FALLBACK):
        base = f"https://firestore.googleapis.com/v1/projects/{pid}/databases/(default)/documents/sites/{site_id}"
        url = f"{base}?key={web_api_key}"
        try:
            sensors_map: dict = {}
            try:
                with urllib.request.urlopen(urllib.request.Request(url), timeout=10) as r:
                    doc = json.loads(r.read()).get("fields", {})
                    sensors_map = (doc.get("sensors", {}).get("mapValue", {}).get("fields", {}))
            except urllib.error.HTTPError:
                pass  # doc doesn't exist yet
            sensors_map[model] = {"mapValue": {"fields": sensor_doc}}
            fields = {
                "name": {"stringValue": site_name or site_id},
                "location": {"stringValue": place or ""},
                "type": {"stringValue": "site"},
                "sensors": {"mapValue": {"fields": sensors_map}},
                "updated_at": {"timestampValue": datetime.utcnow().isoformat() + "Z"},
            }
            req = urllib.request.Request(url, data=json.dumps({"fields": fields}).encode("utf-8"),
                                         headers={"Content-Type": "application/json"}, method="PATCH")
            with urllib.request.urlopen(req, timeout=10):
                return pid, True
        except Exception as e:  # noqa: BLE001 - try next project, then give up
            last_err = str(e)
    logger.warning(f"Firestore site schema registration failed for {site_id}: {last_err}")
    return "", False


class ResearchRequest(BaseModel):
    model: str
    site_id: str
    site_name: str = ""
    place: str = ""
    device_id: Optional[str] = None
    pi_host: str = "ssh.labertus.online"
    pi_port: int = 22
    pi_user: str = "admin"
    max_attempts: int = 3
    force_research: bool = False


@app.post("/pipeline/research")
async def pipeline_research(req: ResearchRequest, request: Request):
    """RESEARCH phase: datasheet facts -> driver code -> on-Pi selftest gate.

    success=true ONLY if the on-Pi selftest returns ok:true. With no sensor
    wired, i2c/gpio drivers honestly report no_device -- that is a correct,
    verified result, reported as success=false with the scan evidence.
    """
    verify_api_key(request)
    if not os.path.exists(_driver_py_path()):
        raise HTTPException(
            status_code=503,
            detail=f"pi driver missing at {_driver_py_path()} -- "
                   "the pi_driver/driver.py module is not written yet")

    mode = "research" if (req.force_research or sd.normalise_model(req.model)[0] is None) else "catalog"
    norm_type = sd.normalise_model(req.model)[1]
    device_id = req.device_id or f"{norm_type.upper()}_{secrets.token_hex(2).upper()}"

    logs: list[str] = []
    code = ""
    sources: list[dict] = []
    protocol = ""

    # a+b+d. Normalise / web research / codegen / pin allocation
    try:
        cfg, code, sources, protocol = sd.research(req.model, req.site_id, force_research=req.force_research)
    except Exception as e:  # noqa: BLE001
        logger.exception("research failed")
        return {
            "success": False, "verified": False, "device_id": device_id,
            "model": req.model, "normalized_type": sd.normalise_model(req.model)[1],
            "mode": mode, "sources": [], "fields": [], "config": {},
            "readings": None, "attempts": 0,
            "message": f"Research failed: {e}", "logs_tail": "\n".join(logs),
        }
    logs.append(f"mode={mode} protocol={protocol} config={json.dumps(cfg, ensure_ascii=False)[:300]}")

    project_id, web_api_key = _get_web_firebase_credentials()
    full_config = {
        "device_id": device_id,
        "site_id": req.site_id,
        **cfg,
        "project_id": sd.FIRESTORE_PROJECT,
        "web_api_key": web_api_key,
    }
    # The generated/hand-written driver source MUST reach the Pi for the
    # first selftest; the fix-loop below only re-uploads it *after* a failure.
    if code:
        full_config["code"] = code

    # e. TEST-ON-PI gate (max `max_attempts` attempts; code fix loop for custom)
    max_attempts = max(1, min(req.max_attempts, 3))
    attempts = 0
    result: dict = {}
    last_code = code
    ssh = None
    try:
        ssh = _get_ssh_client(host=req.pi_host, port=req.pi_port, username=req.pi_user)
        logs.append(f"SSH connected to {req.pi_user}@{req.pi_host}")
        setup_out, setup_err, setup_rc = _exec_command(ssh, _pi_setup_command(device_id), timeout=600)
        logs.append(setup_out.strip()[-300:] or f"setup rc={setup_rc} {setup_err.strip()[-300:]}")
        if setup_rc != 0 or "SETUP_OK" not in setup_out:
            raise RuntimeError(f"Pi setup failed rc={setup_rc}: {setup_err.strip()[-300:]}")

        import tempfile as _tf
        with open(_driver_py_path(), "rb") as f:
            driver_src = f.read().decode("utf-8")
        with _tf.TemporaryDirectory() as td:
            with open(os.path.join(td, "driver.py"), "w", encoding="utf-8") as f:
                f.write(driver_src)
            with open(os.path.join(td, "config.json"), "w", encoding="utf-8") as f:
                json.dump(full_config, f, ensure_ascii=False, indent=2)
            sftp = ssh.open_sftp()
            rdir = sftp.normalize(".") + f"/scv_water_sensors/{device_id}"
            sftp.put(os.path.join(td, "driver.py"), rdir + "/driver.py")
            sftp.put(os.path.join(td, "config.json"), rdir + "/config.json")
            sftp.close()
        logs.append("driver.py + config.json uploaded")

        for attempts in range(1, max_attempts + 1):
            result = _pi_selftest(ssh, device_id)
            logs.append(f"selftest attempt {attempts}: ok={result.get('ok')} error={result.get('error')}")
            if result.get("ok"):
                break
            if attempts < max_attempts and cfg.get("type") == "custom":
                try:
                    raw = sd.qwen_call([
                        {"role": "system", "content": sd.CODEGEN_SYSTEM},
                        {"role": "user", "content": sd.fix_prompt(
                            req.model, last_code, result, "", protocol, cfg.get("pins", {}))},
                    ], max_tokens=8000, timeout=420)
                    fixed = sd._extract_json(raw)
                    if fixed.get("code"):
                        last_code = fixed["code"]
                        full_config["code"] = last_code
                        with _tf.TemporaryDirectory() as td:
                            p = os.path.join(td, "config.json")
                            with open(p, "w", encoding="utf-8") as f:
                                json.dump(full_config, f, ensure_ascii=False, indent=2)
                            sftp = ssh.open_sftp()
                            rdir = sftp.normalize(".") + f"/scv_water_sensors/{device_id}"
                            sftp.put(p, rdir + "/config.json")
                            sftp.close()
                        logs.append(f"attempt {attempts + 1}: re-uploaded fixed code")
                except Exception as e:  # noqa: BLE001
                    logs.append(f"code fix failed: {e}")
            elif attempts < max_attempts:
                logs.append("retrying selftest (catalog driver, hardware may be settling)")
    except Exception as e:  # noqa: BLE001
        logger.exception("research selftest phase failed")
        return {
            "success": False, "verified": False, "device_id": device_id,
            "model": req.model, "normalized_type": cfg.get("type"),
            "mode": mode, "sources": sources, "fields": cfg.get("fields", []),
            "config": full_config, "readings": None, "attempts": attempts,
            "message": f"Pi selftest phase failed: {e}",
            "logs_tail": "\n".join(logs)[-2000:],
        }
    finally:
        if ssh:
            ssh.close()

    # g. persist registry so /pipeline/deploy-draft can reuse without re-researching
    reg = sd.load_json(sd.RESEARCH_REGISTRY_PATH, {})
    reg[device_id] = {
        "model": req.model, "mode": mode, "sources": sources,
        "fields": cfg.get("fields", []), "config": full_config,
        "protocol": protocol, "code": last_code if cfg.get("type") == "custom" else None,
        "verified": bool(result.get("ok")), "attempts": attempts,
        "result": result, "ts": datetime.now().isoformat(),
        "site_id": req.site_id, "site_name": req.site_name, "place": req.place,
    }
    sd.save_json(sd.RESEARCH_REGISTRY_PATH, reg)

    ok = bool(result.get("ok"))
    detail = result.get("detail") or ""
    evidence = result.get("evidence") or {}
    if ok:
        message = f"Selftest passed on {req.pi_host}: sensor verified."
    else:
        err_code = result.get("error") or "unknown"
        if err_code in ("no_device", "bus_error", "timeout"):
            message = (
                f"Driver ran on {req.pi_host} but the sensor was NOT detected "
                f"(error={err_code}). This is an honest hardware result, not a system "
                f"failure — wire/power the sensor and re-run. "
                f"Evidence: {json.dumps(evidence, ensure_ascii=False)[:400]} {detail[:200]}"
            )
        else:
            message = f"Selftest failed: error={err_code} detail={detail[:300]} evidence={json.dumps(evidence, ensure_ascii=False)[:300]}"

    return {
        "success": ok,
        "verified": ok,
        "device_id": device_id,
        "model": req.model,
        "normalized_type": cfg.get("type"),
        "mode": mode,
        "sources": sources,
        "fields": cfg.get("fields", []),
        "config": full_config,
        "readings": result.get("samples"),
        "attempts": attempts,
        "message": message,
        "logs_tail": "\n".join(logs)[-2000:],
    }


class DeployDraftRequest(BaseModel):
    device_id: Optional[str] = None
    model: Optional[str] = None
    site_id: str
    site_name: str = ""
    place: str = ""
    force: bool = False
    pi_host: str = "ssh.labertus.online"
    pi_port: int = 22
    pi_user: str = "admin"


@app.post("/pipeline/deploy-draft")
async def pipeline_deploy_draft(req: DeployDraftRequest, request: Request):
    """Deploy a verified research draft as a systemd user unit + Firestore schema.

    Refuses unless the research selftest was ok (or force=true).
    """
    verify_api_key(request)
    reg = sd.load_json(sd.RESEARCH_REGISTRY_PATH, {})

    device_id = req.device_id
    if not device_id and req.model and req.site_id:
        for did, entry in reg.items():
            if entry.get("model") == req.model and entry.get("site_id") == req.site_id:
                device_id = did
                break
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id (or model+site_id) is required")
    entry = reg.get(device_id)
    if entry is None:
        raise HTTPException(status_code=404,
                            detail=f"no research registry entry for {device_id}; run /pipeline/research first")

    if not entry.get("verified") and not req.force:
        return {
            "success": False, "device_id": device_id, "service": None,
            "verified": False, "readings": (entry.get("result") or {}).get("samples"),
            "message": ("Refusing to deploy: the on-Pi selftest was never ok "
                        f"(error={(entry.get('result') or {}).get('error')}). "
                        "Pass force=true to deploy anyway."),
        }

    service = f"scv-water-{device_id}.service"
    logs: list[str] = []
    ssh = None
    try:
        ssh = _get_ssh_client(host=req.pi_host, port=req.pi_port, username=req.pi_user)
        setup_out, setup_err, setup_rc = _exec_command(ssh, _pi_setup_command(device_id), timeout=600)
        if setup_rc != 0 or "SETUP_OK" not in setup_out:
            raise RuntimeError(f"Pi setup failed: {setup_err.strip()[-300:]}")

        config = dict(entry.get("config") or {})
        config.setdefault("device_id", device_id)
        config["site_id"] = req.site_id
        if entry.get("code"):
            config["code"] = entry["code"]

        import tempfile as _tf
        with _tf.TemporaryDirectory() as td:
            sftp = ssh.open_sftp()
            rdir = sftp.normalize(".") + f"/scv_water_sensors/{device_id}"
            with open(_driver_py_path(), "rb") as f:
                sftp.putfo(f, rdir + "/driver.py")
            with open(os.path.join(td, "config.json"), "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            sftp.put(os.path.join(td, "config.json"), rdir + "/config.json")
            sftp.close()
        logs.append("config uploaded")

        d = _pi_dir(device_id)
        unit = (
            "[Unit]\n"
            f"Description=SCV Water sensor driver - {device_id}\n"
            "After=network.target\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"WorkingDirectory=%h/scv_water_sensors/{device_id}\n"
            f"ExecStart=%h/scv_water_sensors/{device_id}/venv/bin/python "
            f"%h/scv_water_sensors/{device_id}/driver.py --config %h/scv_water_sensors/{device_id}/config.json\n"
            "Restart=on-failure\n"
            "RestartSec=10\n\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
        write_cmd = (
            f"mkdir -p $HOME/.config/systemd/user && cat > $HOME/.config/systemd/user/{service} << 'SCV_UNIT_EOF'\n"
            f"{unit}\nSCV_UNIT_EOF\n"
            f"systemctl --user daemon-reload && "
            f"systemctl --user enable --now {service} && "
            f"systemctl --user restart {service} && sleep 1 && "
            f"echo ACTIVE=$(systemctl --user is-active {service})"
        )
        out, err, rc = _exec_command(ssh, write_cmd, timeout=120)
        logs.append(out.strip()[-300:] or f"rc={rc} {err.strip()[-300:]}")
        if rc != 0:
            raise RuntimeError(f"systemd install failed rc={rc}: {err.strip()[-300:]}")
        active = "ACTIVE=active" in out
        logs.append(f"service {service} active={active}")
    except Exception as e:  # noqa: BLE001
        logger.exception("deploy-draft failed")
        return {
            "success": False, "device_id": device_id, "service": service,
            "verified": entry.get("verified", False),
            "readings": (entry.get("result") or {}).get("samples"),
            "message": f"Deploy failed: {e}", "logs": logs,
        }
    finally:
        if ssh:
            ssh.close()

    # Firestore site schema (reuse _register_firestore_schema logic)
    fs_proj, fs_ok = _write_site_schema_fs(
        req.site_id, req.site_name or req.site_id, req.place,
        entry.get("model", ""), device_id, config,
    )

    return {
        "success": active,
        "device_id": device_id,
        "service": service,
        "verified": entry.get("verified", False),
        "readings": (entry.get("result") or {}).get("samples"),
        "firestore_project": fs_proj if fs_ok else None,
        "message": (f"Deployed {service} to {req.pi_host} (active={active}, "
                    f"verified={entry.get('verified')}, firestore={'ok' if fs_ok else 'skipped'})"),
        "logs": logs,
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
