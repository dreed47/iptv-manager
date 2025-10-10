# schemas.py
from pydantic import BaseModel

class ItemBase(BaseModel):
    name: str
    server_url: str
    username: str
    user_pass: str
    languages: str | None = None
    includes: str | None = None  # Make sure this line exists
    excludes: str | None = None

class ItemCreate(ItemBase):
    pass

class ItemUpdate(ItemBase):
    pass

class ItemResponse(ItemBase):
    id: int

    class Config:
        from_attributes = True
