"""Shared Supabase client.

The client is deliberately created with the same environment variables and
the same startup semantics as the original application.
"""

from supabase import Client, create_client

from .config import SUPABASE_KEY, SUPABASE_URL


supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
