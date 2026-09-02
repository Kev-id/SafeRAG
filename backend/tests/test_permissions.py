"""安全权限边界测试 —— 仅测角色判定逻辑，不依赖模型/DB。

针对 backend/services/auth_service.py 中的预定义依赖工厂（require_roles），
依赖名直接枚举放行角色（user/sys/sec/aud）：
  perm_user_sys_sec_aud  登录即可读
  perm_user_sys_sec      业务操作（建/删报告、对话、知识库上传）
  perm_sys_sec_aud       后台监控
  perm_sec               安全保密员专属（知识库删除、敏感标记）
  perm_sys               系统管理员专属（用户管理）
验证权限矩阵边界：普通用户禁监控/禁用户管理、audadmin 只读、
secadmin 专属删除/敏感标记。
"""
import pytest
from fastapi import HTTPException

from backend.services import auth_service as asvc


def call(dep, role):
    """直接调用依赖工厂返回的 dependency(current=...) 判定角色权限。"""
    return dep(current={"role": role, "username": role})


def assert_ok(dep, role):
    assert call(dep, role)["role"] == role  # 通过则返回 current


def assert_denied(dep, role):
    with pytest.raises(HTTPException) as ei:
        call(dep, role)
    assert ei.value.status_code == 403


# ---- perm_user_sys_sec_aud：登录即可读（四角色全放行） ----
@pytest.mark.parametrize("role", ["user", "sysadmin", "secadmin", "audadmin"])
def test_read_all_allows_all(role):
    assert_ok(asvc.perm_user_sys_sec_aud, role)


# ---- 普通用户(user) 边界 ----
def test_user_can_business_write():
    assert_ok(asvc.perm_user_sys_sec, "user")


def test_user_denied_monitor():
    assert_denied(asvc.perm_sys_sec_aud, "user")


def test_user_denied_user_management():
    assert_denied(asvc.perm_sys, "user")


def test_user_denied_kb_delete():
    assert_denied(asvc.perm_sec, "user")


# ---- audadmin：只读，禁业务写、禁知识库写 ----
@pytest.mark.parametrize(
    "dep", [asvc.perm_user_sys_sec, asvc.perm_sec, asvc.perm_sys]
)
def test_audadmin_denied_write_ops(dep):
    assert_denied(dep, "audadmin")


def test_audadmin_allowed_monitor():
    assert_ok(asvc.perm_sys_sec_aud, "audadmin")


def test_audadmin_allowed_read():
    assert_ok(asvc.perm_user_sys_sec_aud, "audadmin")


# ---- secadmin：专属写（删除/敏感）放行，但用户管理禁止 ----
def test_secadmin_allowed_kb_delete():
    assert_ok(asvc.perm_sec, "secadmin")


def test_secadmin_allowed_monitor():
    assert_ok(asvc.perm_sys_sec_aud, "secadmin")


def test_secadmin_denied_user_management():
    assert_denied(asvc.perm_sys, "secadmin")


# ---- sysadmin：用户管理放行，但同样不可删除知识库文件（权限分离） ----
def test_sysadmin_allowed_user_management():
    assert_ok(asvc.perm_sys, "sysadmin")


def test_sysadmin_denied_kb_delete():
    # 三权分立：删除知识库文件是 secadmin 专属，sysadmin 亦不可
    assert_denied(asvc.perm_sec, "sysadmin")