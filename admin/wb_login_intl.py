"""WorkBuddy 国际版登录 —— 浏览器 OAuth 授权 + 服务端轮询换凭证。

为什么单独一个模块
==================
国内版和国际版**协议相同、域名不同**（实测，见 `admin/edition.py`）。
所以这里不复制 `wb_login` 的整套逻辑，而是只补国际版独有的三件事：

  1. 域名从 profile 取（`login_base`），而不是模块级写死；
  2. Keycloak broker 的身份源是 Google / GitHub（国内版是 OneID 短信），
     要能探测当前有哪些身份源、并生成直连某个身份源的授权 URL；
  3. 整个流程不碰短信、不碰验证码、也不碰用户会话 cookie ——
     用户在**自己的浏览器**里完成第三方 OAuth，服务端只拿 state 换 token。

为什么这条路可行（关键实测）
----------------------------
* `POST {login_base}/v2/plugin/auth/state?platform=workbuddy` 返回
  `{state, authUrl}`，国际版与国内版**完全同构**（实测 2026-10）。
* `GET {login_base}/v2/plugin/auth/token?state=...` **不需要任何 cookie**，
  只认 URL 里的 state —— 所以「谁去轮询」不影响结果，服务端可以零会话地
  反复 poll，用户在哪个浏览器、哪台机器登录都无所谓。
* 国际版登录页 `/login?platform=workbuddy&state=...` 会拉起本站 Keycloak
  （`/auth/realms/copilot/...client_id=console`），Keycloak 页面上有
  `social-google` / `social-github` 两个 broker 按钮，走标准 OAuth 跳转。

安全边界
========
本模块**不做**任何自动化登录、不代填账号密码、不绕验证码。它只负责：
开始一次设备授权、把官方 authUrl 交给用户、轮询结果。
第三方 OAuth 的账号密码只存在于用户自己的浏览器与 Google/GitHub 之间。
"""
from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from admin import edition

_logger = logging.getLogger("admin.wb_login_intl")

# ---------------------------------------------------------------------------
# 上游常量（随 edition 变化的部分一律走 profile）
# ---------------------------------------------------------------------------

#: 浏览器 UA。登录链路全程走 Web 形态，不能用桌面端 UA。
WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

#: 一次登录会话的有效期（秒）。用户在登录页停留超过这个时间就作废重来。
SESSION_TTL = 900

#: 单次 HTTP 超时（秒）。登录链路每一步都是短小 API 调用，收紧到 10s，
#: 异常时快速失败、快速释放线程（理由同 wb_login，见其注释）。
HTTP_TIMEOUT = 10
HTTP_CONNECT_TIMEOUT = 5

#: 会话表上限。每个会话持有一个 httpx.Client（含连接池），是有成本的资源。
MAX_SESSIONS = 500
#: 会话回收间隔（秒）。
GC_INTERVAL_S = 60.0
#: 全局并发闸门：同一时刻允许在途的「上游登录请求」总数。
MAX_CONCURRENT_UPSTREAM = 8

#: 上游「还没登录完」的业务码（实测国内/国际同为 11217 `login ing...`）。
PENDING_CODES = (11217,)
#: 上游「state 不存在 / 已过期」类业务码。
DEAD_STATE_CODES = (11218, 11219, 11220, 40001, 40101)

#: Keycloak 页面上 broker 按钮的 id 形如 `social-google` / `social-github`。
_BROKER_ANCHOR_RE = re.compile(
    r'href="(?P<href>/auth/realms/[^"]*?/broker/(?P<provider>[a-z0-9_-]+)/login[^"]*)"',
    re.I)


class WbIntlLoginError(Exception):
    """登录链路的可展示错误。`step` 便于前端定位；`upstream` 保留原文。"""

    def __init__(self, message: str, *, step: str = "", upstream: Any = None):
        super().__init__(message)
        self.message = message
        self.step = step
        self.upstream = upstream


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------

@dataclass
class BrowserLoginSession:
    """一次国际版浏览器授权会话。"""

    sid: str
    created: float
    client: httpx.Client
    edition_name: str
    #: 设备授权 state（换 token 的凭据）。
    state: str = ""
    #: 官方 authUrl（给用户打开的地址）。
    auth_url: str = ""
    #: 绑定的身份源（google / github / 空=让用户自己选）。
    provider: str = ""
    #: 最近一次 poll 的时间（节流用）。
    last_poll_at: float = 0.0
    #: 一次登录只允许成功一次。
    consumed: bool = False
    #: 串行化状态变更（防并发 poll 各打一次上游）。
    lock: threading.RLock = field(default_factory=threading.RLock)
    #: 已换到的凭证（幂等复用）。
    result_cache: dict | None = None
    consumed_at: float = 0.0

    def expired(self) -> bool:
        return (time.time() - self.created) > SESSION_TTL

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass


_sessions: dict[str, BrowserLoginSession] = {}
_sessions_lock = threading.RLock()
_gc_thread: threading.Thread | None = None
_gc_stop = threading.Event()


class _Slot:
    """上游并发闸门（与 wb_login 同款：非阻塞获取 + 超时）。"""

    def __init__(self, limit: int):
        self._sem = threading.BoundedSemaphore(limit)

    def acquire(self, timeout: float = 0.0) -> bool:
        return self._sem.acquire(timeout=timeout) if timeout > 0 else self._sem.acquire(blocking=False)

    def release(self) -> None:
        try:
            self._sem.release()
        except ValueError:
            pass


_UPSTREAM_SLOT = _Slot(MAX_CONCURRENT_UPSTREAM)


def upstream_slot(what: str = ""):
    """获取一个上游调用名额（上下文管理器风格）。拿不到直接抛错，不排队。"""
    class _Ctx:
        def __enter__(self_inner):
            if not _UPSTREAM_SLOT.acquire():
                raise WbIntlLoginError(
                    f"登录请求过于频繁，请稍后重试{('（' + what + '）') if what else ''}",
                    step="busy")
            return self_inner

        def __exit__(self_inner, *exc):
            _UPSTREAM_SLOT.release()
            return False

    return _Ctx()


def _gc_locked() -> None:
    now = time.time()
    for sid, sess in list(_sessions.items()):
        # 已成功的会话给 60s 宽限期（前端可能还要回读一次结果）
        if sess.consumed and (now - sess.consumed_at) > 60:
            sess.close()
            _sessions.pop(sid, None)
        elif sess.expired():
            sess.close()
            _sessions.pop(sid, None)


def _gc_loop() -> None:
    while not _gc_stop.wait(GC_INTERVAL_S):
        try:
            with _sessions_lock:
                _gc_locked()
        except Exception:  # 回收线程绝不能死
            _logger.exception("wb_login_intl: 会话回收异常")


def start_gc_thread() -> None:
    global _gc_thread
    with _sessions_lock:
        if _gc_thread and _gc_thread.is_alive():
            return
        _gc_stop.clear()
        _gc_thread = threading.Thread(target=_gc_loop, name="wb-intl-login-gc", daemon=True)
        _gc_thread.start()


def stop_gc_thread() -> None:
    _gc_stop.set()


def session_count() -> int:
    with _sessions_lock:
        return len(_sessions)


def get_session(sid: str) -> BrowserLoginSession:
    with _sessions_lock:
        sess = _sessions.get(sid)
    if sess is None:
        raise WbIntlLoginError("登录会话不存在或已过期，请重新发起登录", step="session")
    if sess.expired():
        sess.close()
        with _sessions_lock:
            _sessions.pop(sid, None)
        raise WbIntlLoginError("登录会话已过期，请重新发起登录", step="session")
    return sess


def _register(sess: BrowserLoginSession) -> None:
    with _sessions_lock:
        _gc_locked()
        if len(_sessions) >= MAX_SESSIONS:
            sess.close()
            raise WbIntlLoginError("登录会话数已达上限，请稍后重试", step="busy")
        _sessions[sess.sid] = sess


def _new_session() -> BrowserLoginSession:
    p = edition.profile()
    client = httpx.Client(
        timeout=httpx.Timeout(HTTP_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT),
        follow_redirects=False,
        headers={"User-Agent": WEB_UA, "Accept-Language": "en-US,en;q=0.9"})
    return BrowserLoginSession(
        sid=secrets.token_urlsafe(16), created=time.time(), client=client,
        edition_name=p.name)


# ---------------------------------------------------------------------------
# 上游小工具
# ---------------------------------------------------------------------------

def _login_headers(extra: dict | None = None) -> dict:
    p = edition.profile()
    h = {
        "User-Agent": WEB_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": p.login_base,
        "Referer": p.login_base + "/",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/json",
    }
    h.update(extra or {})
    return h


def _upstream_json(resp: httpx.Response) -> dict:
    try:
        obj = resp.json()
    except Exception:
        raise WbIntlLoginError(
            f"上游返回了非 JSON 响应（HTTP {resp.status_code}）",
            step="upstream", upstream=resp.text[:400]) from None
    if not isinstance(obj, dict):
        raise WbIntlLoginError("上游返回结构异常", step="upstream", upstream=obj)
    return obj


def _envelope_data(obj: dict, step: str) -> dict:
    code = obj.get("code")
    if code != 0:
        raise WbIntlLoginError(
            f"上游拒绝请求：{obj.get('msg') or code}", step=step, upstream=obj)
    data = obj.get("data")
    if not isinstance(data, dict):
        raise WbIntlLoginError("上游未返回 data", step=step, upstream=obj)
    return data


# ---------------------------------------------------------------------------
# 第 1 步：探测身份源 + 开始设备授权
# ---------------------------------------------------------------------------

def _keycloak_auth_url(redirect_uri: str, prof: edition.EditionProfile | None = None) -> str:
    """本站 Keycloak 的 authorize URL（intl 登录页背后就是它）。

    参数形状照抄国际版登录前端（见 download.codebuddy.ai 的登录包）：
    `client_id=console&response_type=code&redirect_uri=<本站回调>&v=2210&product=workbuddy`

    `prof` 显式传入而不是内部 `edition.profile()`：`profile()` 无参时读环境变量，
    而本模块的调用点可能持有**另一个** edition（例如测试里临时切 intl）。
    混用会让 authorize 走 `.cn`、broker 锚点却按 `.ai` 拼，Keycloak 直接
    返回 `400 Cookie not found` —— 用户看到的是一页错误 HTML，不是授权页。
    """
    p = prof or edition.profile()
    import urllib.parse
    q = urllib.parse.urlencode({
        "client_id": "console",
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "v": "2210",
        "product": p.product,
    }, quote_via=urllib.parse.quote)
    return f"{p.login_base}/auth/realms/{p.realm}/protocol/openid-connect/auth?{q}"


def _state_return_url(device_state: str, prof: edition.EditionProfile | None = None) -> str:
    """设备授权 state 的「回跳页」——就是国际版登录页 `/login`。

    ⚠️ 这个函数被改过三次，每一次都是因为「猜」而不是因为「读代码」。
       最终结论来自官方登录包的反编译（`index-BoJ3AJkv.js` = `qa` 组件 `We`），
       也就是国际版 `/login` 路由真正渲染的那个东西：

        const i = _.get("platform") || "website"
        const c = _.get("state") || ""          // ← 设备 state 就从这里读
        ...
        const C = async e => {
          const n = await W()                    // 拿 console webtoken
          await ee({enterpriseId:e.enterpriseId, state:c, domain:hostname, webtoken:n, ...})
          //     ↑ ee = POST /console/login/enterprise?state=<设备state>
          //        **绑定就发生在这里**
          if (U) { s(`/login/started?platform=${i}&state=${c}`); return }
          setTimeout(() => { window.location.href = w }, 1000)
        }
        const R = async () => {
          const e = await W(); const {accounts:n} = await re(hostname, e)
          if (n.length === 1) { await C(n[0]); return }   // 单账号 → 自动绑定
          ...s(`/login/select?...`)                        // 多账号 → 让用户选
        }

    **所以 redirect_uri 就该是 `/login?platform=workbuddy&state=<设备state>`：
    设备 state 是登录页组件的输入，绑定由它自己完成。**

    为什么不能用 `/login/oneid-callback`（第二轮的错误）
    --------------------------------------------------
    国际版的 `Fe = window.IS_INTERNATIONAL_EDITION === 'true'` 为真
    （服务端注入的就是 `'true'`），而路由表按 `Fe` 二选一：

        Fe ? <Route path="/login/oneid-callback" .../> : <只有国际版那套>

    国际版**根本没有 `oneid-callback` 路由**。给它 200 只是 SPA 外壳的 index.html，
    路径匹配落到 `*` → `<Po to="/login" replace/>` → 滑到 `/login`。
    而且 `oneid-callback` 里的绑定函数 `C` 第一行是

        const p = d.current?.contentWindow?.__ONEID__?.loginUrl
        if (p) { ... }        // ← p 为 undefined 就整块跳过，什么都不做

    它依赖 OneID 的隐藏 iframe 注入 `__ONEID__`，那是国内 OneID 体系，
    国际版走 Keycloak 直连，这条路不存在。

    为什么不能是裸 `redirect_uri`（第三轮的错误）
    -------------------------------------------
    `C` 结尾有 `setTimeout(() => window.location.href = w, 1000)`，
    而 `w` 是 query 里的 `redirect_uri`（经 `sanitizeRedirectUri`），
    为空时 `w = S()`（当前 search 原样）。绑定之后浏览器通常会跳回 `/console/*`，
    那正是用户看到的 `www.workbuddy.ai/console/accounts` 账号 JSON ——
    **那是绑定成功后的正常落点，不是错误**。只要 `state` 进得来，绑定就成立。

    为什么 redirect_uri 不能用我们自己的域名
    ----------------------------------------
    Keycloak 对 `client_id=console` 做 redirect_uri 白名单：
    `https://www.workbuddy.ai/login/` → 200；`http://127.0.0.1:8791/cb` → **400**。
    所以回跳必须留在 workbuddy.ai 上，设备 state 只能靠**查询串**捎带 ——
    这正好是 `We` 组件读 `state` 的方式。

    ⚠️ 为什么**必须**带结尾斜杠 `/login/`（第四轮的真因，唯一真因）
    --------------------------------------------------------------
    不带斜杠的 `/login` 会被 nginx **301 到 `/login/`**：

        GET https://www.workbuddy.ai/login?platform=workbuddy&state=ST123
        → 301 Location: /login/?platform=workbuddy&state=ST123
        （有些客户端/中间层直接丢查询串，或浏览器认为「同源目录补斜杠」）

    实测（2026-10，`_probe_slash.py`）：

        as-built ru : .../login?platform=workbuddy&state=SLASHOK
          nginx     : 301 -> /login/?platform=workbuddy&state=SLASHOK
        as-built ru : .../login/?platform=workbuddy&state=SLASHOK
          nginx     : 200 -> (no redirect)          ← 干净，state 原样到达 SPA

    301 是**永久重定向**，浏览器会把它缓存；而且这层跳转发生在 SPA 加载之前，
    `We` 组件根本没机会读 `state` —— 于是绑定不成立、`auth/token` 永远
    `login ing...`，用户看到的却是「登录成功了，跳到了 /console/accounts」。

    这一条和前几轮所有猜测都无关：redirect_uri 一直是白名单放行的，
    `client_data` 也确实被 Keycloak 透传（解 Google state 尾段能读出
    `{"ru":"...state=SLASHOK","rt":"code"}`）。**只是发到浏览器后，
    在 nginx 那一跳把 state 弄丢了。**

    结论：拼 `/login/`，一个字符都不能少。
    """
    p = prof or edition.profile()
    return f"{p.login_base}/login/?platform={p.platform}&state={device_state}"


def _client_data_for(return_url: str) -> str:
    """构造 Keycloak broker 的 client_data（base64url JSON）。"""
    import base64
    import json as _json
    raw = _json.dumps({"ru": return_url, "rt": "code"}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def list_providers() -> list[dict]:
    """探测国际版当前可用的第三方身份源（Google / GitHub …）。

    做法：请求一次 Keycloak authorize，解析服务端渲染页面里的
    `broker/<provider>/login` 链接。**只读**，不创建任何会话。
    """
    p = edition.profile()
    # 探测用与正式链路一致的 redirect_uri（白名单按路径匹配，查询串不参与）。
    redirect = _state_return_url("probe", p)
    url = _keycloak_auth_url(redirect, p)
    with upstream_slot("providers"):
        try:
            resp = httpx.get(url, headers=_login_headers({"Accept": "text/html"}),
                             timeout=httpx.Timeout(HTTP_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT),
                             follow_redirects=True)
        except httpx.HTTPError as e:
            raise WbIntlLoginError(f"探测登录身份源失败：{e}", step="providers") from e
    return _parse_providers(resp.text)


def _parse_providers(html: str) -> list[dict]:
    """从 Keycloak 登录页解析第三方身份源。

    **`oneid` 会被剔除。** 它不是一个出站 OAuth 身份源，而是 Keycloak 内部转回
    OneID 组件的入口：点它走的是 `vendor-oneid` 那套 iframe/短信流程，回跳是
    `/login/oneid-callback` 的另一套语义，**不携带设备 state**。实际表现就是
    用户点进去、登录成功、然后程序一直等 —— 与 `www.workbuddy.ai/console/accounts`
    空等是同一类死路。与其在 UI 上留一个必然失败的按钮，不如不展示。
    """
    _SKIP = {"oneid"}
    out: list[dict] = []
    seen: set[str] = set()
    for m in _BROKER_ANCHOR_RE.finditer(html or ""):
        prov = m.group("provider").lower()
        if prov in seen or prov in _SKIP:
            continue
        seen.add(prov)
        out.append({
            "id": prov,
            "label": {"google": "Google", "github": "GitHub",
                      "twitter": "X (Twitter)"}.get(prov, prov.title()),
            "primary": prov in ("google", "github"),
        })
    return out


def start_login(provider: str = "") -> BrowserLoginSession:
    """开始一次浏览器授权：POST auth/state → 拿 state + 官方 authUrl。

    ## 唯一正确的做法：把官方 authUrl **原样**交给浏览器

    ⚠️ 这一版是推倒重来后的实现。前面几版全错在同一个地方：
    **服务端替浏览器走了 Keycloak 的那几步。**

    实测证据（`_trace_chain.py`，2026-10）：

        authorize  → Set-Cookie: AUTH_SESSION_ID=6713054b-...keycloaky-3-9227
        broker/google/login  → 只认这个 cookie

        GET broker/google/endpoint?code=..&state=..   （全新 client，无 cookie）
          → 400 "未找到 cookie。请确保浏览器已启用 cookie。"
        GET broker/google/endpoint?code=..&state=..   （持有 AUTH_SESSION_ID）
          → 502（过了 cookie 校验，进入下一阶段）

    用户在浏览器里看到的那页「未找到 cookie / 账号访问受限」，
    就是第一种情况的 400 页面。因为 `AUTH_SESSION_ID` 在**服务端的 httpx
    cookie 罐**里，用户浏览器从来没有过它 —— 而 Google 只会把 code 送回
    `/auth/realms/copilot/broker/google/endpoint`（由 `client_id=console`
    的服务端配置写死，我们改不了），所以那条路对用户的浏览器必然 400。

    ## 正确链路（每一步都由浏览器发起，cookie 自然齐全）

        浏览器 GET  authUrl = /login?platform=workbuddy&state=<设备state>
          ↓ 301（nginx 补斜杠，查询串保留）
        /login/  → SPA 加载 → `We` 组件读 state → 自己跳 Keycloak authorize
          ↓ 这一跳是浏览器发的，AUTH_SESSION_ID 落到浏览器 ✔
        accounts.google.com → 用户登录
          ↓
        /broker/google/endpoint  → 浏览器带 cookie → 200 ✔
          ↓ 按 client_data 的 `ru` 跳回
        /login/?platform=..&state=<设备state>&code=..  → `We` 读到 state+code
          ↓  绑定走 POST /console/login/enterprise?state=<设备state>
        服务端轮询 /v2/plugin/auth/token → 拿到 accessToken

    所以 `provider` **不再用来改写 URL**：选哪个身份源由用户在 Keycloak
    页面上点（那是官方页面，按钮就是 Google / GitHub / X）。我们只记录
    用户选了谁，用于「等待 X 授权…」的文案。

    `authUrl` 里的 /login（无斜杠）会被 301，这是官方自己给的形状，
    查询串在 301 里是保留的，`We` 照样能读到 state —— 所以不改写它。
    """
    p = edition.profile()
    sess = _new_session()
    try:
        with upstream_slot("auth_state"):
            resp = sess.client.post(
                f"{p.login_base}/v2/plugin/auth/state?platform={p.platform}",
                json={}, headers=_login_headers())
        data = _envelope_data(_upstream_json(resp), "auth_state")
        state = str(data.get("state") or "")
        auth_url = str(data.get("authUrl") or "")
        if not state or not auth_url:
            raise WbIntlLoginError("上游未返回授权地址", step="auth_state")
        sess.state = state
        sess.auth_url = auth_url

        prov = (provider or "").strip().lower()
        if prov and prov not in p.oauth_providers:
            raise WbIntlLoginError(
                f"{p.label}不支持身份源 {prov}；可用：{'/'.join(p.oauth_providers)}",
                step="provider")
        # 只记录选择，不改写 URL。
        sess.provider = prov
    except WbIntlLoginError:
        sess.close()
        raise
    except Exception as e:
        sess.close()
        raise WbIntlLoginError(f"发起登录失败：{e}", step="start") from e

    _register(sess)
    return sess


def _unused_broker_helper_note() -> None:
    """占位：旧版 `_broker_url_from_auth` 已删除。

    它把「服务端替浏览器走 Keycloak」当成可行路线，实测必然 400
    （`AUTH_SESSION_ID` 在服务端 cookie 罐里，用户浏览器没有）。
    正确做法见 `start_login` 的 docstring：authUrl 原样交给浏览器。
    """
# ---------------------------------------------------------------------------
# 第 2 步：轮询换凭证
# ---------------------------------------------------------------------------

def poll(sess: BrowserLoginSession, *, min_interval: float = 1.5) -> dict | None:
    """轮询一次。未完成返回 None；完成返回 token dict。

    节流**先于请求**执行：前端连点/重试不会打到上游。
    """
    now = time.time()
    if now - sess.last_poll_at < min_interval:
        return None
    sess.last_poll_at = now

    p = edition.profile()
    with upstream_slot("poll"):
        resp = sess.client.get(
            f"{p.login_base}/v2/plugin/auth/token?state={sess.state}",
            headers=_login_headers())
    obj = _upstream_json(resp)
    code = obj.get("code")
    msg = str(obj.get("msg") or "")
    if code in PENDING_CODES or "login ing" in msg.lower():
        return None
    if code in DEAD_STATE_CODES:
        raise WbIntlLoginError("授权已失效，请重新发起登录", step="state_dead", upstream=obj)
    if code != 0:
        raise WbIntlLoginError(f"换取凭证失败：{msg or code}", step="auth_token", upstream=obj)
    data = obj.get("data")
    if not isinstance(data, dict) or not data.get("accessToken"):
        raise WbIntlLoginError("上游未返回 accessToken", step="auth_token", upstream=obj)
    return data


def fetch_account(sess: BrowserLoginSession, token: dict) -> dict:
    """用 accessToken 取账号信息（uid / nickname / enterpriseId）。"""
    p = edition.profile()
    from admin import wb_login  # 复用其账号信息解析（与国内版同构）
    domain = token.get("domain") or p.default_domain
    try:
        with upstream_slot("account"):
            resp = sess.client.get(
                f"{p.login_base}/v2/plugin/accounts",
                headers=_login_headers({
                    "Authorization": "Bearer " + str(token["accessToken"]),
                    "X-Domain": domain,
                    "X-No-User-Id": "true",
                    "X-No-Enterprise-Id": "true",
                    "X-No-Department-Info": "true",
                }))
        obj = _upstream_json(resp)
        if obj.get("code") == 0:
            accts = ((obj.get("data") or {}).get("accounts")) or []
            if accts:
                return accts[0]
    except WbIntlLoginError:
        raise
    except Exception as e:
        _logger.warning("wb_login_intl: 取账号信息失败（继续用 token 里的信息）：%s", e)
    return {}


def finish(sess: BrowserLoginSession) -> dict:
    """完成登录：换 token → 取账号 → 组装与桌面端同构的 `.info` 凭证。

    返回 `{"credential": str, "meta": dict, "token": dict}`。
    """
    from admin import wb_login
    with sess.lock:
        if sess.consumed and sess.result_cache:
            return sess.result_cache
        token = poll(sess)
        if token is None:
            raise WbIntlLoginError("登录尚未完成", step="pending")
        account = fetch_account(sess, token)
        # 国际版凭证形状与国内版完全一致（account/auth），所以复用同一个组装器，
        # 只把 domain 兜底换成国际版域名。
        token = dict(token)
        token.setdefault("domain", edition.profile().default_domain)
        built = wb_login.build_credential_json(token, account)
        check = wb_login.validate_credential(built)
        if not check.ok:
            raise WbIntlLoginError(f"生成的凭证未通过自检：{check.reason}", step="selfcheck")
        result = {"credential": built, "meta": check.meta or {}, "token": token}
        sess.result_cache = result
        sess.consumed = True
        sess.consumed_at = time.time()
        _logger.info("wb_login_intl: 登录完成 uid=%s provider=%s",
                     (check.meta or {}).get("uid"), sess.provider or "auto")
        return result


__all__ = [
    "WbIntlLoginError", "BrowserLoginSession", "start_login", "poll", "finish",
    "list_providers", "get_session", "session_count", "start_gc_thread",
    "stop_gc_thread", "upstream_slot",
]
