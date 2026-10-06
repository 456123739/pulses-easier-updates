"""
make_tls.py — 生成本地 HTTPS 测试用的自签证书
------------------------------------------------
`tests/_tls/` 里的证书**不纳入版本库**（每次重新生成即可），
`bench_ab.py` 的 HTTPS 场景会在缺失时调用本脚本。

优先用 openssl；没有 openssl 时尝试 Python 的 cryptography；
两者都没有就直接返回 False（bench_ab 会跳过 HTTPS 场景，其余照常）。

用法：  python3 tests/make_tls.py
"""

import subprocess
import sys
from pathlib import Path

_TLS = Path(__file__).resolve().parent / "_tls"
_DAYS = "3650"


def _openssl() -> bool:
    try:
        _TLS.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", str(_TLS / "key.pem"),
        "-out", str(_TLS / "cert.pem"),
        "-days", _DAYS, "-nodes",
        "-subj", "/CN=localhost",
        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=60)
        return r.returncode == 0 and (_TLS / "cert.pem").is_file()
    except Exception:  # noqa: BLE001
        return False


def _cryptography() -> bool:
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        import datetime
    except Exception:  # noqa: BLE001
        return False
    try:
        _TLS.mkdir(parents=True, exist_ok=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                             "localhost")])
        now = datetime.datetime.utcnow()
        cert = (x509.CertificateBuilder()
                .subject_name(name).issuer_name(name)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=3650))
                .add_extension(x509.SubjectAlternativeName(
                    [x509.DNSName("localhost"),
                     x509.IPAddress(__import__("ipaddress").ip_address(
                         "127.0.0.1"))]), critical=False)
                .sign(key, hashes.SHA256()))
        (_TLS / "key.pem").write_bytes(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()))
        (_TLS / "cert.pem").write_bytes(
            cert.public_bytes(serialization.Encoding.PEM))
        return True
    except Exception:  # noqa: BLE001
        return False


def ensure_certs() -> bool:
    """证书已存在或生成成功返回 True。"""
    if (_TLS / "cert.pem").is_file() and (_TLS / "key.pem").is_file():
        return True
    return _openssl() or _cryptography()


if __name__ == "__main__":
    ok = ensure_certs()
    print(f"tls certs: {'ok' if ok else 'unavailable'}（{_TLS}）")
    sys.exit(0 if ok else 1)
