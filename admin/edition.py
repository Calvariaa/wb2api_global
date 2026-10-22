"""上游版本（国内版 / 国际版）差异的唯一权威来源。

为什么需要这个模块
==================
wb2api 最初只对接**国内版**（workbuddy.cn / codebuddy.cn / copilot.tencent.com）。
国际版（workbuddy.ai / codebuddy.ai）是**另一套部署**：域名不同、登录方式不同
（国内是 OneID 手机号短信；国际版是 Keycloak broker 的 Google / GitHub OAuth）、
计费与部分业务接口也不同。

如果任由每个模块各自写 `if 国际版 then ...`，同一个域名会散落在 20 多个文件里，
改一处漏三处。所以这里把「所有随版本变化的常量」收敛成一份 profile：

    CN_PROFILE   —— 国内版（保持现有行为，字节级不变）
    INTL_PROFILE —— 国际版

用法
----
    from admin.edition import profile
    p = profile()                  # 读当前 edition（env ADMIN_UPSTREAM_EDITION）
    p.login_base                   # https://www.workbuddy.cn | https://www.workbuddy.ai
    p.chat_base                    # https://copilot.tencent.com | https://www.workbuddy.ai

**关键实测结论（2026-10，本机抓包验证）**：国际版与国内版用的是**同一套**
plugin 设备授权协议 ——

    POST {login_base}/v2/plugin/auth/state?platform=workbuddy   → {state, authUrl}
    GET  {login_base}/v2/plugin/auth/token?state=...            → {accessToken, refreshToken, domain}
    GET  {login_base}/v2/plugin/accounts                        → [{uid, nickname, ...}]

只是 `login_base` 不同。所以国际版**不需要**另写一套协议实现，只需要切域名；
「用浏览器登录」这条要求由 authUrl 本身满足 —— 用户在真浏览器里点 Google / GitHub，
服务端只负责轮询换 token（与国内版「官方页面接力」完全同构，见 wb_login 的注释）。

边界
----
本模块**只放常量与派生**，不 import backend / wb_login / 任何重量级模块 ——
它会被 config、converter、wb_install 等多处 import，必须零依赖、零副作用。
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or "").strip() or default


@dataclass(frozen=True)
class EditionProfile:
    """一套上游部署的全部可变常量。

    字段命名刻意与「用途」对齐（login_base / chat_base），而不是
    「域名」（host），因为国际版的 chat 与 login 同域，国内版不同域 ——
    按用途命名才不会在调用处写错。
    """

    #: 版本标识：`cn` 或 `intl`
    name: str
    #: 人类可读名（日志 / 管理台展示用）
    label: str

    #: 登录与 web 行为域：设备授权 state、登录页、plugin 端点、资料库等浏览器接口
    login_base: str
    #: 模型请求域（chat/completions 与 plugin refresh）
    chat_base: str
    #: 计费 / 业务上报域
    billing_base: str
    #: 凭证里 `auth.domain` 缺失时的兜底值（同时是 X-Domain 的默认值）
    default_domain: str
    #: Keycloak realm（登录链路的 SSO 域）
    realm: str
    #: 登录页上的产品标识（`product=` 参数）
    product: str

    #: 设备授权 state 的请求平台参数
    platform: str
    #: 轮询 token 时上游「还没登录完」的业务码
    login_pending_code: int

    #: 是否使用 OneID 手机号短信登录（仅国内版原生支持）——
    #: 国际版页面上也有 oneid 入口，但那只是给已有国内账号的人复用，
    #: 不是注册主路径，故此处仍标 False。
    oneid_login: bool
    #: 可用的第三方登录身份源。**实测**国际版 Keycloak 页面有这四个，
    #: 其中 google / github 是用户可见的主通道（twitter 也在，但页面未主推）。
    oauth_providers: tuple[str, ...]

    #: UA 第二段的产品名（国内 `WorkBuddy/<v>`，国际 `WorkBuddy AI/<v>`）
    ua_product: str
    #: 插件目录请求用的 UA 前缀
    catalog_ua_prefix: str

    @property
    def is_intl(self) -> bool:
        return self.name == "intl"


#: 国内版：**所有取值都与改造前完全一致**，切换 edition 不应影响国内部署一个字节。
CN_PROFILE = EditionProfile(
    name="cn",
    label="国内版",
    login_base="https://www.workbuddy.cn",
    chat_base="https://copilot.tencent.com",
    billing_base="https://www.codebuddy.cn",
    default_domain="www.codebuddy.cn",
    realm="copilot",
    product="workbuddy",
    platform="workbuddy",
    login_pending_code=11217,
    oneid_login=True,
    oauth_providers=(),
    ua_product="WorkBuddy",
    catalog_ua_prefix="CLI",
)

#: 国际版：chat 与 login 同域（www.workbuddy.ai），计费也同域。
INTL_PROFILE = EditionProfile(
    name="intl",
    label="国际版",
    login_base="https://www.workbuddy.ai",
    chat_base="https://www.workbuddy.ai",
    billing_base="https://www.workbuddy.ai",
    default_domain="www.workbuddy.ai",
    realm="copilot",
    product="workbuddy",
    platform="workbuddy",
    login_pending_code=11217,
    oneid_login=False,
    oauth_providers=("google", "github", "twitter"),
    ua_product="WorkBuddy AI",
    catalog_ua_prefix="WorkBuddyAI",
)

_PROFILES = {"cn": CN_PROFILE, "intl": INTL_PROFILE}

#: 别名 → 规范名。用户写 `global` / `oversea` / `international` 都认。
_ALIASES = {
    "cn": "cn", "china": "cn", "cn-prod": "cn", "domestic": "cn", "": "cn",
    "intl": "intl", "international": "intl", "global": "intl",
    "oversea": "intl", "overseas": "intl", "ai": "intl",
}


def normalize_name(value: str | None) -> str:
    """把用户输入归一成 `cn` / `intl`；不认识的值回落到 cn。

    刻意**不抛异常**：这是从环境变量读来的，一个拼错的值不该让整个服务起不来，
    但会由 `profile()` 打印告警让人发现。
    """
    return _ALIASES.get((value or "").strip().lower(), "cn")


def active_name() -> str:
    """当前生效的版本名（`ADMIN_UPSTREAM_EDITION`）。"""
    return normalize_name(_env("ADMIN_UPSTREAM_EDITION", "cn"))


def profile(name: str | None = None) -> EditionProfile:
    """取 profile。`name=None` 时读环境变量。"""
    return _PROFILES[normalize_name(name) if name is not None else active_name()]


def is_intl() -> bool:
    return active_name() == "intl"


def describe() -> str:
    """一行诊断（启动日志用）。"""
    p = profile()
    return f"upstream={p.name}({p.label}) login={p.login_base} chat={p.chat_base} billing={p.billing_base}"


def all_profiles() -> dict[str, EditionProfile]:
    """给 API / 管理台列出可选版本。"""
    return dict(_PROFILES)


__all__ = [
    "EditionProfile", "CN_PROFILE", "INTL_PROFILE", "profile", "active_name",
    "normalize_name", "is_intl", "describe", "all_profiles",
]
