from supabase import create_client, Client
import os
from typing import Dict, List
from assets.constants import (
    TABLE_METADATA,
    NAME,
    PAGE_ID,
    PRICE,
    DETAIL_LINK,
    ORIGIN,
    ID,
)
from database.db_interface import Database
from models.coffee import Coffee
from models.metadata import Metadata


class SupabaseDB(Database):
    def __init__(self):
        url: str = os.environ.get("SUPABASE_URL")
        key: str = os.environ.get("SUPABASE_KEY")
        self.supabase: Client = create_client(url, key)

    def delete_metadata(self, id: str) -> None:
        self.supabase.table(TABLE_METADATA).delete().eq(ID, id).execute()

    def delete_old_metadata(self, new_metadata_list: List[Metadata]) -> List[int]:
        existing_ids = (
            self.supabase.table(TABLE_METADATA).select(PAGE_ID).execute().data
        )
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

            existing = (
                self.supabase.table(TABLE_METADATA)
                .select("*")
                .eq(PAGE_ID, metadata.page_id)
                .execute()
                .data
            )

            if existing:
                result = (
                    self.supabase.table(TABLE_METADATA)
                    .update(upsert_data)
                    .eq(ID, metadata.page_id)
                    .execute()
                )
                updated.extend(result.data)
            else:
                result = (
                    self.supabase.table(TABLE_METADATA).insert(upsert_data).execute()
                )
                created.extend(result.data)

        return {"created": created, "updated": updated}

    def update_coffee(self, coffee: Coffee) -> bool:
        try:
            upsert_data = {
                "page_id": coffee.id,
                "page": coffee.page,
                "name": coffee.name,
                "price": coffee.price,
                "weight": coffee.weight,
                "region": coffee.origin.region,
                "farm": coffee.origin.farm,
                "altitude": coffee.origin.altitude,
                "variety": coffee.origin.variety,
                "body": coffee.taste.body,
                "bitterness": coffee.taste.bitterness,
                "acidity": coffee.taste.acidity,
                "sweetness": coffee.taste.sweetness,
                "roast_shade": coffee.taste.roast_shade,
                "arabica": coffee.taste.species.arabica,
                "robusta": coffee.taste.species.robusta,
                "processing": coffee.taste.processing,
                "flavor_profile": ";".join(coffee.taste.flavor_profile)
                if coffee.taste.flavor_profile
                else None,
                "reviews": ";".join(coffee.popularity.reviews)
                if coffee.popularity and coffee.popularity.reviews
                else None,
                "review_score": coffee.popularity.review_score
                if coffee.popularity
                else None,
                "buy_count": coffee.popularity.buy_count if coffee.popularity else None,
                "decaf": coffee.decaf,
            }

            existing = (
                self.supabase.table("coffee")
                .select("*")
                .eq("page_id", coffee.id)
                .execute()
            )

            if existing.data:
                self.supabase.table("coffee").update(upsert_data).eq(
                    "page_id", coffee.id
                ).execute()
            else:
                self.supabase.table("coffee").insert(upsert_data).execute()

            return True

        except Exception as e:
            print(f"Error updating/inserting Coffee record: {e}")
            return False
