"""GET /api/v1/tasks — 返回可用的任务类型，供前端下拉框使用。"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.services.template_service import list_templates, get_template

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

@router.get("/tasks/{task_key}", response_model=TaskInfo)
async def get_task(task_key: str):
    """列出指定key任务的信息"""
    try:
        t=get_template(task_key)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"任务不存在:{task_key}")
    return TaskInfo(
        key=t.key,
        name=t.name,
        description=t.description
    )
