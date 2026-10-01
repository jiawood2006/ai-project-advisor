from .base import Adapter
from .dingtalk import DingTalkAdapter
from .wecom_aibot import WeComAIBotAdapter
from .wecom_app import WeComAppAdapter
from .h5 import H5Adapter

ADAPTERS = {
    "dingtalk": DingTalkAdapter,
    "wecom_aibot": WeComAIBotAdapter,
    "wecom_app": WeComAppAdapter,
    "h5": H5Adapter,
}

def get_adapter(platform):
    return ADAPTERS[platform]()
