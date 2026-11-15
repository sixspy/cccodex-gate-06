import re
from datetime import datetime

KIND = {"IN": "进闸", "OUT": "出闸", "RS": "倒箱"}
BOX = re.compile(r"^[A-Z]{4}\d{7}$")

# 补码办法只有两个取值，写进设置后本日不可改口
POLICY_QUEUE = "queue_block"   # 挡在坏号队列不收成
POLICY_PAD = "pad_to_detail"   # 补齐缺位后再入库到明细
POLICY_NAME = {
    POLICY_QUEUE: "队列拦收",
    POLICY_PAD: "缺位补位",
}
POLICIES = set(POLICY_NAME)

# 收成/补码各环节的固定拒因，前端按这些文案对账
REASON_BAD_BOX = "箱号不是四位字母加七位数字"
REASON_EMPTY_BOX = "箱号为空，不进坏号队列"
REASON_BAD_KIND = "事件码不在字典"
REASON_BAD_TIME = "扫描时间无法读取"
REASON_POLICY_UNSET = "补码办法未写定"
REASON_POLICY_MISMATCH = "补码办法不符"
REASON_POLICY_LOCKED = "本日补码办法已写定，不许改口"
REASON_PADDING_CONTENTION = "补码争用"
REASON_FIX_INVALID = "补成箱号不是四位字母加七位数字"
REASON_FIX_PREFIX = "补成箱号必须保留原箱号并只补缺位"


def normalize_box(value) -> str:
    return str(value or "").strip().upper().replace(" ", "")


def is_valid_box(box: str) -> bool:
    return bool(BOX.match(box))


def inspect_scan(payload: dict):
    """只做贴源本身的校验，不涉及补码办法。

    返回 (scan, raw_box, event_at, error)。
    error 非空时 scan/event_at 不可用，但 raw_box 尽量带回已归一化的原文，
    供坏号队列登记（全空时 raw_box 仍为空串）。
    """
    scan = str(payload.get("scan") or "").strip().upper()
    raw_box = normalize_box(payload.get("box"))
    stamp = str(payload.get("at") or "").strip()
    if scan not in KIND:
        return None, raw_box, None, REASON_BAD_KIND
    if not raw_box:
        return None, "", None, REASON_EMPTY_BOX
    try:
        event_at = datetime.fromisoformat(stamp)
    except ValueError:
        return None, raw_box, None, REASON_BAD_TIME
    if not is_valid_box(raw_box):
        # 事件码、时间都已通过，scan/event_at 仍可用，补位分支可直接落库
        return scan, raw_box, event_at, REASON_BAD_BOX
    return scan, raw_box, event_at, None


def check_fixed_box(raw_box: str, fixed_box: str):
    """补成箱号必须合法，且保留原文只补缺位；空原文不允许补位。"""
    fixed_box = normalize_box(fixed_box)
    if not is_valid_box(fixed_box):
        return None, REASON_FIX_INVALID
    if not raw_box or not fixed_box.startswith(raw_box) or fixed_box == raw_box:
        return None, REASON_FIX_PREFIX
    return fixed_box, None
