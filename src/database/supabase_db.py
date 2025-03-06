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
        self._ensure_tables_exist()
