"""
测试外部目录写守卫：工作区根之外的写/执行需确认（ASK）。
"""
from coding_agent.core.permissions import PermissionPolicy, Decision
from coding_agent.tools.base import ToolPermission as TP


def test_write_inside_root_allowed(tmp_path):
    p = PermissionPolicy(workspace_root=str(tmp_path), auto_approve=True)
    inside = str(tmp_path / "sub" / "file.py")
    assert p.decide("file_write", {"path": inside}, TP.WRITE) == Decision.ALLOW


def test_write_outside_root_asks(tmp_path):
    p = PermissionPolicy(workspace_root=str(tmp_path / "proj"), auto_approve=True)
    outside = str(tmp_path / "elsewhere" / "file.py")
    # 即使 auto_approve，根外写也要先问
    assert p.decide("file_write", {"path": outside}, TP.WRITE) == Decision.ASK


def test_read_outside_root_not_guarded(tmp_path):
    p = PermissionPolicy(workspace_root=str(tmp_path / "proj"))
    outside = str(tmp_path / "elsewhere" / "file.py")
    # READ 不受外部目录守卫限制
    assert p.decide("file_read", {"path": outside}, TP.READ) == Decision.ALLOW


def test_allow_external_writes_bypasses(tmp_path):
    p = PermissionPolicy(workspace_root=str(tmp_path / "proj"),
                         allow_external_writes=True, auto_approve=True)
    outside = str(tmp_path / "elsewhere" / "file.py")
    assert p.decide("file_write", {"path": outside}, TP.WRITE) == Decision.ALLOW


def test_no_root_no_guard(tmp_path):
    p = PermissionPolicy(workspace_root=None, auto_approve=True)
    outside = str(tmp_path / "elsewhere" / "file.py")
    assert p.decide("file_write", {"path": outside}, TP.WRITE) == Decision.ALLOW


def test_root_itself_allowed(tmp_path):
    p = PermissionPolicy(workspace_root=str(tmp_path), auto_approve=True)
    f = str(tmp_path / "file.py")
    assert p.decide("file_write", {"path": f}, TP.WRITE) == Decision.ALLOW


def test_explicit_deny_still_wins_over_external(tmp_path):
    from coding_agent.core.permissions import Rule
    p = PermissionPolicy(workspace_root=str(tmp_path),
                         deny_rules=[Rule(tool="file_write")], auto_approve=True)
    f = str(tmp_path / "file.py")
    # deny 规则优先于外部守卫
    assert p.decide("file_write", {"path": f}, TP.WRITE) == Decision.DENY


def test_external_execute_also_guarded(tmp_path):
    p = PermissionPolicy(workspace_root=str(tmp_path / "proj"), auto_approve=True)
    outside = str(tmp_path / "elsewhere")
    assert p.decide("shell_exec", {"workdir": outside}, TP.EXECUTE) == Decision.ASK


def test_from_config_loads_workspace(tmp_path):
    p = PermissionPolicy.from_config({
        "workspace_root": str(tmp_path),
        "allow_external_writes": True,
    })
    assert p.workspace_root == str(tmp_path)
    assert p.allow_external_writes is True
