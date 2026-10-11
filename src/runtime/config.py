"""Fixed Fudan integration endpoints used by the authenticated online client."""

from __future__ import annotations

import os


STUDENT_ID = os.environ.get("StuId", "")
PASSWORD = os.environ.get("UISPsw", "")

WEBVPN_BASE = "https://webvpn.fudan.edu.cn"
IDP_BASE = "https://id.fudan.edu.cn"
ICOURSE_BASE = "https://icourse.fudan.edu.cn"

WEBVPN_AES_KEY = b"wrdvpnisthebest!"
WEBVPN_AES_IV = b"wrdvpnisthebest!"

TENANT_CODE = "222"
GROUP_CODE = "2095000001"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


__all__ = [
    "GROUP_CODE",
    "ICOURSE_BASE",
    "IDP_BASE",
    "PASSWORD",
    "STUDENT_ID",
    "TENANT_CODE",
    "USER_AGENT",
    "WEBVPN_AES_IV",
    "WEBVPN_AES_KEY",
    "WEBVPN_BASE",
]
