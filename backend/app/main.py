"""ASGI 应用入口。

FastAPI/Uvicorn 导入本模块后创建应用实例，并执行
``app.api.app`` 中定义的启动和关闭流程。
"""

from app.api.app import create_app

app = create_app()
