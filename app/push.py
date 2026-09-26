"""웹 푸시 보내기 (2026-09-23). 받는 쪽은 erion PWA — 폰 홈화면에 깐 `/erion/`.

라이브러리(pywebpush)를 안 쓴다. 그게 aiohttp·requests 를 끌고 들어오는데, 필요한 건
암호화 한 번(RFC 8291 aes128gcm)과 서명 한 번(RFC 8292 VAPID)뿐이고 둘 다
이미 깔린 `cryptography` 로 된다. 전송은 urllib.

VAPID 키는 `data/vapid.pem` (chmod 600). 처음 부를 때 만든다. **이 키를 바꾸면 등록된
기기가 전부 무효가 된다** — 폰에서 알림을 다시 켜야 한다.

VAPID `sub` 는 메일 주소 대신 erion 주소를 쓴다. 푸시 서비스(구글 FCM)에 사용자 메일을
넘길 이유가 없다.
"""

import base64
import json
import os
import struct
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, hmac, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import config, tools

KEY_FILE = config.DATA_DIR / "vapid.pem"
SUB = f"{config.ISSUER}/"            # https://…/erion/
TTL = 12 * 3600                   # 폰이 꺼져 있으면 푸시 서비스가 이만큼 들고 있다


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _raw(pub: ec.EllipticCurvePublicKey) -> bytes:
    return pub.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def _key() -> ec.EllipticCurvePrivateKey:
    if KEY_FILE.exists():
        return serialization.load_pem_private_key(KEY_FILE.read_bytes(), None)
    k = ec.generate_private_key(ec.SECP256R1())
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption())
    fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    return k


def public_key() -> str:
    """브라우저 pushManager.subscribe 의 applicationServerKey."""
    return _b64(_raw(_key().public_key()))


def _hmac(key: bytes, msg: bytes) -> bytes:
    h = hmac.HMAC(key, hashes.SHA256())
    h.update(msg)
    return h.finalize()


def _encrypt(p256dh: str, auth: str, plain: bytes) -> bytes:
    """RFC 8291 — aes128gcm 한 레코드."""
    ua_pub = _unb64(p256dh)
    secret = _unb64(auth)
    eph = ec.generate_private_key(ec.SECP256R1())
    as_pub = _raw(eph.public_key())
    shared = eph.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub))
    ikm = _hmac(_hmac(secret, shared), b"WebPush: info\x00" + ua_pub + as_pub + b"\x01")
    salt = os.urandom(16)
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    body = AESGCM(cek).encrypt(nonce, plain + b"\x02", None)
    return salt + struct.pack(">I", 4096) + bytes([len(as_pub)]) + as_pub + body


def _vapid(endpoint: str) -> str:
    k = _key()
    u = urlsplit(endpoint)
    head = _b64(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
    claim = _b64(json.dumps({"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 12 * 3600,
                             "sub": SUB}).encode())
    r, s = decode_dss_signature(k.sign(f"{head}.{claim}".encode(), ec.ECDSA(hashes.SHA256())))
    jwt = f"{head}.{claim}.{_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    return f"vapid t={jwt}, k={_b64(_raw(k.public_key()))}"


def send_one(sub: dict, payload: dict, urgency: str = "normal") -> int:
    """HTTP 상태를 돌려준다. 201 이 정상. 네트워크 오류는 0."""
    data = _encrypt(sub["p256dh"], sub["auth"], json.dumps(payload, ensure_ascii=False).encode())
    req = urllib.request.Request(sub["endpoint"], data=data, method="POST", headers={
        "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
        "TTL": str(TTL), "Urgency": urgency, "Authorization": _vapid(sub["endpoint"])})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0


def send_all(conn, title: str, body: str, url: str | None = None, tag: str | None = None,
             urgency: str = "normal") -> int:
    """등록된 기기 전부에 보낸다. 받은 기기 수. 사라진 기기(404/410)는 지운다."""
    n = 0
    payload = {"title": title, "body": body, "url": url, "tag": tag}
    for s in [dict(r) for r in conn.execute("SELECT * FROM push_sub")]:
        st = send_one(s, payload, urgency)
        if 200 <= st < 300:
            n += 1
            conn.execute("UPDATE push_sub SET last_ok=? WHERE endpoint=?", [tools._now_iso(), s["endpoint"]])
        elif st in (404, 410):
            conn.execute("DELETE FROM push_sub WHERE endpoint=?", [s["endpoint"]])
        else:
            print(f"push {st} {urlsplit(s['endpoint']).netloc}", flush=True)
    conn.commit()
    return n


def subscribe(conn, sub: dict, ua: str | None) -> dict:
    try:
        ep = str(sub["endpoint"])
        keys = sub["keys"]
        p256dh, auth = str(keys["p256dh"]), str(keys["auth"])
        _unb64(p256dh), _unb64(auth)
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "error": "구독 정보가 이상해요"}
    if not ep.startswith("https://"):
        return {"ok": False, "error": "endpoint 는 https"}
    conn.execute(
        "INSERT INTO push_sub (endpoint,p256dh,auth,ua,at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh, auth=excluded.auth, "
        "ua=excluded.ua, at=excluded.at", [ep, p256dh, auth, (ua or "")[:200], tools._now_iso()])
    conn.commit()
    return {"ok": True}
