"""Guest firewall: tab render + rule add/delete/toggle (extracted from _core)."""
from ..common import *  # noqa: F401,F403
from .. import common
from ._core import (
    _require_guest,
    _resolve_guest_detail,
    _guest_tab_context,
        _write_result,
    _guest_put,
    _guest_post,
    _guest_delete,
    _guest_api_get,
    _wants_task_json,
    _guest_action_response,
)


@app_login_required
def guest_firewall(request, object_type: str, vmid: int):
    detail = _require_guest(object_type, vmid)
    opts, opts_err = _guest_api_get(detail, "firewall/options")
    rules, rules_err = _guest_api_get(detail, "firewall/rules")
    option_rows = []
    if isinstance(opts, dict):
        for key in ("enable", "dhcp", "macfilter", "ndp", "ipfilter", "policy_in", "policy_out", "log_level_in", "log_level_out"):
            if key in opts:
                option_rows.append({"label": key, "value": opts[key]})
    rule_list = []
    if isinstance(rules, list):
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            rule_list.append(
                {
                    "pos": rule.get("pos"),
                    "type": rule.get("type", ""),
                    "action": rule.get("action", ""),
                    "enable": str(rule.get("enable", "1")) in ("1", "True", "true"),
                    "source": rule.get("source", ""),
                    "dest": rule.get("dest", ""),
                    "proto": rule.get("proto", ""),
                    "dport": rule.get("dport", ""),
                    "comment": rule.get("comment", ""),
                }
            )
    opts_dict = opts if isinstance(opts, dict) else {}
    context = _guest_tab_context(detail, "firewall")
    context.update(
        {
            "fw_enabled": bool(opts_dict.get("enable")),
            "fw_options": option_rows,
            "fw_policy_in": opts_dict.get("policy_in", "DROP"),
            "fw_policy_out": opts_dict.get("policy_out", "ACCEPT"),
            "fw_rules": rule_list,
            "fw_error": opts_err or rules_err or "",
        }
    )
    return render(request, "core/guest_firewall.html", context)



def guest_firewall_options(request, object_type, vmid):
    detail = _require_guest(object_type, vmid)
    data = {"enable": "1" if request.POST.get("enable") == "on" else "0"}
    for key in ("policy_in", "policy_out"):
        val = request.POST.get(key, "").strip()
        if val:
            data[key] = val
    _d, err = _guest_put(detail, "firewall/options", data)
    return _write_result(request, detail, "core:guest_firewall", err, "guest.firewall.options")


@require_POST
@app_login_required
def guest_firewall_rule_add(request, object_type, vmid):
    detail = _require_guest(object_type, vmid)
    data = {
        "type": request.POST.get("type", "in"),
        "action": request.POST.get("action", "ACCEPT"),
        "enable": "1",
    }
    for key in ("source", "dest", "proto", "dport", "sport", "comment", "macro"):
        val = request.POST.get(key, "").strip()
        if val:
            data[key] = val
    _d, err = _guest_post(detail, "firewall/rules", data)
    return _write_result(request, detail, "core:guest_firewall", err, "guest.firewall.rule_add")


@require_POST
@app_login_required
def guest_firewall_rule_delete(request, object_type, vmid, pos):
    detail = _require_guest(object_type, vmid)
    _d, err = _guest_delete(detail, f"firewall/rules/{pos}")
    return _write_result(request, detail, "core:guest_firewall", err, "guest.firewall.rule_delete", {"pos": pos})


@require_POST
@app_login_required
def guest_firewall_rule_toggle(request, object_type, vmid, pos):
    detail = _require_guest(object_type, vmid)
    enable = "1" if request.POST.get("enable") == "1" else "0"
    _d, err = _guest_put(detail, f"firewall/rules/{pos}", {"enable": enable})
    return _write_result(request, detail, "core:guest_firewall", err, "guest.firewall.rule_toggle", {"pos": pos, "enable": enable})
