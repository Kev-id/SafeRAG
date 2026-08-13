"""GET /api/v1/tasks — 返回可用的任务类型，供前端下拉框使用。"""

from fastapi import APIRouter
from pydantic import BaseModel

from backend.services.template_service import list_templates

router = APIRouter(prefix="/api/v1")


class TaskInfo(BaseModel):
    key: str
    name: str
    description: str


class TaskListResponse(BaseModel):
    tasks: list[TaskInfo]


@router.get("/tasks", response_model=TaskListResponse)
async def list_tasks():
    """列出所有任务类型（只暴露 key/name/description，不暴露 prompt）。"""
    return TaskListResponse(
        tasks=[
            TaskInfo(key=t.key, name=t.name, description=t.description)
            for t in list_templates()
        ]
    )
