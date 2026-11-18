"""访问控制：公网部署时给工作台加一道门。

启用方式：设置环境变量 APP_PASSWORD（未设置则不启用，保持内网/本地开发行为不变）。

- 浏览器：访问任意页面先跳 /login，输入密码后写入签名 Cookie（默认 30 天）。
- 脚本/外部 Agent：请求头带 Authorization: Bearer <API_TOKEN> 即可直接调用 /api/*，
  用于 POST /api/products_updates 等自动化写入。
- 修改 APP_PASSWORD 后旧 Cookie 自动失效；登录失败按 IP 限流，防暴力破解。

相关环境变量：
  APP_PASSWORD     登录密码（启用开关）
  API_TOKEN        可选，自动化调用用的令牌
  SECRET_KEY       可选，Cookie 签名密钥；缺省时由 APP_PASSWORD 派生（多 worker 一致）
  COOKIE_SECURE    1 = 仅 HTTPS 下发送 Cookie（配好域名证书后建议开启）
  SESSION_DAYS     登录有效天数，默认 30
  TRUST_PROXY      1 = 信任 X-Forwarded-For 取客户端 IP（前面有 Caddy/Nginx 时开启）
"""
import os
import hmac
import time
import hashlib
import datetime
import threading
import urllib.parse

from flask import request, session, redirect, jsonify, send_from_directory

PUBLIC_PATHS = {"/healthz", "/login", "/logout", "/api/login", "/api/auth/status",
                "/favicon.ico", "/favicon.svg"}
PUBLIC_PREFIXES = ("/static/",)

_MAX_FAILS = 10          # 窗口内最多失败次数
_FAIL_WINDOW = 10 * 60   # 秒
_fails = {}
_fails_lock = threading.Lock()


def _env(name, default=""):
    return (os.environ.get(name) or default).strip()


def _client_ip():
    if _env("TRUST_PROXY") == "1":
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.remote_addr or "-"


def _too_many_fails(ip):
    now = time.time()
    with _fails_lock:
        hits = [t for t in _fails.get(ip, []) if now - t < _FAIL_WINDOW]
        _fails[ip] = hits
        return len(hits) >= _MAX_FAILS


def _record_fail(ip):
    with _fails_lock:
        _fails.setdefault(ip, []).append(time.time())


def _safe_next(target):
    """只允许站内相对路径，防开放重定向。"""
    if not target or not target.startswith("/") or target.startswith("//") or target.startswith("/\\"):
        return "/"
    return target


def init_auth(app, base_dir):
    password = _env("APP_PASSWORD")
    api_token = _env("API_TOKEN")
    enabled = bool(password)
    app.config["AUTH_ENABLED"] = enabled
    # 改密码即让旧会话失效：会话里只存密码指纹
    pw_fingerprint = hashlib.sha256(("worktab-pw:" + password).encode("utf-8")).hexdigest()[:24]

    def is_logged_in():
        if not enabled:
            return True
        return hmac.compare_digest(str(session.get("pw", "")), pw_fingerprint)

    @app.route("/api/auth/status")
    def auth_status():
        return jsonify({"enabled": enabled, "logged_in": is_logged_in()})

    if not enabled:
        print("[auth] 未设置 APP_PASSWORD，访问控制关闭（仅适用于内网/本地）", flush=True)

        @app.route("/login")
        def login_page_disabled():
            return redirect("/")
        return

    app.secret_key = _env("SECRET_KEY") or hashlib.sha256(
        ("worktab-session:" + password).encode("utf-8")).hexdigest()
    app.config.update(
        SESSION_COOKIE_NAME="worktab_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=_env("COOKIE_SECURE") == "1",
        PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=int(_env("SESSION_DAYS", "30") or 30)),
    )

    def _bearer_ok():
        if not api_token:
            return False
        auth = request.headers.get("Authorization", "")
        if not auth.lower().startswith("bearer "):
            return False
        return hmac.compare_digest(auth[7:].strip(), api_token)

    @app.before_request
    def _guard():
        path = request.path
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return None
        if is_logged_in() or _bearer_ok():
            return None
        if path.startswith("/api/"):
            return jsonify({"error": "unauthorized", "login": "/login"}), 401
        nxt = request.full_path if request.query_string else path
        return redirect("/login?next=" + urllib.parse.quote(nxt, safe=""))

    @app.route("/login")
    def login_page():
        if is_logged_in():
            return redirect(_safe_next(request.args.get("next")))
        resp = send_from_directory(base_dir, "login.html")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/api/login", methods=["POST"])
    def api_login():
        ip = _client_ip()
        if _too_many_fails(ip):
            return jsonify({"ok": False, "error": "尝试次数过多，请 10 分钟后再试"}), 429
        body = request.get_json(force=True, silent=True) or {}
        given = str(body.get("password") or "")
        if not given or not hmac.compare_digest(given, password):
            _record_fail(ip)
            time.sleep(0.6)  # 拉长暴力破解成本
            return jsonify({"ok": False, "error": "密码不正确"}), 401
        session.clear()
        session.permanent = bool(body.get("remember", True))
        session["pw"] = pw_fingerprint
        return jsonify({"ok": True, "next": _safe_next(body.get("next"))})

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect("/login")

    print("[auth] 访问控制已开启：浏览器需登录，自动化调用使用 Bearer API_TOKEN", flush=True)

