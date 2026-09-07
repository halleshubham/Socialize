"""Full-auto mode: for items whose priority_score clears a configurable
threshold, skips the analytical_review, content_review, AND media_review
human gates, running straight through to APPROVED - and, if `auto_send` is
also on, straight through an actual WhatsApp send. Extended past
generation-only per an explicit user decision (the original design kept
media_review manual-only for anything that costs money or gets sent
publicly); `auto_send` is its own separate toggle, off by default, so
turning on auto-approve alone doesn't silently start blasting WhatsApp
messages for an existing auto-mode setup. See orchestrator.py's
auto_advance_content_item for where this actually gets applied, and
routes_board.py's /board/auto-mode for the settings form.

Settings live in the same SyncState key/value table as the reel cost cap
(reel_editor/graph.py) and Gmail search query (integrations/gmail/fetch.py)
- no dedicated table for one small settings row.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from backend.app.db.models import SyncState

_ENABLED_KEY = "auto_mode_enabled"
_MIN_SCORE_KEY = "auto_mode_min_score"
_FORMAT_KEY = "auto_mode_format"
_AUTO_SEND_KEY = "auto_mode_auto_send"

DEFAULT_MIN_SCORE = 0.8
DEFAULT_FORMAT = "poster"
VALID_FORMATS = ("poster", "reel", "text_only")


@dataclass
class AutoModeSettings:
    enabled: bool
    min_score: float
    format: str
    auto_send: bool


def get_auto_mode_settings(db: Session, brand_kit_id: uuid.UUID) -> AutoModeSettings:
    enabled_row = db.get(SyncState, (_ENABLED_KEY, brand_kit_id))
    score_row = db.get(SyncState, (_MIN_SCORE_KEY, brand_kit_id))
    format_row = db.get(SyncState, (_FORMAT_KEY, brand_kit_id))
    auto_send_row = db.get(SyncState, (_AUTO_SEND_KEY, brand_kit_id))
    return AutoModeSettings(
        enabled=bool(enabled_row) and enabled_row.value == "true",
        min_score=float(score_row.value) if score_row else DEFAULT_MIN_SCORE,
        format=format_row.value if format_row and format_row.value in VALID_FORMATS else DEFAULT_FORMAT,
        auto_send=bool(auto_send_row) and auto_send_row.value == "true",
    )


def set_auto_mode_settings(
    db: Session, brand_kit_id: uuid.UUID, enabled: bool, min_score: float, format_: str, auto_send: bool = False
) -> None:
    if format_ not in VALID_FORMATS:
        format_ = DEFAULT_FORMAT
    values = {
        _ENABLED_KEY: "true" if enabled else "false",
        _MIN_SCORE_KEY: str(min_score),
        _FORMAT_KEY: format_,
        _AUTO_SEND_KEY: "true" if auto_send else "false",
    }
    for key, value in values.items():
        row = db.get(SyncState, (key, brand_kit_id))
        if row:
            row.value = value
        else:
            db.add(SyncState(key=key, brand_kit_id=brand_kit_id, value=value))
    db.commit()
