from supabase import create_client, Client
import os
from typing import Dict, List
from assets.constants import TABLE_METADATA,NAME,PAGE_ID,PRICE,DETAIL_LINK,ORIGIN,ID
from database.db_interface import Database
from models.metadata import Metadata
from models.coffee import Coffee, Origin, Taste, Popularity


class SupabaseDB(Database):
    def __init__(self):
        url: str = os.environ.get("SUPABASE_URL")
        key: str = os.environ.get("SUPABASE_KEY")
        self.supabase: Client = create_client(url, key)

    def delete_metadata(self, id: str) -> None:
        self.supabase.table(TABLE_METADATA).delete().eq(ID, id).execute()



    def delete_old_metadata(self, new_metadata_list: List[Metadata]) -> List[int]:
        existing_ids = self.supabase.table(TABLE_METADATA).select(PAGE_ID).execute().data
        existing_id_set = set(item[PAGE_ID] for item in existing_ids)
        
        new_id_set = set(metadata.page_id for metadata in new_metadata_list)
        ids_to_delete = existing_id_set - new_id_set

        deleted_ids = []
        if ids_to_delete:
            for id in ids_to_delete:
                self.supabase.table(TABLE_METADATA).delete().eq(PAGE_ID, id).execute()
                deleted_ids.append(id)
                
        return deleted_ids
    
    def update_metadata(self, new_metadata_list: List[Metadata]) -> Dict:
        created = []
        updated = []
        for metadata in new_metadata_list:
            upsert_data = {
                PAGE_ID: metadata.page_id,
                ORIGIN: metadata.origin,
                NAME: metadata.name,
                PRICE: metadata.price,
                DETAIL_LINK: metadata.detail_link,
            }
            
            existing = self.supabase.table(TABLE_METADATA).select("*").eq(ID, metadata.page_id).execute().data
            
            if existing:
                result = self.supabase.table(TABLE_METADATA).update(upsert_data).eq(ID, metadata.page_id).execute()
                updated.extend(result.data)
            else:
                result = self.supabase.table(TABLE_METADATA).insert(upsert_data).execute()
                created.extend(result.data)

        return {'created': created,'updated': updated}