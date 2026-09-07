import uuid

from sqlalchemy.orm import Session

from backend.app.db.models import NicheConfig


def get_active_niche_config(db: Session, brand_kit_id: uuid.UUID) -> NicheConfig | None:
    return (
        db.query(NicheConfig)
        .filter(NicheConfig.brand_kit_id == brand_kit_id, NicheConfig.is_active.is_(True))
        .order_by(NicheConfig.version.desc())
        .first()
    )
