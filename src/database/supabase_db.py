from supabase import create_client, Client
import os
from typing import Dict, List, Tuple
from database.db_interface import Database
from models.metadata import Metadata
from models.coffee import Coffee, Origin, Taste, PackageInformation, Review


class SupabaseDB(Database):
    def __init__(self):
        url: str = os.environ.get("SUPABASE_URL")
        key: str = os.environ.get("SUPABASE_KEY")
        self.supabase: Client = create_client(url, key)

    def delete_metadata(self, id: str) -> None:
        self.supabase.table("metadata").delete().eq("id", id).execute()



    def delete_old_metadata(self, new_metadata_list: List[Metadata]) -> List[int]:
        # Get all existing ids
        existing_ids = self.supabase.table("metadata").select("page_id").execute().data
        existing_id_set = set(item['page_id'] for item in existing_ids)
        
        # Find ids to delete
        new_id_set = set(metadata.page_id for metadata in new_metadata_list)
        ids_to_delete = existing_id_set - new_id_set

        deleted_ids = []
        if ids_to_delete:
            # Delete the metadata with the ids to delete
            for id in ids_to_delete:
                self.supabase.table("metadata").delete().eq("page_id", id).execute()
                deleted_ids.append(id)
                
        return deleted_ids
    
    def update_metadata(self, new_metadata_list: List[Metadata]) -> Dict:
        created = []
        updated = []
        for metadata in new_metadata_list:
            # Prepare the data for upsert, excluding image_link
            upsert_data = {
                "page_id": metadata.page_id,
                "origin": metadata.origin,
                "name": metadata.name,
                "price": metadata.price,
                "detail_link": metadata.detail_link,
            }
            
            # Check if the metadata already exists
            existing = self.supabase.table("metadata").select("*").eq("id", metadata.page_id).execute().data
            
            if existing:
                result = self.supabase.table("metadata").update(upsert_data).eq("id", metadata.page_id).execute()
                updated.extend(result.data)
            else:
                result = self.supabase.table("metadata").insert(upsert_data).execute()
                created.extend(result.data)

        return {'created':created,'updated':updated}