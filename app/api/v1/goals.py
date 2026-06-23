from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_lawyer, get_db, require_admin
from app.models.goal import Goal

router = APIRouter()


class GoalUpdate(BaseModel):
    value: int = Field(..., ge=0)


@router.get("")
async def get_goals(
    db: Session = Depends(get_db),
    current_lawyer: dict = Depends(get_current_lawyer),
) -> dict[str, int]:
    goals = db.query(Goal).all()
    return {g.key: g.value for g in goals}


@router.put("/{key}")
async def upsert_goal(
    key: str,
    body: GoalUpdate,
    db: Session = Depends(get_db),
    _admin_rut: str = Depends(require_admin),
) -> dict:
    goal = db.query(Goal).filter(Goal.key == key).first()
    if goal is None:
        goal = Goal(key=key, value=body.value)
        db.add(goal)
    else:
        goal.value = body.value
    db.commit()
    db.refresh(goal)
    return {"key": goal.key, "value": goal.value}
