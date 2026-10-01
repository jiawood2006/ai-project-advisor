"""统一接入层：一个引擎，多平台适配器。见 core.PLATFORMS"""
from .core import Message, TenantRegistry, REGISTRY, dispatch, DEDUP, PLATFORMS

__all__ = ["Message", "TenantRegistry", "REGISTRY", "dispatch", "DEDUP", "PLATFORMS"]
