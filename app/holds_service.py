"""付款预留指令（预留 / 取消 / 确认）与可用额。

可用额 = 当前余额（已出具单据累计 - 已登记付款 + 已登记退回）
         - 全部待确认（pending）预留额。

- 预留：凭"权利人编号 + 唯一指令号(hold_ref) + 正整数分额"创建待确认指令；
  仅当金额不超过当时可用额才创建，返回指令状态与可用额；
  同指令号同内容重试幂等返回原指令（status=duplicate，HTTP 200），
  异内容（权利人或金额不同）409 hold_ref_conflict；
- 取消：待确认指令可取消并释放额度（重复取消幂等返回，HTTP 200）；
  已确认指令不可取消（409 hold_already_confirmed）；
- 确认：提交外部付款流水号，同一事务内登记付款（进现有 payments 体系）
  并结束预留，返回付款记录及余额；
  同流水号重试幂等（status=duplicate，HTTP 200）；
  异流水号 409 hold_already_confirmed；流水号被其他付款占用
  409 payment_ref_conflict；已取消指令确认 409 hold_already_cancelled；
  负额追补可能冲减余额：确认时 当前余额 须 >= 更早创建的待确认指令合计
  + 本指令金额，否则 409 hold_exceeds_available 且指令保持待确认不变；
- 指令内容一经创建不可改，仅 pending -> confirmed/cancelled 一次迁移，
  终态不可逆、记录不可删除（数据库触发器强制）。

并发协议（加锁次序全局一致，权利人行锁永远最后获取，无死锁）：
  指令号事务咨询锁 →（仅确认）外部付款流水号事务咨询锁 → 权利人行锁。
  与付款登记（流水号锁 → 权利人行）、退回（退回流水号锁 → 付款行 →
  权利人行）、出单（按编号升序权利人行锁）互斥，可用额校验所见即所入账。
"""
import hashlib

from .errors import APIError
from .payments_service import (
    _PAYMENT_REF_NS,
    _combined_key,
    _conflict as _payment_ref_conflict,
    _ref_lock_key,
    get_available,
    get_balance,
    get_pending_holds_total,
)

# 指令号咨询锁命名空间
_HOLD_REF_NS = 0x484C4431  # "HLD1"


def _hold_lock_key(hold_ref: str) -> int:
    digest = hashlib.blake2b(hold_ref.encode(), digest_size=8).digest()
    n = int.from_bytes(digest, "big", signed=False)
    return n - (1 << 64) if n >= (1 << 63) else n


def _lock_hold_ref(conn, hold_ref: str) -> None:
    conn.execute(
        "SELECT pg_advisory_xact_lock(%s::bigint)",
        (_combined_key(_HOLD_REF_NS, _hold_lock_key(hold_ref)),),
    )


def _lock_holder(conn, holder_code: str):
    return conn.execute(
        "SELECT code FROM rightsholders WHERE code = %s FOR UPDATE",
        (holder_code,),
    ).fetchone()


def reserve_hold(conn, holder_code: str, hold_ref: str, amount: int) -> dict:
    """预留付款额度（创建待确认指令）。

    仅当 当前余额 - 全部待确认预留 >= amount 才创建；
    同指令号同内容重试返回原指令，异内容 409。
    """
    with conn.transaction():
        # 先按指令号加锁：并发的同指令号请求在此串行化，
        # "同内容/异内容"判定确定，不依赖唯一约束竞态。
        _lock_hold_ref(conn, hold_ref)

        existing = conn.execute(
            """
            SELECT id, holder_code, amount, status, created_at
            FROM payment_holds
            WHERE hold_ref = %s
            """,
            (hold_ref,),
        ).fetchone()
        if existing is not None:
            if (
                existing["holder_code"] != holder_code
                or int(existing["amount"]) != amount
            ):
                raise _hold_ref_conflict(hold_ref, existing)
            return _reserve_payload(
                conn, existing, hold_ref, status="duplicate"
            )

        # 锁定权利人行：与付款/退回/出单的余额校验互斥；权利人不存在则 404。
        holder = _lock_holder(conn, holder_code)
        if holder is None:
            raise _holder_not_found(holder_code)

        balance = get_balance(conn, holder_code)
        pending = get_pending_holds_total(conn, holder_code)
        available = balance - pending
        if amount > available:
            raise _exceeds_available(
                holder_code, amount, balance, pending, hold_ref=hold_ref
            )

        row = conn.execute(
            """
            INSERT INTO payment_holds(holder_code, hold_ref, amount)
            VALUES (%s, %s, %s)
            RETURNING id, holder_code, amount, status, created_at
            """,
            (holder_code, hold_ref, amount),
        ).fetchone()
        return _reserve_payload(conn, row, hold_ref, status="created")


def _reserve_payload(conn, row, hold_ref: str, status: str) -> dict:
    """组装预留指令响应；available_after 取操作后的当前可用额。"""
    return {
        "hold_id": row["id"],
        "holder_code": row["holder_code"],
        "hold_ref": hold_ref,
        "amount": int(row["amount"]),
        "hold_status": row["status"],
        "status": status,
        "created_at": row["created_at"],
        "available_after": get_available(conn, row["holder_code"]),
    }


def cancel_hold(conn, hold_ref: str) -> dict:
    """取消待确认指令并释放额度。

    重复取消幂等返回（status=already_cancelled，HTTP 200）；
    已确认指令不可取消（付款已登记且不可变），409 hold_already_confirmed。
    """
    with conn.transaction():
        _lock_hold_ref(conn, hold_ref)

        row = conn.execute(
            """
            SELECT id, holder_code, amount, status, external_ref, resolved_at
            FROM payment_holds
            WHERE hold_ref = %s
            FOR UPDATE
            """,
            (hold_ref,),
        ).fetchone()
        if row is None:
            raise _hold_not_found(hold_ref)
        if row["status"] == "cancelled":
            return _cancel_payload(conn, row, hold_ref, "already_cancelled")
        if row["status"] == "confirmed":
            raise _already_confirmed(hold_ref, row["external_ref"])

        # 锁定权利人行：与预留/确认/付款/出单的可用额校验互斥，
        # 释放额度与可用额读数在同一事务内一致。
        _lock_holder(conn, row["holder_code"])

        row = conn.execute(
            """
            UPDATE payment_holds
            SET status = 'cancelled', resolved_at = now()
            WHERE id = %s
            RETURNING id, holder_code, amount, status, resolved_at
            """,
            (row["id"],),
        ).fetchone()
        return _cancel_payload(conn, row, hold_ref, "cancelled")


def _cancel_payload(conn, row, hold_ref: str, status: str) -> dict:
    return {
        "hold_id": row["id"],
        "holder_code": row["holder_code"],
        "hold_ref": hold_ref,
        "amount": int(row["amount"]),
        "hold_status": "cancelled",
        "status": status,
        "cancelled_at": row["resolved_at"],
        "available_after": get_available(conn, row["holder_code"]),
    }


def confirm_hold(conn, hold_ref: str, external_ref: str) -> dict:
    """确认待确认指令：原子登记付款并结束预留。

    - 同流水号重试幂等返回原付款记录（status=duplicate，HTTP 200）；
    - 异流水号 409 hold_already_confirmed；
      流水号被其他付款占用 409 payment_ref_conflict；
    - 已取消指令 409 hold_already_cancelled；
    - 负额追补冲减余额后，确认时 当前余额 须 >= 更早创建的待确认指令合计
      + 本指令金额，否则 409 hold_exceeds_available，指令保持待确认不变。
    """
    with conn.transaction():
        _lock_hold_ref(conn, hold_ref)

        row = conn.execute(
            """
            SELECT id, holder_code, amount, status, external_ref, payment_id
            FROM payment_holds
            WHERE hold_ref = %s
            FOR UPDATE
            """,
            (hold_ref,),
        ).fetchone()
        if row is None:
            raise _hold_not_found(hold_ref)
        if row["status"] == "cancelled":
            raise _already_cancelled(hold_ref)
        if row["status"] == "confirmed":
            if row["external_ref"] != external_ref:
                raise _already_confirmed(hold_ref, row["external_ref"])
            # 同流水号重试：返回原付款记录与当前余额，不重复入账
            payment = conn.execute(
                "SELECT id, created_at FROM payments WHERE id = %s",
                (row["payment_id"],),
            ).fetchone()
            return _confirm_payload(
                conn, row, hold_ref, external_ref, payment, status="duplicate"
            )

        holder_code = row["holder_code"]
        amount = int(row["amount"])

        # 再按外部流水号加锁：与直接付款登记互斥，
        # "流水号是否被其他付款占用"的判定确定。
        conn.execute(
            "SELECT pg_advisory_xact_lock(%s::bigint)",
            (_combined_key(_PAYMENT_REF_NS, _ref_lock_key(external_ref)),),
        )
        occupied = conn.execute(
            "SELECT id, holder_code, amount FROM payments WHERE external_ref = %s",
            (external_ref,),
        ).fetchone()
        if occupied is not None:
            raise _payment_ref_conflict(external_ref, occupied)

        # 锁定权利人行：与付款/退回/出单互斥，余额校验所见即所入账。
        _lock_holder(conn, holder_code)

        balance = get_balance(conn, holder_code)
        # 负额追补可能已冲减余额：确认时须留足更早创建的待确认指令
        # 及本指令金额（更晚创建的指令不在本指令的留足义务内）。
        earlier_pending = int(
            conn.execute(
                """
                SELECT COALESCE(SUM(amount), 0) AS held
                FROM payment_holds
                WHERE holder_code = %s AND status = 'pending' AND id < %s
                """,
                (holder_code, row["id"]),
            ).fetchone()["held"]
        )
        if balance - earlier_pending - amount < 0:
            raise _exceeds_available(
                holder_code,
                amount,
                balance,
                earlier_pending,
                hold_ref=hold_ref,
                confirming=True,
            )

        payment = conn.execute(
            """
            INSERT INTO payments(holder_code, external_ref, amount)
            VALUES (%s, %s, %s)
            RETURNING id, created_at
            """,
            (holder_code, external_ref, amount),
        ).fetchone()
        conn.execute(
            """
            UPDATE payment_holds
            SET status = 'confirmed', external_ref = %s,
                payment_id = %s, resolved_at = now()
            WHERE id = %s
            """,
            (external_ref, payment["id"], row["id"]),
        )
        return _confirm_payload(
            conn, row, hold_ref, external_ref, payment, status="created"
        )


def _confirm_payload(conn, hold_row, hold_ref, external_ref, payment, status) -> dict:
    """组装确认响应：付款记录 + 登记后余额与当前可用额。"""
    holder_code = hold_row["holder_code"]
    return {
        "hold_id": hold_row["id"],
        "hold_ref": hold_ref,
        "holder_code": holder_code,
        "hold_status": "confirmed",
        "payment_id": payment["id"],
        "external_ref": external_ref,
        "amount": int(hold_row["amount"]),
        "registered_at": payment["created_at"],
        "status": status,
        "balance_after": get_balance(conn, holder_code),
        "available_after": get_available(conn, holder_code),
    }


def get_hold(conn, hold_ref: str):
    """按指令号查询单条指令（含状态与确认时冻结的流水号/付款）。"""
    row = conn.execute(
        """
        SELECT id, holder_code, hold_ref, amount, status,
               external_ref, payment_id, created_at, resolved_at
        FROM payment_holds
        WHERE hold_ref = %s
        """,
        (hold_ref,),
    ).fetchone()
    if row is None:
        return None
    return {
        "hold_id": row["id"],
        "holder_code": row["holder_code"],
        "hold_ref": row["hold_ref"],
        "amount": int(row["amount"]),
        "hold_status": row["status"],
        "external_ref": row["external_ref"],
        "payment_id": row["payment_id"],
        "created_at": row["created_at"],
        "resolved_at": row["resolved_at"],
    }


def _hold_not_found(hold_ref: str):
    return APIError(
        404,
        "hold_not_found",
        f"payment hold {hold_ref} not found",
        detail={"hold_ref": hold_ref},
    )


def _holder_not_found(holder_code: str):
    return APIError(
        404,
        "holder_not_found",
        f"rightsholder {holder_code} not found",
    )


def _hold_ref_conflict(hold_ref: str, existing):
    return APIError(
        409,
        "hold_ref_conflict",
        f"payment hold ref {hold_ref} already registered with different content",
        detail={
            "hold_ref": hold_ref,
            "existing_holder_code": existing["holder_code"],
            "existing_amount": int(existing["amount"]),
        },
    )


def _already_confirmed(hold_ref: str, external_ref: str):
    return APIError(
        409,
        "hold_already_confirmed",
        f"payment hold {hold_ref} already confirmed with external ref "
        f"{external_ref}; confirmed instructions are final",
        detail={"hold_ref": hold_ref, "existing_external_ref": external_ref},
    )


def _already_cancelled(hold_ref: str):
    return APIError(
        409,
        "hold_already_cancelled",
        f"payment hold {hold_ref} already cancelled; "
        f"cancelled instructions cannot be confirmed",
        detail={"hold_ref": hold_ref},
    )


def _exceeds_available(
    holder_code: str,
    amount: int,
    balance: int,
    pending: int,
    hold_ref: str,
    confirming: bool = False,
):
    available = balance - pending
    if confirming:
        message = (
            f"confirming hold {hold_ref} of amount {amount} requires current "
            f"balance {balance} to cover earlier pending holds {pending} plus "
            f"this amount of rightsholder {holder_code}; confirmation rejected, "
            f"instruction left pending"
        )
        detail = {
            "holder_code": holder_code,
            "hold_ref": hold_ref,
            "requested_amount": amount,
            "balance": balance,
            "earlier_pending_holds": pending,
            "available": available,
        }
    else:
        message = (
            f"hold amount {amount} exceeds available balance {available} "
            f"of rightsholder {holder_code} (balance {balance}, pending holds "
            f"{pending}); reservation rejected"
        )
        detail = {
            "holder_code": holder_code,
            "hold_ref": hold_ref,
            "requested_amount": amount,
            "balance": balance,
            "pending_holds": pending,
            "available": available,
        }
    return APIError(409, "hold_exceeds_available", message, detail=detail)
