"""User API URL validation; kept equivalent to read-paper's outbound rules."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit


class UnsafeOutboundUrl(ValueError):
    pass


async def validate_outbound_url(url: str, *, allow_private: bool = False) -> str:
    value = (url or "").strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise UnsafeOutboundUrl("URL 格式不正确") from exc
    if parsed.scheme.lower() != "https":
        raise UnsafeOutboundUrl("仅允许 HTTPS 地址")
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise UnsafeOutboundUrl("URL 不得包含账号信息，且必须包含有效主机名")
    if port is not None and not 1 <= port <= 65535:
        raise UnsafeOutboundUrl("URL 端口不合法")
    if allow_private:
        return value
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise UnsafeOutboundUrl("不允许访问本机或内网地址")
    try:
        addresses = await asyncio.to_thread(
            socket.getaddrinfo, host, port or 443, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise UnsafeOutboundUrl("URL 主机名无法解析") from exc
    if not addresses:
        raise UnsafeOutboundUrl("URL 主机名无法解析")
    for entry in addresses:
        try:
            address = ipaddress.ip_address(entry[4][0].split("%", 1)[0])
        except ValueError as exc:
            raise UnsafeOutboundUrl("URL 解析结果无效") from exc
        if not address.is_global:
            raise UnsafeOutboundUrl("不允许访问本机、内网、链路本地或保留地址")
    return value
