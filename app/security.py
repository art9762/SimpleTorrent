import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import OrderedDict

from fastapi import HTTPException, Request

COOKIE = "st_session"
SESSION_SECONDS = 7 * 24 * 3600


class Security:
    def __init__(self, settings):
        self.settings = settings
        self.attempts = OrderedDict()

    def issue(self):
        csrf = secrets.token_urlsafe(24)
        payload = base64.urlsafe_b64encode(
            json.dumps(
                {"expires": int(time.time()) + SESSION_SECONDS, "csrf": csrf}
            ).encode()
        ).decode()
        signature = hmac.new(
            self.settings.session_secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        return payload + "." + signature, csrf

    def authenticate(self, request: Request):
        try:
            payload, signature = request.cookies.get(COOKIE, "").split(".", 1)
            expected = hmac.new(
                self.settings.session_secret.encode(), payload.encode(), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(expected, signature):
                raise ValueError()
            data = json.loads(base64.urlsafe_b64decode(payload))
            if data["expires"] < time.time():
                raise ValueError()
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                if not hmac.compare_digest(
                    request.headers.get("X-CSRF-Token", ""), data["csrf"]
                ):
                    raise HTTPException(403, "Обновите страницу и повторите действие.")
                self.check_origin(request)
            return data
        except (ValueError, KeyError, TypeError):
            raise HTTPException(401, "Войдите, чтобы продолжить.")

    @staticmethod
    def check_origin(request):
        origin = request.headers.get("origin")
        expected = str(request.base_url).rstrip("/")
        if (origin and origin != expected) or request.headers.get(
            "sec-fetch-site"
        ) == "cross-site":
            raise HTTPException(403, "Запрос с другого сайта запрещён.")

    def check_password(self, request, password):
        self.check_origin(request)
        key = request.client.host if request.client else "unknown"
        now = time.monotonic()
        # Bounded in-memory limiter; cleared on app restart.
        recent = [t for t in self.attempts.get(key, []) if now - t < 300]
        if len(recent) >= 8:
            raise HTTPException(429, "Слишком много попыток. Подождите 5 минут.")
        self.attempts[key] = recent + [now]
        self.attempts.move_to_end(key)
        while len(self.attempts) > 4096:
            self.attempts.popitem(last=False)
        if not hmac.compare_digest(password.encode(), self.settings.password.encode()):
            raise HTTPException(401, "Неверный пароль.")
        self.attempts.pop(key, None)
