from __future__ import annotations

from fastapi import APIRouter

# 扩展点：所有"可选 / 本地功能"的路由集中登记到这里。
# 这样 api/app.py 只需一次性 include 本列表，未来新增本地功能
# （如其它注册渠道、实验性功能）都只改本文件或让功能模块自注册，
# 不再触碰 api/app.py，从而保证 `merge main` 友好。
_OPTIONAL_ROUTERS: list[APIRouter] = []


def register_optional_router(router: APIRouter) -> None:
    if router not in _OPTIONAL_ROUTERS:
        _OPTIONAL_ROUTERS.append(router)


def get_optional_routers() -> list[APIRouter]:
    return list(_OPTIONAL_ROUTERS)
