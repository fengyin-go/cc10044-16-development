"""业务路由汇总：清理作业领域路由优先注册，再注册通用资源路由。

清理作业的签认/计划/执行固定路径必须排在 /api/{module} 动态路由之前，
否则固定路径会被动态路由抢先匹配。
"""
from __future__ import annotations

from app.routers import cleanup, resource

ROUTERS = [cleanup, resource]
