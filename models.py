from dataclasses import dataclass
from typing import Literal, Optional

CardType = Literal["flashcard", "short_answer", "fill_in_blank", "multiple_choice"]

@dataclass
class QAItem:
    id: int
    card_type: CardType
    question: str
    answer: str
    source_pdf: str
    page: int
    subject_id: int
    # Only for multiple choice:
    options: Optional[list[str]] = None


