"""Case service - Case CRUD and movement tracking."""

from typing import List, Optional, Tuple
from sqlalchemy.orm import Session

from app.models.case import Case
from app.models.movement import Movement


class CaseService:
    """Handle case operations."""
    
    def __init__(self, db: Session):
        self.db = db
    
    def list_cases(
        self,
        lawyer_id: int,
        page: int = 1,
        per_page: int = 20,
        court_id: Optional[int] = None,
        status: Optional[str] = None,
    ) -> Tuple[List[Case], int]:
        """
        List cases for a lawyer with pagination.
        
        Returns:
            Tuple of (cases, total_count)
        """
        query = self.db.query(Case).filter(Case.lawyer_id == lawyer_id)
        
        if court_id:
            query = query.filter(Case.court_id == court_id)
        if status:
            query = query.filter(Case.status == status)
        
        total = query.count()
        cases = query.offset((page - 1) * per_page).limit(per_page).all()
        
        return cases, total
    
    def get_case(self, case_id: int, lawyer_id: int) -> Optional[Case]:
        """Get a case by ID if owned by lawyer."""
        return self.db.query(Case).filter(
            Case.id == case_id,
            Case.lawyer_id == lawyer_id
        ).first()
    
    def create_case(self, lawyer_id: int, rol: str, court_id: int) -> Case:
        """Create a new case for a lawyer."""
        case = Case(
            lawyer_id=lawyer_id,
            rol=rol,
            court_id=court_id,
            status="active",
        )
        self.db.add(case)
        self.db.commit()
        self.db.refresh(case)
        return case
    
    def delete_case(self, case_id: int, lawyer_id: int) -> bool:
        """Soft delete a case."""
        case = self.get_case(case_id, lawyer_id)
        if not case:
            return False
        
        case.status = "archived"
        self.db.commit()
        return True
    
    def get_movements(self, case_id: int) -> List[Movement]:
        """Get all movements for a case."""
        return self.db.query(Movement).filter(
            Movement.case_id == case_id
        ).order_by(Movement.movement_date.desc()).all()
    
    def add_movement(self, case_id: int, **movement_data) -> Movement:
        """Add a movement to a case."""
        movement = Movement(case_id=case_id, **movement_data)
        self.db.add(movement)
        self.db.commit()
        self.db.refresh(movement)
        return movement
