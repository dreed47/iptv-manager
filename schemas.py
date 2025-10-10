# schemas.py
from pydantic import BaseModel

class ItemBase(BaseModel):
    name: str
    server_url: str
    username: str
    user_pass: str

class ItemCreate(ItemBase):
    pass

class ItemUpdate(ItemBase):
    pass

class ItemResponse(ItemBase):
    id: int

    class Config:
        from_attributes = True
