"""SQLAlchemy models. Tables arrive in phase 3; Alembic targets this metadata."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
