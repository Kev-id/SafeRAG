"""模板 API — 用户自定义报告模板（CRUD）。

GET    /api/v1/templates              → 我的 + 系统示例（带派生编号）
POST   /api/v1/templates              → 新建（owner = 当前用户）
GET    /api/v1/templates/{id}         → 详情（仅本人/系统可见）
PATCH  /api/v1/templates/{id}         → 编辑自己的（系统示例只读 403）
DELETE /api/v1/templates/{id}         → 删除自己的（系统示例 403）

节编号不存库，由索引派生（一、二、三…）：增删节自动重排。
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from backend.repositories.template_repo import section_no
from backend.services import operation_log_service, template_service
from backend.services.auth_service import perm_user_sys_sec, perm_user_sys_sec_aud

router = APIRouter(prefix="/api/v1", tags=["templates"])


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class SectionSchema(BaseModel):
    title: str = Field(..., min_length=1, max_length=100, description="节标题，不带序号")
    instruction: str = Field("", max_length=4000, description="本节撰写要求/解释")


class TemplateCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field("", max_length=500)
    sections: list[SectionSchema] = Field(..., min_length=1, max_length=100)


class TemplateUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = Field(None, max_length=500)
    sections: list[SectionSchema] | None = Field(None, min_length=1, max_length=100)


class TemplateSection(BaseModel):
    no: str
    title: str
    instruction: str


class TemplateItem(BaseModel):
    id: str
    name: str
    description: str
    is_system: bool
    sections: list[TemplateSection]
    created_at: str
    updated_at: str


class TemplateListResponse(BaseModel):
    templates: list[TemplateItem]


def _to_item(t: object) -> TemplateItem:
    """把 Template 转成 API Item（编号由索引派生）。"""
    return TemplateItem(
        id=t.id,
        name=t.name,
        description=t.description,
        is_system=t.is_system,
        sections=[
            TemplateSection(
                no=section_no(i),
                title=s.get("title", ""),
                instruction=s.get("instruction", ""),
            )
            for i, s in enumerate(t.sections)
        ],
        created_at=t.created_at,
        updated_at=t.updated_at,
    )


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------

@router.get("/templates", response_model=TemplateListResponse)
async def list_templates(_user: dict = Depends(perm_user_sys_sec_aud)):
    """列出当前用户可见的模板（系统示例 + 自己的），系统示例排前。"""
    return TemplateListResponse(
        templates=[_to_item(t) for t in template_service.list_mine(_user["id"])]
    )


@router.post("/templates", response_model=TemplateItem, status_code=201)
async def create_template(req: TemplateCreate, request: Request,
                          _user: dict = Depends(perm_user_sys_sec)):
    """新建自己的模板。"""
    sections = [{"title": s.title, "instruction": s.instruction} for s in req.sections]
    t = template_service.create(req.name, req.description, sections, _user["id"])
    ip = request.client.host if request.client else ""
    operation_log_service.record_user(_user, "create_template", target=t.id, ip=ip)
    return _to_item(t)


@router.get("/templates/{template_id}", response_model=TemplateItem)
async def get_template(template_id: str, _user: dict = Depends(perm_user_sys_sec_aud)):
    """模板详情（仅本人/系统示例可见，别人的私人模板不暴露）。"""
    try:
        t = template_service.get_visible(_user["id"], template_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"模板不存在: {template_id}")
    except ValueError:
        raise HTTPException(status_code=403, detail="无权查看该模板")
    return _to_item(t)


@router.patch("/templates/{template_id}", response_model=TemplateItem)
async def update_template(template_id: str, req: TemplateUpdate, request: Request,
                          _user: dict = Depends(perm_user_sys_sec)):
    """编辑模板（改名字/描述/节列表，支持增删节自动重编号）；系统示例只读。"""
    body: dict = {}
    if req.name is not None:
        body["name"] = req.name
    if req.description is not None:
        body["description"] = req.description
    if req.sections is not None:
        body["sections"] = [{"title": s.title, "instruction": s.instruction} for s in req.sections]
    try:
        t = template_service.update(template_id, _user["id"], **body)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"模板不存在: {template_id}")
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    ip = request.client.host if request.client else ""
    operation_log_service.record_user(_user, "update_template", target=template_id, ip=ip)
    return _to_item(t)


@router.delete("/templates/{template_id}", status_code=204)
async def delete_template(template_id: str, request: Request,
                          _user: dict = Depends(perm_user_sys_sec)):
    """删除自己的模板；系统示例不能删。"""
    try:
        template_service.delete(template_id, _user["id"])
    except KeyError:
        raise HTTPException(status_code=404, detail=f"模板不存在: {template_id}")
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    ip = request.client.host if request.client else ""
    operation_log_service.record_user(_user, "delete_template", target=template_id, ip=ip)