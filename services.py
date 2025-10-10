# services.py
import logging
from sqlalchemy.orm import Session
from models import Item

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def create_item(db: Session, name: str, server_url: str, username: str, user_pass: str, languages: str, includes: str, excludes: str):
    try:
        db_item = Item(name=name, server_url=server_url, username=username, user_pass=user_pass, languages=languages, includes=includes, excludes=excludes)
        db.add(db_item)
        db.commit()
        db.refresh(db_item)
        logger.info(f"Created item with id {db_item.id} and name '{name}'")
        return db_item
    except Exception as e:
        logger.error(f"Failed to create item: {str(e)}")
        db.rollback()
        return None

def update_item(db: Session, item_id: int, name: str, server_url: str, username: str, user_pass: str, languages: str, includes: str, excludes: str):
    try:
        db_item = db.query(Item).filter(Item.id == item_id).first()
        if db_item:
            db_item.name = name
            db_item.server_url = server_url
            db_item.username = username
            db_item.user_pass = user_pass
            db_item.languages = languages
            db_item.includes = includes  # Make sure this line exists
            db_item.excludes = excludes
            db.commit()
            db.refresh(db_item)
            logger.info(f"Updated item with id {item_id} to name '{name}'")
            return db_item
        logger.warning(f"Item with id {item_id} not found for update")
        return None
    except Exception as e:
        logger.error(f"Failed to update item: {str(e)}")
        db.rollback()
        return None
    
def delete_item(db: Session, item_id: int):
    try:
        db_item = db.query(Item).filter(Item.id == item_id).first()
        if db_item:
            db.delete(db_item)
            db.commit()
            logger.info(f"Deleted item with id {item_id}")
            return True
        logger.warning(f"Item with id {item_id} not found for deletion")
        return False
    except Exception as e:
        logger.error(f"Failed to delete item: {str(e)}")
        db.rollback()
        return False

def get_all_items(db: Session):
    try:
        items = db.query(Item).all()
        logger.info(f"Retrieved {len(items)} items from database")
        return items
    except Exception as e:
        logger.error(f"Failed to retrieve items: {str(e)}")
        return []
