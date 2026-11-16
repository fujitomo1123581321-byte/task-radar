"""
Envelope Encryption Vault for Task Radar (Zero external dependencies).
Encrypts assignments.json and assignments.js using HMAC-SHA256 CTR stream cipher + HMAC-SHA256 tag
with PBKDF2-HMAC-SHA256 for user passcodes and SHA-256 key derivation for backend secrets.
"""
import os
import json
import hmac
import hashlib
import base64

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JSON_PATH = os.path.join(BASE_DIR, "assignments.json")
JS_PATH = os.path.join(BASE_DIR, "assignments.js")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
VAULT_PATH = os.path.join(BASE_DIR, "auth_vault.json")

_CACHED_DATA_KEY = None


def derive_key(passcode: str, salt: bytes, iterations: int = 10000) -> bytes:
    """Derive a 32-byte KEK from a user passcode via PBKDF2-HMAC-SHA256."""
    return hashlib.pbkdf2_hmac("sha256", passcode.encode("utf-8"), salt, iterations, dklen=32)


def _xor_keystream(enc_key: bytes, iv: bytes, data: bytes) -> bytes:
    """Generate deterministic HMAC-SHA256 counter-mode keystream and XOR with data."""
    out = bytearray(len(data))
    offset = 0
    counter = 0
    total = len(data)
    while offset < total:
        block = hmac.new(enc_key, iv + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        chunk_len = min(32, total - offset)
        for i in range(chunk_len):
            out[offset + i] = data[offset + i] ^ block[i]
        offset += chunk_len
        counter += 1
    return bytes(out)


def encrypt_bytes(key: bytes, plaintext: bytes) -> dict:
    """Encrypt plaintext bytes with Encrypt-then-MAC (HMAC-SHA256)."""
    enc_key = hmac.new(key, b"enc", hashlib.sha256).digest()
    mac_key = hmac.new(key, b"mac", hashlib.sha256).digest()
    iv = os.urandom(16)
    ct = _xor_keystream(enc_key, iv, plaintext)
    tag = hmac.new(mac_key, iv + ct, hashlib.sha256).digest()
    return {
        "encrypted": True,
        "v": 1,
        "iv": base64.b64encode(iv).decode("ascii"),
        "ct": base64.b64encode(ct).decode("ascii"),
        "tag": base64.b64encode(tag).decode("ascii"),
    }


def decrypt_bytes(key: bytes, env: dict) -> bytes:
    """Verify HMAC tag and decrypt envelope dictionary back to plaintext bytes."""
    if not isinstance(env, dict) or not env.get("encrypted"):
        raise ValueError("Invalid encrypted envelope")
    iv = base64.b64decode(env["iv"])
    ct = base64.b64decode(env["ct"])
    tag = base64.b64decode(env["tag"])

    mac_key = hmac.new(key, b"mac", hashlib.sha256).digest()
    expected_tag = hmac.new(mac_key, iv + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected_tag):
        raise ValueError("Authentication tag mismatch (wrong key or tampered data)")

    enc_key = hmac.new(key, b"enc", hashlib.sha256).digest()
    return _xor_keystream(enc_key, iv, ct)


def _load_local_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {}


def _get_secret_urls() -> tuple:
    cfg = _load_local_config()
    letus_url = (os.environ.get("LETUS_CALENDAR_URL") or "").strip()
    if not letus_url:
        url_file = os.path.join(BASE_DIR, "letus_url.txt")
        if os.path.exists(url_file):
            try:
                with open(url_file, "r", encoding="utf-8") as f:
                    letus_url = f.read().strip()
            except Exception:
                pass
    if not letus_url:
        letus_url = (cfg.get("letus_calendar_url") or "").strip()

    webhook_url = (os.environ.get("DISCORD_WEBHOOK_URL") or cfg.get("discord_webhook_url") or "").strip()
    return letus_url, webhook_url


def load_vault() -> dict:
    """Load auth_vault.json if present."""
    if os.path.exists(VAULT_PATH):
        try:
            with open(VAULT_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {}


def save_vault(vault_obj: dict):
    """Write auth_vault.json atomically."""
    tmp = VAULT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(vault_obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, VAULT_PATH)


def get_data_key() -> bytes:
    """Obtain the 32-byte master DATA_KEY from cache, config, env, or auth_vault.json."""
    global _CACHED_DATA_KEY
    if _CACHED_DATA_KEY is not None:
        return _CACHED_DATA_KEY

    # 1. Explicit env var or local config.json
    env_key = (os.environ.get("DATA_ENCRYPTION_KEY") or "").strip()
    if env_key:
        _CACHED_DATA_KEY = base64.b64decode(env_key)
        return _CACHED_DATA_KEY

    cfg = _load_local_config()
    cfg_key = (cfg.get("data_encryption_key") or "").strip()
    if cfg_key:
        _CACHED_DATA_KEY = base64.b64decode(cfg_key)
        return _CACHED_DATA_KEY

    # 2. Unwrap from auth_vault.json using existing environment secrets
    vault = load_vault()
    if not vault:
        raise RuntimeError("auth_vault.json not found and no DATA_ENCRYPTION_KEY configured")

    letus_url, webhook_url = _get_secret_urls()

    if letus_url and vault.get("server_wrapped_key"):
        try:
            kek = hashlib.sha256(letus_url.encode("utf-8")).digest()
            _CACHED_DATA_KEY = decrypt_bytes(kek, vault["server_wrapped_key"])
            return _CACHED_DATA_KEY
        except Exception:
            pass

    if webhook_url and vault.get("webhook_wrapped_key"):
        try:
            kek = hashlib.sha256(webhook_url.encode("utf-8")).digest()
            _CACHED_DATA_KEY = decrypt_bytes(kek, vault["webhook_wrapped_key"])
            return _CACHED_DATA_KEY
        except Exception:
            pass

    raise RuntimeError("Unable to unwrap DATA_KEY from auth_vault.json (missing or mismatched secrets)")


def init_vault(initial_passcode: str = "2026") -> dict:
    """Initialize or re-key auth_vault.json and persist data_encryption_key to local config.json."""
    global _CACHED_DATA_KEY
    cfg = _load_local_config()
    data_key = None
    if cfg.get("data_encryption_key"):
        try:
            candidate = base64.b64decode(cfg["data_encryption_key"])
            if len(candidate) == 32:
                data_key = candidate
        except Exception:
            data_key = None

    if data_key is None:
        data_key = os.urandom(32)
        if os.path.exists(CONFIG_PATH) or cfg:
            cfg["data_encryption_key"] = base64.b64encode(data_key).decode("ascii")
            tmp_cfg = CONFIG_PATH + ".tmp"
            with open(tmp_cfg, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp_cfg, CONFIG_PATH)

    _CACHED_DATA_KEY = data_key
    user_salt = os.urandom(16)
    iterations = 10000
    user_kek = derive_key(initial_passcode, user_salt, iterations)
    user_wrapped_key = encrypt_bytes(user_kek, data_key)

    letus_url, webhook_url = _get_secret_urls()
    vault = {
        "v": 1,
        "key_id": hashlib.sha256(data_key).hexdigest()[:16],
        "user_salt": base64.b64encode(user_salt).decode("ascii"),
        "user_iterations": iterations,
        "user_wrapped_key": user_wrapped_key,
    }
    if letus_url:
        server_kek = hashlib.sha256(letus_url.encode("utf-8")).digest()
        vault["server_wrapped_key"] = encrypt_bytes(server_kek, data_key)
    if webhook_url:
        webhook_kek = hashlib.sha256(webhook_url.encode("utf-8")).digest()
        vault["webhook_wrapped_key"] = encrypt_bytes(webhook_kek, data_key)

    save_vault(vault)
    return vault


def decrypt_json_data(raw_or_obj, default=None):
    """Decrypt JSON payload if encrypted, or pass through plaintext list."""
    if raw_or_obj is None:
        return default
    obj = raw_or_obj
    if isinstance(raw_or_obj, bytes):
        raw_str = raw_or_obj.decode("utf-8").strip()
        if not raw_str:
            return default
        obj = json.loads(raw_str)
    elif isinstance(raw_or_obj, str):
        raw_str = raw_or_obj.strip()
        if not raw_str:
            return default
        obj = json.loads(raw_str)

    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict) and obj.get("encrypted") is True:
        plaintext_bytes = decrypt_bytes(get_data_key(), obj)
        return json.loads(plaintext_bytes.decode("utf-8"))
    return obj if obj is not None else default


def encrypt_json_data(obj) -> dict:
    """Serialize obj to UTF-8 JSON and encrypt into envelope dict."""
    plaintext = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return encrypt_bytes(get_data_key(), plaintext)


def load_assignments_file(json_path: str = JSON_PATH) -> list:
    """Load assignments from json_path (supports both plaintext list and encrypted envelope)."""
    if not os.path.exists(json_path):
        return []
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            raw = f.read()
        res = decrypt_json_data(raw, default=[])
        return res if isinstance(res, list) else []
    except Exception as e:
        print(f"[CryptoVault Load Error] {json_path}: {e}")
        return []


def save_assignments_files(items: list, json_path: str = JSON_PATH, js_path: str = JS_PATH):
    """Encrypt items and write assignments.json and assignments.js atomically."""
    enc = encrypt_json_data(items)

    tmp_json = json_path + ".tmp"
    with open(tmp_json, "w", encoding="utf-8") as f:
        json.dump(enc, f, ensure_ascii=False, indent=2)
    os.replace(tmp_json, json_path)

    vault_obj = load_vault()
    js_content = (
        f"window.AUTH_VAULT = {json.dumps(vault_obj, ensure_ascii=False)};\n"
        f"window.INITIAL_ASSIGNMENTS_ENC = {json.dumps(enc, ensure_ascii=False)};\n"
        "window.INITIAL_ASSIGNMENTS = [];\n"
    )
    tmp_js = js_path + ".tmp"
    with open(tmp_js, "w", encoding="utf-8") as f:
        f.write(js_content)
    os.replace(tmp_js, js_path)
