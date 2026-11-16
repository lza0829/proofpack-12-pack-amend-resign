from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.services import amendment_service as svc

router = APIRouter(tags=["amendments"])


class AmendOp(BaseModel):
    op: str
    business_key: str
    from_business_key: str | None = None


class CreateAmendmentBody(BaseModel):
    pack_id: int
    strategy: str = Field(description="incremental=增量补证，resign=整包重签")
    failure_policy: str = Field(description="rollback=失败整笔回滚，failed_state=失败进入补证失败态")
    ops: list[AmendOp]


@router.get("/packs/{pack_id}/pool")
def pack_pool(pack_id: int) -> dict[str, Any]:
    return svc.pool_view(pack_id)


@router.get("/packs/{pack_id}/verify")
def verify_pack(pack_id: int) -> dict[str, Any]:
    return svc.verify_pack(pack_id)


@router.get("/amendments")
def list_amendments(pack_id: int | None = Query(default=None)):
    return svc.list_amendments(pack_id=pack_id)


@router.get("/amendments/{amendment_id}")
def get_amendment(amendment_id: int):
    return svc.get_amendment(amendment_id)


@router.post("/amendments")
def create_amendment(body: CreateAmendmentBody) -> dict[str, Any]:
    return svc.create_amendment(
        pack_id=body.pack_id,
        strategy=body.strategy,
        failure_policy=body.failure_policy,
        ops=[op.model_dump() for op in body.ops],
    )


@router.post("/amendments/{amendment_id}/submit")
def submit_amendment(amendment_id: int) -> dict[str, Any]:
    return svc.submit_amendment(amendment_id)


@router.post("/amendments/{amendment_id}/cancel")
def cancel_amendment(amendment_id: int) -> dict[str, Any]:
    return svc.cancel_amendment(amendment_id)
