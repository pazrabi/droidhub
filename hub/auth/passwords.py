"""Хэширование и проверка паролей (scrypt/pbkdf2)."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets



logger = logging.getLogger(__name__)


def hash_password(pw: str, algo: str = "scrypt") -> str:
    """
    Хэширует пароль с солью с использованием stdlib (scrypt или pbkdf2).
    Формат результата: algo$params$salt$hash.
    """
    salt = secrets.token_hex(16)
    if algo == "scrypt" and hasattr(hashlib, "scrypt"):
        n, r, p = 16384, 8, 1
        dk = hashlib.scrypt(
            pw.encode("utf-8"),
            salt=bytes.fromhex(salt),
            n=n,
            r=r,
            p=p,
            maxmem=0,
            dklen=32,
        )
        return f"scrypt$n={n},r={r},p={p}${salt}${dk.hex()}"
    else:
        iterations = 100_000
        dk = hashlib.pbkdf2_hmac(
            "sha256",
            pw.encode("utf-8"),
            bytes.fromhex(salt),
            iterations,
            dklen=32,
        )
        return f"pbkdf2_sha256$iterations={iterations}${salt}${dk.hex()}"


def verify_password(pw: str, hashed: str) -> bool:
    """
    Проверяет пароль против хэша в формате algo$params$salt$hash за постоянное время.
    """
    if not isinstance(pw, str) or not isinstance(hashed, str):
        return False
    if not pw or not hashed:
        return False

    try:
        parts = hashed.split("$")
        if len(parts) != 4:
            return False
        algo, params_str, salt_str, hash_hex = parts
        salt = bytes.fromhex(salt_str)
        target = bytes.fromhex(hash_hex)
        dklen = len(target)
        if dklen == 0:
            return False

        if algo == "scrypt":
            params: dict[str, int] = {}
            if "=" in params_str:
                for item in params_str.split(","):
                    if "=" in item:
                        k, v = item.split("=", 1)
                        params[k.strip()] = int(v.strip())
            else:
                nums = [int(x.strip()) for x in params_str.split(",") if x.strip()]
                if len(nums) >= 3:
                    params["n"], params["r"], params["p"] = nums[0], nums[1], nums[2]

            n = params.get("n", 16384)
            r = params.get("r", 8)
            p = params.get("p", 1)
            candidate = hashlib.scrypt(
                pw.encode("utf-8"),
                salt=salt,
                n=n,
                r=r,
                p=p,
                maxmem=0,
                dklen=dklen,
            )
            return hmac.compare_digest(candidate, target)

        elif algo in ("pbkdf2", "pbkdf2_sha256"):
            params = {}
            if "=" in params_str:
                for item in params_str.split(","):
                    if "=" in item:
                        k, v = item.split("=", 1)
                        params[k.strip()] = int(v.strip())
            else:
                nums = [int(x.strip()) for x in params_str.split(",") if x.strip()]
                if nums:
                    params["iterations"] = nums[0]

            iterations = params.get("iterations", 100_000)
            candidate = hashlib.pbkdf2_hmac(
                "sha256",
                pw.encode("utf-8"),
                salt=salt,
                iterations=iterations,
                dklen=dklen,
            )
            return hmac.compare_digest(candidate, target)

        return False
    except Exception:
        return False
