from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from .. import holds_service as svc
from ..db import get_conn
from ..errors import APIError
from ..schemas import (
    HoldCancelOut,
    HoldConfirmIn,
    HoldConfirmOut,
    HoldDetail,
    HoldIn,
    HoldOut,
)

router = APIRouter(tags=["payment-holds"])


@router.post("/payment-holds")
def reserve_hold(body: HoldIn, conn=Depends(get_conn)):
    """预留付款额度（创建待确认指令）。

    - 凭权利人编号、唯一指令号与正整数分额预留；
      仅当 当前余额 - 全部待确认预留 >= 本额 才创建，返回指令状态与可用额；
    - 同指令号同内容重试幂等返回原指令（status=duplicate，HTTP 200）；
      同指令号异内容（权利人或金额不同）409 hold_ref_conflict；
    - 权利人不存在 404 holder_not_found；金额非正整数 422；
      可用额不足 409 hold_exceeds_available（detail 含余额/待确认预留/可用额），
      拒绝时不留任何指令；
    - 指令内容一经创建不可改，仅可取消或确认（触发器强制单向迁移）。
    """
    result = svc.reserve_hold(conn, body.holder_code, body.hold_ref, body.amount)
    # 新预留 201；同指令号同内容重试 200 且返回原指令
    status = 201 if result["status"] == "created" else 200
    return JSONResponse(
        status_code=status,
        content=HoldOut(**result).model_dump(mode="json"),
    )


@router.get("/payment-holds/{hold_ref}", response_model=HoldDetail)
def get_hold(hold_ref: str, conn=Depends(get_conn)):
    """按指令号查询单条预留指令（状态、确认时冻结的流水号与付款）。"""
    result = svc.get_hold(conn, hold_ref)
    if result is None:
        raise APIError(
            404,
            "hold_not_found",
            f"payment hold {hold_ref} not found",
            detail={"hold_ref": hold_ref},
        )
    return result


@router.post("/payment-holds/{hold_ref}/cancel")
def cancel_hold(hold_ref: str, conn=Depends(get_conn)):
    """取消待确认指令并释放额度。

    - 仅待确认指令可取消；取消后额度立即释放（可用额回升）；
    - 重复取消幂等返回原指令（status=already_cancelled，HTTP 200）；
    - 已确认指令不可取消（付款已登记且不可变）409 hold_already_confirmed；
      指令号不存在 404 hold_not_found。
    """
    result = svc.cancel_hold(conn, hold_ref)
    return JSONResponse(
        status_code=200,
        content=HoldCancelOut(**result).model_dump(mode="json"),
    )


@router.post("/payment-holds/{hold_ref}/confirm")
def confirm_hold(hold_ref: str, body: HoldConfirmIn, conn=Depends(get_conn)):
    """确认待确认指令：提交外部付款流水号，原子登记付款并结束预留。

    - 同一事务内登记付款（进现有 payments 体系）并结束预留，
      返回付款记录及登记后余额；
    - 同流水号重试幂等返回原付款记录（status=duplicate，HTTP 200）；
      异流水号 409 hold_already_confirmed；
      流水号被其他付款占用 409 payment_ref_conflict；
    - 已取消指令确认 409 hold_already_cancelled；指令号不存在 404；
    - 负额追补可能冲减余额：确认时 当前余额 须 >= 更早创建的待确认指令合计
      + 本指令金额，否则 409 hold_exceeds_available 且指令保持待确认不变。
    """
    result = svc.confirm_hold(conn, hold_ref, body.external_ref)
    # 新登记付款 201；同流水号重试 200 且返回原付款记录
    status = 201 if result["status"] == "created" else 200
    return JSONResponse(
        status_code=status,
        content=HoldConfirmOut(**result).model_dump(mode="json"),
    )
